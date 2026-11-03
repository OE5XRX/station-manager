"""Task 10: E2E acceptance for the module FW update state (spec §11).

A fake agent drives the REAL endpoints: the signed HTTP heartbeat (online +
capabilities), the control-channel inventory ingest (``apply_inventory``, the
same call the control consumer makes), and the signed reconcile check / status /
commit API. Each step is verified across every consumer of the state: the
user API (``update_status``), the HTML views (detail page, pill partial), the
drain (``pending_updates``) and the station audit log.
"""

import json
from pathlib import Path

import pytest
from django.urls import reverse

from apps.control.registry import apply_inventory
from apps.module_firmware.drain import pending_updates
from apps.module_firmware.mcuboot import (
    DEV_KEY_HASH_PREFIX,
    KEY_UNKNOWN_HINT,
    NOT_UPDATABLE_DEV_TEXT,
    parse_boot_key_hash,
)
from apps.module_firmware.models import (
    DRAIN_TRANSIENT_LIMIT,
    QUARANTINE_ATTEMPT_LIMIT,
    Module,
    ModuleFirmwareConvergenceState,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
    ModuleType,
)
from apps.stations.models import StationAuditLog
from tests.conftest import device_auth_headers

E = StationAuditLog.EventType
State = ModuleFirmwareConvergenceState.State
TARGET = "26.09.15-01"
OLD = "26.09.10-01"
UID = "E2EUID"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "mcuboot"
DFU = {"module_ota": {"backends": ["dfu"]}}

# Flash-lifecycle events (excludes discovery/assignment noise from the ingest).
LIFECYCLE = (
    E.MODULE_FLASH_INSTRUCTED,
    E.MODULE_FLASH_STARTED,
    E.MODULE_FLASH_PHASE,
    E.MODULE_FLASH_SUCCESS,
    E.MODULE_FLASH_FAILED,
    E.MODULE_FLASH_ROLLED_BACK,
    E.MODULE_FLASH_REJECTED,
    E.MODULE_FLASH_CONFLICT,
    E.MODULE_FLASH_GAVE_UP,
    E.MODULE_FLASH_RETRY,
    E.MODULE_QUARANTINED,
    E.MODULE_NOT_UPDATABLE,
    E.MODULE_UPDATABLE_AGAIN,
)


def _key(name):
    return parse_boot_key_hash((FIXTURES / name).read_bytes())


# ------------------------------------------------------------------ fake agent


class FakeAgent:
    """Signed device requests + inventory reports for one station."""

    def __init__(self, client, station, priv):
        self.client, self.station, self.priv = client, station, priv

    def _post(self, url, payload):
        body = json.dumps(payload).encode()
        return self.client.post(
            url,
            data=body,
            content_type="application/json",
            **device_auth_headers(self.priv, self.station.pk, body),
        )

    def heartbeat(self, capabilities=None):
        payload = {
            "hostname": "station",
            "os_version": "1.0",
            "uptime": 1.0,
            "module_versions": {},
            "ip_address": "10.0.0.2",
            "agent_version": "1.0",
        }
        if capabilities is not None:
            payload["capabilities"] = capabilities
        resp = self._post(reverse("api:heartbeat"), payload)
        assert resp.status_code == 200, resp.content
        self.station.refresh_from_db()
        return resp

    def inventory(self, version, *, boot_key=None, uid=UID):
        identity = {
            "type": "fm",
            "model": "SA818",
            "uid": uid,
            "version": version,
            "variant": "vhf",
        }
        if boot_key is not None:
            identity["boot_key"] = boot_key
        apply_inventory(
            self.station,
            [
                {
                    "slot": "slot0",
                    "modules": [
                        {"module": "fm", "identity": identity, "capabilities": [], "state": {}}
                    ],
                }
            ],
        )

    def check(self):
        return self._post(reverse("module_firmware_api:reconcile_check"), {})

    def status(self, cid, status, error_message=""):
        payload = {"status": status}
        if error_message:
            payload["error_message"] = error_message
        return self._post(reverse("module_firmware_api:reconcile_status", args=[cid]), payload)

    def commit(self, cid, version):
        return self._post(
            reverse("module_firmware_api:reconcile_commit"),
            {"convergence_id": cid, "version": version},
        )


# --------------------------------------------------------------------- fixtures


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


@pytest.fixture
def agent(client, station_with_key):
    station, priv = station_with_key
    return FakeAgent(client, station, priv)


