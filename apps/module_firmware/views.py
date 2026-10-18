from django.contrib import messages
from django.db import transaction
from django.db.models import Prefetch, Q
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import DetailView, ListView

from apps.accounts.permissions import AreaPermissionMixin
from apps.images import github_releases
from apps.stations.models import Station

from . import services, trust
from .audit import audit_config
from .models import (
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareImportJob,
    ModuleFirmwareRelease,
    ModuleType,
)
from .releases import parse_variant_assets
from .views_convergence import firmware_row

# ---------------------------------------------------------------------------
# Module-type helpers
# ---------------------------------------------------------------------------


def _trusted_repo_q():
    """Q matching module types whose firmware_repo owner is a trusted signer."""
    q = Q(pk__in=[])
    for owner in trust.trusted_owners():
        q |= Q(firmware_repo__istartswith=f"{owner}/")
    return q


def _importable_module_types():
    """Module types that can actually produce a successful import job.

    Requires firmware_repo AND release_asset_prefix, and a firmware_repo owner
    in MODULE_FIRMWARE_TRUSTED_REPO_OWNERS (the importer rejects anything else),
    so the UI never offers doomed imports.
    """
    return (
        ModuleType.objects.exclude(firmware_repo="")
        .exclude(release_asset_prefix="")
        .filter(_trusted_repo_q())
    )


def import_blockers(module_type):
    """Human-readable reasons why ``module_type`` is not import-ready ([] = ready)."""
    reasons = []
    if not module_type.firmware_repo:
        reasons.append(str(ModuleType._meta.get_field("firmware_repo").verbose_name))
    if not module_type.release_asset_prefix:
        reasons.append(str(ModuleType._meta.get_field("release_asset_prefix").verbose_name))
    if module_type.firmware_repo and not trust.is_trusted_repo(module_type.firmware_repo):
        reasons.append(_("untrusted repo owner"))
    return reasons


_ACTIVE_JOB_STATUSES = [
    ModuleFirmwareImportJob.Status.PENDING,
    ModuleFirmwareImportJob.Status.RUNNING,
]


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


class ModuleListView(AreaPermissionMixin, ListView):
    area = "modules"
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


class ModuleDetailView(AreaPermissionMixin, DetailView):
    area = "modules"
    model = Module
    template_name = "module_firmware/module_detail.html"
    context_object_name = "module"
    slug_field = "uid"
    slug_url_kwarg = "uid"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        m = self.object
        ctx["assignments"] = m.assignments.select_related("station").order_by("-from_ts")
        ctx["current_assignment"] = (
            m.assignments.filter(to_ts__isnull=True).select_related("station").first()
        )
        ctx["firmware"] = firmware_row(m, ctx["current_assignment"])
        ctx["audit_logs"] = m.audit_logs.select_related("station", "user").all()[:200]
        # Don't offer `ready` while an assignment is open — it's assignment-derived
        # and set_lifecycle rejects it in that state.
        ctx["lifecycles"] = operator_lifecycle_choices(
            has_open_assignment=bool(ctx["current_assignment"])
        )
        return ctx


class _ModuleActionMixin:
    """Helpers shared by the module POST actions (permission comes from the area mixin)."""

    def get_module(self):
        return get_object_or_404(Module, uid=self.kwargs["uid"])

    def _redirect(self, uid):
        return HttpResponseRedirect(reverse("module_firmware:module_detail", args=[uid]))


class ModuleConfirmView(AreaPermissionMixin, _ModuleActionMixin, View):
    area = "modules"
    verb = "manage"

    def post(self, request, uid):
        m = self.get_module()
        services.confirm_registration(m, user=request.user)
        return self._redirect(uid)


class ModuleLifecycleView(AreaPermissionMixin, _ModuleActionMixin, View):
    area = "modules"
    verb = "manage"

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


class ModuleNotesView(AreaPermissionMixin, _ModuleActionMixin, View):
    area = "modules"
    verb = "manage"

    def post(self, request, uid):
        m = self.get_module()
        m.notes = request.POST.get("notes", "")
        m.save(update_fields=["notes", "updated_at"])
        return self._redirect(uid)


# ---------------------------------------------------------------------------
# Firmware release views
# ---------------------------------------------------------------------------


class FirmwareReleaseListView(AreaPermissionMixin, ListView):
    area = "modules"
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
        all_types = list(ModuleType.objects.all())
        ctx["all_types_count"] = len(all_types)
        ctx["incomplete_types"] = [
            {"type": t, "missing": missing} for t in all_types if (missing := import_blockers(t))
        ]
        ctx.update(_import_jobs_context())
        return ctx


def _import_jobs_context():
    jobs = list(
        ModuleFirmwareImportJob.objects.select_related("module_type", "requested_by", "release")[
            :20
        ]
    )
    # has_active is queried over ALL jobs, not just the 20 shown: an old
    # still-running job must keep the panel polling.
    has_active = ModuleFirmwareImportJob.objects.filter(status__in=_ACTIVE_JOB_STATUSES).exists()
    return {"jobs": jobs, "has_active": has_active}


class ImportJobsPartialView(AreaPermissionMixin, View):
    """HTMX partial: last 20 import jobs; self-polls while any is pending/running."""

    area = "modules"

    def get(self, request):
        ctx = _import_jobs_context()
        # Only the self-poll fetches this partial via HTMX; when that poll sees
        # the queue drained, prompt a reload so the release table catches up.
        ctx["polled"] = request.headers.get("HX-Request") == "true"
        return render(request, "module_firmware/_import_jobs.html", ctx)


