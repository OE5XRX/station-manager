"""django-filter FilterSets for the user/automation read API."""

import django_filters

from apps.deployments.models import Deployment, DeploymentResult
from apps.images.models import ImageRelease
from apps.module_firmware.models import (
    Module,
    ModuleFirmwareImportJob,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
)
from apps.monitoring.models import Alert
from apps.provisioning.models import ProvisioningJob
from apps.stations.models import Station


class StationFilter(django_filters.FilterSet):
    class Meta:
        model = Station
        fields = {"status": ["exact"], "region": ["exact"], "tags": ["exact"]}


class DeploymentFilter(django_filters.FilterSet):
    class Meta:
        model = Deployment
        fields = {"status": ["exact"], "target_type": ["exact"]}


class DeploymentResultFilter(django_filters.FilterSet):
    class Meta:
        model = DeploymentResult
        fields = {"deployment": ["exact"], "station": ["exact"], "status": ["exact"]}


class AlertFilter(django_filters.FilterSet):
    class Meta:
        model = Alert
        fields = {
            "station": ["exact"],
            "severity": ["exact"],
            "is_resolved": ["exact"],
            "is_acknowledged": ["exact"],
            "alert_rule": ["exact"],
        }


class ImageReleaseFilter(django_filters.FilterSet):
    class Meta:
        model = ImageRelease
        fields = {"machine": ["exact"], "channel": ["exact"], "is_latest": ["exact"]}


class ProvisioningJobFilter(django_filters.FilterSet):
    class Meta:
        model = ProvisioningJob
        fields = {"station": ["exact"], "status": ["exact"]}


class ModuleFilter(django_filters.FilterSet):
    module_type_key = django_filters.CharFilter(field_name="module_type__key")
    # Current location only: the open assignment row, never history.
    station = django_filters.NumberFilter(method="filter_station")

    class Meta:
        model = Module
        fields = {
            "module_type": ["exact"],
            "lifecycle_status": ["exact"],
            "registration_status": ["exact"],
            "uid_source": ["exact"],
            "firmware_convergence": ["exact"],
            "uid": ["exact"],
        }

    def filter_station(self, queryset, name, value):
        return queryset.filter(
            assignments__station_id=value, assignments__to_ts__isnull=True
        ).distinct()


class ModuleFirmwareReleaseFilter(django_filters.FilterSet):
    """List-only (see ModuleFirmwareReleaseViewSet.filter_queryset); runs over
    ``all_objects`` and drops archived (soft-deleted) rows unless
    ``include_archived=true`` — same default as the GUI list."""

    include_archived = django_filters.BooleanFilter(method="filter_include_archived")

    class Meta:
        model = ModuleFirmwareRelease
        fields = {"module_type": ["exact"], "variant": ["exact"]}

    def filter_queryset(self, queryset):
        if not self.form.cleaned_data.get("include_archived"):
            queryset = queryset.filter(archived_at__isnull=True)
        return super().filter_queryset(queryset)

    def filter_include_archived(self, queryset, name, value):
        return queryset  # applied in filter_queryset (must also act when absent)


class ModuleFirmwareImportJobFilter(django_filters.FilterSet):
    class Meta:
        model = ModuleFirmwareImportJob
        fields = {"module_type": ["exact"], "status": ["exact"]}


class ModuleFirmwareTargetFilter(django_filters.FilterSet):
    class Meta:
        model = ModuleFirmwareTarget
        fields = {
            "module_type": ["exact"],
            "scope": ["exact"],
            "station": ["exact"],
            "tag": ["exact"],
        }
