from django.urls import path

from apps.deployments.views import (
    DeploymentCancelView,
    DeploymentCreateView,
    DeploymentDetailView,
    DeploymentListView,
)

app_name = "deployments"

urlpatterns = [
    path("", DeploymentListView.as_view(), name="deployment_list"),
    path("create/", DeploymentCreateView.as_view(), name="deployment_create"),
    path("<int:pk>/", DeploymentDetailView.as_view(), name="deployment_detail"),
    path("<int:pk>/cancel/", DeploymentCancelView.as_view(), name="deployment_cancel"),
]
