import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import AccountAuditLog
from apps.module_firmware.forms import ModuleFirmwareTargetForm
from apps.module_firmware.models import (
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
    ModuleType,
)
from apps.stations.models import StationAuditLog

User = get_user_model()


@pytest.fixture
def staff(db):
    return User.objects.create_user(username="s", password="x", membership_level="staff")


@pytest.fixture
def member(db):
    return User.objects.create_user(username="m", password="x", membership_level="member")


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


def _release(mtype, version="1.0.0", variant="vhf", archived=False):
    rel = ModuleFirmwareRelease.objects.create(
        module_type=mtype,
        variant=variant,
        version=version,
        storage_key=f"k-{mtype.key}-{variant}-{version}",
        sha256="a" * 64,
        size_bytes=1,
        source_repo="r",
        source_tag="t",
    )
    if archived:
        ModuleFirmwareRelease.all_objects.filter(pk=rel.pk).update(archived_at=timezone.now())
    return rel


def _data(mtype, version="1.0.0", **kw):
    data = {
        "release": f"{mtype.pk}:{version}",
        "scope": "fleet",
        "tag": "",
        "station": "",
        "canary_tag": "",
    }
    data.update(kw)
    return data


def _config_logs():
    return AccountAuditLog.objects.filter(event_type=AccountAuditLog.EventType.CONFIG_CHANGED)


def test_list_for_applicant(client, db, fm):
    _release(fm)
    ModuleFirmwareTarget.objects.create(module_type=fm, version="1.0.0")
    client.force_login(User.objects.create_user(username="a", password="x"))
    resp = client.get(reverse("module_firmware:target_list"))
    assert resp.status_code == 200
    assert b"1.0.0" in resp.content
    assert b"FM" in resp.content


def test_writes_forbidden_for_member(client, member, fm):
    _release(fm)
    target = ModuleFirmwareTarget.objects.create(module_type=fm, version="1.0.0")
    client.force_login(member)
    for url in (
        reverse("module_firmware:target_create"),
        reverse("module_firmware:target_update", args=[target.pk]),
    ):
        assert client.get(url).status_code == 403
        assert client.post(url, _data(fm)).status_code == 403
    assert (
        client.post(reverse("module_firmware:target_delete", args=[target.pk])).status_code == 403
    )
    assert ModuleFirmwareTarget.objects.count() == 1


def test_delete_is_post_only(client, staff, fm):
    target = ModuleFirmwareTarget.objects.create(module_type=fm, version="1.0.0")
    client.force_login(staff)
    resp = client.get(reverse("module_firmware:target_delete", args=[target.pk]))
    assert resp.status_code == 405
    assert ModuleFirmwareTarget.objects.filter(pk=target.pk).exists()


def test_staff_create_fleet_target_audited(client, staff, fm):
    _release(fm)
    client.force_login(staff)
    resp = client.post(reverse("module_firmware:target_create"), _data(fm))
    assert resp.status_code == 302
    target = ModuleFirmwareTarget.objects.get()
    assert target.module_type == fm
    assert target.version == "1.0.0"
    assert target.scope == "fleet"
    assert target.created_by == staff
    log = _config_logs().get()
    assert log.details["object"] == "module_firmware_target"
    assert log.details["action"] == "created"
    assert log.details["module_type"] == "fm"
    assert log.details["scope"] == "fleet"
    assert log.details["version"] == "1.0.0"


def test_station_target_writes_station_audit(client, staff, fm, station_factory):
    _release(fm)
    st = station_factory()
    client.force_login(staff)
    resp = client.post(
        reverse("module_firmware:target_create"),
        _data(fm, scope="station", station=str(st.pk)),
    )
    assert resp.status_code == 302
    row = StationAuditLog.objects.get(event_type=StationAuditLog.EventType.FIRMWARE_TARGET_SET)
    assert row.station == st
    assert row.user == staff
    assert _config_logs().get().details["station"] == st.name


def test_update_audits_changes(client, staff, fm):
    _release(fm)
    _release(fm, version="1.1.0")
    target = ModuleFirmwareTarget.objects.create(module_type=fm, version="1.0.0")
    client.force_login(staff)
    resp = client.post(
        reverse("module_firmware:target_update", args=[target.pk]), _data(fm, version="1.1.0")
    )
    assert resp.status_code == 302
    target.refresh_from_db()
    assert target.version == "1.1.0"
    log = _config_logs().get()
    assert log.details["action"] == "updated"
    assert log.details["changes"]["version"] == {"old": "1.0.0", "new": "1.1.0"}


def test_delete_station_target_writes_removed_audit(client, staff, fm, station_factory):
    st = station_factory()
    target = ModuleFirmwareTarget.objects.create(
        module_type=fm, version="1.0.0", scope="station", station=st
    )
    client.force_login(staff)
    resp = client.post(reverse("module_firmware:target_delete", args=[target.pk]))
    assert resp.status_code == 302
    assert not ModuleFirmwareTarget.objects.exists()
    row = StationAuditLog.objects.get(event_type=StationAuditLog.EventType.FIRMWARE_TARGET_REMOVED)
    assert row.station == st
    assert row.user == staff
    log = _config_logs().get()
    assert log.details["action"] == "deleted"


