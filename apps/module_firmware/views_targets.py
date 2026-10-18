"""Firmware targets (desired version per module type) in the GUI."""

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import CreateView, ListView, UpdateView

from apps.accounts.permissions import AreaPermissionMixin

from . import ops
from .forms import ModuleFirmwareTargetForm
from .models import ModuleFirmwareRelease, ModuleFirmwareTarget, ModuleType
from .ops import target_details
from .ops import target_label as _target_label

SCOPE_ORDER = {
    ModuleFirmwareTarget.Scope.FLEET: 0,
    ModuleFirmwareTarget.Scope.TAG: 1,
    ModuleFirmwareTarget.Scope.STATION: 2,
}


class TargetListView(AreaPermissionMixin, ListView):
    area = "modules"
    model = ModuleFirmwareTarget
    template_name = "module_firmware/target_list.html"
    context_object_name = "targets"

    def get_queryset(self):
        return ModuleFirmwareTarget.objects.select_related(
            "module_type", "tag", "station", "canary_tag", "created_by"
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        targets = list(context["targets"])
        type_ids = {t.module_type_id for t in targets}
        coverage = ops.variant_coverage_map(type_ids)
        groups = {}
        for t in sorted(
            targets,
            key=lambda t: (
                t.module_type.display_name,
                SCOPE_ORDER.get(t.scope, 9),
                t.tag.name if t.tag_id else "",
                t.station.name if t.station_id else "",
            ),
        ):
            t.variant_coverage = sorted(coverage.get((t.module_type_id, t.version), []))
            groups.setdefault(t.module_type_id, {"module_type": t.module_type, "targets": []})
            groups[t.module_type_id]["targets"].append(t)
        context["groups"] = list(groups.values())
        context["has_releases"] = ModuleFirmwareRelease.objects.exists()
        context["types_without_target"] = (
            # Default manager = non-archived; pk__in avoids the reverse-FK
            # LEFT JOIN that also matched types with no release at all.
            ModuleType.objects.exclude(pk__in=type_ids)
            .filter(pk__in=ModuleFirmwareRelease.objects.values("module_type_id"))
            .order_by("display_name")
        )
        return context


class _TargetFormMixin:
    model = ModuleFirmwareTarget
    form_class = ModuleFirmwareTargetForm
    template_name = "module_firmware/target_form.html"
    success_url = reverse_lazy("module_firmware:target_list")

    def get_queryset(self):
        return ModuleFirmwareTarget.objects.select_related(
            "module_type", "tag", "station", "canary_tag"
        )


class TargetCreateView(AreaPermissionMixin, _TargetFormMixin, CreateView):
    area = "modules"
    verb = "manage"

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        response = super().form_valid(form)
        ops.after_target_created(self.request, self.object)
        messages.success(self.request, _("Firmware target created."))
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_title"] = _("New firmware target")
        return context


class TargetUpdateView(AreaPermissionMixin, _TargetFormMixin, UpdateView):
    area = "modules"
    verb = "manage"

    def form_valid(self, form):
        before = ModuleFirmwareTarget.objects.select_related(
            "module_type", "tag", "station", "canary_tag"
        ).get(pk=self.object.pk)
        response = super().form_valid(form)
        if form.has_changed():
            # The op itself no-ops (no audit/reconcile) when no audited field changed.
            ops.after_target_updated(self.request, before, self.object)
        messages.success(self.request, _("Firmware target updated."))
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_title"] = _("Edit firmware target")
        return context


class TargetDeleteView(AreaPermissionMixin, View):
    area = "modules"
    verb = "manage"
    http_method_names = ["post"]

    def post(self, request, pk):
        target = get_object_or_404(
            ModuleFirmwareTarget.objects.select_related(
                "module_type", "tag", "station", "canary_tag"
            ),
            pk=pk,
        )
        details = target_details(target)
        label = _target_label(target)
        target.delete()
        ops.after_target_deleted(request, target, details, label)
        messages.success(request, _("Firmware target deleted."))
        return redirect("module_firmware:target_list")
