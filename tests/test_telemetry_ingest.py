import pytest

from apps.stations.ingest import ingest_telemetry
from apps.stations.models import Station, StationAuditLog


@pytest.mark.django_db
def test_ingest_first_boot_no_reboot_event():
    station = Station.objects.create(name="OE5A")
    tel = ingest_telemetry(
        station,
        {
            "boot": {
                "boot_id": "b1",
                "boot_count": 1,
                "reboot_reason": "clean",
                "uptime_seconds": 10.0,
            },
        },
    )
    assert tel.boot_id == "b1"
    assert tel.last_reboot_at is None
    assert not StationAuditLog.objects.filter(
        station=station, event_type=StationAuditLog.EventType.REBOOT
    ).exists()


@pytest.mark.django_db
def test_ingest_boot_id_change_logs_reboot():
    station = Station.objects.create(name="OE5A")
    ingest_telemetry(
        station, {"boot": {"boot_id": "b1", "boot_count": 1, "reboot_reason": "clean"}}
    )
    tel = ingest_telemetry(
        station, {"boot": {"boot_id": "b2", "boot_count": 2, "reboot_reason": "crash"}}
    )
    assert tel.boot_id == "b2"
    assert tel.last_reboot_at is not None
    assert tel.last_reboot_reason == "crash"
    assert (
        StationAuditLog.objects.filter(
            station=station, event_type=StationAuditLog.EventType.REBOOT
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_ingest_extracts_power_slot_storage():
    station = Station.objects.create(name="OE5A")
    tel = ingest_telemetry(
        station,
        {
            "power": {
                "undervoltage_now": False,
                "undervoltage_occurred": True,
                "throttled_now": False,
                "throttled_occurred": False,
            },
            "slot": {"active_slot": "b", "image_version": "v9", "last_ota_result": "success"},
            "storage": {
                "root_device": "mmcblk0",
                "devices": [
                    {
                        "name": "mmcblk0",
                        "kind": "emmc",
                        "life_time_a_pct": 30,
                        "life_time_b_pct": 10,
                        "pre_eol": "warning",
                        "io_error_count": 2,
                    }
                ],
            },
        },
    )
    assert tel.undervoltage_occurred is True
    assert tel.active_slot == "b"
    assert tel.image_version == "v9"
    assert tel.worst_life_time_pct == 30
    assert tel.worst_pre_eol == "warning"
    assert tel.io_error_count == 2


@pytest.mark.django_db
def test_ingest_malformed_blob_returns_none():
    station = Station.objects.create(name="OE5A")
    assert ingest_telemetry(station, "not-a-dict") is None
    assert ingest_telemetry(station, {"boot": "bad"}) is not None  # partial tolerated
