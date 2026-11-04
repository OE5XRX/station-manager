import json
import logging

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import ListView, TemplateView

from apps.accounts.client_ip import client_ip
from apps.accounts.models import AccountAuditLog
from apps.accounts.permissions import AreaPermissionMixin
from apps.monitoring.forms import MetricDefinitionForm, ThresholdRuleForm
from apps.monitoring.models import Alert, AlertRule, MetricDefinition
from apps.monitoring.notifications import send_test_notification
from apps.stations.models import Station
from station_agent.metrics_contract import GROUPS, key_matches_pattern

logger = logging.getLogger(__name__)

THRESHOLD = AlertRule.AlertType.THRESHOLD

# Fields recorded in the audit diff of a threshold-rule / definition change.
_RULE_AUDIT_FIELDS = tuple(ThresholdRuleForm.Meta.fields)
_DEFINITION_AUDIT_FIELDS = tuple(MetricDefinitionForm.Meta.fields)


def _snapshot(obj, fields):
    return {f: getattr(obj, f) for f in fields}


def _audit(request, *, obj, action, message, before=None, after=None):
    """Best-effort CONFIG_CHANGED row; an audit failure never undoes the write."""
    changes = {}
    details = {"object": obj, "action": action, "changes": changes}
    if before is not None and after is not None:
        changes.update(
            {f: {"old": before[f], "new": after[f]} for f in before if before[f] != after[f]}
        )
    elif after is not None or before is not None:
        # Create/delete: record the full configuration, not an empty diff.
        details["config"] = after if after is not None else before
    try:
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.CONFIG_CHANGED,
            actor=request.user,
            ip_address=client_ip(request),
            message=message,
            details=details,
        )
    except Exception:
        logger.warning("monitoring audit write failed", exc_info=True)


def metric_key_patterns(keys):
    """``*`` pattern suggestions for the rule key ``<datalist>``.

    For every key with >= 3 segments each inner segment is replaced by ``*``
    (``disk.root.used_pct`` -> ``disk.*.used_pct``). Sorted, deduped.
    """
    patterns = set()
    for key in keys:
        parts = key.split(".")
        for i in range(1, len(parts) - 1):
            patterns.add(".".join([*parts[:i], "*", *parts[i + 1 :]]))
    return sorted(patterns)


def _unit_for(metric_key, units):
    """Unit of the definition ``metric_key`` names (first match for a pattern)."""
    if metric_key in units:
        return units[metric_key]
    if "*" in metric_key.split("."):
        for key, unit in units.items():
            if key_matches_pattern(key, metric_key):
                return unit
    return ""


class AlertListView(AreaPermissionMixin, ListView):
    area = "alerts"
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


class AlertAcknowledgeView(AreaPermissionMixin, View):
    area = "alerts"
    verb = "manage"

    def post(self, request, pk):
        alert = get_object_or_404(Alert, pk=pk)
        alert.is_acknowledged = True
        alert.acknowledged_by = request.user
        alert.acknowledged_at = timezone.now()
        alert.save(update_fields=["is_acknowledged", "acknowledged_by", "acknowledged_at"])
        return JsonResponse({"status": "ok"})


class AlertResolveView(AreaPermissionMixin, View):
    area = "alerts"
    verb = "manage"

    def post(self, request, pk):
        alert = get_object_or_404(Alert, pk=pk)
        alert.is_resolved = True
        alert.resolved_at = timezone.now()
        alert.save(update_fields=["is_resolved", "resolved_at"])
        return JsonResponse({"status": "ok"})


