import io
from unittest import mock

import pytest
from django.urls import reverse

from apps.module_firmware.models import (
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareRelease,
    ModuleType,
)
from tests.conftest import device_auth_headers


@pytest.fixture
def module_type(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


@pytest.fixture
def fw_release(module_type):
    return ModuleFirmwareRelease.objects.create(
        module_type=module_type,
        variant="vhf",
        version="26.07.04-01",
        storage_key="module_firmware/fm/26.07.04-01/vhf.signed.bin",
        sha256="a" * 64,
        size_bytes=5,
        source_repo="r",
        source_tag="26.07.04-01",
    )


@pytest.mark.django_db
def test_download_forbidden_without_matching_assignment(client, station_with_key, fw_release):
    station, priv = station_with_key
    resp = client.get(
        reverse("module_firmware_api:download", args=[fw_release.pk]),
        **device_auth_headers(priv, station.pk, b""),
    )
    assert resp.status_code == 403


@pytest.mark.django_db
def test_download_streams_with_assignment(client, station_with_key, fw_release, module_type):
    station, priv = station_with_key
    mod = Module.objects.create(uid="U1", module_type=module_type)
    ModuleAssignmentHistory.objects.create(module=mod, station=station, slot="slot1")
    with mock.patch(
        "apps.module_firmware.api_views.storage.open_stream",
        return_value=io.BytesIO(b"hello"),
    ):
        resp = client.get(
            reverse("module_firmware_api:download", args=[fw_release.pk]),
            **device_auth_headers(priv, station.pk, b""),
        )
    assert resp.status_code == 200
    assert b"".join(resp.streaming_content) == b"hello"


@pytest.mark.django_db
def test_download_range_206(client, station_with_key, fw_release, module_type):
    station, priv = station_with_key
    mod = Module.objects.create(uid="U2", module_type=module_type)
    ModuleAssignmentHistory.objects.create(module=mod, station=station, slot="slot1")
    with mock.patch(
        "apps.module_firmware.api_views.storage.open_stream",
        return_value=io.BytesIO(b"hello"),
    ):
        resp = client.get(
            reverse("module_firmware_api:download", args=[fw_release.pk]),
            HTTP_RANGE="bytes=0-2",
            **device_auth_headers(priv, station.pk, b""),
        )
    assert resp.status_code == 206
    assert resp["Content-Range"] == "bytes 0-2/5"
    assert b"".join(resp.streaming_content) == b"hel"
