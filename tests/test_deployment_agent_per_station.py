"""Agent endpoints serve each station its own machine's image.

A fleet deployment of one version reaches a qemu and an RPi station; the
agent protocol (URLs + JSON keys) is unchanged, but check/download/commit
read the image from the station's DeploymentResult.
"""

import io
import json

import pytest
from django.urls import reverse

from apps.api.models import DeviceKey
from apps.deployments.models import Deployment, DeploymentResult
from apps.deployments.services import create_deployment
from apps.images.models import ImageRelease
from tests.conftest import _make_ed25519_keypair, device_auth_headers

QEMU = ImageRelease.Machine.QEMU
RPI = ImageRelease.Machine.RPI

CHECK_KEYS = {
    "deployment_result_id",
    "deployment_id",
    "deployment_result_status",
    "target_tag",
    "checksum_sha256",
    "size_bytes",
    "download_url",
}


def _release(tag, machine, sha_char, size):
    return ImageRelease.objects.create(
        tag=tag,
        machine=machine,
        s3_key=f"images/{tag}/{machine}.wic.bz2",
        sha256="a" * 64,
        size_bytes=1000,
        rootfs_s3_key=f"images/{tag}/{machine}.rootfs.bz2",
        rootfs_sha256=sha_char * 64,
        rootfs_size_bytes=size,
    )


@pytest.fixture
def fleet(db, station_factory, staff_user):
    old_q, old_r = _release("v1", QEMU, "1", 10), _release("v1", RPI, "2", 20)
    new_q, new_r = _release("v2", QEMU, "c", 300), _release("v2", RPI, "d", 400)
    stations = {}
    for name, current in (("qemu", old_q), ("rpi", old_r)):
        s = station_factory(name=name, current_image_release=current, current_os_version="v1")
        priv, _pem, pub = _make_ed25519_keypair()
        DeviceKey.objects.create(station=s, current_public_key=pub)
        stations[name] = (s, priv)
    dep = create_deployment(
        tag="v2", channel="release", target_type=Deployment.TargetType.ALL, user=staff_user
    )
    return {"dep": dep, "stations": stations, "images": {"qemu": new_q, "rpi": new_r}}


def _post(client, url, station, priv, payload):
    body = json.dumps(payload).encode()
    return client.post(
        url,
        data=body,
        content_type="application/json",
        **device_auth_headers(priv, station.pk, body),
    )


@pytest.mark.parametrize("name", ["qemu", "rpi"])
def test_check_returns_station_own_artifact(client, fleet, name):
    station, priv = fleet["stations"][name]
    image = fleet["images"][name]
    dep = fleet["dep"]

    r = _post(client, reverse("api:deployment_check"), station, priv, {"current_version": "v1"})

    assert r.status_code == 200
    data = r.json()
    assert set(data) == CHECK_KEYS
    result = DeploymentResult.objects.get(deployment=dep, station=station)
    assert data["deployment_result_id"] == result.pk
    assert data["deployment_id"] == dep.pk
    assert data["deployment_result_status"] == DeploymentResult.Status.PENDING
    assert data["target_tag"] == "v2"
    assert data["checksum_sha256"] == image.rootfs_sha256
    assert data["size_bytes"] == image.rootfs_size_bytes
    assert data["download_url"] == f"/api/v1/deployments/{dep.pk}/download/"


@pytest.mark.parametrize("name", ["qemu", "rpi"])
def test_download_serves_station_own_rootfs(client, fleet, name, monkeypatch):
    station, priv = fleet["stations"][name]
    image = fleet["images"][name]
    opened = []

    def fake_open(key):
        opened.append(key)
        return io.BytesIO(key.encode())

    monkeypatch.setattr("apps.images.storage.open_stream", fake_open)
    r = client.get(
        reverse("api:deployment_download", args=[fleet["dep"].pk]),
        **device_auth_headers(priv, station.pk, b""),
    )

    assert r.status_code == 200
    assert opened == [image.rootfs_s3_key]
    assert b"".join(r.streaming_content) == image.rootfs_s3_key.encode()
    assert f"oe5xrx-{image.machine}-v2.rootfs.bz2" in r["Content-Disposition"]


def test_commit_matches_result_tag_for_both(client, fleet):
    dep = fleet["dep"]
    for name, (station, priv) in fleet["stations"].items():
        DeploymentResult.objects.filter(deployment=dep, station=station).update(
            status=DeploymentResult.Status.REBOOTING
        )
        r = _post(client, reverse("api:deployment_commit"), station, priv, {"version": "v2"})
        assert r.status_code == 200, r.content
        station.refresh_from_db()
        assert station.current_image_release == fleet["images"][name]
        result = DeploymentResult.objects.get(deployment=dep, station=station)
        assert result.status == DeploymentResult.Status.SUCCESS

    dep.refresh_from_db()
    assert dep.status == Deployment.Status.COMPLETED


def test_status_update_per_station(client, fleet):
    dep = fleet["dep"]
    station, priv = fleet["stations"]["rpi"]
    result = DeploymentResult.objects.get(deployment=dep, station=station)
    r = _post(
        client,
        reverse("api:deployment_status_update", args=[result.pk]),
        station,
        priv,
        {"status": "downloading"},
    )
    assert r.status_code == 200
    result.refresh_from_db()
    assert result.status == DeploymentResult.Status.DOWNLOADING
