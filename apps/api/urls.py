from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerSplitView

from apps.api.router import router
from apps.api.views import HealthCheckView, HeartbeatView

app_name = "api"

urlpatterns = [
    path("v1/health/", HealthCheckView.as_view(), name="health"),
    path("v1/heartbeat/", HeartbeatView.as_view(), name="heartbeat"),
    path("v1/deployments/", include("apps.deployments.api_urls")),
    path("v1/", include(router.urls)),
    path("v1/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "v1/docs/",
        SpectacularSwaggerSplitView.as_view(url_name="api:schema"),
        name="docs",
    ),
]
