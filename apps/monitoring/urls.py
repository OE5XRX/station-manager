from django.urls import path

from . import views

app_name = "monitoring"

urlpatterns = [
    path("", views.AlertListView.as_view(), name="alert_list"),
    path("<int:pk>/acknowledge/", views.AlertAcknowledgeView.as_view(), name="alert_acknowledge"),
    path("<int:pk>/resolve/", views.AlertResolveView.as_view(), name="alert_resolve"),
    path("settings/", views.AlertSettingsView.as_view(), name="alert_settings"),
    path(
        "settings/rules/<int:pk>/",
        views.AlertRuleUpdateView.as_view(),
        name="alert_rule_update",
    ),
    path(
        "test/email/",
        views.TestNotificationView.as_view(),
        name="test_email",
        kwargs={"channel": "email"},
    ),
    path(
        "test/telegram/",
        views.TestNotificationView.as_view(),
        name="test_telegram",
        kwargs={"channel": "telegram"},
    ),
    path("count/", views.AlertCountView.as_view(), name="alert_count"),
]
