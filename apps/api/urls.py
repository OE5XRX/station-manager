from django.urls import include, path

from apps.api.views import HealthCheckView, HeartbeatView, StationInventoryView

app_name = "api"

urlpatterns = [
    path("v1/health/", HealthCheckView.as_view(), name="health"),
    path("v1/heartbeat/", HeartbeatView.as_view(), name="heartbeat"),
    path(
        "v1/stations/<int:station_id>/inventory/",
        StationInventoryView.as_view(),
        name="station_inventory",
    ),
    path("v1/deployments/", include("apps.deployments.api_urls")),
]
