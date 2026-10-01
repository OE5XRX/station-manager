"""Tests for _check_power_warning (Task 14)."""

import pytest

from apps.monitoring.engine import _check_power_warning
from apps.monitoring.models import Alert, AlertRule
from apps.stations.models import Station, StationTelemetry


@pytest.fixture
def power_rule(db):
    rule, _ = AlertRule.objects.get_or_create(
        alert_type=AlertRule.AlertType.POWER_WARNING,
        defaults={
            "threshold": 0,
            "severity": AlertRule.Severity.WARNING,
            "is_active": True,
        },
    )
    return rule


@pytest.mark.django_db
def test_undervoltage_now_is_critical(power_rule):
    station = Station.objects.create(name="OE5A")
    StationTelemetry.objects.create(
        station=station,
        boot_id="b1",
        undervoltage_now=True,
        undervoltage_occurred=True,
        throttled_now=False,
    )
    alerts = _check_power_warning()
    assert len(alerts) == 1
    assert alerts[0].severity == Alert.Severity.CRITICAL


@pytest.mark.django_db
def test_occurred_only_is_warning(power_rule):
    station = Station.objects.create(name="OE5A")
    StationTelemetry.objects.create(
        station=station,
        boot_id="b1",
        undervoltage_now=False,
        throttled_now=False,
        undervoltage_occurred=True,
    )
    alerts = _check_power_warning()
    assert len(alerts) == 1
    assert alerts[0].severity == Alert.Severity.WARNING


@pytest.mark.django_db
def test_no_power_issue_no_alert(power_rule):
    station = Station.objects.create(name="OE5A")
    StationTelemetry.objects.create(
        station=station,
        boot_id="b1",
        undervoltage_occurred=False,
        throttled_occurred=False,
    )
    assert _check_power_warning() == []


@pytest.mark.django_db
def test_power_dedup(power_rule):
    station = Station.objects.create(name="OE5A")
    StationTelemetry.objects.create(
        station=station,
        boot_id="b1",
        undervoltage_occurred=True,
        undervoltage_now=True,
        throttled_now=False,
    )
    assert len(_check_power_warning()) == 1
    # Second cycle: unresolved alert already exists, now bit still set → no new alert.
    assert _check_power_warning() == []


# M4: escalation — an unresolved WARNING must escalate to CRITICAL when *_now fires
@pytest.mark.django_db
def test_power_warning_escalates_to_critical_when_now_fires(power_rule):
    """M4: an unresolved WARNING alert escalates to CRITICAL (one row, no second alert).

    To have an unresolved WARNING to escalate, the first cycle must create an alert
    that is NOT auto-resolved. We model that by directly creating a WARNING alert
    (as would persist from a prior ongoing episode) and marking the episode as
    already alerted, then letting a *_now bit fire.
    """
    from apps.monitoring.models import Alert as AlertModel

    station = Station.objects.create(name="OE5A")
    tel = StationTelemetry.objects.create(
        station=station,
        boot_id="b1",
        undervoltage_occurred=True,
        undervoltage_now=False,
        throttled_now=False,
        power_alerted_boot_id="b1",
    )
    AlertModel.objects.create(
        station=station,
        alert_rule=power_rule,
        severity=AlertModel.Severity.WARNING,
        title="Power warning: Undervoltage",
        message="x",
    )

    # *_now bit fires → ongoing undervoltage.
    tel.undervoltage_now = True
    tel.save()

    # Should escalate the existing alert to CRITICAL (one row, no second alert).
    alerts2 = _check_power_warning()
    assert len(alerts2) == 1
    assert alerts2[0].severity == Alert.Severity.CRITICAL
    db_alert = AlertModel.objects.get(station=station, is_resolved=False)
    assert db_alert.severity == AlertModel.Severity.CRITICAL
    assert AlertModel.objects.filter(station=station).count() == 1


# M5: auto-resolve when *_now bits are false (even if occurred is True)
@pytest.mark.django_db
def test_power_alert_resolves_when_now_bits_clear(power_rule):
    """M5: alert auto-resolves when both *_now bits are False, regardless of occurred."""
    station = Station.objects.create(name="OE5A")
    tel = StationTelemetry.objects.create(
        station=station,
        boot_id="b1",
        undervoltage_occurred=True,
        undervoltage_now=True,
        throttled_now=False,
    )
    # Create an active alert
    alerts = _check_power_warning()
    assert len(alerts) == 1

    # Now bits clear but occurred stays True (sticky bit)
    tel.undervoltage_now = False
    tel.throttled_now = False
    tel.save()

    # Second call: alert should be auto-resolved
    _check_power_warning()
    from apps.monitoring.models import Alert as AlertModel

    assert not AlertModel.objects.filter(station=station, is_resolved=False).exists()


