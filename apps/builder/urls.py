from django.urls import path

from apps.builder.views import (
    BuildConfigCreateView,
    BuildConfigDetailView,
    BuildConfigListView,
    BuildConfigUpdateView,
    BuildJobDetailView,
    BuildJobListView,
    BuildJobTriggerView,
)

app_name = "builder"

urlpatterns = [
    path("configs/", BuildConfigListView.as_view(), name="buildconfig_list"),
    path("configs/create/", BuildConfigCreateView.as_view(), name="buildconfig_create"),
    path("configs/<int:pk>/", BuildConfigDetailView.as_view(), name="buildconfig_detail"),
    path("configs/<int:pk>/edit/", BuildConfigUpdateView.as_view(), name="buildconfig_edit"),
    path("configs/<int:pk>/build/", BuildJobTriggerView.as_view(), name="buildconfig_build"),
    path("jobs/", BuildJobListView.as_view(), name="buildjob_list"),
    path("jobs/<int:pk>/", BuildJobDetailView.as_view(), name="buildjob_detail"),
]
