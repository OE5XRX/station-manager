from django.urls import path

from . import api_views, reconcile_api_views

urlpatterns = [
    path("<int:pk>/download/", api_views.ModuleFirmwareDownloadView.as_view(), name="download"),
    path(
        "reconcile/check/",
        reconcile_api_views.ReconcileCheckView.as_view(),
        name="reconcile_check",
    ),
]
