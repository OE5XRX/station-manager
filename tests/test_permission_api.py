import pytest
from rest_framework.test import APIClient

from apps.accounts.models import User

pytestmark = pytest.mark.django_db


def _c(level):
    c = APIClient()
    c.force_authenticate(
        User.objects.create_user(username=f"api-{level}", password="x", membership_level=level)
    )
    return c


def test_member_without_assignment_sees_all_stations(station_factory):
    station_factory()
    station_factory()
    r = _c("member").get("/api/v1/stations/")
    assert r.status_code == 200 and r.json()["count"] == 2


def test_applicant_can_read_api(station):
    assert _c("applicant").get("/api/v1/stations/").status_code == 200


def test_staff_cannot_create_region():
    r = _c("staff").post("/api/v1/regions/", {"name": "X"}, format="json")
    assert r.status_code == 403 and r.json()["required"] == "topology.manage"


def test_staff_can_edit_station_name(station):
    r = _c("staff").patch(f"/api/v1/stations/{station.pk}/", {"name": "new"}, format="json")
    assert r.status_code == 200


def test_staff_cannot_set_station_region_via_api(station):
    from apps.stations.models import Region

    region = Region.objects.create(name="R")
    c = _c("staff")
    r = c.patch(f"/api/v1/stations/{station.pk}/", {"region": region.pk}, format="json")
    assert r.status_code == 403 and r.json()["required"] == "topology.manage"
    r = c.post("/api/v1/stations/", {"name": "n2", "region": region.pk}, format="json")
    assert r.status_code == 403


def test_region_manager_has_no_api_extra_rights(station):
    from apps.stations.models import Region, RegionAssignment

    region = Region.objects.create(name="R")
    station.region = region
    station.save(update_fields=["region"])
    c = APIClient()
    u = User.objects.create_user(username="rm", password="x", membership_level="member")
    RegionAssignment.objects.create(user=u, region=region, role="manager")
    c.force_authenticate(u)
    assert (
        c.patch(f"/api/v1/stations/{station.pk}/", {"name": "z"}, format="json").status_code == 403
    )


def test_staff_cannot_archive_image_api(image_release):
    r = _c("staff").post(f"/api/v1/images/{image_release.pk}/archive/")
    assert r.status_code == 403 and r.json()["required"] == "images.manage"


def test_staff_cannot_touch_provisioning_or_alert_rules():
    c = _c("staff")
    assert c.get("/api/v1/provisioning-jobs/").status_code == 403
    assert c.post("/api/v1/alert-rules/", {}, format="json").status_code == 403


def test_deployment_delete_not_allowed(deployment):
    assert _c("admin").delete(f"/api/v1/deployments/{deployment.pk}/").status_code == 405


def test_audio_diagnostics_requires_control_manage(station):
    r = _c("member").post(f"/api/v1/stations/{station.pk}/audio-diagnostics/", {}, format="json")
    assert r.status_code == 403 and r.json()["required"] == "control.manage"


def test_device_key_principal_rejected_on_user_api(client, station_with_key):
    from apps.accounts.models import AccountAuditLog
    from tests.conftest import device_auth_headers

    station, private_key = station_with_key
    r = client.get("/api/v1/stations/", **device_auth_headers(private_key, station.pk, b""))
    assert r.status_code in (401, 403)
    assert not AccountAuditLog.objects.filter(event_type="permission_denied").exists()


def test_audio_diagnostics_unknown_station_404_for_staff():
    r = _c("staff").post("/api/v1/stations/999999/audio-diagnostics/", {}, format="json")
    assert r.status_code == 404


def test_staff_can_delete_station(station):
    assert _c("staff").delete(f"/api/v1/stations/{station.pk}/").status_code == 204


def test_applicant_cannot_write_station(station):
    r = _c("applicant").patch(f"/api/v1/stations/{station.pk}/", {"name": "x"}, format="json")
    assert r.status_code == 403 and r.json()["required"] == "stations.manage"


@pytest.mark.parametrize("level", ["applicant", "member"])
def test_viewer_gets_no_station_ip(station, level):
    station.last_ip_address = "203.0.113.77"
    station.save(update_fields=["last_ip_address"])
    c = _c(level)
    rows = c.get("/api/v1/stations/").json()["results"]
    assert rows and all("last_ip_address" not in row for row in rows)
    detail = c.get(f"/api/v1/stations/{station.pk}/").json()
    assert "last_ip_address" not in detail
    assert "203.0.113.77" not in c.get(f"/api/v1/stations/{station.pk}/").content.decode()


@pytest.mark.parametrize("level", ["staff", "admin"])
def test_station_manager_gets_station_ip(station, level):
    station.last_ip_address = "203.0.113.77"
    station.save(update_fields=["last_ip_address"])
    c = _c(level)
    assert c.get(f"/api/v1/stations/{station.pk}/").json()["last_ip_address"] == "203.0.113.77"
    rows = c.get("/api/v1/stations/").json()["results"]
    assert rows[0]["last_ip_address"] == "203.0.113.77"


def _inventory(station):
    from apps.stations.models import StationInventory

    return StationInventory.objects.create(
        station=station,
        data={
            "cpu": {"cores": 4},
            "network": [{"interface": "eth0", "ip_address": "10.9.8.7", "mac_address": "aa:bb"}],
        },
    )


