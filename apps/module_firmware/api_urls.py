from django.urls import path

from . import api_views, reconcile_api_views

urlpatterns = [
    path("<int:pk>/download/", api_views.ModuleFirmwareDownloadView.as_view(), name="download"),
    path(
        "reconcile/check/",
        reconcile_api_views.ReconcileCheckView.as_view(),
        name="reconcile_check",
    ),
    path(
        "reconcile/<int:convergence_id>/status/",
        reconcile_api_views.ReconcileStatusUpdateView.as_view(),
        name="reconcile_status",
    ),
    path(
        "reconcile/commit/",
        reconcile_api_views.ReconcileCommitView.as_view(),
        name="reconcile_commit",
    ),
]