def _target_and_release(fm, boot_key=""):
    ModuleFirmwareTarget.objects.create(
        module_type=fm, scope=ModuleFirmwareTarget.Scope.FLEET, version=TARGET
    )
    return ModuleFirmwareRelease.objects.create(
        module_type=fm,
        variant="vhf",
        version=TARGET,
        storage_key="k",
        sha256="a" * 64,
        size_bytes=42,
        source_repo="r",
        source_tag="t",
        boot_key_hash=boot_key,
    )


def _api_status(bearer, user, module):
    resp = bearer(user).get(reverse("api:module-detail", args=[module.pk]))
    assert resp.status_code == 200, resp.content
    return resp.json()["update_status"]


def _detail_html(client, user, module):
    client.force_login(user)
    resp = client.get(reverse("module_firmware:module_detail", args=[module.uid]))
    client.logout()
    assert resp.status_code == 200
    return resp.content.decode()


def _partial_html(client, user, module):
    client.force_login(user)
    resp = client.get(reverse("module_firmware:module_update_status", args=[module.uid]))
    client.logout()
    assert resp.status_code == 200
    return resp.content.decode()


def _lifecycle(module):
    return list(
        StationAuditLog.objects.filter(module=module, event_type__in=LIFECYCLE)
        .order_by("created_at", "pk")
        .values_list("event_type", flat=True)
    )


def _count(module, event_type):
    return StationAuditLog.objects.filter(module=module, event_type=event_type).count()


def _module():
    return Module.objects.get(uid=UID)


# -------------------------------------------------------------------------- AC1


@pytest.mark.django_db
def test_ac1_old_agent_shows_waiting_not_updating(agent, fm, bearer, client, staff_user):
    """Old agent (no module_ota capability): the module waits, it is not 'updating'."""
    _target_and_release(fm)
    agent.heartbeat()  # old agent: no "capabilities" key at all
    assert agent.station.agent_capabilities == {}
    agent.inventory(OLD)
    m = _module()

    assert agent.check().status_code == 204
    assert _count(m, E.MODULE_FLASH_INSTRUCTED) == 0
    assert pending_updates(agent.station) == []

    text = "Wartet: Station-Agent kann keine Module flashen (Image zu alt)"
    st = _api_status(bearer, staff_user, m)
    assert st["state"] == "waiting"
    assert st["state"] != "running"
    assert st["reason_code"] == "agent_no_module_ota"
    assert st["reason_text"] == text
    assert st["phase"] == ""
    assert text in _detail_html(client, staff_user, m)
    assert text in _partial_html(client, staff_user, m)

    # The agent gets upgraded: the next heartbeat reports the backend -> pending.
    agent.heartbeat(DFU)
    st = _api_status(bearer, staff_user, m)
    assert st["state"] == "pending"
    assert st["reason_text"] == "Update ausstehend"
    assert agent.check().status_code == 200


# -------------------------------------------------------------------------- AC2


