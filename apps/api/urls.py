from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerSplitView

from apps.api.diagnostics_views import StationAudioDiagnosticView
from apps.api.router import router
from apps.api.views import HealthCheckView, HeartbeatView

app_name = "api"

urlpatterns = [
    path("v1/health/", HealthCheckView.as_view(), name="health"),
    path("v1/heartbeat/", HeartbeatView.as_view(), name="heartbeat"),
    path("v1/deployments/", include("apps.deployments.api_urls")),
    # Station-scoped action endpoints — must come BEFORE the router include so
    # the router's catch-all doesn't shadow them.
    path(
        "v1/stations/<int:pk>/audio-diagnostics/",
        StationAudioDiagnosticView.as_view(),
        name="station-audio-diagnostics",
    ),
    path("v1/", include(router.urls)),
    path("v1/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "v1/docs/",
        SpectacularSwaggerSplitView.as_view(url_name="api:schema"),
        name="docs",
    ),
]
