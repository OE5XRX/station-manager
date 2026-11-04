"""Alert engine: checks station health and creates/resolves alerts."""

import logging
from datetime import UTC, datetime, timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.deployments.models import DeploymentResult
from apps.stations.models import Station
from station_agent.metrics_contract import key_matches_pattern

from .models import Alert, AlertRule, MetricDefinition, ThresholdState
from .rules import ABOVE_OPERATORS, compare, evaluation_changed, format_duration, format_value

logger = logging.getLogger(__name__)

OFFLINE_THRESHOLD = timedelta(minutes=5)
OTA_CHECK_WINDOW = timedelta(minutes=5)


def _get_active_rule(alert_type):
    """Return the active AlertRule for a given type, or None."""
    try:
        return AlertRule.objects.get(alert_type=alert_type, is_active=True)
    except AlertRule.DoesNotExist:
        return None


_unresolved_cache = None


def _build_unresolved_cache():
    """Pre-fetch all unresolved alerts into a set for O(1) lookups."""
    global _unresolved_cache
    _unresolved_cache = set(
        Alert.objects.filter(is_resolved=False).values_list("station_id", "alert_rule__alert_type")
    )


def _has_unresolved_alert(station, alert_type):
    """Check if an unresolved alert already exists (uses pre-fetched cache)."""
    if _unresolved_cache is not None:
        return (station.id, alert_type) in _unresolved_cache
    return Alert.objects.filter(
        station=station,
        alert_rule__alert_type=alert_type,
        is_resolved=False,
    ).exists()


def _create_alert(station, rule, title, message, severity=None):
    """Create an alert and return it.

    severity: override the rule's default severity (Alert.Severity value).
    When None (the default), rule.severity is used.
    """
    alert = Alert.objects.create(
        station=station,
        alert_rule=rule,
        severity=severity if severity is not None else rule.severity,
        title=title,
        message=message,
    )
    logger.info("Alert created: %s for station %s", title, station.name)
    return alert


def _auto_resolve(alert_type, station=None):
    """Resolve all unresolved alerts of a given type, optionally filtered by station."""
    qs = Alert.objects.filter(
        alert_rule__alert_type=alert_type,
        is_resolved=False,
    )
    if station is not None:
        qs = qs.filter(station=station)
    now = timezone.now()
    count = qs.update(is_resolved=True, resolved_at=now)
    if count:
        logger.info(
            "Auto-resolved %d alert(s) of type %s%s",
            count,
            alert_type,
            f" for station {station.name}" if station else "",
        )
    return count


REBOOT_CHECK_WINDOW = timedelta(minutes=10)
UNEXPECTED_REBOOT_REASONS = {"crash", "watchdog", "undervoltage", "unknown"}


def _check_unexpected_reboot():
    """Alert on reboots whose reason is not clean/ota_rollback (event alert, per-reboot dedup)."""
    from apps.stations.models import StationTelemetry

    rule = _get_active_rule(AlertRule.AlertType.UNEXPECTED_REBOOT)
    if rule is None:
        return []

    window_start = timezone.now() - REBOOT_CHECK_WINDOW
    new_alerts = []
    qs = StationTelemetry.objects.select_related("station").filter(
        last_reboot_at__isnull=False,
        last_reboot_at__gte=window_start,
        last_reboot_reason__in=UNEXPECTED_REBOOT_REASONS,
    )
    for tel in qs:
        already = Alert.objects.filter(
            station=tel.station,
            alert_rule__alert_type=AlertRule.AlertType.UNEXPECTED_REBOOT,
            created_at__gte=tel.last_reboot_at,
        ).exists()
        if already:
            continue
        alert = _create_alert(
            station=tel.station,
            rule=rule,
            title=f"Unexpected reboot: {tel.last_reboot_reason}",
            message=(
                f"Station {tel.station.name} rebooted unexpectedly "
                f"(reason: {tel.last_reboot_reason}, boot #{tel.boot_count})."
            ),
        )
        new_alerts.append(alert)
    return new_alerts