@pytest.mark.django_db
def test_ac2_phases_visible_and_audited_once(agent, fm, bearer, client, staff_user):
    _target_and_release(fm)
    agent.heartbeat(DFU)
    agent.inventory(OLD)
    m = _module()
    assert _api_status(bearer, staff_user, m)["state"] == "pending"

    # Repeated check polling hands out the same instruction, audited once.
    cids = set()
    for _ in range(3):
        resp = agent.check()
        assert resp.status_code == 200
        cids.add(resp.json()["convergence_id"])
    assert len(cids) == 1
    (cid,) = cids
    assert _lifecycle(m) == [E.MODULE_FLASH_INSTRUCTED]

    labels = {"downloading": "Download", "flashing": "Flashen", "verifying": "Prüfen"}
    for phase, repeats in (("downloading", 2), ("flashing", 1), ("verifying", 2)):
        for _ in range(repeats):
            assert agent.status(cid, phase).status_code == 200
        st = _api_status(bearer, staff_user, m)
        assert st["state"] == "running"
        assert st["phase"] == phase
        assert st["reason_text"] == f"Update läuft: {labels[phase]}"
        assert st["since"] is not None
        assert f"Update läuft: {labels[phase]}" in _detail_html(client, staff_user, m)
        # Polling during the flash (agent resume path) re-offers, never re-audits.
        resp = agent.check()
        assert resp.status_code == 200
        assert resp.json()["convergence_id"] == cid
        assert _api_status(bearer, staff_user, m)["phase"] == phase

    # Post-flash heartbeat reports the target version, then the commit lands.
    agent.inventory(TARGET)
    assert agent.commit(cid, TARGET).status_code == 200
    st = _api_status(bearer, staff_user, m)
    assert st["state"] == "current"
    assert st["reason_text"] == "Aktuell"
    assert st["phase"] == ""
    assert agent.check().status_code == 204

    assert _lifecycle(m) == [
        E.MODULE_FLASH_INSTRUCTED,
        E.MODULE_FLASH_STARTED,
        E.MODULE_FLASH_PHASE,
        E.MODULE_FLASH_PHASE,
        E.MODULE_FLASH_SUCCESS,
    ]
    phases = list(
        StationAuditLog.objects.filter(module=m, event_type=E.MODULE_FLASH_PHASE)
        .order_by("created_at", "pk")
        .values_list("message", flat=True)
    )
    assert phases == [
        f"Modul {UID}: Phase downloading → flashing.",
        f"Modul {UID}: Phase flashing → verifying.",
    ]
    for entry in StationAuditLog.objects.filter(module=m, event_type__in=LIFECYCLE):
        assert entry.station == agent.station

    # A late progress callback after the commit is a benign 409 (no audit), and a
    # re-sent commit (agent marker recovery) stays an idempotent 200 that never
    # re-opens the attempt (no new INSTRUCTED / STARTED / PHASE).
    assert agent.status(cid, "verifying").status_code == 409
    assert agent.commit(cid, TARGET).status_code == 200
    assert _lifecycle(m)[:5] == [
        E.MODULE_FLASH_INSTRUCTED,
        E.MODULE_FLASH_STARTED,
        E.MODULE_FLASH_PHASE,
        E.MODULE_FLASH_PHASE,
        E.MODULE_FLASH_SUCCESS,
    ]
    assert set(_lifecycle(m)[5:]) <= {E.MODULE_FLASH_SUCCESS}
    assert _api_status(bearer, staff_user, m)["state"] == "current"

    cs = ModuleFirmwareConvergenceState.objects.get(pk=cid)
    assert cs.state == State.OK
    assert (cs.phase, cs.phase_at, cs.instructed_at) == ("", None, None)
    m.refresh_from_db()
    assert m.firmware_convergence == Module.Convergence.OK


# -------------------------------------------------------------------------- AC3


def _drive_to_failed(agent, message="dfu timeout"):
    cid = None
    for _ in range(DRAIN_TRANSIENT_LIMIT):
        resp = agent.check()
        assert resp.status_code == 200
        cid = resp.json()["convergence_id"]
        assert agent.status(cid, "failed", message).status_code == 200
    return cid


@pytest.mark.django_db
def test_ac3_five_transients_failed_then_retry(agent, fm, bearer, client, staff_user):
    _target_and_release(fm)
    agent.heartbeat(DFU)
    agent.inventory(OLD)
    m = _module()

    cid = _drive_to_failed(agent)

    st = _api_status(bearer, staff_user, m)
    assert st["state"] == "failed"
    assert st["reason_code"] == "transient_limit"
    assert st["reason_text"] == "Fehler: dfu timeout"
    assert pending_updates(agent.station) == []
    assert _count(m, E.MODULE_FLASH_GAVE_UP) == 1
    assert _count(m, E.MODULE_FLASH_FAILED) == DRAIN_TRANSIENT_LIMIT
    assert _count(m, E.MODULE_FLASH_INSTRUCTED) == DRAIN_TRANSIENT_LIMIT
    gave_up = StationAuditLog.objects.get(module=m, event_type=E.MODULE_FLASH_GAVE_UP)
    assert "Letzte Meldung: dfu timeout" in gave_up.message
    assert gave_up.station == agent.station
    m.refresh_from_db()
    assert m.firmware_convergence == Module.Convergence.FAILED

    # Given up: further heartbeats/checks do nothing, a late callback is a 409.
    agent.inventory(OLD)
    assert agent.check().status_code == 204
    assert agent.status(cid, "flashing").status_code == 409
    assert _count(m, E.MODULE_FLASH_CONFLICT) == 1
    assert _count(m, E.MODULE_FLASH_GAVE_UP) == 1
    assert _api_status(bearer, staff_user, m)["state"] == "failed"

    detail = _detail_html(client, staff_user, m)
    assert "Fehler: dfu timeout" in detail
    assert "Erneut versuchen" in detail

    # Staff retries via the GUI.
    client.force_login(staff_user)
    resp = client.post(reverse("module_firmware:module_retry_update", args=[m.uid]))
    client.logout()
    assert resp.status_code == 302
    assert _count(m, E.MODULE_FLASH_RETRY) == 1
    retry = StationAuditLog.objects.get(module=m, event_type=E.MODULE_FLASH_RETRY)
    assert retry.user == staff_user
    assert retry.station == agent.station

    st = _api_status(bearer, staff_user, m)
    assert st["state"] == "pending"
    assert st["reason_text"] == "Update ausstehend"
    assert [cs.pk for cs in pending_updates(agent.station)] == [cid]

    resp = agent.check()
    assert resp.status_code == 200
    assert resp.json()["convergence_id"] == cid
    assert _count(m, E.MODULE_FLASH_INSTRUCTED) == DRAIN_TRANSIENT_LIMIT + 1

    # One more transient after the retry is not an immediate give-up (fresh streak).
    assert agent.status(cid, "failed", "dfu timeout").status_code == 200
    assert _api_status(bearer, staff_user, m)["state"] == "pending"
    assert _count(m, E.MODULE_FLASH_GAVE_UP) == 1


