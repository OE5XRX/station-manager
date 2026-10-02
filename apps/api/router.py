"""DefaultRouter for the user/automation read API (v1)."""

from rest_framework.routers import DefaultRouter

from apps.api import read_views

router = DefaultRouter()
router.register(r"stations", read_views.StationViewSet, basename="station")
router.register(r"regions", read_views.RegionViewSet, basename="region")
router.register(r"station-tags", read_views.StationTagViewSet, basename="station-tag")