# REGRESSION: sticky occurred=True + now=False must not flap an alert/notification per cycle
@pytest.mark.django_db
def test_power_no_flapping_in_post_brownout_state(power_rule):
    """Regression: occurred=True, both now bits False (normal post-brownout state the
    agent reports) must create EXACTLY ONE alert across many cycles, and it ends
    resolved — not a new alert + notification every check_alerts cycle until reboot.
    """
    from apps.monitoring.models import Alert as AlertModel

    station = Station.objects.create(name="OE5A")
    StationTelemetry.objects.create(
        station=station,
        boot_id="b1",
        undervoltage_occurred=True,
        undervoltage_now=False,
        throttled_now=False,
    )

    total_new = 0
    for _ in range(4):
        total_new += len(_check_power_warning())

    # Exactly one alert ever created (one notification), not one per cycle.
    assert total_new == 1
    assert AlertModel.objects.filter(station=station).count() == 1
    # And it ends resolved (now bits are clear).
    assert not AlertModel.objects.filter(station=station, is_resolved=False).exists()


# A new boot episode (boot_id change) after a resolved alert must allow a fresh alert.
@pytest.mark.django_db
def test_power_new_boot_resets_episode(power_rule):
    """After a resolved alert, a new boot_id resets the episode so a fresh event alerts."""
    from apps.monitoring.models import Alert as AlertModel

    station = Station.objects.create(name="OE5A")
    tel = StationTelemetry.objects.create(
        station=station,
        boot_id="b1",
        undervoltage_occurred=True,
        undervoltage_now=False,
        throttled_now=False,
    )
    # First episode: one alert, then resolved.
    assert len(_check_power_warning()) == 1
    assert not AlertModel.objects.filter(station=station, is_resolved=False).exists()
    # Same boot: no re-alert.
    assert _check_power_warning() == []

    # New boot with a fresh brownout event.
    tel.refresh_from_db()
    tel.boot_id = "b2"
    tel.undervoltage_occurred = True
    tel.undervoltage_now = False
    tel.throttled_now = False
    tel.save()

    alerts = _check_power_warning()
    assert len(alerts) == 1
    assert AlertModel.objects.filter(station=station).count() == 2


# H1 regression: after a transient warning (occurred-not-now) fires and resolves in the same
# boot, a subsequent is_now sample must still create a CRITICAL (must not be suppressed).
@pytest.mark.django_db
def test_power_live_critical_not_suppressed_after_transient_warning(power_rule):
    """H1: episode gate must NOT suppress a live CRITICAL in the same boot.

    Sequence:
      cycle 1 — occurred=True, now=False  → WARNING created, boot_id recorded, then resolved
      cycle 2 — occurred=True, now=True   → CRITICAL must be created despite same boot_id
    """
    from apps.monitoring.models import Alert as AlertModel

    station = Station.objects.create(name="OE5A")
    tel = StationTelemetry.objects.create(
        station=station,
        boot_id="b1",
        undervoltage_occurred=True,
        undervoltage_now=False,
        throttled_now=False,
    )

    # Cycle 1: transient warning fires (one alert created, then auto-resolved because now=False)
    alerts1 = _check_power_warning()
    assert len(alerts1) == 1
    assert alerts1[0].severity == Alert.Severity.WARNING
    assert not AlertModel.objects.filter(station=station, is_resolved=False).exists()

    # Refresh from DB so the episode gate set by cycle-1 is not clobbered by a stale save.
    tel.refresh_from_db()
    # Same boot, now the live bit fires — must create a CRITICAL (not suppressed by gate)
    tel.undervoltage_now = True
    tel.save()

    alerts2 = _check_power_warning()
    assert len(alerts2) == 1
    assert alerts2[0].severity == Alert.Severity.CRITICAL
    assert AlertModel.objects.filter(station=station, is_resolved=False).exists()


# H1: blank boot_id ("") must not skip the first alert
@pytest.mark.django_db
def test_power_blank_boot_id_fires_first_alert(power_rule):
    """H1: blank boot_id must not collide with the default power_alerted_boot_id=""
    and silently suppress the very first alert.
    """
    station = Station.objects.create(name="OE5A")
    StationTelemetry.objects.create(
        station=station,
        boot_id="",  # blank — no boot block
        undervoltage_occurred=True,
        undervoltage_now=False,
        throttled_now=False,
    )
    alerts = _check_power_warning()
    assert len(alerts) == 1
    assert alerts[0].severity == Alert.Severity.WARNING