def test_unknown_release_value_rejected(client, staff, fm):
    _release(fm)
    client.force_login(staff)
    resp = client.post(reverse("module_firmware:target_create"), _data(fm, version="9.9.9"))
    assert resp.status_code == 200
    assert "release" in resp.context["form"].errors
    assert not ModuleFirmwareTarget.objects.exists()


def test_garbage_release_value_rejected(fm):
    _release(fm)
    form = ModuleFirmwareTargetForm(data=_data(fm, release="nonsense"))
    assert not form.is_valid()
    assert "release" in form.errors


def test_archived_release_not_offered_on_create(fm):
    _release(fm, version="0.9.0", archived=True)
    _release(fm)
    values = [
        v
        for _group, opts in ModuleFirmwareTargetForm().fields["release"].choices[1:]
        for v, _label in opts
    ]
    assert f"{fm.pk}:1.0.0" in values
    assert f"{fm.pk}:0.9.0" not in values


def test_release_choices_grouped_and_variants_joined(fm):
    _release(fm, variant="vhf")
    _release(fm, variant="uhf")
    _release(fm, version="1.1.0", variant="vhf")
    choices = ModuleFirmwareTargetForm().fields["release"].choices
    groups = dict(choices[1:])
    assert list(groups) == ["FM"]
    assert groups["FM"] == [
        (f"{fm.pk}:1.1.0", "1.1.0 — vhf"),
        (f"{fm.pk}:1.0.0", "1.0.0 — uhf, vhf"),
    ]


def test_tag_scope_requires_tag(client, staff, fm):
    _release(fm)
    client.force_login(staff)
    resp = client.post(reverse("module_firmware:target_create"), _data(fm, scope="tag"))
    assert resp.status_code == 200
    # One message from Model.clean(), not duplicated by the CheckConstraint mirror.
    assert resp.context["form"].non_field_errors() == [
        "A tag target must set a tag and no station."
    ]
    assert not ModuleFirmwareTarget.objects.exists()


def test_canary_only_on_fleet(client, staff, fm, station_factory, make_station_tag):
    _release(fm)
    st = station_factory()
    canary = make_station_tag("canary")
    client.force_login(staff)
    resp = client.post(
        reverse("module_firmware:target_create"),
        _data(fm, scope="station", station=str(st.pk), canary_tag=str(canary.pk)),
    )
    assert resp.status_code == 200
    assert resp.context["form"].errors
    assert not ModuleFirmwareTarget.objects.exists()
    # ... but allowed on fleet
    resp = client.post(
        reverse("module_firmware:target_create"), _data(fm, canary_tag=str(canary.pk))
    )
    assert resp.status_code == 302
    assert ModuleFirmwareTarget.objects.get().canary_tag == canary


def test_duplicate_fleet_target_is_form_error(client, staff, fm):
    _release(fm)
    ModuleFirmwareTarget.objects.create(module_type=fm, version="1.0.0")
    client.force_login(staff)
    resp = client.post(reverse("module_firmware:target_create"), _data(fm))
    assert resp.status_code == 200
    assert resp.context["form"].non_field_errors() == [
        "This module type already has a fleet target."
    ]
    assert ModuleFirmwareTarget.objects.count() == 1


def test_duplicate_tag_target_is_form_error(client, staff, fm, make_station_tag):
    _release(fm)
    tag = make_station_tag("lte")
    ModuleFirmwareTarget.objects.create(module_type=fm, version="1.0.0", scope="tag", tag=tag)
    client.force_login(staff)
    resp = client.post(
        reverse("module_firmware:target_create"), _data(fm, scope="tag", tag=str(tag.pk))
    )
    assert resp.status_code == 200
    assert resp.context["form"].non_field_errors()
    assert ModuleFirmwareTarget.objects.count() == 1


def test_same_scope_other_type_is_allowed(client, staff, fm):
    other = ModuleType.objects.create(key="pwr", display_name="Power")
    _release(fm)
    _release(other)
    ModuleFirmwareTarget.objects.create(module_type=fm, version="1.0.0")
    client.force_login(staff)
    resp = client.post(reverse("module_firmware:target_create"), _data(other))
    assert resp.status_code == 302
    assert ModuleFirmwareTarget.objects.count() == 2


