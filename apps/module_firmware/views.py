from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db import transaction
from django.db.models import Prefetch, Q
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views import View
from django.views.generic import DetailView, ListView

from apps.images import github_releases
from apps.stations.models import Station

from . import services
from .models import (
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareImportJob,
    ModuleFirmwareRelease,
    ModuleType,
)
from .releases import parse_variant_assets

# ---------------------------------------------------------------------------
# Module-type helpers
# ---------------------------------------------------------------------------


def _importable_module_types():
    """Module types that have BOTH firmware_repo and release_asset_prefix set.

    Only fully-configured types can ever produce a successful import job, so
    restricting the UI to this set avoids offering doomed imports.
    """
    return ModuleType.objects.exclude(firmware_repo="").exclude(release_asset_prefix="")


# Lifecycle states an operator may set by hand. ``deployed`` is excluded: it is
# auto-derived from an open assignment, so a manual set would be overwritten by
# the next ingest — offering it as a control is misleading.
OPERATOR_LIFECYCLE_CHOICES = [
    (value, label)
    for value, label in Module.Lifecycle.choices
    if value != Module.Lifecycle.DEPLOYED
]


def operator_lifecycle_choices(*, has_open_assignment):
    """Operator-settable lifecycle choices. ``ready`` is also assignment-derived,
    so it's dropped while the module has an open assignment (set_lifecycle rejects
    it in that state)."""
    choices = OPERATOR_LIFECYCLE_CHOICES
    if has_open_assignment:
        choices = [(v, label) for v, label in choices if v != Module.Lifecycle.READY]
    return choices


class ModuleListView(LoginRequiredMixin, ListView):
    model = Module
    template_name = "module_firmware/module_list.html"
    context_object_name = "modules"
    paginate_by = 50

    def get_queryset(self):
        # Prefetch only the *open* assignment so the list shows each module's
        # current location, not a stale historical station_modules row.
        open_assignments = ModuleAssignmentHistory.objects.filter(
            to_ts__isnull=True
        ).select_related("station")
        qs = Module.objects.select_related("module_type").prefetch_related(
            Prefetch("assignments", queryset=open_assignments, to_attr="open_assignments")
        )
        g = self.request.GET
        if g.get("type"):
            qs = qs.filter(module_type__key=g["type"])
        if g.get("lifecycle"):
            qs = qs.filter(lifecycle_status=g["lifecycle"])
        if g.get("registration"):
            qs = qs.filter(registration_status=g["registration"])
        if g.get("uid_source"):
            qs = qs.filter(uid_source=g["uid_source"])
        if g.get("station", "").isdigit():
            # Filter by *current* location (open assignment), not history.
            qs = qs.filter(
                assignments__station_id=g["station"], assignments__to_ts__isnull=True
            ).distinct()
        if g.get("q"):
            qs = qs.filter(Q(uid__icontains=g["q"]))
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["lifecycles"] = Module.Lifecycle.choices
        ctx["registrations"] = Module.Registration.choices
        ctx["uid_sources"] = Module.UidSource.choices
        ctx["types"] = ModuleType.objects.all()
        # Only stations that currently host a module (open assignment), matching
        # what the station filter actually selects on.
        ctx["stations"] = (
            Station.objects.filter(module_assignments__to_ts__isnull=True)
            .distinct()
            .order_by("name")
        )
        ctx["total_count"] = Module.objects.count()
        ctx["unregistered_count"] = Module.objects.filter(
            registration_status=Module.Registration.UNREGISTERED
        ).count()
        g = self.request.GET
        ctx["filters"] = {
            "type": g.get("type", ""),
            "lifecycle": g.get("lifecycle", ""),
            "registration": g.get("registration", ""),
            "uid_source": g.get("uid_source", ""),
            "station": g.get("station", ""),
            "q": g.get("q", ""),
        }
        return ctx


