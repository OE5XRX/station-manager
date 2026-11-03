import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.module_firmware.mcuboot import DEV_KEY_HASH_PREFIX
from apps.module_firmware.models import (
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareConvergenceState,
    ModuleType,
)
from apps.stations.models import StationAuditLog
from tests.module_firmware.update_status_helpers import (
    _cs,
    _module,
    _online,
    _release,
    _target,
)

User = get_user_model()


@pytest.fixture
def module(db, station_factory):
    t = ModuleType.objects.create(key="fm", display_name="FM")
    m = Module.objects.create(uid="LIFE1", module_type=t)
    ModuleAssignmentHistory.objects.create(module=m, station=station_factory(), slot="slot1")
    StationAuditLog.log(
        module=m, event_type=StationAuditLog.EventType.MODULE_DISCOVERED, message="x"
    )
    return m


@pytest.mark.django_db
def test_detail_shows_history_and_audit(client, module):
    client.force_login(User.objects.create_user(username="u", password="x"))
    resp = client.get(reverse("module_firmware:module_detail", args=[module.uid]))
    assert resp.status_code == 200
    assert b"LIFE1" in resp.content
    assert b"slot1" in resp.content


@pytest.mark.django_db
def test_detail_edit_controls_only_for_staff(client, module):
    client.force_login(User.objects.create_user(username="u", password="x"))
    resp = client.get(reverse("module_firmware:module_detail", args=[module.uid]))
    assert b"Manage module" not in resp.content
    client.force_login(
        User.objects.create_user(username="s", password="x", membership_level="staff")
    )
    resp = client.get(reverse("module_firmware:module_detail", args=[module.uid]))
    assert b"Manage module" in resp.content


# --- Task 9: update status, retry button, boot key -------------------------

State = ModuleFirmwareConvergenceState.State
RETRY = "Erneut versuchen"


@pytest.fixture
def drifted(db, station):
    fm = ModuleType.objects.create(key="fmd", display_name="FMD")
    _online(station)
    _target(fm)
    rel = _release(fm, boot_key="1111111111111111")
    m = _module(fm, station)
    return rel, m


def _detail(client, m):
    return client.get(reverse("module_firmware:module_detail", args=[m.uid])).content.decode()


@pytest.mark.django_db
def test_detail_shows_full_reason_and_retry_button_for_staff(client, staff_user, drifted):
    rel, m = drifted
    _cs(m, rel, state=State.FAILED, last_error_message="dfu-util timeout")
    client.force_login(staff_user)
    body = _detail(client, m)
    assert "Fehler: dfu-util timeout" in body
    assert body.count(RETRY) == 1  # rendered by the polled pill, not duplicated
    assert reverse("module_firmware:module_retry_update", args=[m.uid]) in body
    assert f'data-confirm="Update für {m.uid} erneut versuchen?"' in body
    assert 'hx-trigger="every 5s"' not in body  # failed is not live


@pytest.mark.django_db
def test_detail_no_retry_button_for_member(client, member_user, drifted):
    rel, m = drifted
    _cs(m, rel, state=State.FAILED, last_error_message="dfu-util timeout")
    client.force_login(member_user)
    body = _detail(client, m)
    assert "Fehler: dfu-util timeout" in body
    assert RETRY not in body


@pytest.mark.django_db
def test_detail_no_retry_button_for_not_updatable(client, staff_user, drifted):
    rel, m = drifted
    m.reported_boot_key = DEV_KEY_HASH_PREFIX
    m.save(update_fields=["reported_boot_key"])
    _cs(m, rel, state=State.NOT_UPDATABLE, reason_code="boot_key_mismatch")
    client.force_login(staff_user)
    body = _detail(client, m)
    assert "Nicht updatebar" in body
    assert RETRY not in body


@pytest.mark.django_db
def test_detail_live_status_polls(client, member_user, drifted):
    rel, m = drifted
    client.force_login(member_user)
    body = _detail(client, m)
    assert "Update ausstehend" in body
    assert 'hx-trigger="every 5s"' in body
    assert reverse("module_firmware:module_update_status", args=[m.uid]) in body


@pytest.mark.django_db
def test_detail_shows_boot_key_and_dev_badge(client, member_user, drifted):
    rel, m = drifted
    m.reported_boot_key = DEV_KEY_HASH_PREFIX
    m.save(update_fields=["reported_boot_key"])
    client.force_login(member_user)
    body = _detail(client, m)
    assert "Bootloader-Schlüssel" in body
    assert "e30466f6b8470c1f" in body
    assert "Dev-Key" in body


@pytest.mark.django_db
def test_detail_boot_key_unknown_shows_dash_without_badge(client, member_user, drifted):
    rel, m = drifted
    client.force_login(member_user)
    body = _detail(client, m)
    assert "Bootloader-Schlüssel" in body
    assert "Dev-Key" not in body


@pytest.mark.django_db
def test_detail_settled_status_does_not_poll(client, member_user, drifted):
    rel, m = drifted
    m.last_reported_version = rel.version
    m.save(update_fields=["last_reported_version"])
    client.force_login(member_user)
    body = _detail(client, m)
    assert "Aktuell" in body
    assert "/update-status/" not in body
    assert "fw-status-refresh" not in body
