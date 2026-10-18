import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from apps.module_firmware.models import (
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareConvergenceState,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
    ModuleType,
)
from apps.module_firmware.views_convergence import firmware_row
from apps.stations.models import StationAuditLog

User = get_user_model()

REPORTED = "26.09.10-01"
TARGET = "26.09.15-01"


@pytest.fixture
def staff(db):
    return User.objects.create_user(username="s", password="x", membership_level="staff")


@pytest.fixture
def member(db):
    return User.objects.create_user(username="m", password="x", membership_level="member")


@pytest.fixture
def applicant(db):
    return User.objects.create_user(username="a", password="x")


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


def _release(mtype, version=TARGET, variant="vhf"):
    return ModuleFirmwareRelease.objects.create(
        module_type=mtype,
        variant=variant,
        version=version,
        storage_key=f"k-{variant}-{version}",
        sha256="a" * 64,
        size_bytes=1,
        source_repo="r",
        source_tag="t",
    )


def _module(mtype, station, *, uid="U1", variant="vhf", slot="slot0", **kw):
    m = Module.objects.create(
        uid=uid, module_type=mtype, variant=variant, last_reported_version=REPORTED, **kw
    )
    if station is not None:
        ModuleAssignmentHistory.objects.create(module=m, station=station, slot=slot)
    return m


def _quarantine(module, release):
    Module.objects.filter(pk=module.pk).update(firmware_convergence=Module.Convergence.QUARANTINED)
    return ModuleFirmwareConvergenceState.objects.create(
        module=module,
        target_release=release,
        state=ModuleFirmwareConvergenceState.State.QUARANTINED,
        attempts=3,
        last_error_mode=ModuleFirmwareConvergenceState.ErrorMode.REJECTED,
        last_error_message="signature mismatch",
    )


def test_overview_for_applicant(client, applicant, fm, station_factory):
    st = station_factory()
    _release(fm)
    ModuleFirmwareTarget.objects.create(module_type=fm, version=TARGET)
    _module(fm, st, uid="UID-OVERVIEW-1")
    client.force_login(applicant)
    resp = client.get(reverse("module_firmware:convergence"))
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "UID-OVERVIEW-1" in body
    assert REPORTED in body
    assert TARGET in body


def test_overview_anonymous_redirects(client, db):
    resp = client.get(reverse("module_firmware:convergence"))
    assert resp.status_code == 302


def test_overview_kpi_counts(client, applicant, fm):
    Module.objects.create(uid="A", module_type=fm, firmware_convergence="ok")
    Module.objects.create(uid="B", module_type=fm, firmware_convergence="ok")
    Module.objects.create(uid="C", module_type=fm, firmware_convergence="quarantined")
    client.force_login(applicant)
    resp = client.get(reverse("module_firmware:convergence"))
    assert "kpis" not in resp.context
    kpis = {value: n for value, _label, n in resp.context["kpi_list"]}
    assert kpis["ok"] == 2
    assert kpis["quarantined"] == 1
    assert kpis["updating"] == 0
    assert kpis["unknown"] == 0


def test_overview_filter_state(client, applicant, fm):
    Module.objects.create(uid="OK-MOD", module_type=fm, firmware_convergence="ok")
    Module.objects.create(uid="Q-MOD", module_type=fm, firmware_convergence="quarantined")
    client.force_login(applicant)
    resp = client.get(reverse("module_firmware:convergence"), {"state": "quarantined"})
    uids = [row["module"].uid for row in resp.context["rows"]]
    assert uids == ["Q-MOD"]


def test_overview_filter_station_and_q(client, applicant, fm, station_factory):
    st1, st2 = station_factory(), station_factory()
    _module(fm, st1, uid="AAA-1")
    _module(fm, st2, uid="BBB-2")
    client.force_login(applicant)
    url = reverse("module_firmware:convergence")
    resp = client.get(url, {"station": str(st2.pk)})
    assert [r["module"].uid for r in resp.context["rows"]] == ["BBB-2"]
    resp = client.get(url, {"q": "aaa"})
    assert [r["module"].uid for r in resp.context["rows"]] == ["AAA-1"]