def test_edit_target_with_archived_version_keeps_choice(client, staff, fm, make_station_tag):
    _release(fm, version="0.9.0", archived=True)
    _release(fm)
    target = ModuleFirmwareTarget.objects.create(module_type=fm, version="0.9.0")
    client.force_login(staff)
    url = reverse("module_firmware:target_update", args=[target.pk])
    resp = client.get(url)
    form = resp.context["form"]
    assert form.initial["release"] == f"{fm.pk}:0.9.0"
    labels = dict(opt for _g, opts in form.fields["release"].choices[1:] for opt in opts)
    assert "(archived)" in labels[f"{fm.pk}:0.9.0"]
    canary = make_station_tag("canary")
    resp = client.post(url, _data(fm, version="0.9.0", canary_tag=str(canary.pk)))
    assert resp.status_code == 302
    target.refresh_from_db()
    assert target.version == "0.9.0"
    assert target.canary_tag == canary


def test_target_create_reconciles_assigned_modules(client, staff, fm, station_factory):
    _release(fm)
    st = station_factory()
    m = Module.objects.create(
        uid="A", module_type=fm, variant="vhf", last_reported_version="0.1.0"
    )
    ModuleAssignmentHistory.objects.create(module=m, station=st, slot="slot0")
    client.force_login(staff)
    resp = client.post(reverse("module_firmware:target_create"), _data(fm))
    assert resp.status_code == 302
    m.refresh_from_db()
    assert m.firmware_convergence == Module.Convergence.UPDATING


def test_list_shows_variant_coverage_and_manage_actions(client, staff, fm):
    _release(fm, variant="vhf")
    _release(fm, variant="uhf")
    target = ModuleFirmwareTarget.objects.create(module_type=fm, version="1.0.0")
    client.force_login(staff)
    resp = client.get(reverse("module_firmware:target_list"))
    content = resp.content.decode()
    assert "uhf" in content and "vhf" in content
    assert reverse("module_firmware:target_update", args=[target.pk]) in content
    assert reverse("module_firmware:target_delete", args=[target.pk]) in content


def test_list_hides_manage_actions_for_member(client, member, fm):
    target = ModuleFirmwareTarget.objects.create(module_type=fm, version="1.0.0")
    client.force_login(member)
    content = client.get(reverse("module_firmware:target_list")).content.decode()
    assert reverse("module_firmware:target_update", args=[target.pk]) not in content
    assert reverse("module_firmware:target_create") not in content


def test_list_empty_state_links_to_releases(client, staff, db):
    client.force_login(staff)
    content = client.get(reverse("module_firmware:target_list")).content.decode()
    assert reverse("module_firmware:release_list") in content


def test_form_widgets_carry_scope_data_attributes(fm):
    form = ModuleFirmwareTargetForm()
    assert "data-scope-select" in form.fields["scope"].widget.attrs
    assert form.fields["tag"].widget.attrs["data-scope-field"] == "tag"
    assert form.fields["station"].widget.attrs["data-scope-field"] == "station"
    assert form.fields["canary_tag"].widget.attrs["data-scope-field"] == "canary_tag"


def test_update_moving_station_override_audits_removal(client, staff, fm, station_factory):
    _release(fm)
    st = station_factory()
    target = ModuleFirmwareTarget.objects.create(
        module_type=fm, version="1.0.0", scope="station", station=st
    )
    client.force_login(staff)
    resp = client.post(reverse("module_firmware:target_update", args=[target.pk]), _data(fm))
    assert resp.status_code == 302
    row = StationAuditLog.objects.get(event_type=StationAuditLog.EventType.FIRMWARE_TARGET_REMOVED)
    assert row.station == st
    assert not StationAuditLog.objects.filter(
        event_type=StationAuditLog.EventType.FIRMWARE_TARGET_SET
    ).exists()


def test_fleet_target_delete_reconciles_rollup(client, staff, fm, station_factory):
    _release(fm)
    st = station_factory()
    m = Module.objects.create(
        uid="DEL", module_type=fm, variant="vhf", last_reported_version="0.1.0"
    )
    ModuleAssignmentHistory.objects.create(module=m, station=st, slot="slot0")
    client.force_login(staff)
    client.post(reverse("module_firmware:target_create"), _data(fm))
    m.refresh_from_db()
    assert m.firmware_convergence == Module.Convergence.UPDATING
    target = ModuleFirmwareTarget.objects.get()
    resp = client.post(reverse("module_firmware:target_delete", args=[target.pk]))
    assert resp.status_code == 302
    m.refresh_from_db()
    assert m.firmware_convergence != Module.Convergence.UPDATING


def test_target_update_noop_writes_no_audit(client, staff, fm, station_factory):
    _release(fm)
    st = station_factory()
    target = ModuleFirmwareTarget.objects.create(
        module_type=fm, version="1.0.0", scope="station", station=st
    )
    client.force_login(staff)
    resp = client.post(
        reverse("module_firmware:target_update", args=[target.pk]),
        _data(fm, scope="station", station=st.pk),
    )
    assert resp.status_code == 302
    assert not _config_logs().exists()
    assert not StationAuditLog.objects.filter(
        event_type=StationAuditLog.EventType.FIRMWARE_TARGET_SET
    ).exists()
