from django.urls import path

from . import views

app_name = "sso"

urlpatterns = [
    path("", views.SsoDashboardView.as_view(), name="dashboard"),
    path(
        "applications/<int:pk>/",
        views.ApplicationDetailView.as_view(),
        name="application_detail",
    ),
    path(
        "grants/toggle/<int:user_id>/<int:application_id>/",
        views.GrantToggleView.as_view(),
        name="grant_toggle",
    ),
]
