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
