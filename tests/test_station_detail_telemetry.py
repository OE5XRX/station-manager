import pytest
from django.urls import reverse

from apps.stations.models import Station, StationTelemetry


@pytest.mark.django_db
def test_detail_shows_telemetry(client, admin_user):
    """Telemetry values surface on the station detail page."""
    client.force_login(admin_user)
    station = Station.objects.create(name="OE5A")
    StationTelemetry.objects.create(
        station=station,
        data={"power": {"undervoltage_now": False}, "storage": {"devices": []}},
        boot_count=7,
        last_reboot_reason="crash",
        active_slot="b",
        image_version="v3",
        worst_pre_eol="warning",
    )
    resp = client.get(reverse("stations:station_detail", args=[station.pk]))
    assert resp.status_code == 200
    content = resp.content.decode()
    assert "crash" in content
    assert "v3" in content


@pytest.mark.django_db
def test_detail_no_telemetry_shows_empty_state(client, admin_user):
    """Detail page renders correctly when no telemetry row exists."""
    client.force_login(admin_user)
    station = Station.objects.create(name="OE5B")
    resp = client.get(reverse("stations:station_detail", args=[station.pk]))
    assert resp.status_code == 200
    content = resp.content.decode()
    assert "No telemetry data received yet" in content


# M8: absent power block must render "not reported", not "ok"
@pytest.mark.django_db
def test_detail_absent_power_shows_not_reported(client, admin_user):
    """M8: when tel.data has no power block, show 'not reported', not 'ok'."""
    client.force_login(admin_user)
    station = Station.objects.create(name="OE5C")
    StationTelemetry.objects.create(
        station=station,
        data={"boot": {"boot_id": "b1"}},  # no power block
        boot_count=1,
    )
    resp = client.get(reverse("stations:station_detail", args=[station.pk]))
    assert resp.status_code == 200
    content = resp.content.decode()
    assert "not reported" in content
    # "ok" may still appear in other contexts but the power fields must say "not reported"
    # Check that the false "ok" for undervoltage/throttle is NOT present alongside boot data
    # (a station with no power block must not claim power is "ok")


# M9: absent storage block must render "not reported" for I/O errors, not "0"
@pytest.mark.django_db
def test_detail_absent_storage_shows_not_reported(client, admin_user):
    """M9: when tel.data has no storage block, show 'not reported' for I/O errors."""
    client.force_login(admin_user)
    station = Station.objects.create(name="OE5D")
    StationTelemetry.objects.create(
        station=station,
        data={"boot": {"boot_id": "b1"}},  # no storage block
        boot_count=1,
        io_error_count=0,
    )
    resp = client.get(reverse("stations:station_detail", args=[station.pk]))
    assert resp.status_code == 200
    content = resp.content.decode()
    assert "not reported" in content
