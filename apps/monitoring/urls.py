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
        "settings/thresholds/new/",
        views.ThresholdRuleCreateView.as_view(),
        name="threshold_rule_create",
    ),
    path(
        "settings/thresholds/<int:pk>/",
        views.ThresholdRuleUpdateView.as_view(),
        name="threshold_rule_update",
    ),
    path(
        "settings/thresholds/<int:pk>/delete/",
        views.ThresholdRuleDeleteView.as_view(),
        name="threshold_rule_delete",
    ),
    path("metrics/", views.MetricDefinitionListView.as_view(), name="metric_definitions"),
    path(
        "metrics/<int:pk>/",
        views.MetricDefinitionUpdateView.as_view(),
        name="metric_definition_update",
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
    path(
        "test/push/",
        views.TestNotificationView.as_view(),
        name="test_push",
        kwargs={"channel": "webpush"},
    ),
    path("count/", views.AlertCountView.as_view(), name="alert_count"),
]
