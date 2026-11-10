from datetime import timedelta

import pytest
from django.utils import timezone

from apps.monitoring import engine
from apps.monitoring.models import Alert, AlertRule
from apps.passive.models import AprsSettings, PassiveServiceRun

pytestmark = pytest.mark.django_db


def _run(station, started_ago_h, last_packet_ago_h=None, state="running"):
    now = timezone.now()
    run = PassiveServiceRun.objects.create(
        station=station, service="aprs", slot="slot0", state=state
    )
    PassiveServiceRun.objects.filter(pk=run.pk).update(
        started_at=now - timedelta(hours=started_ago_h),
        last_packet_at=None
        if last_packet_ago_h is None
        else now - timedelta(hours=last_packet_ago_h),
    )
    return run


def _alerts():
    return Alert.objects.filter(alert_rule__alert_type="passive_no_rx")


def test_rule_seeded():
    rule = AlertRule.objects.get(alert_type="passive_no_rx")
    assert rule.is_active and rule.severity == "warning"


def test_no_alert_for_run_shorter_than_threshold(station):
    _run(station, started_ago_h=5)
    assert engine.check_alerts() == [] and _alerts().count() == 0


def test_alert_raised_after_6h_without_packets(station):
    _run(station, started_ago_h=7)
    new = engine.check_alerts()
    assert [a.title for a in new] == [f"APRS RX still: {station.name}"]
    assert "7 h" in new[0].message


def test_recent_packet_keeps_run_quiet(station):
    _run(station, started_ago_h=30, last_packet_ago_h=1)
    assert engine.check_alerts() == []


def test_old_last_packet_alerts(station):
    _run(station, started_ago_h=30, last_packet_ago_h=8)
    assert len(engine.check_alerts()) == 1


def test_alert_not_duplicated(station):
    _run(station, started_ago_h=7)
    engine.check_alerts()
    assert engine.check_alerts() == [] and _alerts().count() == 1


def test_alert_resolved_on_next_packet(station):
    run = _run(station, started_ago_h=7)
    engine.check_alerts()
    PassiveServiceRun.objects.filter(pk=run.pk).update(last_packet_at=timezone.now())
    engine.check_alerts()
    assert _alerts().get().is_resolved is True


def test_ended_run_neither_raises_nor_resolves(station):
    run = _run(station, started_ago_h=7)
    engine.check_alerts()
    PassiveServiceRun.objects.filter(pk=run.pk).update(ended_at=timezone.now(), state="stopped")
    assert engine.check_alerts() == []
    assert _alerts().get().is_resolved is False


def test_non_running_state_not_alerted(station):
    _run(station, started_ago_h=7, state="starting")
    assert engine.check_alerts() == []


def test_threshold_from_settings(station):
    AprsSettings.objects.update_or_create(pk=1, defaults={"no_rx_alert_hours": 1})
    _run(station, started_ago_h=2)
    assert len(engine.check_alerts()) == 1


def test_inactive_rule_disables(station):
    AlertRule.objects.filter(alert_type="passive_no_rx").update(is_active=False)
    _run(station, started_ago_h=7)
    assert engine.check_alerts() == []


def test_open_alert_resolved_even_if_rule_disabled(station):
    run = _run(station, started_ago_h=7)
    engine.check_alerts()
    AlertRule.objects.filter(alert_type="passive_no_rx").update(is_active=False)
    PassiveServiceRun.objects.filter(pk=run.pk).update(last_packet_at=timezone.now())
    engine.check_alerts()
    assert _alerts().get().is_resolved is True
