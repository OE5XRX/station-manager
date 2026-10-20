"""User API create (`POST /api/v1/deployments/`) on the version contract.

Inputs are a version (``image_tag`` + ``image_channel``) and one
``target_station``; the server resolves the station's machine image. The old
``image_release`` input is gone (no compat layer). Every rejection is a 400
before any write.
"""

import pytest
from django.urls import reverse

from apps.deployments.models import Deployment, DeploymentResult
from apps.images.models import ImageRelease

QEMU = ImageRelease.Machine.QEMU
RPI = ImageRelease.Machine.RPI
URL = "/api/v1/deployments/"


def _release(tag, machine=QEMU, channel="release", ota=True):
    extra = {}
    if ota:
        extra = {
            "rootfs_s3_key": f"r/{channel}/{tag}/{machine}",
            "rootfs_sha256": "b" * 64,
            "rootfs_size_bytes": 5,
        }
    return ImageRelease.objects.create(
        tag=tag,
        machine=machine,
        channel=channel,
        s3_key=f"i/{channel}/{tag}/{machine}",
        sha256="a" * 64,
        size_bytes=10,
        **extra,
    )


@pytest.fixture
def rpi_station(api_topology):
    station = api_topology["station_in"]
    station.current_image_release = _release("2026.10.01-01", machine=RPI)
    station.save(update_fields=["current_image_release"])
    return station


def _post(bearer, user, payload):
    return bearer(user).post(URL, payload, format="json")


@pytest.mark.django_db
def test_resolves_station_machine_image_and_defaults_to_release(api_topology, bearer, rpi_station):
    _release("2026.10.10-01", machine=QEMU)
    rpi_new = _release("2026.10.10-01", machine=RPI)
    _release("2026.10.10-01", machine=RPI, channel="dev")

    r = _post(
        bearer,
        api_topology["staff"],
        {"image_tag": "2026.10.10-01", "target_station": rpi_station.pk},
    )

    assert r.status_code == 201, r.data
    dep = Deployment.objects.get(pk=r.data["id"])
    assert (dep.image_tag, dep.image_channel) == ("2026.10.10-01", "release")
    assert dep.results.get().image_release == rpi_new
    assert r.data["image_tag"] == "2026.10.10-01"
    assert r.data["image_channel"] == "release"
    assert r.data["skipped_stations"] == []
    assert r.data["target_station"] == rpi_station.pk
    assert "image_release" not in r.data


@pytest.mark.django_db
def test_explicit_dev_channel(api_topology, bearer, rpi_station):
    _release("2026.10.10-01", machine=RPI)
    dev = _release("2026.10.10-01", machine=RPI, channel="dev")

    r = _post(
        bearer,
        api_topology["staff"],
        {"image_tag": "2026.10.10-01", "image_channel": "dev", "target_station": rpi_station.pk},
    )

    assert r.status_code == 201, r.data
    dep = Deployment.objects.get(pk=r.data["id"])
    assert dep.image_channel == "dev"
    assert dep.results.get().image_release == dev


@pytest.mark.django_db
def test_image_release_input_is_gone(api_topology, bearer, rpi_station):
    rel = _release("2026.10.10-01", machine=RPI)
    r = _post(
        bearer, api_topology["staff"], {"image_release": rel.pk, "target_station": rpi_station.pk}
    )
    assert r.status_code == 400
    assert "image_tag" in r.data
    assert not Deployment.objects.exists()


@pytest.mark.django_db
def test_target_station_required(api_topology, bearer):
    _release("2026.10.10-01")
    r = _post(bearer, api_topology["staff"], {"image_tag": "2026.10.10-01"})
    assert r.status_code == 400
    assert "target_station" in r.data
    assert not Deployment.objects.exists()


@pytest.mark.django_db
def test_unknown_version_400_on_image_tag(api_topology, bearer, rpi_station):
    _release("2026.10.10-01", machine=RPI)  # exists, but only on release
    r = _post(
        bearer,
        api_topology["staff"],
        {"image_tag": "2026.10.10-01", "image_channel": "dev", "target_station": rpi_station.pk},
    )
    assert r.status_code == 400
    assert "image_tag" in r.data
    assert "Unknown version" in str(r.data["image_tag"])
    assert not Deployment.objects.exists()


@pytest.mark.django_db
def test_version_missing_for_station_machine_400_on_image_tag(api_topology, bearer, rpi_station):
    _release("2026.10.10-01", machine=QEMU)  # version exists, not for RPi
    r = _post(
        bearer,
        api_topology["staff"],
        {"image_tag": "2026.10.10-01", "target_station": rpi_station.pk},
    )
    assert r.status_code == 400
    assert "image_tag" in r.data
    assert RPI in str(r.data["image_tag"])
    assert not Deployment.objects.exists()


