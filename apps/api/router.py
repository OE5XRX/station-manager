"""DefaultRouter for the user/automation read API (v1)."""

from rest_framework.authentication import SessionAuthentication
from rest_framework.routers import APIRootView, DefaultRouter

from apps.api import read_views
from apps.api.authentication import PersonalAccessTokenAuthentication
from apps.api.permissions import TopologyScopedPermission


class ScopedAPIRootView(APIRootView):
    """API root view with the same auth/permission policy as the viewsets."""

    authentication_classes = [PersonalAccessTokenAuthentication, SessionAuthentication]
    permission_classes = [TopologyScopedPermission]


class ScopedDefaultRouter(DefaultRouter):
    APIRootView = ScopedAPIRootView


router = ScopedDefaultRouter()
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
router.register(r"deployments", read_views.DeploymentViewSet, basename="deployment")
router.register(
    r"deployment-results", read_views.DeploymentResultViewSet, basename="deployment-result"
)
router.register(r"alert-rules", read_views.AlertRuleViewSet, basename="alert-rule")
router.register(r"alerts", read_views.AlertViewSet, basename="alert")
router.register(
    r"provisioning-jobs", read_views.ProvisioningJobViewSet, basename="provisioning-job"
)
router.register(r"images", read_views.ImageReleaseViewSet, basename="image")
router.register(
    r"image-import-jobs", read_views.ImageImportJobViewSet, basename="image-import-job"
)
router.register(r"users", read_views.UserViewSet, basename="user")
