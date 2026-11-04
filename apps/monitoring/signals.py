"""AlertRule change/delete and metric blocking → reset threshold evaluation (spec §5.2).

Model signals rather than view hooks so UI, API and Django admin are all
covered. Not fired by ``QuerySet.update()`` or by data migrations (historical
models) — both intentional.
"""

from django.db.models.signals import post_save, pre_delete, pre_save
from django.dispatch import receiver

from .models import AlertRule, MetricDefinition
from .rules import (
    evaluation_changed,
    reset_metric_evaluation,
    reset_rule_evaluation,
    resolve_open_alerts,
)


@receiver(pre_save, sender=AlertRule, dispatch_uid="monitoring_alertrule_detect_change")
def detect_threshold_rule_change(sender, instance, raw=False, **kwargs):
    # Detect against the persisted row here; reset only in post_save, after the
    # UPDATE, so an engine run cannot re-evaluate the old values between the
    # reset and the write and leave a stale alert open.
    instance._reset_threshold_evaluation = False
    if raw or instance.pk is None:
        return
    old = AlertRule.objects.filter(pk=instance.pk).first()
    if old is None:  # explicit pk on a create
        return
    threshold = AlertRule.AlertType.THRESHOLD
    if threshold not in (old.alert_type, instance.alert_type):
        return
    instance._reset_threshold_evaluation = evaluation_changed(old, instance)


@receiver(post_save, sender=AlertRule, dispatch_uid="monitoring_alertrule_reset_on_change")
def reset_threshold_rule_on_change(sender, instance, created=False, raw=False, **kwargs):
    if raw or created or not getattr(instance, "_reset_threshold_evaluation", False):
        return
    instance._reset_threshold_evaluation = False
    reset_rule_evaluation(instance)


@receiver(pre_delete, sender=AlertRule, dispatch_uid="monitoring_alertrule_resolve_on_delete")
def resolve_threshold_alerts_on_delete(sender, instance, **kwargs):
    # ThresholdState rows cascade with the rule; alerts are SET_NULL, so
    # resolve them first while the FK still points at the rule.
    if instance.alert_type == AlertRule.AlertType.THRESHOLD:
        resolve_open_alerts(instance)


@receiver(pre_save, sender=MetricDefinition, dispatch_uid="monitoring_metricdef_detect_block")
def detect_metric_block(sender, instance, raw=False, **kwargs):
    # Detect the False→True flip against the persisted row here; reset only in
    # post_save, after the UPDATE, so an engine run cannot evaluate the still
    # unblocked metric between the reset and the write.
    instance._reset_metric_evaluation = False
    if raw or instance.pk is None or not instance.is_blocked:
        return
    was_blocked = (
        MetricDefinition.objects.filter(pk=instance.pk)
        .values_list("is_blocked", flat=True)
        .first()
    )
    instance._reset_metric_evaluation = was_blocked is False


@receiver(post_save, sender=MetricDefinition, dispatch_uid="monitoring_metricdef_reset_on_block")
def reset_threshold_states_on_block(sender, instance, created=False, raw=False, **kwargs):
    # A blocked metric's values are discarded, so its open threshold alerts
    # could never auto-resolve: resolve them and drop the states now.
    if raw or created or not getattr(instance, "_reset_metric_evaluation", False):
        return
    instance._reset_metric_evaluation = False
    reset_metric_evaluation(instance.key)
