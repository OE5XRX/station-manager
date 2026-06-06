from django.conf import settings
from django.contrib import messages
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

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["import_form"] = ImageImportForm()
        ctx["recent_jobs"] = ImageImportJob.objects.order_by("-created_at")[:10]
        # KPI tile aggregates: cheap counts over small tables.
        ctx["latest_total"] = ImageRelease.objects.filter(is_latest=True).count()
        ctx["pending_jobs"] = ImageImportJob.objects.filter(
            status__in=[
                ImageImportJob.Status.PENDING,
                ImageImportJob.Status.RUNNING,
            ],
        ).count()
        ctx["storage_backend_label"] = _storage_backend_label()
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
        release = get_object_or_404(ImageRelease, pk=pk)
        release.is_latest = True
        release.save()
        messages.success(request, _("Marked as latest."))
        return redirect("images:list")


class ImageDeleteView(AdminRequiredMixin, View):
    def post(self, request, pk):
        release = get_object_or_404(ImageRelease, pk=pk)
        storage.delete(release.s3_key)
        if release.cosign_bundle_s3_key:
            storage.delete(release.cosign_bundle_s3_key)
        if release.rootfs_s3_key:
            storage.delete(release.rootfs_s3_key)
        release.delete()
        messages.success(request, _("Release deleted."))
        return redirect("images:list")
