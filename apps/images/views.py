from django.conf import settings
from django.contrib import messages
from django.db.models import ProtectedError
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import FormView, ListView

from apps.accounts.views import AdminRequiredMixin

from . import storage
from .forms import ImageImportForm
from .models import ImageImportJob, ImageRelease


def _storage_backend_label() -> str:
    """Short label for the active default-storage backend.

    Settings flip STORAGES["default"]["BACKEND"] to the S3 backend
    only when USE_S3=true; otherwise Django's FileSystemStorage is
    used. The Image-Releases KPI tile shows this label so operators
    can tell at a glance whether artifacts are landing in object
    storage or on the local filesystem.
    """
    backend = settings.STORAGES.get("default", {}).get("BACKEND", "")
    if "s3" in backend.lower():
        return _("S3")
    return _("Local FS")


class ImageListView(AdminRequiredMixin, ListView):
    model = ImageRelease
    template_name = "images/image_list.html"
    context_object_name = "releases"

    def _show_archived(self) -> bool:
        return self.request.GET.get("show_archived") == "1"

    def get_queryset(self):
        # all_objects when the toggle is on so archived rows appear
        # alongside active; default manager (objects) otherwise.
        manager = ImageRelease.all_objects if self._show_archived() else ImageRelease.objects
        return manager.all()

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["import_form"] = ImageImportForm()
        ctx["recent_jobs"] = ImageImportJob.objects.order_by("-created_at")[:10]
        # KPI tile aggregates — always over the ACTIVE set (the default
        # manager) regardless of the show_archived toggle. KPIs should
        # describe the operational state, not the toggle's UI mode.
        # The "Releases on file" tile needs its own count rather than
        # reusing `releases|length` because that queryset switches to
        # all_objects when the toggle is on.
        ctx["active_total"] = ImageRelease.objects.count()
        ctx["latest_total"] = ImageRelease.objects.filter(is_latest=True).count()
        ctx["pending_jobs"] = ImageImportJob.objects.filter(
            status__in=[
                ImageImportJob.Status.PENDING,
                ImageImportJob.Status.RUNNING,
            ],
        ).count()
        ctx["storage_backend_label"] = _storage_backend_label()
        ctx["show_archived"] = self._show_archived()
        return ctx


class ImageImportView(AdminRequiredMixin, FormView):
    form_class = ImageImportForm
    template_name = "images/image_list.html"
    success_url = reverse_lazy("images:list")

    def form_valid(self, form):
        ImageImportJob.objects.create(
            tag=form.cleaned_data["tag"],
            machine=form.cleaned_data["machine"],
            mark_as_latest=form.cleaned_data["mark_as_latest"],
            requested_by=self.request.user,
        )
        messages.success(
            self.request,
            _("Import queued. It will appear below in a minute or two."),
        )
        return super().form_valid(form)

    def form_invalid(self, form):
        # The list template needs import_form/releases/recent_jobs context,
        # which FormView does not supply. Since the form has only two fields
        # (tag + machine choice) a dedicated error page is not warranted —
        # surface the errors via messages and bounce back to the list.
        messages.error(
            self.request,
            _("Invalid import request: %(errors)s") % {"errors": form.errors.as_text()},
        )
        return redirect("images:list")


class ImageMarkLatestView(AdminRequiredMixin, View):
    def post(self, request, pk):
        release = get_object_or_404(ImageRelease.all_objects, pk=pk)
        if release.archived_at is not None:
            # "Latest archived" cannot exist — see ImageRelease.archive().
            # Without this guard a hand-crafted URL (or a UI bug exposing
            # the button on an archived row) could flip is_latest=True on
            # an archived row and leave the machine with no UI-visible
            # latest release (the default manager hides archived rows).
            messages.error(
                request,
                _(
                    "Cannot mark archived release %(tag)s (%(machine)s) "
                    "as latest. Restore it first."
                )
                % {"tag": release.tag, "machine": release.machine},
            )
            return redirect("images:list")
        release.is_latest = True
        release.save()
        messages.success(request, _("Marked as latest."))
        return redirect("images:list")


class ImageDeleteView(AdminRequiredMixin, View):
    def post(self, request, pk):
        release = get_object_or_404(ImageRelease.all_objects, pk=pk)

        # Probe DB-level deletability BEFORE touching S3. Deployment and
        # ProvisioningJob hold PROTECT FKs to ImageRelease because they
        # are an audit trail — we must not silently destroy that history,
        # and we must not orphan the DB row by deleting the S3 objects
        # first and then failing the DB delete (rolling back release.delete
        # doesn't un-delete S3).
        blockers = _delete_blockers(release)
        if blockers:
            messages.error(
                request,
                _(
                    "Cannot delete release %(tag)s (%(machine)s): "
                    "still referenced by %(blockers)s. "
                    "Remove the referencing records first."
                )
                % {
                    "tag": release.tag,
                    "machine": release.machine,
                    "blockers": ", ".join(blockers),
                },
            )
            return redirect("images:list")

        try:
            storage.delete(release.s3_key)
            if release.cosign_bundle_s3_key:
                storage.delete(release.cosign_bundle_s3_key)
            if release.rootfs_s3_key:
                storage.delete(release.rootfs_s3_key)
            release.delete()
        except ProtectedError as exc:
            # Defensive — a race could land a Deployment between our probe
            # and the delete. Surface it the same way; S3 objects MAY have
            # been removed by now, but the audit row is preserved.
            messages.error(
                request,
                _("Cannot delete release %(tag)s (%(machine)s): %(detail)s")
                % {
                    "tag": release.tag,
                    "machine": release.machine,
                    "detail": str(exc),
                },
            )
            return redirect("images:list")

        messages.success(request, _("Release deleted."))
        return redirect("images:list")


def _delete_blockers(release: ImageRelease) -> list[str]:
    """Return human-readable labels for objects that PROTECT this release.

    Kept in sync with the on_delete=PROTECT FKs declared on
    ``apps.deployments.models.Deployment.image_release`` and
    ``apps.provisioning.models.ProvisioningJob.image_release``. The
    ``stations.Station.current_image_release`` FK is SET_NULL and
    therefore does NOT block deletion.
    """
    parts: list[str] = []
    deployments = release.deployments.count()
    if deployments:
        parts.append(
            _("%(n)d deployment(s)") % {"n": deployments},
        )
    provisioning = release.provisioning_jobs.count()
    if provisioning:
        parts.append(
            _("%(n)d provisioning job(s)") % {"n": provisioning},
        )
    return [str(p) for p in parts]


class ImageArchiveView(AdminRequiredMixin, View):
    """Soft-delete a release. Always succeeds (vs hard delete which
    PROTECT-FKs from Deployment/ProvisioningJob can block).

    Operates on ``all_objects`` so the same view also accepts an
    already-archived row (idempotent) — but the UI only renders the
    Archive button for active rows, so in practice this is the
    active-row entry path.
    """

    def post(self, request, pk):
        release = get_object_or_404(ImageRelease.all_objects, pk=pk)
        release.archive()
        messages.success(
            request,
            _("Release %(tag)s archived.") % {"tag": release.tag},
        )
        return redirect("images:list")


class ImageRestoreView(AdminRequiredMixin, View):
    """Undo a previous archive. Operates on ``all_objects`` because
    archived rows are hidden from the default manager."""

    def post(self, request, pk):
        release = get_object_or_404(ImageRelease.all_objects, pk=pk)
        release.restore()
        messages.success(
            request,
            _("Release %(tag)s restored.") % {"tag": release.tag},
        )
        return redirect("images:list")
