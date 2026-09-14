import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.module_firmware.models import ModuleFirmwareImportJob, ModuleFirmwareRelease, ModuleType

User = get_user_model()


@pytest.fixture
def rel(db):
    t = ModuleType.objects.create(
        key="fm",
        display_name="FM",
        firmware_repo="OE5XRX/FW-RemoteStation",
        release_asset_prefix="fm-sa818",
    )
    return ModuleFirmwareRelease.objects.create(
        module_type=t,
        variant="vhf",
        version="26.07.04-01",
        storage_key="k",
        sha256="a" * 64,
        size_bytes=5,
        source_repo="r",
        source_tag="26.07.04-01",
    )


@pytest.mark.django_db
def test_list_visible_to_any_user(client, rel):
    client.force_login(User.objects.create_user(username="u", password="x"))
    resp = client.get(reverse("module_firmware:release_list"))
    assert resp.status_code == 200 and b"26.07.04-01" in resp.content


@pytest.mark.django_db
def test_import_is_staff_only(client, rel):
    client.force_login(User.objects.create_user(username="u", password="x"))
    resp = client.post(
        reverse("module_firmware:import"),
        {"module_type": rel.module_type_id, "tag": "26.07.04-02"},
    )
    assert resp.status_code == 403
    client.force_login(User.objects.create_user(username="s", password="x", is_staff=True))
    resp = client.post(
        reverse("module_firmware:import"),
        {"module_type": rel.module_type_id, "tag": "26.07.04-02"},
    )
    assert resp.status_code in (302, 200)
    assert ModuleFirmwareImportJob.objects.filter(tag="26.07.04-02").exists()
