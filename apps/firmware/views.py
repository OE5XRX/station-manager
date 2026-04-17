import re

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    ListView,
    UpdateView,
    View,
)

from apps.firmware.forms import FirmwareArtifactForm, FirmwareArtifactUpdateForm
from apps.firmware.models import FirmwareArtifact


class AdminOrOperatorMixin(UserPassesTestMixin):
    """Restrict access to admin or operator roles."""

    def test_func(self):
        return self.request.user.is_authenticated and (
            self.request.user.is_admin or self.request.user.is_operator
        )


class FirmwareListView(LoginRequiredMixin, ListView):
    model = FirmwareArtifact
    template_name = "firmware/firmware_list.html"
    context_object_name = "artifacts"
    paginate_by = 25

    def get_template_names(self):
        if self.request.htmx:
            return ["firmware/_firmware_table.html"]
        return [self.template_name]

    def get_queryset(self):
        qs = super().get_queryset().select_related("target_module", "uploaded_by")

        artifact_type = self.request.GET.get("type")
        if artifact_type in ("os_image", "module_firmware"):
            qs = qs.filter(artifact_type=artifact_type)

        module_id = self.request.GET.get("module")
        if module_id:
            qs = qs.filter(target_module_id=module_id)

        stable = self.request.GET.get("stable")
        if stable == "1":
            qs = qs.filter(is_stable=True)

        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["current_type"] = self.request.GET.get("type", "")
        context["current_stable"] = self.request.GET.get("stable", "")
        return context


class FirmwareDetailView(LoginRequiredMixin, DetailView):
    model = FirmwareArtifact
    template_name = "firmware/firmware_detail.html"
    context_object_name = "artifact"

    def get_queryset(self):
        return super().get_queryset().select_related("target_module", "uploaded_by")


class FirmwareCreateView(LoginRequiredMixin, AdminOrOperatorMixin, CreateView):
    model = FirmwareArtifact
    form_class = FirmwareArtifactForm
    template_name = "firmware/firmware_form.html"

    def form_valid(self, form):
        form.instance.uploaded_by = self.request.user
        messages.success(self.request, _("Firmware artifact uploaded successfully."))
        return super().form_valid(form)

    def get_success_url(self):
        return reverse_lazy("firmware:firmware_detail", kwargs={"pk": self.object.pk})


class FirmwareUpdateView(LoginRequiredMixin, AdminOrOperatorMixin, UpdateView):
    model = FirmwareArtifact
    form_class = FirmwareArtifactUpdateForm
    template_name = "firmware/firmware_form.html"

    def form_valid(self, form):
        messages.success(self.request, _("Firmware artifact updated successfully."))
        return super().form_valid(form)

    def get_success_url(self):
        return reverse_lazy("firmware:firmware_detail", kwargs={"pk": self.object.pk})


class FirmwareDeleteView(LoginRequiredMixin, AdminOrOperatorMixin, DeleteView):
    model = FirmwareArtifact
    template_name = "firmware/firmware_confirm_delete.html"
    context_object_name = "artifact"
    success_url = reverse_lazy("firmware:firmware_list")

    def form_valid(self, form):
        messages.success(self.request, _("Firmware artifact deleted."))
        return super().form_valid(form)


class FirmwareDownloadView(LoginRequiredMixin, AdminOrOperatorMixin, View):
    """Serve firmware file for download (admin/operator only)."""

    def get(self, request, pk):
        artifact = get_object_or_404(FirmwareArtifact, pk=pk)
        # Sanitize filename for Content-Disposition header
        safe_name = re.sub(r'["\r\n]', "_", f"{artifact.name}-v{artifact.version}")
        response = FileResponse(
            artifact.file.open("rb"),
            content_type="application/octet-stream",
        )
        response["Content-Disposition"] = f'attachment; filename="{safe_name}"'
        return response