class ModuleDetailView(LoginRequiredMixin, DetailView):
    model = Module
    template_name = "module_firmware/module_detail.html"
    context_object_name = "module"
    slug_field = "uid"
    slug_url_kwarg = "uid"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        m = self.object
        ctx["assignments"] = m.assignments.select_related("station").order_by("-from_ts")
        ctx["current_assignment"] = m.assignments.filter(to_ts__isnull=True).first()
        ctx["audit_logs"] = m.audit_logs.select_related("station", "user").all()[:200]
        ctx["can_edit"] = self.request.user.is_staff
        # Don't offer `ready` while an assignment is open — it's assignment-derived
        # and set_lifecycle rejects it in that state.
        ctx["lifecycles"] = operator_lifecycle_choices(
            has_open_assignment=bool(ctx["current_assignment"])
        )
        return ctx


class _StaffModuleMixin(LoginRequiredMixin, UserPassesTestMixin):
    raise_exception = True

    def test_func(self):
        return self.request.user.is_staff

    def get_module(self):
        return get_object_or_404(Module, uid=self.kwargs["uid"])

    def _redirect(self, uid):
        return HttpResponseRedirect(reverse("module_firmware:module_detail", args=[uid]))


class ModuleConfirmView(_StaffModuleMixin, View):
    def post(self, request, uid):
        m = self.get_module()
        services.confirm_registration(m, user=request.user)
        return self._redirect(uid)


class ModuleLifecycleView(_StaffModuleMixin, View):
    def post(self, request, uid):
        m = self.get_module()
        status = request.POST.get("lifecycle_status")
        # Only operator-settable states, and only what's valid for the module's
        # current assignment state (``deployed`` is auto-derived; ``ready`` is
        # rejected while an assignment is open). Anything else is a no-op — the
        # service is the authority and would raise on an invalid transition.
        has_open = m.assignments.filter(to_ts__isnull=True).exists()
        allowed = dict(operator_lifecycle_choices(has_open_assignment=has_open))
        if status in allowed:
            services.set_lifecycle(m, status, user=request.user)
        return self._redirect(uid)


class ModuleNotesView(_StaffModuleMixin, View):
    def post(self, request, uid):
        m = self.get_module()
        m.notes = request.POST.get("notes", "")
        m.save(update_fields=["notes", "updated_at"])
        return self._redirect(uid)


# ---------------------------------------------------------------------------
# Firmware release views
# ---------------------------------------------------------------------------


class _StaffFirmwareMixin(LoginRequiredMixin, UserPassesTestMixin):
    raise_exception = True

    def test_func(self):
        return self.request.user.is_staff


class FirmwareReleaseListView(LoginRequiredMixin, ListView):
    template_name = "module_firmware/release_list.html"
    context_object_name = "releases"
    paginate_by = 50

    def _show_archived(self) -> bool:
        return self.request.GET.get("show_archived") == "1"

    def get_queryset(self):
        manager = (
            ModuleFirmwareRelease.all_objects
            if self._show_archived()
            else ModuleFirmwareRelease.objects
        )
        qs = manager.select_related("module_type")
        g = self.request.GET
        if g.get("type"):
            qs = qs.filter(module_type__key=g["type"])
        if g.get("variant"):
            qs = qs.filter(variant=g["variant"])
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["module_types"] = _importable_module_types()
        ctx["show_archived"] = self._show_archived()
        g = self.request.GET
        ctx["filters"] = {
            "type": g.get("type", ""),
            "variant": g.get("variant", ""),
        }
        ctx["recent_jobs"] = ModuleFirmwareImportJob.objects.select_related(
            "module_type", "requested_by"
        )[:10]
        return ctx