# -------------------------------------------------------------------------- AC4


def _dev_key_module_against_prod_release(agent, fm, beats=20):
    prod = _key("other-signed.bin")
    assert prod and prod != DEV_KEY_HASH_PREFIX
    assert _key("dev-signed.bin") == DEV_KEY_HASH_PREFIX
    _target_and_release(fm, boot_key=prod)
    agent.heartbeat(DFU)
    for _ in range(beats):
        agent.inventory(OLD, boot_key=DEV_KEY_HASH_PREFIX.upper())  # normalized on ingest
        assert agent.check().status_code == 204
    return prod


@pytest.mark.django_db
def test_ac4_dev_key_vs_prod_release(agent, fm, bearer, client, staff_user):
    _dev_key_module_against_prod_release(agent, fm)
    m = _module()
    assert m.reported_boot_key == DEV_KEY_HASH_PREFIX

    assert pending_updates(agent.station) == []
    st = _api_status(bearer, staff_user, m)
    assert st["state"] == "not_updatable"
    assert st["reason_code"] == "boot_key_mismatch"
    assert st["reason_text"] == NOT_UPDATABLE_DEV_TEXT
    assert _count(m, E.MODULE_NOT_UPDATABLE) == 1
    assert _count(m, E.MODULE_FLASH_INSTRUCTED) == 0
    (entry,) = StationAuditLog.objects.filter(module=m, event_type=E.MODULE_NOT_UPDATABLE)
    assert entry.message == NOT_UPDATABLE_DEV_TEXT
    assert entry.station == agent.station
    m.refresh_from_db()
    assert m.firmware_convergence == Module.Convergence.NOT_UPDATABLE

    resp = bearer(staff_user).get(reverse("api:module-detail", args=[m.pk]))
    assert resp.json()["reported_boot_key"] == DEV_KEY_HASH_PREFIX

    detail = _detail_html(client, staff_user, m)
    assert NOT_UPDATABLE_DEV_TEXT in detail
    assert DEV_KEY_HASH_PREFIX in detail
    assert "Dev-Key" in detail
    assert "Erneut versuchen" not in detail  # retry is for failed only


@pytest.mark.django_db
def test_ac4b_bench_reflash_makes_updatable_again(agent, fm, bearer, staff_user):
    prod = _dev_key_module_against_prod_release(agent, fm, beats=3)
    m = _module()

    # Bootloader reflashed with the prod key at the bench: next inventory reports it.
    agent.inventory(OLD, boot_key=prod)
    st = _api_status(bearer, staff_user, m)
    assert st["state"] == "pending"
    assert st["reason_text"] == "Update ausstehend"
    assert len(pending_updates(agent.station)) == 1
    assert _count(m, E.MODULE_UPDATABLE_AGAIN) == 1
    assert _count(m, E.MODULE_NOT_UPDATABLE) == 1

    resp = agent.check()
    assert resp.status_code == 200
    cs = ModuleFirmwareConvergenceState.objects.get(pk=resp.json()["convergence_id"])
    assert cs.state == State.UPDATING
    assert (cs.attempts, cs.transient_failures, cs.reason_code) == (0, 0, "")

    # Further heartbeats with the matching key do not re-audit.
    agent.inventory(OLD, boot_key=prod)
    assert _count(m, E.MODULE_UPDATABLE_AGAIN) == 1