def test_overview_no_release_for_variant_hint(client, applicant, fm, station_factory):
    st = station_factory()
    _release(fm, variant="vhf")
    ModuleFirmwareTarget.objects.create(module_type=fm, version=TARGET)
    _module(fm, st, uid="UHF-MOD", variant="uhf")
    client.force_login(applicant)
    resp = client.get(reverse("module_firmware:convergence"))
    row = resp.context["rows"][0]
    assert row["target"].version == TARGET
    assert row["has_release"] is False
    assert "no release for variant" in resp.content.decode()


def test_overview_shows_quarantine_details(client, applicant, fm, station_factory):
    st = station_factory()
    rel = _release(fm)
    ModuleFirmwareTarget.objects.create(module_type=fm, version=TARGET)
    m = _module(fm, st, uid="QQ")
    _quarantine(m, rel)
    client.force_login(applicant)
    resp = client.get(reverse("module_firmware:convergence"))
    body = resp.content.decode()
    assert "signature mismatch" in body
    # Viewer gets no release button.
    assert reverse("module_firmware:module_release_quarantine", args=["QQ"]) not in body


def test_overview_staff_sees_release_button(client, staff, fm, station_factory):
    st = station_factory()
    rel = _release(fm)
    ModuleFirmwareTarget.objects.create(module_type=fm, version=TARGET)
    _quarantine(_module(fm, st, uid="QQ"), rel)
    client.force_login(staff)
    resp = client.get(reverse("module_firmware:convergence"))
    assert reverse("module_firmware:module_release_quarantine", args=["QQ"]) in (
        resp.content.decode()
    )


def test_firmware_row_no_assignment(fm):
    m = Module.objects.create(uid="LOOSE", module_type=fm)
    row = firmware_row(m, None)
    assert row["target"] is None
    assert row["has_release"] is False
    assert row["quarantine"] is None


def test_release_quarantine_member_forbidden(client, member, fm, station_factory):
    st = station_factory()
    rel = _release(fm)
    ModuleFirmwareTarget.objects.create(module_type=fm, version=TARGET)
    cs = _quarantine(_module(fm, st, uid="QM"), rel)
    client.force_login(member)
    resp = client.post(reverse("module_firmware:module_release_quarantine", args=["QM"]))
    assert resp.status_code == 403
    cs.refresh_from_db()
    assert cs.state == ModuleFirmwareConvergenceState.State.QUARANTINED
    assert cs.attempts == 3


def test_release_quarantine_get_not_allowed(client, staff, fm):
    Module.objects.create(uid="G", module_type=fm)
    client.force_login(staff)
    resp = client.get(reverse("module_firmware:module_release_quarantine", args=["G"]))
    assert resp.status_code == 405


def test_release_quarantine_staff(client, staff, fm, station_factory):
    st = station_factory()
    rel = _release(fm)
    ModuleFirmwareTarget.objects.create(module_type=fm, version=TARGET)
    m = _module(fm, st, uid="QS")
    cs = _quarantine(m, rel)
    client.force_login(staff)
    resp = client.post(reverse("module_firmware:module_release_quarantine", args=["QS"]))
    assert resp.status_code == 302
    assert resp.url == reverse("module_firmware:module_detail", args=["QS"])
    cs.refresh_from_db()
    assert cs.state == ModuleFirmwareConvergenceState.State.UPDATING
    assert cs.attempts == 0
    assert cs.last_error_message == ""
    m.refresh_from_db()
    assert m.firmware_convergence == Module.Convergence.UPDATING
    row = StationAuditLog.objects.get(
        event_type=StationAuditLog.EventType.MODULE_QUARANTINE_RELEASED
    )
    assert row.user == staff
    assert row.module == m
    assert row.station == st


def test_release_quarantine_redirects_to_safe_next(client, staff, fm, station_factory):
    st = station_factory()
    rel = _release(fm)
    ModuleFirmwareTarget.objects.create(module_type=fm, version=TARGET)
    _quarantine(_module(fm, st, uid="QN"), rel)
    client.force_login(staff)
    nxt = reverse("module_firmware:convergence") + "?state=quarantined"
    resp = client.post(
        reverse("module_firmware:module_release_quarantine", args=["QN"]), {"next": nxt}
    )
    assert resp.status_code == 302
    assert resp.url == nxt


