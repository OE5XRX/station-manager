import json

from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views import View
from django.views.generic import ListView, TemplateView

from apps.monitoring.models import Alert, AlertRule
from apps.monitoring.notifications import send_test_notification
from apps.stations.models import Station


class AdminRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Mixin that restricts access to users with admin role."""

    def test_func(self):
        return self.request.user.role == "admin"


class AdminOrOperatorRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Restrict access to users with admin or operator role."""

    def test_func(self):
        return self.request.user.role in ("admin", "operator")


class AlertListView(AdminRequiredMixin, ListView):
    model = Alert
    template_name = "monitoring/alert_list.html"
    context_object_name = "alerts"
    paginate_by = 25

    def get_queryset(self):
        qs = Alert.objects.select_related("station", "alert_rule", "acknowledged_by")

        severity = self.request.GET.get("severity")
        if severity in ("warning", "critical"):
            qs = qs.filter(severity=severity)

        is_resolved = self.request.GET.get("is_resolved")
        if is_resolved == "true":
            qs = qs.filter(is_resolved=True)
        elif is_resolved == "false":
            qs = qs.filter(is_resolved=False)

        station_id = self.request.GET.get("station")
        if station_id:
            qs = qs.filter(station_id=station_id)

        return qs

    def get_template_names(self):
        if self.request.htmx:
            return ["monitoring/_alert_cards.html"]
        return [self.template_name]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["current_severity"] = self.request.GET.get("severity", "")
        context["current_is_resolved"] = self.request.GET.get("is_resolved", "")
        context["current_station"] = self.request.GET.get("station", "")
        context["active_count"] = Alert.objects.filter(is_resolved=False).count()
        context["critical_count"] = Alert.objects.filter(
            is_resolved=False, severity="critical"
        ).count()
        context["stations"] = Station.objects.all().order_by("name")
        return context


class AlertAcknowledgeView(AdminOrOperatorRequiredMixin, View):
    def post(self, request, pk):
        alert = get_object_or_404(Alert, pk=pk)
        alert.is_acknowledged = True
        alert.acknowledged_by = request.user
        alert.acknowledged_at = timezone.now()
        alert.save(update_fields=["is_acknowledged", "acknowledged_by", "acknowledged_at"])
        return JsonResponse({"status": "ok"})


class AlertResolveView(AdminOrOperatorRequiredMixin, View):
    def post(self, request, pk):
        alert = get_object_or_404(Alert, pk=pk)
        alert.is_resolved = True
        alert.resolved_at = timezone.now()
        alert.save(update_fields=["is_resolved", "resolved_at"])
        return JsonResponse({"status": "ok"})


class AlertSettingsView(AdminRequiredMixin, TemplateView):
    template_name = "monitoring/alert_settings.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["alert_rules"] = AlertRule.objects.all()
        return context


class AlertRuleUpdateView(AdminRequiredMixin, View):
    def post(self, request, pk):
        rule = get_object_or_404(AlertRule, pk=pk)

        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            data = request.POST

        if "threshold" in data:
            try:
                rule.threshold = float(data["threshold"])
            except (ValueError, TypeError):
                return JsonResponse(
                    {"status": "error", "message": "Invalid threshold value."},
                    status=400,
                )

        if "is_active" in data:
            value = data["is_active"]
            if isinstance(value, str):
                rule.is_active = value.lower() in ("true", "1", "on")
            else:
                rule.is_active = bool(value)

        rule.save(update_fields=["threshold", "is_active"])
        return JsonResponse({"status": "ok"})


class TestNotificationView(AdminRequiredMixin, View):
    def post(self, request, channel):
        success, error_message = send_test_notification(channel)
        return JsonResponse(
            {
                "success": success,
                "error": error_message,
            }
        )


class AlertCountView(LoginRequiredMixin, View):
    def get(self, request):
        unresolved = Alert.objects.filter(is_resolved=False)
        return JsonResponse(
            {
                "total": unresolved.count(),
                "critical": unresolved.filter(severity="critical").count(),
                "warning": unresolved.filter(severity="warning").count(),
            }
        )