class AlertSettingsView(AreaPermissionMixin, TemplateView):
    area = "alert_rules"
    template_name = "monitoring/alert_settings.html"

    def get_context_data(self, create_form=None, bound_forms=None, **kwargs):
        context = super().get_context_data(**kwargs)
        bound_forms = bound_forms or {}
        context["event_rules"] = AlertRule.objects.exclude(alert_type=THRESHOLD)
        threshold_rules = list(
            AlertRule.objects.filter(alert_type=THRESHOLD).annotate(
                open_alert_count=Count("alerts", filter=Q(alerts__is_resolved=False))
            )
        )
        definitions = list(
            MetricDefinition.objects.only("key", "unit", "label", "label_override").order_by("key")
        )
        units = {d.key: d.unit for d in definitions}
        context["threshold_rules"] = threshold_rules
        context["threshold_rows"] = [
            {
                "rule": rule,
                "form": bound_forms.get(rule.pk)
                or ThresholdRuleForm(instance=rule, auto_id=f"rule{rule.pk}_%s"),
                "unit": _unit_for(rule.metric_key, units),
            }
            for rule in threshold_rules
        ]
        context["create_form"] = create_form or ThresholdRuleForm(
            initial={"aggregate": AlertRule.Aggregate.AVG, "is_active": True},
            auto_id="rulenew_%s",
        )
        context["metric_definitions"] = definitions
        context["metric_key_patterns"] = metric_key_patterns(units)
        context["metric_key_choices"] = [(d.key, d.display_label) for d in definitions]
        context["operator_choices"] = AlertRule.Operator.choices
        context["aggregate_choices"] = AlertRule.Aggregate.choices
        context["severity_choices"] = AlertRule.Severity.choices
        return context


class _ThresholdRuleMixin(AreaPermissionMixin):
    area = "alert_rules"
    verb = "manage"
    http_method_names = ["post"]

    def get_rule(self, pk):
        return get_object_or_404(AlertRule, pk=pk, alert_type=THRESHOLD)

    def render_invalid(self, request, *, create_form=None, bound_forms=None):
        view = AlertSettingsView()
        view.setup(request)
        context = view.get_context_data(create_form=create_form, bound_forms=bound_forms)
        context["form_error"] = True
        return view.response_class(
            request=request, template=[view.template_name], context=context, status=400
        )

    def done(self):
        return redirect(reverse("monitoring:alert_settings") + "#threshold-rules")


class ThresholdRuleCreateView(_ThresholdRuleMixin, View):
    def post(self, request):
        form = ThresholdRuleForm(request.POST, auto_id="rulenew_%s")
        if not form.is_valid():
            return self.render_invalid(request, create_form=form)
        with transaction.atomic():
            rule = form.save()
        _audit(
            request,
            obj="alert_rule",
            action="created",
            message=f"AlertRule {rule} created",
            after=_snapshot(rule, _RULE_AUDIT_FIELDS),
        )
        messages.success(request, _("Threshold rule “%(rule)s” created.") % {"rule": rule})
        return self.done()


class ThresholdRuleUpdateView(_ThresholdRuleMixin, View):
    def post(self, request, pk):
        rule = self.get_rule(pk)
        before = _snapshot(rule, _RULE_AUDIT_FIELDS)
        form = ThresholdRuleForm(request.POST, instance=rule, auto_id=f"rule{pk}_%s")
        if not form.is_valid():
            return self.render_invalid(request, bound_forms={rule.pk: form})
        # Signals: pre_save detects an evaluation change, post_save (after the
        # UPDATE, under the rule row lock) resolves open alerts + drops the
        # state. One transaction, so the UPDATE and the reset commit together.
        with transaction.atomic():
            rule = form.save()
        _audit(
            request,
            obj="alert_rule",
            action="updated",
            message=f"AlertRule {rule} updated",
            before=before,
            after=_snapshot(rule, _RULE_AUDIT_FIELDS),
        )
        messages.success(request, _("Threshold rule “%(rule)s” saved.") % {"rule": rule})
        return self.done()


class ThresholdRuleDeleteView(_ThresholdRuleMixin, View):
    def post(self, request, pk):
        rule = self.get_rule(pk)
        label = str(rule)
        before = _snapshot(rule, _RULE_AUDIT_FIELDS)
        with transaction.atomic():
            rule.delete()  # pre_delete signal resolves the rule's open alerts
        _audit(
            request,
            obj="alert_rule",
            action="deleted",
            message=f"AlertRule {label} deleted",
            before=before,
        )
        messages.success(request, _("Threshold rule “%(rule)s” deleted.") % {"rule": label})
        return self.done()