def test_release_quarantine_rejects_external_next(client, staff, fm, station_factory):
    st = station_factory()
    rel = _release(fm)
    ModuleFirmwareTarget.objects.create(module_type=fm, version=TARGET)
    _quarantine(_module(fm, st, uid="QX"), rel)
    client.force_login(staff)
    resp = client.post(
        reverse("module_firmware:module_release_quarantine", args=["QX"]),
        {"next": "https://evil.example.com/"},
    )
    assert resp.status_code == 302
    assert resp.url == reverse("module_firmware:module_detail", args=["QX"])


def test_release_quarantine_noop_message(client, staff, fm):
    Module.objects.create(uid="NOOP", module_type=fm)
    client.force_login(staff)
    resp = client.post(
        reverse("module_firmware:module_release_quarantine", args=["NOOP"]), follow=True
    )
    assert resp.status_code == 200
    msgs = [m.level_tag for m in resp.context["messages"]]
    assert msgs == ["info"]
    assert not StationAuditLog.objects.filter(
        event_type=StationAuditLog.EventType.MODULE_QUARANTINE_RELEASED
    ).exists()


def test_release_quarantine_unknown_module_404(client, staff, db):
    client.force_login(staff)
    resp = client.post(reverse("module_firmware:module_release_quarantine", args=["NOPE"]))
    assert resp.status_code == 404


def test_detail_shows_effective_target(client, applicant, fm, station_factory):
    st = station_factory()
    _release(fm)
    ModuleFirmwareTarget.objects.create(module_type=fm, version=TARGET)
    _module(fm, st, uid="DET")
    client.force_login(applicant)
    resp = client.get(reverse("module_firmware:module_detail", args=["DET"]))
    assert resp.status_code == 200
    assert resp.context["firmware"]["target"].version == TARGET
    body = resp.content.decode()
    assert TARGET in body
    assert reverse("module_firmware:target_list") in body


def test_detail_quarantine_release_button_for_staff(client, staff, fm, station_factory):
    st = station_factory()
    rel = _release(fm)
    ModuleFirmwareTarget.objects.create(module_type=fm, version=TARGET)
    _quarantine(_module(fm, st, uid="DQ"), rel)
    client.force_login(staff)
    resp = client.get(reverse("module_firmware:module_detail", args=["DQ"]))
    body = resp.content.decode()
    assert resp.context["firmware"]["quarantine"] is not None
    assert reverse("module_firmware:module_release_quarantine", args=["DQ"]) in body
    assert "data-confirm" in body
    assert "signature mismatch" in body


def test_stale_quarantine_hidden_after_fix_by_new_target(client, staff, fm, station_factory):
    """Quarantine on v1, target moved to v2 and module reports v2 -> OK: the old
    QUARANTINED row must neither be shown nor offered for release."""
    from apps.module_firmware.reconciler import reconcile_module

    st = station_factory()
    rel1 = _release(fm)
    target = ModuleFirmwareTarget.objects.create(module_type=fm, version=TARGET)
    m = _module(fm, st, uid="STALE")
    _quarantine(m, rel1)
    fixed = "26.09.20-01"
    _release(fm, version=fixed)
    target.version = fixed
    target.save()
    Module.objects.filter(pk=m.pk).update(last_reported_version=fixed)
    m.refresh_from_db()
    reconcile_module(m)
    m.refresh_from_db()
    assert m.firmware_convergence == Module.Convergence.OK
    client.force_login(staff)
    release_url = reverse("module_firmware:module_release_quarantine", args=["STALE"])

    overview = client.get(reverse("module_firmware:convergence"))
    assert overview.context["rows"][0]["quarantine"] is None
    assert release_url not in overview.content.decode()
    assert "signature mismatch" not in overview.content.decode()

    detail = client.get(reverse("module_firmware:module_detail", args=["STALE"]))
    assert detail.context["firmware"]["quarantine"] is None
    assert release_url not in detail.content.decode()


