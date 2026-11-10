import re
from datetime import timedelta
from pathlib import Path

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.control.models import ControlLock
from apps.passive.models import (
    AprsPacket,
    AprsSettings,
    HandoverProfile,
    PassiveServiceConfig,
    PassiveServiceRun,
    StationPassiveConfig,
)


def _login(client, user):
    client.force_login(user)
    return client


def _packet(station, seq, **kw):
    now = timezone.now()
    defaults = dict(
        epoch="e1",
        seq=seq,
        heard_at=now,
        received_at=now,
        raw_tnc2="OE5ABC-9>APRS:>hi",
        source="OE5ABC-9",
        destination="APRS",
        packet_type="status",
    )
    defaults.update(kw)
    return AprsPacket.objects.create(station=station, **defaults)


def _settings_post(**over):
    data = {
        "cfg-enabled": "on",
        "cfg-idle_minutes": "10",
        "cfg-frequency": "144.8",
        "hov-frequency": "145.5",
        "aprs-aprsis_host": "rotate.aprs2.net",
        "aprs-aprsis_port": "14580",
        "aprs-aprsis_filter": "",
        "aprs-max_gate_age_s": "30",
        "aprs-retention_days": "30",
        "aprs-no_rx_alert_hours": "6",
        "aprs-beacon_interval_min": "30",
        "aprs-beacon_comment": "OE5XRX RX-iGate",
    }
    data.update(over)
    return data


@pytest.mark.django_db
def test_applicant_sees_page_without_settings_tab(client, applicant_user):
    r = _login(client, applicant_user).get(reverse("passive:aprs"))
    assert r.status_code == 200
    body = r.content.decode()
    assert 'data-tab="karte"' in body and 'data-tab="igates"' in body
    assert 'data-tab="einstellungen"' not in body


@pytest.mark.django_db
def test_admin_sees_settings_tab_and_can_save(client, admin_user):
    _login(client, admin_user)
    r = client.get(reverse("passive:aprs"))
    body = r.content.decode()
    assert 'data-tab="einstellungen"' in body
    # every numeric input forces dot-decimal
    for tag in re.findall(r'<input[^>]*type="number"[^>]*>', body):
        assert 'lang="en"' in tag, tag
    r = client.post(
        reverse("passive:aprs_settings"), _settings_post(**{"aprs-retention_days": "14"})
    )
    assert r.status_code == 302
    assert AprsSettings.get().retention_days == 14
    assert PassiveServiceConfig.get("aprs").enabled is True
    assert HandoverProfile.get().values == {"frequency": 145.5}


@pytest.mark.django_db
def test_invalid_settings_rerender_and_save_nothing(client, admin_user):
    _login(client, admin_user)
    r = client.post(
        reverse("passive:aprs_settings"), _settings_post(**{"aprs-beacon_interval_min": "5"})
    )
    assert r.status_code == 200
    assert 'data-initial-tab="einstellungen"' in r.content.decode()
    assert PassiveServiceConfig.get("aprs").enabled is False


@pytest.mark.django_db
def test_staff_cannot_post_settings(client, staff_user):
    r = _login(client, staff_user).post(reverse("passive:aprs_settings"), _settings_post())
    assert r.status_code == 403
    assert PassiveServiceConfig.get("aprs").enabled is False


@pytest.mark.django_db
def test_settings_profile_blank_fields_omitted(client, admin_user):
    _login(client, admin_user)
    client.post(reverse("passive:aprs_settings"), _settings_post())
    assert PassiveServiceConfig.get("aprs").profile == {"frequency": 144.8}


@pytest.mark.django_db
def test_settings_bad_tone_rejected(client, admin_user):
    _login(client, admin_user)
    r = client.post(reverse("passive:aprs_settings"), _settings_post(**{"cfg-rx_tone": "999"}))
    assert r.status_code == 200
    assert PassiveServiceConfig.get("aprs").enabled is False


@pytest.mark.django_db
def test_packets_api_after_id_and_station_filter(client, applicant_user, station_factory):
    s1, s2 = station_factory(), station_factory()
    p1 = _packet(s1, 1, latitude="48.2", longitude="14.3")
    p2 = _packet(s2, 2)
    _packet(s1, 3, heard_at=timezone.now() - timedelta(hours=30))
    _login(client, applicant_user)
    url = reverse("passive:aprs_packets")
    data = client.get(url).json()
    assert [p["id"] for p in data["packets"]] == [p2.id, p1.id]  # newest first, 30 h old excluded
    assert data["last_id"] == p2.id
    assert data["packets"][1]["latitude"] == 48.2 and data["packets"][1]["gated"] is False
    assert [p["id"] for p in client.get(url, {"after_id": p1.id}).json()["packets"]] == [p2.id]
    assert [p["id"] for p in client.get(url, {"station": s1.id}).json()["packets"]] == [p1.id]
    empty = client.get(url, {"after_id": p2.id}).json()
    assert empty == {"packets": [], "last_id": p2.id}


@pytest.mark.django_db
def test_packets_api_requires_login(client):
    assert client.get(reverse("passive:aprs_packets")).status_code == 302