@pytest.mark.django_db
def test_not_ota_ready_400_on_image_tag(api_topology, bearer, rpi_station):
    _release("2026.10.10-01", machine=RPI, ota=False)
    r = _post(
        bearer,
        api_topology["staff"],
        {"image_tag": "2026.10.10-01", "target_station": rpi_station.pk},
    )
    assert r.status_code == 400
    assert "image_tag" in r.data
    assert "OTA-ready" in str(r.data["image_tag"])
    assert not Deployment.objects.exists()


@pytest.mark.django_db
def test_station_without_platform_400_on_target_station(api_topology, bearer):
    _release("2026.10.10-01")
    station = api_topology["station_in"]
    assert station.current_image_release is None
    r = _post(
        bearer, api_topology["staff"], {"image_tag": "2026.10.10-01", "target_station": station.pk}
    )
    assert r.status_code == 400
    assert "target_station" in r.data
    assert not Deployment.objects.exists()


@pytest.mark.django_db
def test_active_deployment_conflict_400_non_field(api_topology, bearer, rpi_station):
    cur = rpi_station.current_image_release
    _release("2026.10.10-01", machine=RPI)
    blocking = Deployment.objects.create(
        image_tag=cur.tag,
        image_channel=cur.channel,
        target_type=Deployment.TargetType.STATION,
        target_station=rpi_station,
        status=Deployment.Status.IN_PROGRESS,
    )
    DeploymentResult.objects.create(
        deployment=blocking,
        station=rpi_station,
        image_release=cur,
        status=DeploymentResult.Status.INSTALLING,
    )

    r = _post(
        bearer,
        api_topology["staff"],
        {"image_tag": "2026.10.10-01", "target_station": rpi_station.pk},
    )

    assert r.status_code == 400
    assert "non_field_errors" in r.data or isinstance(r.data, list)
    assert "image_tag" not in r.data and "target_station" not in r.data
    assert list(Deployment.objects.values_list("pk", flat=True)) == [blocking.pk]


@pytest.mark.django_db
def test_read_api_exposes_version_and_result_image(api_topology, bearer, rpi_station):
    rel = _release("2026.10.10-01", machine=RPI)
    r = _post(
        bearer,
        api_topology["staff"],
        {"image_tag": "2026.10.10-01", "target_station": rpi_station.pk},
    )
    dep_id = r.data["id"]

    detail = bearer(api_topology["staff"]).get(reverse("api:deployment-detail", args=[dep_id]))
    assert detail.status_code == 200
    assert detail.data["image_tag"] == "2026.10.10-01"
    assert detail.data["image_channel"] == "release"
    assert detail.data["skipped_stations"] == []
    assert "image_release" not in detail.data

    results = bearer(api_topology["staff"]).get(f"/api/v1/deployments/{dep_id}/results/")
    assert results.status_code == 200
    rows = results.data["results"] if isinstance(results.data, dict) else results.data
    assert [row["image_release"] for row in rows] == [rel.pk]


@pytest.mark.django_db
def test_schema_deployment_version_fields(api_topology, bearer):
    resp = bearer(api_topology["admin"]).get("/api/v1/schema/?format=json")
    assert resp.status_code == 200
    schema = resp.data
    components = schema["components"]["schemas"]

    post_op = schema["paths"]["/api/v1/deployments/"]["post"]
    req_ref = post_op["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    request = components[req_ref.rsplit("/", 1)[-1]]
    req_props = request["properties"]
    assert {"image_tag", "image_channel", "target_station", "strategy", "phase_config"} <= set(
        req_props
    )
    assert "image_release" not in req_props
    assert {"image_tag", "target_station"} <= set(request.get("required", []))
    assert "image_channel" not in request.get("required", [])
    assert req_props["image_channel"].get("default") == "release"

    resp_ref = post_op["responses"]["201"]["content"]["application/json"]["schema"]["$ref"]
    created = components[resp_ref.rsplit("/", 1)[-1]]["properties"]
    assert {"id", "image_tag", "image_channel", "skipped_stations"} <= set(created)
    assert "image_release" not in created

    read = components["Deployment"]["properties"]
    assert {"image_tag", "image_channel", "skipped_stations"} <= set(read)
    assert "image_release" not in read

    assert "image_release" in components["DeploymentResult"]["properties"]
