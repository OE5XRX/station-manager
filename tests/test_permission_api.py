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
