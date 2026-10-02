"""DefaultRouter for the user/automation read API (v1)."""

from rest_framework.routers import DefaultRouter

from apps.api import read_views

router = DefaultRouter()
router.register(r"stations", read_views.StationViewSet, basename="station")
router.register(r"regions", read_views.RegionViewSet, basename="region")
router.register(r"station-tags", read_views.StationTagViewSet, basename="station-tag")
router.register(
    r"station-assignments", read_views.StationAssignmentViewSet, basename="station-assignment"
)
router.register(
    r"region-assignments", read_views.RegionAssignmentViewSet, basename="region-assignment"
)
router.register(
    r"rollout-sequences", read_views.RolloutSequenceViewSet, basename="rollout-sequence"
)
router.register(
    r"rollout-sequence-entries",
    read_views.RolloutSequenceEntryViewSet,
    basename="rollout-sequence-entry",
)