# -------------------------------------------------------------------------- AC5


@pytest.mark.django_db
def test_ac5_no_boot_key_legacy_path_with_hint(agent, fm, bearer, client, staff_user):
    """Old module FW (no boot_key in identity): unchanged legacy behaviour — the
    flash is attempted; rolled_back N× quarantines; the text carries the key hint."""
    _target_and_release(fm, boot_key=_key("other-signed.bin"))
    agent.heartbeat(DFU)
    agent.inventory(OLD)  # no boot_key
    m = _module()
    assert m.reported_boot_key == ""
    assert _api_status(bearer, staff_user, m)["state"] == "pending"

    for _ in range(QUARANTINE_ATTEMPT_LIMIT):
        resp = agent.check()
        assert resp.status_code == 200
        cid = resp.json()["convergence_id"]
        assert agent.status(cid, "downloading").status_code == 200
        assert agent.status(cid, "rolled_back").status_code == 200

    cs = ModuleFirmwareConvergenceState.objects.get(pk=cid)
    assert cs.state == State.QUARANTINED
    assert cs.attempts == QUARANTINE_ATTEMPT_LIMIT
    m.refresh_from_db()
    assert m.firmware_convergence == Module.Convergence.QUARANTINED
    assert agent.check().status_code == 204
    assert pending_updates(agent.station) == []

    (q,) = StationAuditLog.objects.filter(module=m, event_type=E.MODULE_QUARANTINED)
    assert KEY_UNKNOWN_HINT in q.message
    rolled = StationAuditLog.objects.filter(module=m, event_type=E.MODULE_FLASH_ROLLED_BACK)
    assert rolled.count() == QUARANTINE_ATTEMPT_LIMIT
    assert all(KEY_UNKNOWN_HINT in e.message for e in rolled)
    assert _count(m, E.MODULE_NOT_UPDATABLE) == 0

    st = _api_status(bearer, staff_user, m)
    assert st["state"] == "quarantined"
    assert st["reason_code"] == "rolled_back"
    assert KEY_UNKNOWN_HINT in st["reason_text"]
    assert st["reason_text"].startswith(f"Quarantäne (Update {QUARANTINE_ATTEMPT_LIMIT}×")
    assert KEY_UNKNOWN_HINT in _detail_html(client, staff_user, m)


# -------------------------------------------------------------------------- AC7


@pytest.mark.django_db
def test_ac7_visibility_unchanged(agent, fm, bearer, client, anon_client, applicant_user):
    _target_and_release(fm)
    agent.heartbeat(DFU)
    agent.inventory(OLD)
    m = _module()
    _drive_to_failed(agent)

    # Anonymous: redirected to login everywhere, no state leaks.
    for url in (
        reverse("module_firmware:module_list"),
        reverse("module_firmware:module_detail", args=[m.uid]),
        reverse("module_firmware:module_update_status", args=[m.uid]),
    ):
        resp = client.get(url)
        assert resp.status_code == 302, url
        assert "dfu timeout" not in resp.content.decode()
    assert anon_client.get(reverse("api:module-detail", args=[m.pk])).status_code in (401, 403)

    # Applicant: sees the state (HTML + API) but gets no retry button.
    detail = _detail_html(client, applicant_user, m)
    assert "Fehler: dfu timeout" in detail
    assert "Erneut versuchen" not in detail
    assert reverse("module_firmware:module_retry_update", args=[m.uid]) not in detail
    assert "Fehler: dfu timeout" in _partial_html(client, applicant_user, m)
    client.force_login(applicant_user)
    listing = client.get(reverse("module_firmware:module_list"))
    assert listing.status_code == 200
    assert m.uid in listing.content.decode()

    # ...and cannot retry (GUI or API): no state change, no audit.
    resp = client.post(reverse("module_firmware:module_retry_update", args=[m.uid]))
    assert resp.status_code == 403
    client.logout()
    # Applicant tokens are not accepted by the API at all (401), same as before.
    resp = bearer(applicant_user).post(reverse("api:module-retry-update", args=[m.pk]))
    assert resp.status_code in (401, 403)
    assert _count(m, E.MODULE_FLASH_RETRY) == 0
    assert ModuleFirmwareConvergenceState.objects.get(module=m).state == State.FAILED