def _check_power_warning():
    """Alert on undervoltage/throttling with per-episode creation + now-based resolution.

    H1 algorithm:
      - is_now=True  → live incident; ALWAYS create/escalate CRITICAL (never gated by episode).
      - is_now=False, occurred=True → transient event; warn ONCE per boot episode.
        episode_key = boot_id or "∅" avoids the blank-boot-id collision with the default "".

    M4: escalate an existing unresolved WARNING to CRITICAL when a *_now bit fires.
    M5: auto-resolve when both *_now bits are false (occurred stays sticky, so
        current conditions govern whether the alert remains active). A resolved
        alert is NOT recreated for the same boot (creation gate); a new boot_id
        resets the episode so a genuinely fresh event alerts again.
    """
    from apps.stations.models import StationTelemetry

    rule = _get_active_rule(AlertRule.AlertType.POWER_WARNING)
    if rule is None:
        return []

    new_alerts = []
    qs = StationTelemetry.objects.select_related("station").filter(
        Q(undervoltage_occurred=True) | Q(throttled_occurred=True)
    )
    for tel in qs:
        is_now = bool(tel.undervoltage_now) or bool(tel.throttled_now)
        occurred = bool(tel.undervoltage_occurred) or bool(tel.throttled_occurred)
        if not occurred:
            continue
        what = "Undervoltage" if tel.undervoltage_occurred else "Throttling"

        existing = Alert.objects.filter(
            station=tel.station,
            alert_rule__alert_type=AlertRule.AlertType.POWER_WARNING,
            is_resolved=False,
        ).first()

        if is_now:
            # H1: live incident — ALWAYS ensure a CRITICAL alert (never gated by episode).
            if existing is None:
                alert = _create_alert(
                    station=tel.station,
                    rule=rule,
                    title=f"Power warning: {what}",
                    message=f"Station {tel.station.name}: {what} ongoing.",
                    severity=Alert.Severity.CRITICAL,
                )
                new_alerts.append(alert)
            elif existing.severity == Alert.Severity.WARNING:
                # M4: escalate an existing unresolved WARNING to CRITICAL.
                existing.severity = Alert.Severity.CRITICAL
                existing.title = f"Power warning: {what}"
                existing.message = (
                    f"Station {tel.station.name}: {what} ongoing (escalated to critical)."
                )
                existing.save(update_fields=["severity", "title", "message"])
                logger.info("Escalated power alert to CRITICAL for station %s", tel.station.name)
                new_alerts.append(existing)
            # else: already-unresolved CRITICAL → dedup, no action.
        else:
            # Transient event (occurred but not now) — warn ONCE per boot episode.
            # Use "∅" sentinel so a blank boot_id ("") doesn't collide with the
            # default field value of "" and suppress the very first alert.
            episode_key = tel.boot_id or "∅"
            if existing is None and tel.power_alerted_boot_id != episode_key:
                alert = _create_alert(
                    station=tel.station,
                    rule=rule,
                    title=f"Power warning: {what}",
                    message=f"Station {tel.station.name}: {what} occurred.",
                    severity=Alert.Severity.WARNING,
                )
                tel.power_alerted_boot_id = episode_key
                tel.save(update_fields=["power_alerted_boot_id"])
                new_alerts.append(alert)

    # M5: auto-resolve power alerts for stations where both *_now bits are false
    # (regardless of occurred — occurred bits are sticky until reboot).
    cleared_ids = list(
        StationTelemetry.objects.filter(
            undervoltage_now=False,
            throttled_now=False,
        ).values_list("station_id", flat=True)
    )
    if cleared_ids:
        now = timezone.now()
        Alert.objects.filter(
            alert_rule__alert_type=AlertRule.AlertType.POWER_WARNING,
            is_resolved=False,
            station_id__in=cleared_ids,
        ).update(is_resolved=True, resolved_at=now)

    return new_alerts