def test_quarantine_prefers_row_for_current_target(fm, station_factory):
    st = station_factory()
    rel1 = _release(fm)
    other = "26.09.20-01"
    rel2 = _release(fm, version=other)
    ModuleFirmwareTarget.objects.create(module_type=fm, version=TARGET)
    m = _module(fm, st, uid="PREF")
    current = _quarantine(m, rel1)
    stale = _quarantine(m, rel2)  # newer updated_at, but not the current target
    assert stale.updated_at >= current.updated_at
    m.refresh_from_db()
    row = firmware_row(m, m.assignments.get())
    assert row["quarantine"] == current


def test_station_filter_lists_only_stations_with_modules(client, applicant, fm, station_factory):
    # Finding 6: no LEFT JOIN leak of stations without (open) assignments.
    with_module = station_factory()
    empty = station_factory()
    closed_only = station_factory()
    _module(fm, with_module, uid="SF-1")
    m2 = _module(fm, None, uid="SF-2")
    ModuleAssignmentHistory.objects.create(
        module=m2, station=closed_only, slot="slot1", to_ts=timezone.now()
    )
    client.force_login(applicant)
    resp = client.get(reverse("module_firmware:convergence"))
    pks = {s.pk for s in resp.context["stations"]}
    assert pks == {with_module.pk}
    assert empty.pk not in pks


def test_overview_unicode_digit_station_filter(client, applicant, fm):
    # Finding 4: '²'.isdigit() is True but int('²') fails.
    client.force_login(applicant)
    resp = client.get(reverse("module_firmware:convergence"), {"station": "²"})
    assert resp.status_code == 200


def test_release_quarantine_view_noop_when_not_quarantined(client, staff, fm, station_factory):
    # Finding 2: a stale QUARANTINED row on a non-quarantined module -> no-op.
    st = station_factory()
    rel = _release(fm)
    ModuleFirmwareTarget.objects.create(module_type=fm, version=TARGET)
    m = _module(fm, st, uid="NQ")
    cs = _quarantine(m, rel)
    Module.objects.filter(pk=m.pk).update(firmware_convergence=Module.Convergence.OK)
    client.force_login(staff)
    resp = client.post(
        reverse("module_firmware:module_release_quarantine", args=["NQ"]), follow=True
    )
    assert [x.level_tag for x in resp.context["messages"]] == ["info"]
    cs.refresh_from_db()
    assert cs.state == ModuleFirmwareConvergenceState.State.QUARANTINED
    assert not StationAuditLog.objects.filter(
        event_type=StationAuditLog.EventType.MODULE_QUARANTINE_RELEASED
    ).exists()


def test_release_quarantine_unassigned_module(client, staff, fm, station_factory):
    """Ingest closed the assignment without reconciling: rollup stays
    QUARANTINED, the button shows (latest-row fallback) — and the POST must
    release that very row and let the rollup self-heal."""
    st = station_factory()
    rel = _release(fm)
    ModuleFirmwareTarget.objects.create(module_type=fm, version=TARGET)
    m = _module(fm, st, uid="UNASSIGNED")
    cs = _quarantine(m, rel)
    ModuleAssignmentHistory.objects.filter(module=m).update(to_ts=timezone.now())
    client.force_login(staff)
    release_url = reverse("module_firmware:module_release_quarantine", args=["UNASSIGNED"])

    overview = client.get(reverse("module_firmware:convergence"))
    assert overview.context["rows"][0]["quarantine"] == cs
    assert release_url in overview.content.decode()

    resp = client.post(release_url, follow=True)
    assert [msg.level_tag for msg in resp.context["messages"]] == ["success"]
    cs.refresh_from_db()
    assert cs.state == ModuleFirmwareConvergenceState.State.UPDATING
    m.refresh_from_db()
    assert m.firmware_convergence != Module.Convergence.QUARANTINED
    row = StationAuditLog.objects.get(
        event_type=StationAuditLog.EventType.MODULE_QUARANTINE_RELEASED
    )
    assert row.module == m
    assert row.user == staff
    assert row.station is None
