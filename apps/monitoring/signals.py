"""AlertRule change/delete and metric blocking → reset threshold evaluation (spec §5.2).

Model signals rather than view hooks so UI, API and Django admin are all
covered. Not fired by ``QuerySet.update()`` or by data migrations (historical
models) — both intentional.
"""

from django.db.models.signals import pre_delete, pre_save
from django.dispatch import receiver

from .models import AlertRule, MetricDefinition
from .rules import (
    evaluation_changed,
    reset_metric_evaluation,
    reset_rule_evaluation,
    resolve_open_alerts,
)


@receiver(pre_save, sender=AlertRule, dispatch_uid="monitoring_alertrule_reset_on_change")
def reset_threshold_rule_on_change(sender, instance, raw=False, **kwargs):
    if raw or instance.pk is None:
        return
    old = AlertRule.objects.filter(pk=instance.pk).first()
    if old is None:  # explicit pk on a create
        return
    threshold = AlertRule.AlertType.THRESHOLD
    if threshold not in (old.alert_type, instance.alert_type):
        return
    if evaluation_changed(old, instance):
        reset_rule_evaluation(instance)


@receiver(pre_delete, sender=AlertRule, dispatch_uid="monitoring_alertrule_resolve_on_delete")
def resolve_threshold_alerts_on_delete(sender, instance, **kwargs):
    # ThresholdState rows cascade with the rule; alerts are SET_NULL, so
    # resolve them first while the FK still points at the rule.
    if instance.alert_type == AlertRule.AlertType.THRESHOLD:
        resolve_open_alerts(instance)


@receiver(pre_save, sender=MetricDefinition, dispatch_uid="monitoring_metricdef_reset_on_block")
def reset_threshold_states_on_block(sender, instance, raw=False, **kwargs):
    # A blocked metric's values are discarded, so its open threshold alerts
    # could never auto-resolve: resolve them and drop the states now.
    if raw or instance.pk is None or not instance.is_blocked:
        return
    was_blocked = (
        MetricDefinition.objects.filter(pk=instance.pk)
        .values_list("is_blocked", flat=True)
        .first()
    )
    if was_blocked is False:
        reset_metric_evaluation(instance.key)