class MetricDefinitionListView(AreaPermissionMixin, ListView):
    area = "alert_rules"
    model = MetricDefinition
    template_name = "monitoring/metric_definitions.html"
    context_object_name = "definitions"
    paginate_by = 100

    def get_queryset(self):
        qs = MetricDefinition.objects.order_by("group", "key")
        group = self.request.GET.get("group")
        if group in GROUPS:
            qs = qs.filter(group=group)
        state = self.request.GET.get("state")
        if state == "hidden":
            qs = qs.filter(is_hidden=True)
        elif state == "blocked":
            qs = qs.filter(is_blocked=True)
        elif state == "rejected":
            qs = qs.filter(last_rejected_at__isnull=False)
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        counts = dict(
            MetricDefinition.objects.values_list("group").annotate(n=Count("pk")).order_by()
        )
        context["groups"] = [(g, counts.get(g, 0)) for g in GROUPS]
        context["total_count"] = sum(counts.values())
        context["current_group"] = self.request.GET.get("group", "")
        context["current_state"] = self.request.GET.get("state", "")
        context["rejected_count"] = MetricDefinition.objects.filter(
            last_rejected_at__isnull=False
        ).count()
        context["current_url"] = self.request.get_full_path()
        return context


class MetricDefinitionUpdateView(AreaPermissionMixin, View):
    area = "alert_rules"
    verb = "manage"
    http_method_names = ["post"]

    def post(self, request, pk):
        definition = get_object_or_404(MetricDefinition, pk=pk)
        before = _snapshot(definition, _DEFINITION_AUDIT_FIELDS)
        form = MetricDefinitionForm(request.POST, instance=definition)
        if form.is_valid():
            # Only the operator fields: a concurrent heartbeat may have updated
            # the agent fields since the row was fetched.
            # Atomic: a block's post_save reset commits with the UPDATE.
            with transaction.atomic():
                form.save(commit=False).save(update_fields=list(form._meta.fields))
            _audit(
                request,
                obj="metric_definition",
                action="updated",
                message=f"MetricDefinition {definition.key} updated",
                before=before,
                after=_snapshot(definition, _DEFINITION_AUDIT_FIELDS),
            )
            messages.success(request, _("Metric “%(key)s” saved.") % {"key": definition.key})
        else:
            errors = "; ".join(str(e) for errs in form.errors.values() for e in errs)
            messages.error(
                request,
                _("Metric “%(key)s” not saved: %(errors)s")
                % {"key": definition.key, "errors": errors},
            )
        target = request.POST.get("next", "")
        if not url_has_allowed_host_and_scheme(
            target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        ):
            target = reverse("monitoring:metric_definitions")
        return redirect(f"{target}#metric-{definition.pk}")


class AlertRuleUpdateView(AreaPermissionMixin, View):
    area = "alert_rules"
    verb = "manage"

    def post(self, request, pk):
        # Threshold rules have their own form views (all fields, validation).
        rule = get_object_or_404(AlertRule.objects.exclude(alert_type=THRESHOLD), pk=pk)

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


class TestNotificationView(AreaPermissionMixin, View):
    area = "alert_rules"
    verb = "manage"

    def post(self, request, channel):
        success, error_message = send_test_notification(channel, requesting_user=request.user)
        return JsonResponse(
            {
                "success": success,
                "error": error_message,
            }
        )


class AlertCountView(AreaPermissionMixin, View):
    area = "alerts"

    def get(self, request):
        unresolved = Alert.objects.filter(is_resolved=False)
        return JsonResponse(
            {
                "total": unresolved.count(),
                "critical": unresolved.filter(severity="critical").count(),
                "warning": unresolved.filter(severity="warning").count(),
            }
        )
