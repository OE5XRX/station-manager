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


@pytest.fixture
def staff(db):
    return User.objects.create_user(username="s", password="x", is_staff=True)


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


# ---------------------------------------------------------------------------
# Finding 3: reject import for partially-configured module type
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_import_rejects_missing_release_asset_prefix(client, staff):
    """Module type without release_asset_prefix must not queue a job."""
    t = ModuleType.objects.create(
        key="bare",
        display_name="Bare",
        firmware_repo="OE5XRX/FW-RemoteStation",
        release_asset_prefix="",  # missing
    )
    client.force_login(staff)
    resp = client.post(
        reverse("module_firmware:import"),
        {"module_type": t.pk, "tag": "v1.0"},
    )
    assert resp.status_code == 302
    assert not ModuleFirmwareImportJob.objects.exists()


@pytest.mark.django_db
def test_import_rejects_missing_firmware_repo(client, staff):
    """Module type without firmware_repo must not queue a job."""
    t = ModuleType.objects.create(
        key="norepo",
        display_name="NoRepo",
        firmware_repo="",  # missing
        release_asset_prefix="norepo-fw",
    )
    client.force_login(staff)
    resp = client.post(
        reverse("module_firmware:import"),
        {"module_type": t.pk, "tag": "v1.0"},
    )
    assert resp.status_code == 302
    assert not ModuleFirmwareImportJob.objects.exists()


# ---------------------------------------------------------------------------
# Finding 4: dedup — active release exists
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_import_dedup_active_release_exists(client, staff, rel):
    """Staff POST for a tag that already has an active release must not create a job."""
    # rel fixture already has an active release for (fm, 26.07.04-01)
    client.force_login(staff)
    resp = client.post(
        reverse("module_firmware:import"),
        {"module_type": rel.module_type_id, "tag": rel.source_tag},
    )
    assert resp.status_code == 302
    assert not ModuleFirmwareImportJob.objects.exists()


# ---------------------------------------------------------------------------
# Finding 4: dedup — pending/running job exists
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_import_dedup_pending_job_exists(client, staff, rel):
    """Staff POST for a tag with a pending job must not create a second job."""
    existing_job = ModuleFirmwareImportJob.objects.create(
        module_type=rel.module_type,
        source_repo=rel.source_repo,
        tag="26.07.04-new",
        status=ModuleFirmwareImportJob.Status.PENDING,
        requested_by=staff,
    )
    client.force_login(staff)
    resp = client.post(
        reverse("module_firmware:import"),
        {"module_type": rel.module_type_id, "tag": "26.07.04-new"},
    )
    assert resp.status_code == 302
    # Still only one job
    assert ModuleFirmwareImportJob.objects.filter(tag="26.07.04-new").count() == 1
    _ = existing_job  # just reference it


@pytest.mark.django_db
def test_import_dedup_running_job_exists(client, staff, rel):
    """Staff POST for a tag with a running job must not create a second job."""
    ModuleFirmwareImportJob.objects.create(
        module_type=rel.module_type,
        source_repo=rel.source_repo,
        tag="26.07.04-new",
        status=ModuleFirmwareImportJob.Status.RUNNING,
        requested_by=staff,
    )
    client.force_login(staff)
    resp = client.post(
        reverse("module_firmware:import"),
        {"module_type": rel.module_type_id, "tag": "26.07.04-new"},
    )
    assert resp.status_code == 302
    assert ModuleFirmwareImportJob.objects.filter(tag="26.07.04-new").count() == 1


# ---------------------------------------------------------------------------
# Finding 4: happy path — fully-configured type, new tag, no existing job
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_import_happy_path_creates_one_job(client, staff, rel):
    """Fully-configured type + new tag + no existing job → exactly one job created."""
    client.force_login(staff)
    resp = client.post(
        reverse("module_firmware:import"),
        {"module_type": rel.module_type_id, "tag": "26.07.04-99"},
    )
    assert resp.status_code == 302
    assert ModuleFirmwareImportJob.objects.filter(tag="26.07.04-99").count() == 1