def _check_storage_health():
    """Alert on eMMC/SD wear (pre-eol / life-time) or I/O errors.

    M6: io_error_count is cumulative (dmesg counter, persists until reboot).
    After an operator resolves a storage alert, we must not immediately
    re-alert because io_error_count is still non-zero.  We track
    alerted_io_error_count on the telemetry row and only (re)alert on an
    increase — wear/pre-eol alerts are monotonic and retain the old behaviour.
    """
    from apps.stations.models import StationTelemetry

    rule = _get_active_rule(AlertRule.AlertType.STORAGE_HEALTH)
    if rule is None:
        return []

    new_alerts = []
    for tel in StationTelemetry.objects.select_related("station"):
        urgent = tel.worst_pre_eol == "urgent"
        warning = tel.worst_pre_eol == "warning"
        high_wear = (tel.worst_life_time_pct or 0) >= rule.threshold
        current_io = tel.io_error_count or 0
        # M6: only fire/re-fire when the count has increased past the last-alerted baseline.
        io_errors = current_io > (tel.alerted_io_error_count or 0)
        if not (urgent or warning or high_wear or io_errors):
            continue
        if _has_unresolved_alert(tel.station, AlertRule.AlertType.STORAGE_HEALTH):
            continue
        severity = Alert.Severity.CRITICAL if (urgent or high_wear) else Alert.Severity.WARNING
        reasons = []
        if urgent or warning:
            reasons.append(f"PRE_EOL={tel.worst_pre_eol}")
        if high_wear:
            reasons.append(f"life={tel.worst_life_time_pct}%")
        if io_errors:
            reasons.append(f"{current_io} I/O errors")
        alert = _create_alert(
            station=tel.station,
            rule=rule,
            title="Storage health warning",
            message=f"Station {tel.station.name}: {', '.join(reasons)}.",
            severity=severity,
        )
        # M6: record the count that triggered this alert so we don't re-alert
        # on the same value after an operator resolves it.
        if io_errors:
            tel.alerted_io_error_count = current_io
            tel.save(update_fields=["alerted_io_error_count"])
        new_alerts.append(alert)
    return new_alerts


def _check_station_offline():
    """Check for stations that have gone offline (no heartbeat for >5 min)."""
    new_alerts = []
    rule = _get_active_rule(AlertRule.AlertType.STATION_OFFLINE)
    if not rule:
        return new_alerts

    cutoff = timezone.now() - OFFLINE_THRESHOLD
    stale_stations = Station.objects.filter(
        last_seen__lt=cutoff,
    ).exclude(status=Station.Status.OFFLINE)

    for station in stale_stations:
        if not _has_unresolved_alert(station, AlertRule.AlertType.STATION_OFFLINE):
            alert = _create_alert(
                station=station,
                rule=rule,
                title=f"Station offline: {station.name}",
                message=(
                    f"Station {station.name} has not sent a heartbeat "
                    f"for more than {OFFLINE_THRESHOLD}. "
                    f"Last seen: {station.last_seen}."
                ),
            )
            new_alerts.append(alert)

    # Auto-resolve: stations that are back online
    online_stations = Station.objects.filter(
        last_seen__gte=cutoff,
    )
    for station in online_stations:
        _auto_resolve(AlertRule.AlertType.STATION_OFFLINE, station=station)

    return new_alerts


def _check_ota_failed():
    """Check for recent failed/rolled-back OTA deployments."""
    new_alerts = []
    rule = _get_active_rule(AlertRule.AlertType.OTA_FAILED)
    if not rule:
        return new_alerts

    cutoff = timezone.now() - OTA_CHECK_WINDOW
    failed_results = DeploymentResult.objects.filter(
        status__in=[DeploymentResult.Status.FAILED, DeploymentResult.Status.ROLLED_BACK],
        completed_at__gte=cutoff,
    ).select_related("station", "image_release")

    for result in failed_results:
        station = result.station
        if not _has_unresolved_alert(station, AlertRule.AlertType.OTA_FAILED):
            alert = _create_alert(
                station=station,
                rule=rule,
                title=f"OTA deployment failed: {station.name}",
                message=(
                    f"Deployment #{result.deployment_id} "
                    f"({result.image_release}) "
                    f"on {station.name} has {result.get_status_display().lower()}. "
                    f"Error: {result.error_message or 'No details available.'}"
                ),
            )
            new_alerts.append(alert)

    return new_alerts


