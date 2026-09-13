import pytest

from apps.module_firmware.models import Module, ModuleType
from apps.stations.models import StationAuditLog


@pytest.fixture
def module(db):
    t = ModuleType.objects.create(key="fm", display_name="FM")
    return Module.objects.create(uid="U1", module_type=t)


@pytest.mark.django_db
def test_audit_can_log_module_without_station(module):
    entry = StationAuditLog.log(
        module=module,
        event_type=StationAuditLog.EventType.MODULE_DISCOVERED,
        message="discovered",
    )
    assert entry.module == module
    assert entry.station is None
    assert module.audit_logs.count() == 1


@pytest.mark.django_db
def test_audit_dual_subject(module, station_factory):
    station = station_factory()
    StationAuditLog.log(
        station=station, module=module,
        event_type=StationAuditLog.EventType.MODULE_SWAPPED, message="swap",
    )
    assert station.audit_logs.filter(module=module).count() == 1
    assert module.audit_logs.filter(station=station).count() == 1


@pytest.mark.django_db
def test_audit_requires_a_subject():
    with pytest.raises(ValueError):
        StationAuditLog.log(event_type=StationAuditLog.EventType.MODULE_DISCOVERED)
