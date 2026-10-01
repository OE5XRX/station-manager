import pytest

from apps.stations.models import Station, StationAuditLog, StationTelemetry


@pytest.mark.django_db
def test_station_telemetry_defaults():
    station = Station.objects.create(name="OE5A")
    tel = StationTelemetry.objects.create(station=station)
    assert tel.boot_count == 0
    assert tel.io_error_count == 0
    assert station.telemetry == tel


@pytest.mark.django_db
def test_reboot_audit_event_type_exists():
    assert hasattr(StationAuditLog.EventType, "REBOOT")
