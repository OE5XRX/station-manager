"""django-filter FilterSets for the user/automation read API."""

import django_filters

from apps.deployments.models import Deployment, DeploymentResult
from apps.images.models import ImageRelease
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
