from django.urls import path

from . import views

app_name = "provisioning"

urlpatterns = [
    path(
        "station/<int:station_pk>/new/",
        views.CreateProvisioningJobView.as_view(),
        name="new",
    ),
    path(
        "<uuid:pk>/status/",
        views.ProvisioningJobStatusView.as_view(),
        name="status",
    ),
    path(
        "<uuid:pk>/download/",
        views.ProvisioningJobDownloadView.as_view(),
        name="download",
    ),
]
