"""Tests for check_alerts() wiring of new telemetry checks (Task 15)."""

from unittest import mock

from apps.monitoring import engine


def test_check_alerts_calls_new_checks():
    with (
        mock.patch.object(engine, "_check_unexpected_reboot", return_value=[]) as r,
        mock.patch.object(engine, "_check_power_warning", return_value=[]) as p,
        mock.patch.object(engine, "_check_storage_health", return_value=[]) as s,
        mock.patch.object(engine, "_check_station_offline", return_value=[]),
        mock.patch.object(engine, "_check_cpu_temperature", return_value=[]),
        mock.patch.object(engine, "_check_disk_usage", return_value=[]),
        mock.patch.object(engine, "_check_ram_usage", return_value=[]),
        mock.patch.object(engine, "_check_ota_failed", return_value=[]),
        mock.patch.object(engine, "_build_unresolved_cache"),
    ):
        engine.check_alerts()
    r.assert_called_once()
    p.assert_called_once()
    s.assert_called_once()
