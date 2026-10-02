"""django-filter FilterSets for the user/automation read API."""

import django_filters

from apps.stations.models import Station


class StationFilter(django_filters.FilterSet):
    class Meta:
        model = Station
        fields = {"status": ["exact"], "region": ["exact"], "tags": ["exact"]}