def _threshold_message(defn, station, rule, value, elapsed_seconds):
    unit = defn.unit
    direction = "above" if rule.operator in ABOVE_OPERATORS else "below"
    msg = (
        f"{defn.display_label} on {station.name}: "
        f"{format_value(value, defn.precision)}{unit} {direction} "
        f"{format_value(rule.threshold, defn.precision)}{unit}"
    )
    if elapsed_seconds > 0:
        msg += f" for {format_duration(elapsed_seconds)}"
    return msg + "."


def _sample_is_current(sample, now) -> bool:
    """True if the sample's ``ts`` (ISO string) is at most OFFLINE_THRESHOLD old.

    Missing/unparseable ``ts`` counts as no current value. A naive ``ts`` is
    read as UTC; a ``ts`` in the future (clock skew) counts as current.
    """
    ts = sample.get("ts")
    if not isinstance(ts, str) or not ts:
        return False
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return now - parsed <= OFFLINE_THRESHOLD


def _threshold_candidates(current, definitions, pattern):
    """(station, key, sample, definition) for every current sample matching ``pattern``."""
    return [
        (station, key, sample, definitions[key])
        for station, metrics in current
        for key, sample in metrics.items()
        if key in definitions and key_matches_pattern(key, pattern)
    ]


def _evaluate_threshold_rule(rule, candidates, states, now):
    """Evaluate one (locked) rule over its candidates and persist the changed states.

    ``states`` maps ``(station_id, metric_key)`` to the rule's ThresholdState rows.
    Returns the newly created alerts.
    """
    new_alerts, to_create, to_update = [], [], []
    duration = timedelta(seconds=rule.duration_seconds)
    for station, key, sample, defn in candidates:
        value = sample.get(rule.aggregate)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue

        state = states.get((station.pk, key))
        is_new = state is None
        if is_new:
            state = ThresholdState(rule=rule, station=station, metric_key=key)
        before = (state.breach_since, state.ok_since, state.alert_id, state.last_evaluated_at)
        open_alert = state.alert if state.alert and not state.alert.is_resolved else None

        # A gap without current values (offline / key missing / stale ts)
        # froze the state; it must not count toward the duration.
        if (
            state.last_evaluated_at is not None
            and now - state.last_evaluated_at > OFFLINE_THRESHOLD
        ):
            if state.breach_since is not None and open_alert is None:
                state.breach_since = now
            if state.ok_since is not None:
                state.ok_since = now

        if compare(rule.operator, float(value), rule.threshold):
            state.ok_since = None
            state.breach_since = state.breach_since or now
            if open_alert is None and now - state.breach_since >= duration:
                elapsed = int((now - state.breach_since).total_seconds())
                alert = _create_alert(
                    station=station,
                    rule=rule,
                    title=f"{defn.display_label}: {station.name}",
                    message=_threshold_message(defn, station, rule, value, elapsed),
                )
                state.alert = alert
                open_alert = alert
                new_alerts.append(alert)
        else:
            state.breach_since = None
            if open_alert is not None:
                state.ok_since = state.ok_since or now
                if now - state.ok_since >= duration:
                    Alert.objects.filter(pk=open_alert.pk, is_resolved=False).update(
                        is_resolved=True, resolved_at=now
                    )
                    logger.info("Auto-resolved threshold alert %s", open_alert.pk)
                    state.ok_since = None
                    state.alert = None
                    open_alert = None
            else:
                state.ok_since = None
                state.alert = None

        pending = state.ok_since is not None or (
            state.breach_since is not None and open_alert is None
        )
        if pending:
            state.last_evaluated_at = now

        if (state.breach_since, state.ok_since, state.alert_id, state.last_evaluated_at) == before:
            continue
        if is_new:
            to_create.append(state)
            states[(station.pk, key)] = state
        else:
            to_update.append(state)

    if to_create:
        ThresholdState.objects.bulk_create(to_create)
    if to_update:
        ThresholdState.objects.bulk_update(
            to_update, ["breach_since", "ok_since", "alert", "last_evaluated_at"]
        )
    return new_alerts


