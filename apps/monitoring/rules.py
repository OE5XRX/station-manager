"""Threshold-rule helpers: metric-key validation, evaluation reset, formatting.

Shared by the engine, the rule-change signals and the UI/API validation.
"""

import logging
import operator as _op

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from station_agent.metrics_contract import is_valid_pattern, key_matches_pattern

logger = logging.getLogger(__name__)

_COMPARATORS = {
    "gt": _op.gt,
    "gte": _op.ge,
    "lt": _op.lt,
    "lte": _op.le,
}

# Operators whose breach means "value above the threshold" (message wording).
ABOVE_OPERATORS = frozenset({"gt", "gte"})

# AlertRule fields whose change invalidates the running evaluation. Cosmetic
# fields (name, description) are deliberately excluded so a rename does not
# resolve (and immediately re-fire / re-notify) an open alert.
EVALUATION_FIELDS = (
    "alert_type",
    "metric_key",
    "operator",
    "aggregate",
    "threshold",
    "duration_seconds",
    "severity",
    "is_active",
)


def compare(op: str, value: float, threshold: float) -> bool:
    """True if ``value <op> threshold`` is a breach. Unknown operators never breach."""
    fn = _COMPARATORS.get(op)
    return bool(fn(value, threshold)) if fn is not None else False


def format_duration(seconds: int) -> str:
    """Compact human duration: ``"45 s"``, ``"2 min"``, ``"2 min 30 s"``, ``"1 h 5 min"``."""
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    if minutes:
        return f"{minutes} min {secs} s" if secs else f"{minutes} min"
    return f"{secs} s"


def format_value(value: float, precision: int) -> str:
    """Render ``value`` with the definition's decimal precision (dot decimal)."""
    return f"{value:.{max(0, int(precision))}f}"


def validate_metric_key(value: str) -> None:
    """Validate a threshold rule's ``metric_key``.

    Must be a valid key or a pattern with exactly one whole-segment ``*``, and
    must match at least one existing ``MetricDefinition`` key.
    Raises ``django.core.exceptions.ValidationError``.
    """
    from .models import MetricDefinition

    if not value or not is_valid_pattern(value):
        raise ValidationError(
            _(
                "Enter a metric key (e.g. cpu.temp_c) or a pattern with one '*' "
                "segment (e.g. disk.*.used_pct)."
            ),
            code="invalid_metric_key",
        )
    if "*" not in value.split("."):
        matched = MetricDefinition.objects.filter(key=value).exists()
    else:
        matched = any(
            key_matches_pattern(key, value)
            for key in MetricDefinition.objects.values_list("key", flat=True).iterator()
        )
    if not matched:
        raise ValidationError(
            _("No known metric matches '%(key)s'."),
            code="unknown_metric_key",
            params={"key": value},
        )


def persisted_threshold_key(rule):
    """The rule's stored metric key if it is a persisted threshold rule, else None."""
    if rule is not None and rule.pk and rule.alert_type == "threshold":
        return rule.metric_key
    return None


def validate_changed_metric_key(value: str, persisted_key) -> None:
    """``validate_metric_key`` for a new or changed key only.

    Seeded rules (e.g. the 12 V rule) may reference keys whose definition only
    appears once an agent describes it; editing them must not be blocked.
    """
    if value and value == persisted_key:
        return
    validate_metric_key(value)


def _lock_rule(rule) -> None:
    """Take the AlertRule row lock (no-op on SQLite).

    The engine evaluates each threshold rule under the same lock, so this
    waits for an in-flight evaluation — and then also sees the alert/state it
    created. Must be called inside ``transaction.atomic()``.
    """
    from .models import AlertRule

    AlertRule.objects.select_for_update().filter(pk=rule.pk).exists()


def _resolve_open_alerts_locked(rule) -> int:
    from .models import Alert

    count = Alert.objects.filter(alert_rule_id=rule.pk, is_resolved=False).update(
        is_resolved=True, resolved_at=timezone.now()
    )
    if count:
        logger.info("Rule %s changed/deleted: auto-resolved %d alert(s).", rule.pk, count)
    return count