@pytest.mark.django_db
def test_igates_api_states(client, applicant_user, station_factory, admin_user):
    now = timezone.now()
    fresh = {"last_seen": now}
    off = station_factory(**fresh)  # not opted in
    gone = station_factory()  # opted in, offline
    run = station_factory(**fresh, latitude="48.1", longitude="14.1")
    busy = station_factory(**fresh, latitude="48.1", longitude="14.1")
    err = station_factory(**fresh, latitude="48.1", longitude="14.1")
    idle = station_factory(**fresh)  # no coordinates, no call
    for st, call in (
        (gone, "OE5AAA"),
        (run, "OE5BBB"),
        (busy, "OE5CCC"),
        (err, "OE5DDD"),
        (idle, ""),
    ):
        StationPassiveConfig.objects.create(
            station=st, enabled=True, igate_callsign=call, igate_ssid=11 if call else None
        )
    PassiveServiceRun.objects.create(station=run, service="aprs", slot="1", state="running")
    PassiveServiceRun.objects.create(
        station=err, service="aprs", slot="1", state="error", last_error="sa818 timeout"
    )
    ControlLock.objects.create(station=busy, holder=admin_user, acquired_at=now)
    _packet(run, 1)
    rows = {
        r["station_id"]: r
        for r in _login(client, applicant_user).get(reverse("passive:aprs_igates")).json()
    }
    assert rows[off.id]["state"] == "not_opted_in"
    assert rows[gone.id]["state"] == "agent_offline"
    assert rows[run.id]["state"] == "running" and rows[run.id]["call"] == "OE5BBB-11"
    assert rows[run.id]["packets_24h"] == 1 and rows[run.id]["last_packet_at"]
    assert rows[run.id]["warning"] is None
    assert rows[busy.id]["state"] == "in_use"
    assert rows[err.id]["state"] == "error" and rows[err.id]["reason"] == "sa818 timeout"
    assert rows[idle.id]["state"] == "idle" and rows[idle.id]["warning"] == "no_igate_call"
    assert rows[off.id]["warning"] is None


@pytest.mark.django_db
def test_igate_warning_no_coordinates(client, applicant_user, station_factory):
    st = station_factory(last_seen=timezone.now())
    StationPassiveConfig.objects.create(
        station=st, enabled=True, igate_callsign="OE5AAA", igate_ssid=1
    )
    rows = _login(client, applicant_user).get(reverse("passive:aprs_igates")).json()
    assert rows[0]["warning"] == "no_coordinates"


def test_template_no_multiline_hash_comments():
    tpl = Path(__file__).resolve().parent.parent / "apps/passive/templates/passive/aprs.html"
    assert not re.search(r"\{#(?:(?!#\}).)*\n", tpl.read_text(), re.S)


@pytest.mark.django_db
def test_packets_api_cap_initial_newest_and_after_id_no_gap(
    client, applicant_user, station, monkeypatch
):
    monkeypatch.setattr("apps.passive.views.MAX_PACKETS", 3)
    ids = [_packet(station, i).id for i in range(1, 8)]
    _login(client, applicant_user)
    url = reverse("passive:aprs_packets")
    first = client.get(url).json()
    assert [p["id"] for p in first["packets"]] == ids[:-4:-1]  # newest 3
    assert first["last_id"] == ids[-1]
    # a burst bigger than the cap is consumed oldest-first: last_id never skips rows
    page = client.get(url, {"after_id": ids[0]}).json()
    assert [p["id"] for p in page["packets"]] == [ids[3], ids[2], ids[1]]
    assert page["last_id"] == ids[3]
    nxt = client.get(url, {"after_id": page["last_id"]}).json()
    assert [p["id"] for p in nxt["packets"]] == [ids[6], ids[5], ids[4]]


@pytest.mark.django_db
def test_packets_api_initial_includes_latest_position_per_source(
    client, applicant_user, station, monkeypatch
):
    monkeypatch.setattr("apps.passive.views.MAX_PACKETS", 1)
    _packet(station, 1, source="OE5POS-1", latitude="48.1", longitude="14.1")
    _packet(station, 2, source="OE5POS-1", latitude="48.2", longitude="14.2")
    _packet(station, 3, source="OE5NOP-1")
    data = _login(client, applicant_user).get(reverse("passive:aprs_packets")).json()
    assert len(data["packets"]) == 1
    assert [(p["source"], p["latitude"]) for p in data["positions"]] == [("OE5POS-1", 48.2)]
    assert "positions" not in client.get(reverse("passive:aprs_packets"), {"after_id": 1}).json()


@pytest.mark.django_db
def test_packets_api_hours_clamped_and_garbage_params_ignored(client, applicant_user, station):
    old = _packet(station, 1, heard_at=timezone.now() - timedelta(days=40))
    _login(client, applicant_user)
    url = reverse("passive:aprs_packets")
    assert client.get(url, {"hours": "99999"}).json()["packets"] == []  # clamped to 30 d
    assert client.get(url, {"hours": "x", "after_id": "y", "station": "z"}).json()["packets"] == []
    assert old.id  # row exists, only outside the window


@pytest.mark.django_db
def test_packets_api_untrusted_payload_round_trips_as_data(client, applicant_user, station):
    evil = "<img src=x onerror=alert(1)>"
    _packet(station, 1, comment=evil, raw_tnc2=f"X>APRS:>{evil}")
    data = _login(client, applicant_user).get(reverse("passive:aprs_packets")).json()
    assert data["packets"][0]["comment"] == evil
    assert data["packets"][0]["raw_tnc2"].endswith(evil)


def test_aprs_map_js_never_uses_innerhtml():
    src = (Path(__file__).resolve().parent.parent / "static/js/aprs-map.js").read_text()
    assert "innerHTML" not in src and "insertAdjacentHTML" not in src
