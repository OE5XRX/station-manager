import pytest
from django.urls import reverse

from apps.api.models import DeviceKey
from apps.stations.models import Station


@pytest.mark.django_db
class TestStationPermissions:
    def test_member_can_view_station_list(self, client, member_user, station):
        """Members should be able to view the station list."""
        client.force_login(member_user)
        response = client.get(reverse("stations:station_list"))
        assert response.status_code == 200

    def test_member_can_view_station_detail(self, client, member_user, station):
        """Members should be able to view station detail."""
        client.force_login(member_user)
        response = client.get(
            reverse("stations:station_detail", kwargs={"pk": station.pk}),
        )
        assert response.status_code == 200

    def test_member_cannot_create_station(self, client, member_user):
        """Members should get 403 on station create."""
        client.force_login(member_user)
        response = client.get(reverse("stations:station_create"))
        assert response.status_code == 403

    def test_member_cannot_create_station_post(self, client, member_user):
        """Members should get 403 when POSTing to station create."""
        client.force_login(member_user)
        response = client.post(
            reverse("stations:station_create"),
            data={"name": "Hacker Station", "callsign": "OE5HAX"},
        )
        assert response.status_code == 403

    def test_member_cannot_edit_station(self, client, member_user, station):
        """Members should get 403 on station edit."""
        client.force_login(member_user)
        response = client.get(
            reverse("stations:station_edit", kwargs={"pk": station.pk}),
        )
        assert response.status_code == 403

    def test_member_cannot_delete_station(self, client, member_user, station):
        """Members should get 403 on station delete."""
        client.force_login(member_user)
        response = client.post(
            reverse("stations:station_delete", kwargs={"pk": station.pk}),
        )
        assert response.status_code == 403

    def test_operator_can_create_station(self, client, operator_user):
        """Operators should be able to create stations."""
        client.force_login(operator_user)
        response = client.post(
            reverse("stations:station_create"),
            data={"name": "New Station", "callsign": "OE5NEW"},
        )
        # Successful create redirects to detail
        assert response.status_code == 302
        assert Station.objects.filter(name="New Station").exists()

    def test_operator_can_edit_station(self, client, operator_user, station):
        """Operators should be able to edit stations."""
        client.force_login(operator_user)
        response = client.post(
            reverse("stations:station_edit", kwargs={"pk": station.pk}),
            data={"name": "Updated Station", "callsign": "OE5UPD"},
        )
        assert response.status_code == 302
        station.refresh_from_db()
        assert station.name == "Updated Station"

    def test_member_cannot_upload_photo(self, client, member_user, station):
        """Members should get 403 on photo upload."""
        client.force_login(member_user)
        response = client.post(
            reverse("stations:station_photo_upload", kwargs={"pk": station.pk}),
        )
        assert response.status_code == 403

    def test_member_cannot_add_log_entry(self, client, member_user, station):
        """Members should get 403 on adding log entries."""
        client.force_login(member_user)
        response = client.post(
            reverse("stations:station_log_add", kwargs={"pk": station.pk}),
            data={"title": "Test", "message": "Test log", "entry_type": "note"},
        )
        assert response.status_code == 403

    def test_unauthenticated_cannot_view_stations(self, client, station):
        """Unauthenticated users should be redirected to login."""
        response = client.get(reverse("stations:station_list"))
        assert response.status_code == 302


@pytest.mark.django_db
class TestDeviceKeyManagement:
    def test_generate_ed25519_key(self, client, operator_user, station):
        """Generating an Ed25519 key should create DeviceKey."""
        client.force_login(operator_user)
        response = client.post(
            reverse("stations:station_generate_key", kwargs={"pk": station.pk}),
        )
        assert response.status_code == 302
        assert DeviceKey.objects.filter(station=station).exists()

    def test_generate_ed25519_shows_private_key_once(self, client, operator_user, station):
        """After generating a key, the detail page should show the PEM once."""
        client.force_login(operator_user)
        client.post(
            reverse("stations:station_generate_key", kwargs={"pk": station.pk}),
        )
        response = client.get(
            reverse("stations:station_detail", kwargs={"pk": station.pk}),
        )
        assert response.status_code == 200

    def test_revoke_ed25519_key(self, client, operator_user, station):
        """Revoking should delete the DeviceKey."""
        client.force_login(operator_user)
        # First generate
        client.post(
            reverse("stations:station_generate_key", kwargs={"pk": station.pk}),
        )
        assert DeviceKey.objects.filter(station=station).exists()
        # Then revoke
        response = client.post(
            reverse("stations:station_revoke_key", kwargs={"pk": station.pk}),
        )
        assert response.status_code == 302
        assert not DeviceKey.objects.filter(station=station).exists()

    def test_member_cannot_generate_key(self, client, member_user, station):
        """Members should get 403 when trying to generate an Ed25519 key."""
        client.force_login(member_user)
        response = client.post(
            reverse("stations:station_generate_key", kwargs={"pk": station.pk}),
        )
        assert response.status_code == 403

    def test_member_cannot_revoke_key(self, client, member_user, station_with_key):
        """Members should get 403 when trying to revoke an Ed25519 key."""
        station, _ = station_with_key
        client.force_login(member_user)
        response = client.post(
            reverse("stations:station_revoke_key", kwargs={"pk": station.pk}),
        )
        assert response.status_code == 403