def resolve_open_alerts(rule) -> int:
    """Resolve all open alerts of ``rule``; returns the resolved count."""
    with transaction.atomic():
        _lock_rule(rule)
        return _resolve_open_alerts_locked(rule)


def reset_rule_evaluation(rule) -> int:
    """Resolve the rule's open alerts and drop its threshold state.

    The next engine run evaluates the rule from scratch. Returns the number
    of resolved alerts.
    """
    from .models import ThresholdState

    with transaction.atomic():
        _lock_rule(rule)
        count = _resolve_open_alerts_locked(rule)
        ThresholdState.objects.filter(rule_id=rule.pk).delete()
    return count


def reset_metric_evaluation(metric_key: str) -> int:
    """Resolve open threshold alerts of ``metric_key`` and drop their states.

    Used when a metric gets blocked: its values are discarded from now on, so
    alerts on it could otherwise never resolve. Returns the resolved count.

    Takes the row locks of every threshold rule that can evaluate the key
    (pattern match) or still holds a state for it — ordered by pk, so two
    concurrent resets cannot deadlock — before the states are read. An
    in-flight engine evaluation of such a rule finishes first (its new
    alert/state is then reset here); a later one sees the block.
    """
    from .models import Alert, AlertRule, ThresholdState

    with transaction.atomic():
        rule_ids = {
            pk
            for pk, pattern in AlertRule.objects.filter(
                alert_type=AlertRule.AlertType.THRESHOLD
            ).values_list("pk", "metric_key")
            if key_matches_pattern(metric_key, pattern)
        }
        rule_ids.update(
            ThresholdState.objects.filter(metric_key=metric_key).values_list("rule_id", flat=True)
        )
        if rule_ids:
            list(
                AlertRule.objects.select_for_update()
                .filter(pk__in=rule_ids)
                .order_by("pk")
                .values_list("pk", flat=True)
            )
        states = ThresholdState.objects.filter(metric_key=metric_key)
        alert_ids = [pk for pk in states.values_list("alert_id", flat=True) if pk is not None]
        count = 0
        if alert_ids:
            count = Alert.objects.filter(pk__in=alert_ids, is_resolved=False).update(
                is_resolved=True, resolved_at=timezone.now()
            )
        states.delete()
    if count:
        logger.info("Metric %s blocked: auto-resolved %d alert(s).", metric_key, count)
    return count


def inverted_override_field(min_override, max_override, min_value, max_value):
    """Field to blame if the *effective* plausible range is inverted, else None.

    The effective bound is the override if set, otherwise the agent value —
    so a one-sided override is checked against the agent's other bound
    (agent 0–30 + ``min_override=40`` alone rejects every value).
    Without any override nothing is checked: a legacy inverted agent range is
    not the operator's to fix, so flag/label edits on such a row still save.
    """
    if min_override is None and max_override is None:
        return None
    lo = min_override if min_override is not None else min_value
    hi = max_override if max_override is not None else max_value
    if lo is None or hi is None or lo <= hi:
        return None
    return "min_override" if min_override is not None else "max_override"


# AlertRule fields recorded in the CONFIG_CHANGED audit (UI + API).
RULE_AUDIT_FIELDS = (
    "alert_type",
    "name",
    "metric_key",
    "operator",
    "aggregate",
    "threshold",
    "duration_seconds",
    "severity",
    "is_active",
    "description",
)


def rule_snapshot(rule) -> dict:
    """Audit snapshot of a rule's configuration (JSON-serialisable values)."""
    return {f: getattr(rule, f) for f in RULE_AUDIT_FIELDS}


def snapshot_changes(before: dict, after: dict) -> dict:
    """``{field: {old, new}}`` for every field that differs between two snapshots."""
    return {f: {"old": before[f], "new": after[f]} for f in before if before[f] != after[f]}


def evaluation_changed(old, new) -> bool:
    """True if any evaluation-relevant field differs between two rule instances."""
    return any(getattr(old, f) != getattr(new, f) for f in EVALUATION_FIELDS)
