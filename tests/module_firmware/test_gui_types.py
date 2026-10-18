import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.accounts.models import AccountAuditLog
from apps.module_firmware.models import (
    Module,
    ModuleFirmwareRelease,
    ModuleType,
)

User = get_user_model()


@pytest.fixture
def staff(db):
    return User.objects.create_user(username="s", password="x", membership_level="staff")


@pytest.fixture
def member(db):
    return User.objects.create_user(username="m", password="x", membership_level="member")


@pytest.fixture
def mtype(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


def _post_data(**kw):
    data = {
        "key": "power",
        "display_name": "Power",
        "hw_repo": "",
        "firmware_repo": "OE5XRX/FW-RemoteStation",
        "release_asset_prefix": "power",
    }
    data.update(kw)
    return data


def test_list_for_applicant(client, db, mtype):
    client.force_login(User.objects.create_user(username="a", password="x"))
    resp = client.get(reverse("module_firmware:type_list"))
    assert resp.status_code == 200
    assert b"FM" in resp.content


def test_mutations_forbidden_for_member(client, member, mtype):
    client.force_login(member)
    urls = [
        reverse("module_firmware:type_create"),
        reverse("module_firmware:type_update", args=[mtype.pk]),
        reverse("module_firmware:type_delete", args=[mtype.pk]),
    ]
    for url in urls:
        assert client.get(url).status_code == 403
        assert client.post(url, _post_data()).status_code == 403
    assert ModuleType.objects.count() == 1


def test_mutations_redirect_anonymous(client, mtype):
    for url in (
        reverse("module_firmware:type_create"),
        reverse("module_firmware:type_update", args=[mtype.pk]),
        reverse("module_firmware:type_delete", args=[mtype.pk]),
    ):
        resp = client.post(url, _post_data())
        assert resp.status_code == 302 and "login" in resp["Location"]


def test_staff_create_audited(client, staff):
    client.force_login(staff)
    resp = client.post(reverse("module_firmware:type_create"), _post_data())
    assert resp.status_code == 302
    assert ModuleType.objects.filter(key="power").exists()
    logs = AccountAuditLog.objects.filter(event_type=AccountAuditLog.EventType.CONFIG_CHANGED)
    assert logs.count() == 1
    assert logs[0].details["object"] == "module_type"
    assert logs[0].details["action"] == "created"
    assert logs[0].details["key"] == "power"


def test_update_audits_changes(client, staff, mtype):
    client.force_login(staff)
    resp = client.post(
        reverse("module_firmware:type_update", args=[mtype.pk]),
        _post_data(key="fm", display_name="FM 2"),
    )
    assert resp.status_code == 302
    mtype.refresh_from_db()
    assert mtype.display_name == "FM 2"
    log = AccountAuditLog.objects.get(event_type=AccountAuditLog.EventType.CONFIG_CHANGED)
    assert log.details["action"] == "updated"
    assert log.details["changes"]["display_name"] == {"old": "FM", "new": "FM 2"}


def test_repo_format_validated(client, staff):
    client.force_login(staff)
    resp = client.post(
        reverse("module_firmware:type_create"), _post_data(firmware_repo="not a repo")
    )
    assert resp.status_code == 200
    assert "firmware_repo" in resp.context["form"].errors
    assert not ModuleType.objects.exists()


def test_prefix_trailing_dash_rejected(client, staff):
    client.force_login(staff)
    resp = client.post(
        reverse("module_firmware:type_create"), _post_data(release_asset_prefix="fm-")
    )
    assert resp.status_code == 200
    assert "release_asset_prefix" in resp.context["form"].errors


def test_key_readonly_on_edit(client, staff, mtype):
    client.force_login(staff)
    client.post(
        reverse("module_firmware:type_update", args=[mtype.pk]),
        _post_data(key="hacked"),
    )
    mtype.refresh_from_db()
    assert mtype.key == "fm"


def test_type_delete_unused_ok(client, staff, mtype):
    client.force_login(staff)
    assert client.get(reverse("module_firmware:type_delete", args=[mtype.pk])).status_code == 200
    resp = client.post(reverse("module_firmware:type_delete", args=[mtype.pk]))
    assert resp.status_code == 302
    assert not ModuleType.objects.exists()
    log = AccountAuditLog.objects.get(event_type=AccountAuditLog.EventType.CONFIG_CHANGED)
    assert log.details["action"] == "deleted"


def test_type_delete_blocked_by_module(client, staff, mtype):
    Module.objects.create(uid="u1", module_type=mtype)
    client.force_login(staff)
    resp = client.post(reverse("module_firmware:type_delete", args=[mtype.pk]), follow=True)
    assert ModuleType.objects.filter(pk=mtype.pk).exists()
    assert any("in use" in str(m) for m in resp.context["messages"])


def test_type_delete_blocked_by_archived_release(client, staff, mtype):
    from django.utils import timezone

    ModuleFirmwareRelease.objects.create(
        module_type=mtype,
        variant="v",
        version="1",
        storage_key="k",
        sha256="a" * 64,
        size_bytes=1,
        source_repo="r",
        source_tag="1",
        archived_at=timezone.now(),
    )
    client.force_login(staff)
    client.post(reverse("module_firmware:type_delete", args=[mtype.pk]))
    assert ModuleType.objects.filter(pk=mtype.pk).exists()


def test_form_rejects_untrusted_owner_accepts_case_insensitive(db):
    from apps.module_firmware.forms import ModuleTypeForm

    bad = ModuleTypeForm(_post_data(firmware_repo="evil/FW"))
    assert not bad.is_valid() and "firmware_repo" in bad.errors
    assert "OE5XRX" in bad.errors["firmware_repo"][0]
    ok = ModuleTypeForm(_post_data(firmware_repo="oe5xrx/FW-RemoteStation"))
    assert ok.is_valid(), ok.errors
