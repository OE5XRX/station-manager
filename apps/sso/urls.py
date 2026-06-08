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
    path(
        "sessions/<int:pk>/revoke/",
        views.SessionRevokeView.as_view(),
        name="session_revoke",
    ),
    path(
        "applications/<int:pk>/policy/",
        views.ApplicationPolicyUpdateView.as_view(),
        name="app_policy_update",
    ),
]