class GitHubFirmwareReleasesPartialView(_StaffFirmwareMixin, View):
    def get(self, request):
        module_type_id = request.GET.get("module_type")
        if not module_type_id:
            return render(
                request,
                "module_firmware/_github_releases.html",
                {
                    "error": "No module type selected.",
                    "module_types": _importable_module_types(),
                },
            )
        module_type = get_object_or_404(ModuleType, pk=module_type_id)
        if not module_type.firmware_repo:
            return render(
                request,
                "module_firmware/_github_releases.html",
                {
                    "error": "This module type has no firmware repo configured.",
                    "module_type": module_type,
                },
            )
        if not module_type.release_asset_prefix:
            return render(
                request,
                "module_firmware/_github_releases.html",
                {
                    "error": "This module type has no release asset prefix configured.",
                    "module_type": module_type,
                },
            )
        try:
            releases = github_releases.fetch_releases(module_type.firmware_repo)
        except github_releases.GitHubAPIError as exc:
            return render(
                request,
                "module_firmware/_github_releases.html",
                {"error": str(exc), "module_type": module_type},
            )

        in_flight_tags = set(
            ModuleFirmwareImportJob.objects.filter(
                module_type=module_type,
                status__in=[
                    ModuleFirmwareImportJob.Status.PENDING,
                    ModuleFirmwareImportJob.Status.RUNNING,
                ],
            ).values_list("tag", flat=True)
        )

        rows = []
        for rel in releases:
            # Determine per-release state by checking whether ALL parsed
            # variants have an active (non-archived) row.  A tag is only
            # "imported" if every variant the release offers has a live row;
            # a partial import (e.g. vhf active, uhf missing/archived) must
            # remain importable so the operator can fill the gap.
            parsed_variants = parse_variant_assets(
                set(rel.asset_names), module_type.release_asset_prefix
            )
            if parsed_variants and all(
                ModuleFirmwareRelease.objects.filter(
                    module_type=module_type,
                    source_tag=rel.tag,
                    variant=va.variant,
                ).exists()
                for va in parsed_variants
            ):
                state = "imported"
            elif rel.tag in in_flight_tags:
                state = "queued"
            else:
                state = "ready"
            rows.append({"release": rel, "state": state})

        return render(
            request,
            "module_firmware/_github_releases.html",
            {
                "rows": rows,
                "module_type": module_type,
                "module_types": _importable_module_types(),
            },
        )


class FirmwareImportView(_StaffFirmwareMixin, View):
    def post(self, request):
        module_type_id = request.POST.get("module_type")
        tag = request.POST.get("tag", "").strip()
        # Blank tag / id guard stays outside the atomic block — these never
        # touch the DB and need no lock.
        if not tag or not module_type_id:
            return HttpResponseRedirect(reverse("module_firmware:release_list"))
        # Finding 7: resolve + lock the module type row INSIDE the atomic block
        # to serialise concurrent POSTs for the same type on Postgres.  SQLite
        # silently ignores select_for_update but stays green.
        with transaction.atomic():
            module_type = ModuleType.objects.select_for_update().filter(pk=module_type_id).first()
            if module_type is None:
                return HttpResponseRedirect(reverse("module_firmware:release_list"))
            # Finding 3: reject partially-configured module types — a doomed job
            # can never succeed and just adds noise to the queue.
            if not module_type.firmware_repo or not module_type.release_asset_prefix:
                return HttpResponseRedirect(reverse("module_firmware:release_list"))
            # Dedup: only prevent duplicate in-flight work.  The importer is
            # per-variant idempotent (skips active variants, restores archived),
            # so we do NOT block on active-release existence — a tag with some
            # variants active and others archived/missing must be re-importable.
            in_flight_exists = ModuleFirmwareImportJob.objects.filter(
                module_type=module_type,
                tag=tag,
                status__in=[
                    ModuleFirmwareImportJob.Status.PENDING,
                    ModuleFirmwareImportJob.Status.RUNNING,
                ],
            ).exists()
            if not in_flight_exists:
                ModuleFirmwareImportJob.objects.create(
                    module_type=module_type,
                    source_repo=module_type.firmware_repo,
                    tag=tag,
                    status=ModuleFirmwareImportJob.Status.PENDING,
                    requested_by=request.user,
                )
        return HttpResponseRedirect(reverse("module_firmware:release_list"))


class FirmwareArchiveView(_StaffFirmwareMixin, View):
    def post(self, request, pk):
        release = get_object_or_404(ModuleFirmwareRelease.all_objects, pk=pk)
        release.archive()
        return HttpResponseRedirect(reverse("module_firmware:release_list"))


class FirmwareRestoreView(_StaffFirmwareMixin, View):
    def post(self, request, pk):
        release = get_object_or_404(ModuleFirmwareRelease.all_objects, pk=pk)
        release.restore()
        return HttpResponseRedirect(reverse("module_firmware:release_list"))
