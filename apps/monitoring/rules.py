"""Threshold-rule helpers: metric-key validation, evaluation reset, formatting.

Shared by the engine, the rule-change signals and the UI/API validation.
"""

import logging
import operator as _op

from django.core.exceptions import ValidationError
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


def resolve_open_alerts(rule) -> int:
    """Resolve all open alerts of ``rule``; returns the resolved count."""
    from .models import Alert

    count = Alert.objects.filter(alert_rule_id=rule.pk, is_resolved=False).update(
        is_resolved=True, resolved_at=timezone.now()
    )
    if count:
        logger.info("Rule %s changed/deleted: auto-resolved %d alert(s).", rule.pk, count)
    return count


def reset_rule_evaluation(rule) -> int:
    """Resolve the rule's open alerts and drop its threshold state.

    The next engine run evaluates the rule from scratch. Returns the number
    of resolved alerts.
    """
    from .models import ThresholdState

    count = resolve_open_alerts(rule)
    ThresholdState.objects.filter(rule_id=rule.pk).delete()
    return count


def evaluation_changed(old, new) -> bool:
    """True if any evaluation-relevant field differs between two rule instances."""
    return any(getattr(old, f) != getattr(new, f) for f in EVALUATION_FIELDS)
