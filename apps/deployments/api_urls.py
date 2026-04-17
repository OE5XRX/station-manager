from django.urls import path

from apps.deployments.api_views import (
    DeploymentCheckView,
    DeploymentCommitView,
    DeploymentDownloadView,
    DeploymentStatusUpdateView,
)

urlpatterns = [
    path("check/", DeploymentCheckView.as_view(), name="deployment_check"),
    path(
        "<int:pk>/status/",
        DeploymentStatusUpdateView.as_view(),
        name="deployment_status_update",
    ),
    path("commit/", DeploymentCommitView.as_view(), name="deployment_commit"),
    path("<int:pk>/download/", DeploymentDownloadView.as_view(), name="deployment_download"),
]
