"""django-filter FilterSets for the user/automation read API."""

import django_filters

from apps.deployments.models import Deployment, DeploymentResult
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