def _check_thresholds(now=None):
    """Evaluate generic threshold rules per (rule, station, concrete metric key).

    Duration + hysteresis (spec §5.2): a breach must hold ``duration_seconds``
    before an alert fires, and the value must stay OK for the same duration
    before it auto-resolves. No current value (station offline, key absent, or
    the sample's ``ts`` missing/older than OFFLINE_THRESHOLD) freezes the
    state — neither fire nor resolve — and a pending timer restarts after
    such a gap (``last_evaluated_at``), so the gap never counts toward the
    duration. State lives in ``ThresholdState`` so a restart keeps the timers.

    Each rule is evaluated and persisted inside a transaction holding the
    AlertRule row lock (``select_for_update``); the rule-change/delete reset
    takes the same lock, so a stale evaluation can never create an alert
    after the rule was reset or disabled. The locked, fresh row's fields are
    used; a rule that is gone/inactive by then is skipped.

    Queries are bounded per run: rules, definitions, online telemetry and
    existing states are each loaded once; per rule with matching samples a
    constant number of lock/savepoint queries follows, plus writes only for
    alerts that fire/resolve and state rows that changed (or have a pending
    timer).
    """
    from apps.stations.models import StationTelemetry

    now = now or timezone.now()
    rules = list(
        AlertRule.objects.filter(alert_type=AlertRule.AlertType.THRESHOLD, is_active=True)
    )
    if not rules:
        return []
    definitions = {
        d.key: d
        for d in MetricDefinition.objects.filter(is_blocked=False).only(
            "key", "label", "label_override", "unit", "precision"
        )
    }
    # Offline / never-seen stations are skipped entirely → their state is frozen.
    telemetry = list(
        StationTelemetry.objects.select_related("station").filter(
            station__last_seen__gte=now - OFFLINE_THRESHOLD
        )
    )
    current = []
    for tel in telemetry:
        metrics = tel.metrics if isinstance(tel.metrics, dict) else {}
        # Stale samples (e.g. heartbeat bumped last_seen but the error-isolated
        # metrics ingest failed) are no current value.
        current.append(
            (
                tel.station,
                {
                    k: v
                    for k, v in metrics.items()
                    if isinstance(v, dict) and _sample_is_current(v, now)
                },
            )
        )
    states_by_rule = {}
    for st in ThresholdState.objects.filter(rule__in=rules).select_related("alert"):
        states_by_rule.setdefault(st.rule_id, {})[(st.station_id, st.metric_key)] = st

    new_alerts = []
    for rule in rules:
        if not _threshold_candidates(current, definitions, rule.metric_key):
            continue
        with transaction.atomic():
            locked = (
                AlertRule.objects.select_for_update()
                .filter(pk=rule.pk, alert_type=AlertRule.AlertType.THRESHOLD, is_active=True)
                .first()
            )
            if locked is None:  # deleted / disabled since the load
                continue
            if evaluation_changed(rule, locked):
                # Changed since the load: the reset signal dropped the state we
                # loaded — evaluate the fresh rule against the DB state.
                states = {
                    (st.station_id, st.metric_key): st
                    for st in ThresholdState.objects.filter(rule=locked).select_related("alert")
                }
            else:
                states = states_by_rule.get(rule.pk, {})
            candidates = _threshold_candidates(current, definitions, locked.metric_key)
            new_alerts.extend(_evaluate_threshold_rule(locked, candidates, states, now))
    return new_alerts


def check_alerts():
    """Run all alert checks and return a list of newly created alerts."""
    global _unresolved_cache
    _build_unresolved_cache()

    new_alerts = []
    new_alerts.extend(_check_station_offline())
    new_alerts.extend(_check_ota_failed())
    new_alerts.extend(_check_unexpected_reboot())
    new_alerts.extend(_check_power_warning())
    new_alerts.extend(_check_storage_health())
    new_alerts.extend(_check_thresholds())

    if new_alerts:
        logger.info("Alert check complete: %d new alert(s) created.", len(new_alerts))

    _unresolved_cache = None  # Clear cache after run
    return new_alerts