def test_viewer_inventory_has_no_network_block(station):
    _inventory(station)
    r = _c("member").get(f"/api/v1/stations/{station.pk}/inventory/")
    assert r.status_code == 200
    assert r.json()["data"] == {"cpu": {"cores": 4}}
    assert "10.9.8.7" not in r.content.decode()


def test_staff_inventory_includes_network_block(station):
    _inventory(station)
    r = _c("staff").get(f"/api/v1/stations/{station.pk}/inventory/")
    assert r.json()["data"]["network"][0]["ip_address"] == "10.9.8.7"


def test_station_serializer_without_request_fails_closed(station):
    from apps.api.read_serializers import StationInventorySerializer, StationSerializer

    station.last_ip_address = "203.0.113.77"
    station.save(update_fields=["last_ip_address"])
    assert "last_ip_address" not in StationSerializer(station).data
    assert "network" not in StationInventorySerializer(_inventory(station)).data["data"]


def test_schema_documents_optional_last_ip_address():
    from drf_spectacular.generators import SchemaGenerator

    schema = SchemaGenerator().get_schema(request=None, public=True)
    station = schema["components"]["schemas"]["Station"]
    assert "last_ip_address" in station["properties"]
    assert "last_ip_address" not in station.get("required", [])


def test_options_metadata_probe_does_not_audit_denial():
    from apps.accounts.models import AccountAuditLog

    r = _c("member").options("/api/v1/stations/")
    assert r.status_code == 200
    assert "POST" not in r.json().get("actions", {})
    assert not AccountAuditLog.objects.filter(event_type="permission_denied").exists()


def test_options_metadata_advertises_post_for_staff():
    r = _c("staff").options("/api/v1/stations/")
    assert r.status_code == 200
    assert "POST" in r.json().get("actions", {})


def test_staff_api_does_not_expose_private_fields():
    target = User.objects.create_user(
        username="tgt", password="x", membership_level="member", phone="+43 1", address="Weg 1"
    )
    r = _c("staff").get(f"/api/v1/users/{target.pk}/")
    assert r.status_code == 200
    body = r.json()
    assert "phone" not in body and "address" not in body
    assert "is_admin" not in body and "is_internal" not in body


def test_admin_api_sees_private_fields():
    target = User.objects.create_user(
        username="tgt2", password="x", membership_level="member", phone="+43 1"
    )
    assert _c("admin").get(f"/api/v1/users/{target.pk}/").json()["phone"] == "+43 1"


def test_member_does_not_see_applicants():
    User.objects.create_user(username="app1", password="x", membership_level="applicant")
    names = [u["username"] for u in _c("member").get("/api/v1/users/").json()["results"]]
    assert "app1" not in names


def test_member_cannot_retrieve_applicant_404():
    app = User.objects.create_user(username="app2", password="x", membership_level="applicant")
    assert _c("member").get(f"/api/v1/users/{app.pk}/").status_code == 404


def test_applicant_users_api_forbidden():
    assert _c("applicant").get("/api/v1/users/").status_code == 403


def test_staff_cannot_patch_users():
    target = User.objects.create_user(username="t3", password="x", membership_level="member")
    r = _c("staff").patch(f"/api/v1/users/{target.pk}/", {"first_name": "X"}, format="json")
    assert r.status_code == 403 and r.json()["required"] == "users.manage"


def test_users_api_list_matches_ui_directory_for_member(client):
    """The API returns exactly the set the UI directory shows a Member."""
    from django.urls import reverse
    from django.utils import timezone

    User.objects.create_user(username="dir-mem", password="x", membership_level="member")
    User.objects.create_user(username="dir-staff", password="x", membership_level="staff")
    User.objects.create_user(username="dir-app", password="x", membership_level="applicant")
    User.objects.create_user(
        username="dir-inactive", password="x", membership_level="member", is_active=False
    )
    User.objects.create_user(
        username="dir-gone", password="x", membership_level="member", deleted_at=timezone.now()
    )
    viewer = User.objects.create_user(
        username="dir-viewer", password="x", membership_level="member"
    )
    client.force_login(viewer)
    ui = {u.username for u in client.get(reverse("accounts:user_list")).context["users"]}
    api = APIClient()
    api.force_authenticate(viewer)
    api_names = {u["username"] for u in api.get("/api/v1/users/").json()["results"]}
    assert api_names == ui
    assert {"dir-mem", "dir-staff", "dir-viewer"} <= api_names
    assert not {"dir-app", "dir-inactive", "dir-gone"} & api_names


def test_admin_users_api_lists_all_non_deleted():
    """Admins get every non-deleted user: active and inactive, incl. Applicants."""
    from django.utils import timezone

    User.objects.create_user(username="adm-app", password="x", membership_level="applicant")
    User.objects.create_user(
        username="adm-inactive", password="x", membership_level="member", is_active=False
    )
    User.objects.create_user(
        username="adm-gone", password="x", membership_level="member", deleted_at=timezone.now()
    )
    names = {u["username"] for u in _c("admin").get("/api/v1/users/").json()["results"]}
    assert {"adm-app", "adm-inactive"} <= names
    assert "adm-gone" not in names