class GitHubFirmwareReleasesPartialView(AreaPermissionMixin, View):
    area = "modules"
    verb = "manage"

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
        blockers = import_blockers(module_type)
        if blockers:
            return render(
                request,
                "module_firmware/_github_releases.html",
                {
                    "error": _("This module type is not ready for import: %(reasons)s.")
                    % {"reasons": ", ".join(blockers)},
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
                module_type=module_type, status__in=_ACTIVE_JOB_STATUSES
            ).values_list("tag", flat=True)
        )
        # Variants actually present in the fleet for this type — a release
        # lacking one of them would leave those modules without a candidate.
        fleet_variants = set(
            Module.objects.filter(module_type=module_type)
            .exclude(variant="")
            .values_list("variant", flat=True)
        )
        existing = set(
            ModuleFirmwareRelease.objects.filter(module_type=module_type).values_list(
                "source_tag", "variant"
            )
        )

        rows = []
        for rel in releases:
            parsed_variants = parse_variant_assets(
                set(rel.asset_names), module_type.release_asset_prefix
            )
            variants = [va.variant for va in parsed_variants]
            # no_assets first: a release with no matching asset pair can never
            # import, whatever job history it has.  A tag is only "imported"
            # once EVERY variant it offers has an active row; a
            # partial import stays importable so the operator can fill the gap.
            if not parsed_variants:
                state = "no_assets"
            elif all((rel.tag, v) in existing for v in variants):
                state = "imported"
            elif rel.tag in in_flight_tags:
                state = "queued"
            else:
                state = "ready"
            rows.append(
                {
                    "release": rel,
                    "state": state,
                    "variants": variants,
                    "missing_variants": sorted(fleet_variants - set(variants)),
                }
            )

        return render(
            request,
            "module_firmware/_github_releases.html",
            {
                "rows": rows,
                "module_type": module_type,
                "module_types": _importable_module_types(),
            },
        )


class FirmwareImportView(AreaPermissionMixin, View):
    area = "modules"
    verb = "manage"

    def post(self, request):
        module_type_id = request.POST.get("module_type")
        tag = request.POST.get("tag", "").strip()
        redirect = HttpResponseRedirect(reverse("module_firmware:release_list"))
        # Blank tag / id guard stays outside the atomic block — these never
        # touch the DB and need no lock.
        if not tag or not module_type_id:
            messages.error(request, _("Select a module type and a release tag to import."))
            return redirect
        created = False
        # Resolve + lock the module type row INSIDE the atomic block to
        # serialise concurrent POSTs for the same type on Postgres.  SQLite
        # silently ignores select_for_update but stays green.
        with transaction.atomic():
            module_type = ModuleType.objects.select_for_update().filter(pk=module_type_id).first()
            if module_type is None:
                messages.error(request, _("Module type not found."))
                return redirect
            # Reject not-import-ready types (missing repo/prefix, untrusted
            # owner) — a doomed job can never succeed and just adds noise.
            blockers = import_blockers(module_type)
            if blockers:
                messages.error(
                    request,
                    _("%(type)s is not ready for import: %(reasons)s.")
                    % {"type": module_type, "reasons": ", ".join(blockers)},
                )
                return redirect
            # Dedup: only prevent duplicate in-flight work.  The importer is
            # per-variant idempotent (skips active variants, restores archived),
            # so we do NOT block on active-release existence — a tag with some
            # variants active and others archived/missing must be re-importable.
            in_flight_exists = ModuleFirmwareImportJob.objects.filter(
                module_type=module_type, tag=tag, status__in=_ACTIVE_JOB_STATUSES
            ).exists()
            if not in_flight_exists:
                ModuleFirmwareImportJob.objects.create(
                    module_type=module_type,
                    source_repo=module_type.firmware_repo,
                    tag=tag,
                    status=ModuleFirmwareImportJob.Status.PENDING,
                    requested_by=request.user,
                )
                created = True
        if created:
            audit_config(
                request,
                obj="module_firmware_import",
                action="queued",
                message=f"Firmware import queued: {module_type.key} {tag}",
                details={"module_type": module_type.key, "tag": tag},
            )
            messages.success(request, _("Import of %(tag)s queued.") % {"tag": tag})
        else:
            messages.info(
                request, _("An import of %(tag)s is already in progress.") % {"tag": tag}
            )
        return redirect


def _audit_release(request, release, action):
    audit_config(
        request,
        obj="module_firmware_release",
        action=action,
        message=f"Firmware release {action}: {release}",
        details={
            "module_type": release.module_type.key,
            "variant": release.variant,
            "version": release.version,
        },
    )


class FirmwareArchiveView(AreaPermissionMixin, View):
    area = "modules"
    verb = "manage"

    def post(self, request, pk):
        release = get_object_or_404(
            ModuleFirmwareRelease.all_objects.select_related("module_type"), pk=pk
        )
        if release.archive():
            _audit_release(request, release, "archived")
            messages.success(request, _("Release %(rel)s archived.") % {"rel": release})
        else:
            messages.info(request, _("Release %(rel)s was already archived.") % {"rel": release})
        return HttpResponseRedirect(reverse("module_firmware:release_list"))


class FirmwareRestoreView(AreaPermissionMixin, View):
    area = "modules"
    verb = "manage"

    def post(self, request, pk):
        release = get_object_or_404(
            ModuleFirmwareRelease.all_objects.select_related("module_type"), pk=pk
        )
        if release.restore():
            _audit_release(request, release, "restored")
            messages.success(request, _("Release %(rel)s restored.") % {"rel": release})
        else:
            messages.info(request, _("Release %(rel)s is not archived.") % {"rel": release})
        return HttpResponseRedirect(reverse("module_firmware:release_list"))
