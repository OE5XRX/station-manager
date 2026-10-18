"""Firmware targets (desired version per module type) in the GUI."""

from collections import defaultdict

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import CreateView, ListView, UpdateView

from apps.accounts.permissions import AreaPermissionMixin
from apps.stations.models import StationAuditLog

from .audit import audit_config, audit_station
from .forms import ModuleFirmwareTargetForm
from .models import ModuleFirmwareRelease, ModuleFirmwareTarget, ModuleType
from .services import reconcile_module_type

SCOPE_ORDER = {
    ModuleFirmwareTarget.Scope.FLEET: 0,
    ModuleFirmwareTarget.Scope.TAG: 1,
    ModuleFirmwareTarget.Scope.STATION: 2,
}


def target_details(target):
    """Audit payload describing a target (slugs/names, not pks)."""
    return {
        "module_type": target.module_type.key,
        "scope": target.scope,
        "version": target.version,
        "tag": target.tag.slug if target.tag_id else None,
        "station": target.station.name if target.station_id else None,
        "canary_tag": target.canary_tag.slug if target.canary_tag_id else None,
    }


def _target_label(target):
    if target.scope == ModuleFirmwareTarget.Scope.TAG:
        return f"{target.module_type.key} tag {target.tag.slug}"
    if target.scope == ModuleFirmwareTarget.Scope.STATION:
        return f"{target.module_type.key} station {target.station.name}"
    return f"{target.module_type.key} fleet"


def _audit_station_scope(request, target, event_type, message):
    if target.scope == ModuleFirmwareTarget.Scope.STATION and target.station_id:
        audit_station(
            request,
            event_type=event_type,
            station=target.station,
            message=message,
            changes={"module_type": target.module_type.key, "version": target.version},
        )


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
        coverage = defaultdict(list)
        type_ids = {t.module_type_id for t in targets}
        for type_id, version, variant in ModuleFirmwareRelease.objects.filter(
            module_type_id__in=type_ids
        ).values_list("module_type_id", "version", "variant"):
            coverage[(type_id, version)].append(variant or "—")
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
            ModuleType.objects.exclude(pk__in=type_ids)
            .filter(firmware_releases__archived_at__isnull=True)
            .distinct()
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
        target = self.object
        audit_config(
            self.request,
            obj="module_firmware_target",
            action="created",
            message=f"Firmware target {_target_label(target)} = {target.version} created",
            details=target_details(target),
        )
        _audit_station_scope(
            self.request,
            target,
            StationAuditLog.EventType.FIRMWARE_TARGET_SET,
            f"Firmware target {target.module_type.key} = {target.version} set for station.",
        )
        reconcile_module_type(target.module_type)
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
        old = target_details(before)
        response = super().form_valid(form)
        target = self.object
        new = target_details(target)
        changes = {k: {"old": old[k], "new": new[k]} for k in new if old[k] != new[k]}
        if not form.has_changed() or not changes:
            # No-op save: nothing to audit, nothing to reconcile.
            messages.success(self.request, _("Firmware target updated."))
            return response
        audit_config(
            self.request,
            obj="module_firmware_target",
            action="updated",
            message=f"Firmware target {_target_label(target)} = {target.version} updated",
            details={**new, "changes": changes},
        )
        _audit_station_scope(
            self.request,
            target,
            StationAuditLog.EventType.FIRMWARE_TARGET_SET,
            f"Firmware target {target.module_type.key} = {target.version} set for station.",
        )
        if before.station_id and before.station_id != target.station_id:
            # Moved off a station (other station or other scope): that station
            # lost its override.
            _audit_station_scope(
                self.request,
                before,
                StationAuditLog.EventType.FIRMWARE_TARGET_REMOVED,
                f"Firmware target {before.module_type.key} = {before.version} removed "
                "for station.",
            )
        reconcile_module_type(target.module_type)
        if before.module_type_id != target.module_type_id:
            reconcile_module_type(before.module_type)
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
        audit_config(
            request,
            obj="module_firmware_target",
            action="deleted",
            message=f"Firmware target {label} = {details['version']} deleted",
            details=details,
        )
        _audit_station_scope(
            request,
            target,
            StationAuditLog.EventType.FIRMWARE_TARGET_REMOVED,
            f"Firmware target {target.module_type.key} = {target.version} removed for station.",
        )
        reconcile_module_type(target.module_type)
        messages.success(request, _("Firmware target deleted."))
        return redirect("module_firmware:target_list")
