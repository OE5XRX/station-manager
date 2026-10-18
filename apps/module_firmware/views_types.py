"""Module-type CRUD in the GUI (staff-managed registry of flashable types)."""

from django.contrib import messages
from django.db.models import Count
from django.db.models.deletion import ProtectedError
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

from apps.accounts.permissions import AreaPermissionMixin

from .audit import audit_config
from .forms import ModuleTypeForm
from .models import ModuleFirmwareRelease, ModuleType


def type_in_use(module_type):
    """True if modules, releases (incl. archived) or targets reference the type."""
    return (
        module_type.modules.exists()
        or ModuleFirmwareRelease.all_objects.filter(module_type=module_type).exists()
        or module_type.firmware_targets.exists()
    )


class ModuleTypeListView(AreaPermissionMixin, ListView):
    area = "modules"
    model = ModuleType
    template_name = "module_firmware/type_list.html"
    context_object_name = "types"

    def get_queryset(self):
        return ModuleType.objects.annotate(
            module_count=Count("modules", distinct=True),
            release_count=Count("firmware_releases", distinct=True),
            target_count=Count("firmware_targets", distinct=True),
        ).order_by("key")


class ModuleTypeCreateView(AreaPermissionMixin, CreateView):
    area = "modules"
    verb = "manage"
    model = ModuleType
    form_class = ModuleTypeForm
    template_name = "module_firmware/type_form.html"
    success_url = reverse_lazy("module_firmware:type_list")

    def form_valid(self, form):
        response = super().form_valid(form)
        audit_config(
            self.request,
            obj="module_type",
            action="created",
            message=f"Module type {self.object.key} created",
            details={"key": self.object.key},
        )
        messages.success(self.request, _("Module type created."))
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_title"] = _("New module type")
        return context


class ModuleTypeUpdateView(AreaPermissionMixin, UpdateView):
    area = "modules"
    verb = "manage"
    model = ModuleType
    form_class = ModuleTypeForm
    template_name = "module_firmware/type_form.html"
    success_url = reverse_lazy("module_firmware:type_list")

    def form_valid(self, form):
        changes = {
            f: {
                "old": str(form.initial.get(f, "")),
                "new": str(form.cleaned_data.get(f, "")),
            }
            for f in form.changed_data
        }
        response = super().form_valid(form)
        audit_config(
            self.request,
            obj="module_type",
            action="updated",
            message=f"Module type {self.object.key} updated",
            details={"key": self.object.key, "changes": changes},
        )
        messages.success(self.request, _("Module type updated."))
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_title"] = _("Edit module type")
        return context


class ModuleTypeDeleteView(AreaPermissionMixin, DeleteView):
    area = "modules"
    verb = "manage"
    model = ModuleType
    template_name = "module_firmware/type_confirm_delete.html"
    context_object_name = "mtype"
    success_url = reverse_lazy("module_firmware:type_list")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["in_use"] = type_in_use(self.object)
        return context

    def _blocked(self):
        messages.error(
            self.request,
            _("This module type is in use by modules, releases or targets and cannot be deleted."),
        )
        return redirect(self.success_url)

    def form_valid(self, form):
        if type_in_use(self.object):
            return self._blocked()
        key = self.object.key
        try:
            response = super().form_valid(form)
        except ProtectedError:
            return self._blocked()
        audit_config(
            self.request,
            obj="module_type",
            action="deleted",
            message=f"Module type {key} deleted",
            details={"key": key},
        )
        messages.success(self.request, _("Module type deleted."))
        return response
