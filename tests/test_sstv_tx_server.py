"""SSTV transmit on the server (SSTV plan Task 10): preview endpoint, send guards at send
time, synthesis, agent download, ``sstv_tx_state``/``abort_tx`` (Review Focus 2–4)."""

import asyncio
import hashlib
import io
from datetime import timedelta

import pytest
from channels.db import database_sync_to_async
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from apps.accounts.models import User
from apps.control import lock, lock_hooks
from apps.control.models import AgentConnection, ControlLock, StationModule
from apps.sstv import control as sstv_control
from apps.sstv import tx
from apps.sstv.models import SstvImage, SstvTransmission
from apps.stations.models import Station
from station_agent.sstv.modes import MODES, duration_s
from tests.conftest import _make_ed25519_keypair, device_auth_headers
from tests.test_sstv_rx_server import (
    _agent,
    _browser,
    _code,
    advertise,
    connect,
    drain,
    is_decoder,
    is_state,
    setup_station,
    started,
    until,
)

pytestmark = pytest.mark.django_db(transaction=True)
TxState = SstvTransmission.State


def _png_upload(size=(800, 600), colour=(200, 30, 30), fmt="PNG", name="photo.png"):
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, format=fmt)
    buf.seek(0)
    buf.name = name
    return buf


def _hold(station, user):
    assert lock.acquire(station, user)


def _advertise_sync(station):
    AgentConnection.objects.update_or_create(
        station=station, kind="control", defaults={"channel_name": "x", "features": ["sstv"]}
    )


def _draft(station, user, mode="robot36", callsigns=("OE5ABC",), age_s=0, slot=1):
    m = MODES[mode]
    buf = io.BytesIO()
    Image.new("RGB", (m.width, m.height), (10, 200, 10)).save(buf, format="PNG")
    t = SstvTransmission(
        station=station,
        slot=slot,
        module="fm0",
        user=user,
        mode=mode,
        duration_s=duration_s(m),
        callsigns=list(callsigns),
    )
    t.preview.save("p.png", ContentFile(buf.getvalue()), save=False)
    t.save()
    if age_s:
        then = timezone.now() - timedelta(seconds=age_s)
        SstvTransmission.objects.filter(pk=t.pk).update(created_at=then)
    return t


def _set_tx(station, mhz):
    StationModule.objects.filter(station=station, module_id="fm0").update(
        last_state={"rx_frequency": mhz, "tx_frequency": mhz}
    )


# -- preview -----------------------------------------------------------------------


@pytest.mark.parametrize("mode", sorted(MODES))
def test_test_picture_preview_has_exact_mode_size(client, mode):
    station, user = setup_station(f"pv-{mode}")
    _hold(station, user)
    client.force_login(user)
    resp = client.post(
        reverse("sstv:preview", args=[station.pk]),
        {"source": "test", "mode": mode, "cs_user": "1"},
    )
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["warnings"] == [] and body["mode"] == mode
    assert body["duration_s"] == round(tx.air_time_s(MODES[mode]), 1)
    png = client.get(body["preview_url"])
    assert png.status_code == 200 and png["Content-Type"] == "image/png"
    img = Image.open(io.BytesIO(b"".join(png.streaming_content)))
    assert img.size == (MODES[mode].width, MODES[mode].height)
    draft = SstvTransmission.objects.get(pk=body["id"])
    assert (draft.state, draft.slot, draft.module) == (TxState.DRAFT, 1, "fm0")
    assert draft.callsigns == [user.username]


def test_upload_preview_is_filled_and_drafts_are_replaced(client):
    station, user = setup_station("pv-up")
    _hold(station, user)
    client.force_login(user)
    url = reverse("sstv:preview", args=[station.pk])
    first = client.post(url, {"source": "upload", "mode": "pd120", "file": _png_upload()})
    assert first.status_code == 200, first.content
    second = client.post(
        url, {"source": "upload", "mode": "martin1", "file": _png_upload(size=(1, 4000))}
    )
    assert second.status_code == 200
    assert second.json()["warnings"] == ["no_callsign"]
    (draft,) = SstvTransmission.objects.filter(station=station)
    assert str(draft.pk) == second.json()["id"]
    with draft.preview.open("rb") as f:
        assert Image.open(f).size == (320, 256)


def test_station_callsign_only_when_set(client):
    station, user = setup_station("pv-cs")
    _hold(station, user)
    client.force_login(user)
    url = reverse("sstv:preview", args=[station.pk])
    resp = client.post(url, {"source": "test", "mode": "robot36", "cs_station": "1"})
    assert resp.json()["warnings"] == ["no_callsign"]  # station has no callsign
    Station.objects.filter(pk=station.pk).update(callsign="OE5XRX")
    resp = client.post(url, {"source": "test", "mode": "robot36", "cs_station": "on"})
    assert resp.json()["warnings"] == []
    assert SstvTransmission.objects.get().callsigns == ["OE5XRX"]


@pytest.mark.parametrize(
    "data, code",
    [
        ({"source": "test", "mode": "fax"}, "bad_mode"),
        ({"source": "paint", "mode": "robot36"}, "bad_source"),
        ({"source": "upload", "mode": "robot36"}, "no_file"),
    ],
)
def test_preview_rejects_bad_requests(client, data, code):
    station, user = setup_station(f"pv-bad-{code}")
    _hold(station, user)
    client.force_login(user)
    resp = client.post(reverse("sstv:preview", args=[station.pk]), data)
    assert resp.status_code == 400 and resp.json()["error"] == code
    assert resp.json()["message"]


def test_preview_rejects_non_images(client):
    station, user = setup_station("pv-junk")
    _hold(station, user)
    client.force_login(user)
    junk = io.BytesIO(b"definitely not a picture")
    junk.name = "x.png"
    resp = client.post(
        reverse("sstv:preview", args=[station.pk]),
        {"source": "upload", "mode": "robot36", "file": junk},
    )
    assert resp.status_code == 400 and resp.json()["error"] == "unsupported"
    assert not SstvTransmission.objects.exists()


def test_preview_needs_the_lock(client):
    station, user = setup_station("pv-nolock")
    client.force_login(user)
    resp = client.post(
        reverse("sstv:preview", args=[station.pk]), {"source": "test", "mode": "robot36"}
    )
    assert resp.status_code == 403 and resp.json()["error"] == "not_locked"


def test_preview_needs_sstv_use(client):
    station, user = setup_station("pv-appl", level=User.MembershipLevel.APPLICANT)
    client.force_login(user)
    resp = client.post(
        reverse("sstv:preview", args=[station.pk]), {"source": "test", "mode": "robot36"}
    )
    assert resp.status_code == 403


def test_preview_needs_a_transmitting_module(client):
    station, user = setup_station("pv-nomod")
    StationModule.objects.filter(station=station).update(online=False)
    _hold(station, user)
    client.force_login(user)
    resp = client.post(
        reverse("sstv:preview", args=[station.pk]), {"source": "test", "mode": "robot36"}
    )
    assert resp.status_code == 400 and resp.json()["error"] == "no_module"


def test_preview_image_owner_only(client):
    station, user = setup_station("pv-owner")
    other = User.objects.create(username="pv-other", membership_level="member")
    draft = _draft(station, user)
    url = reverse("sstv:preview_image", args=[station.pk, draft.pk])
    client.force_login(other)
    assert client.get(url).status_code == 404
    client.force_login(user)
    assert client.get(url).status_code == 200


# -- send guards (sync) ------------------------------------------------------------


@pytest.fixture
def ready():
    station, user = setup_station("guard")
    _advertise_sync(station)
    _hold(station, user)
    return station, user


def _claim(station, user, draft, confirm=False):
    return tx.claim(station, user, str(draft.pk), confirm_no_callsign=confirm)[0]


def test_guard_happy_path_claims_the_draft(ready):
    station, user = ready
    draft = _draft(station, user)
    code, claimed = tx.claim(station, user, str(draft.pk), confirm_no_callsign=False)
    assert code is None and claimed.pk == draft.pk
    draft.refresh_from_db()
    assert (draft.state, draft.frequency_hz) == (TxState.SENDING, 145_500_000)


def test_guard_not_locked(ready):
    station, user = ready
    draft = _draft(station, user)
    lock.release(station, user)
    assert _claim(station, user, draft) == "not_locked"


def test_guard_unsupported(ready):
    station, user = ready
    draft = _draft(station, user)
    AgentConnection.objects.filter(station=station).update(features=["decoders"])
    assert _claim(station, user, draft) == "unsupported"


def test_guard_foreign_or_unknown_draft(ready):
    station, user = ready
    other = User.objects.create(username="g-other", membership_level="member")
    assert _claim(station, user, _draft(station, other)) == "not_found"
    assert tx.claim(station, user, "nope", confirm_no_callsign=False)[0] == "not_found"


def test_guard_expired(ready):
    station, user = ready
    assert _claim(station, user, _draft(station, user, age_s=3601)) == "expired"


def test_guard_no_callsign_needs_confirmation(ready):
    station, user = ready
    draft = _draft(station, user, callsigns=())
    assert _claim(station, user, draft) == "no_callsign"
    assert _claim(station, user, draft, confirm=True) is None


@pytest.mark.parametrize(
    "mhz, code",
    [
        (145.800, "iss_downlink"),
        (145.8099, "iss_downlink"),
        (145.7901, "iss_downlink"),
        (145.811, None),
        (146.5, "out_of_range"),
        (None, "no_frequency"),
    ],
)
def test_guard_frequency(ready, mhz, code):
    station, user = ready
    _set_tx(station, mhz)
    assert _claim(station, user, _draft(station, user)) == code


def test_guard_module_offline(ready):
    station, user = ready
    draft = _draft(station, user)
    StationModule.objects.filter(station=station).update(online=False)
    assert _claim(station, user, draft) == "module_offline"


def test_guard_without_ranges_refuses(ready):
    station, user = ready
    StationModule.objects.filter(station=station).update(
        capability_descriptor=[{"name": "tx_frequency", "kind": "setting", "type": "float"}]
    )
    assert _claim(station, user, _draft(station, user)) == "out_of_range"


def test_guard_busy_while_another_transmission_is_sending(ready):
    station, user = ready
    sending = _draft(station, user)
    SstvTransmission.objects.filter(pk=sending.pk).update(state=TxState.SENDING)
    assert _claim(station, user, _draft(station, user)) == "sstv_busy"
    assert _claim(station, user, sending) == "sstv_busy"  # the same one, twice


# -- WS send flow ------------------------------------------------------------------


def send(draft, rid="t1", confirm=False):
    return {
        "type": "sstv_tx_start",
        "request_id": rid,
        "transmission_id": str(draft.pk),
        "confirm_no_callsign": confirm,
    }


def is_tx_state(state):
    return lambda m: m.get("type") == "sstv_tx_state" and m.get("state") == state


async def sent_start(agent, browser, draft):
    await browser.send_json_to(send(draft))
    return await until(agent, lambda m: m.get("type") == "sstv_tx_start")


def test_send_hands_the_audio_to_the_agent(control_agent_auth):
    station, user = setup_station("tx-send")

    async def go():
        agent, browser = await connect(station, user)
        draft = await database_sync_to_async(_draft)(station, user)
        frame = await sent_start(agent, browser, draft)
        await until(browser, is_tx_state("sending"))
        await agent.disconnect()
        await browser.disconnect()
        return draft, frame

    draft, frame = asyncio.run(go())
    assert frame["tx_id"] == draft.pk.hex and frame["request_id"].startswith("sstv-tx-")
    assert (frame["slot"], frame["module"]) == (1, "fm0")
    assert frame["url"] == reverse("sstv_api:tx_audio", args=[draft.pk])
    assert frame["duration_s"] == pytest.approx(590_560 / 16000)
    draft.refresh_from_db()
    # The agent disconnect afterwards aborted it (Review Focus 4) and dropped the audio.
    assert draft.state == TxState.ABORTED and not draft.audio
    assert frame["size"] == 590_560 and len(frame["sha256"]) == 64


def test_frequency_changed_after_preview_is_refused_at_send(control_agent_auth):
    # Review Focus 2: preview on 145.500, then the user presses "ISS" -> 145.800.
    station, user = setup_station("tx-iss")

    async def go():
        agent, browser = await connect(station, user)
        draft = await database_sync_to_async(_draft)(station, user)
        await agent.send_json_to(
            {"type": "state", "slot": "1", "module": "fm0",
             "values": {"tx_frequency": 145.8, "rx_frequency": 145.8}}
        )  # fmt: skip
        await until(browser, lambda m: m.get("type") == "state")
        await browser.send_json_to(send(draft))
        err = await until(browser, lambda m: m.get("type") == "error")
        assert _code(err) == "iss_downlink" and err["request_id"] == "t1"
        assert not any(m.get("type") == "sstv_tx_start" for m in await drain(agent))
        await browser.disconnect()
        await agent.disconnect()
        return draft

    draft = asyncio.run(go())
    draft.refresh_from_db()
    assert draft.state == TxState.DRAFT  # nothing was claimed


def test_double_send_from_two_tabs_starts_exactly_one(control_agent_auth):
    # Review Focus 3: two tabs of the same holder (or a double click).
    station, user = setup_station("tx-double")

    async def go():
        agent, tab1 = await connect(station, user)
        tab2 = _browser(user, station.id)
        await tab2.connect()
        draft = await database_sync_to_async(_draft)(station, user)
        await tab1.send_json_to(send(draft, rid="a"))
        await tab2.send_json_to(send(draft, rid="b"))
        await tab1.send_json_to(send(draft, rid="c"))
        out = await drain(agent, timeout=1.0)
        errors = [m for t in (tab1, tab2) for m in await drain(t) if m.get("type") == "error"]
        for comm in (tab1, tab2, agent):
            await comm.disconnect()
        return out, errors

    out, errors = asyncio.run(go())
    assert len([m for m in out if m.get("type") == "sstv_tx_start"]) == 1
    assert sorted(_code(e) for e in errors) == ["sstv_busy", "sstv_busy"]


def test_done_moves_the_preview_into_the_gallery(control_agent_auth):
    station, user = setup_station("tx-done")

    async def go():
        agent, browser = await connect(station, user)
        draft = await database_sync_to_async(_draft)(station, user)
        frame = await sent_start(agent, browser, draft)
        stored = await database_sync_to_async(SstvTransmission.objects.get)(pk=draft.pk)
        audio_name = stored.audio.name
        await agent.send_json_to(
            {"type": "result", "request_id": frame["request_id"], "ok": True,
             "value": {"tx_id": frame["tx_id"]}}
        )  # fmt: skip
        for state, progress in (("downloading", 0.0), ("keyed", 0.5)):
            await agent.send_json_to(
                {"type": "sstv_tx_state", "tx_id": frame["tx_id"], "state": state,
                 "progress": progress, "detail": None}
            )  # fmt: skip
            got = await until(browser, is_tx_state(state))
            assert got["progress"] == progress and got["id"] == str(draft.pk)
        await agent.send_json_to(
            {"type": "sstv_tx_state", "tx_id": frame["tx_id"], "state": "done", "progress": 1}
        )
        await until(browser, is_tx_state("done"))
        shown = await until(browser, lambda m: m.get("type") == "sstv_image")
        await browser.disconnect()
        await agent.disconnect()
        return draft, audio_name, shown

    draft, audio_name, shown = asyncio.run(go())
    image = SstvImage.objects.get(station=station)
    assert (image.direction, image.callsign, image.user_id) == ("tx", user.username, user.pk)
    assert (image.mode, image.frequency_hz, image.complete) == ("robot36", 145_500_000, True)
    assert shown["id"] == str(image.pk) and shown["direction"] == "tx"
    with image.image.open("rb") as f:
        assert Image.open(f).size == (320, 240)
    assert not SstvTransmission.objects.filter(pk=draft.pk).exists()
    assert not default_storage.exists(audio_name)


@pytest.mark.parametrize("state", ["aborted", "error"])
def test_agent_abort_or_error_keeps_nothing(control_agent_auth, state):
    station, user = setup_station(f"tx-{state}")

    async def go():
        agent, browser = await connect(station, user)
        draft = await database_sync_to_async(_draft)(station, user)
        frame = await sent_start(agent, browser, draft)
        await agent.send_json_to(
            {"type": "sstv_tx_state", "tx_id": frame["tx_id"], "state": state,
             "progress": 0.3, "detail": "player_failed"}
        )  # fmt: skip
        got = await until(browser, is_tx_state(state))
        assert got["detail"] == "player_failed"
        await browser.disconnect()
        await agent.disconnect()
        return draft

    draft = asyncio.run(go())
    draft.refresh_from_db()
    assert draft.state == state and not draft.audio
    assert not SstvImage.objects.exists()


def test_agent_refusal_ends_the_transmission(control_agent_auth):
    station, user = setup_station("tx-refused")

    async def go():
        agent, browser = await connect(station, user)
        draft = await database_sync_to_async(_draft)(station, user)
        frame = await sent_start(agent, browser, draft)
        await agent.send_json_to(
            {"type": "result", "request_id": frame["request_id"], "ok": False,
             "error": {"code": "slot_busy", "msg": ""}}
        )  # fmt: skip
        got = await until(browser, is_tx_state("error"))
        assert got["detail"] == "slot_busy"
        assert not any(m.get("type") == "result" for m in await drain(browser))
        await browser.disconnect()
        await agent.disconnect()
        return draft

    draft = asyncio.run(go())
    draft.refresh_from_db()
    assert draft.state == TxState.ERROR


def test_agent_reconnect_during_tx_never_rekeys(control_agent_auth):
    # Review Focus 4: the server aborts on agent disconnect and never resends.
    station, user = setup_station("tx-recon")

    async def go():
        agent, browser = await connect(station, user, acquire=True)
        draft = await database_sync_to_async(_draft)(station, user)
        await sent_start(agent, browser, draft)
        await agent.disconnect()
        await until(browser, is_tx_state("aborted"))
        agent2 = _agent(station.id)
        await agent2.connect()
        await advertise(station)
        await agent2.send_json_to({"v": 1, "type": "inventory", "slots": [], "features": ["sstv"]})
        out = await drain(agent2, timeout=0.6)
        # A late state of the old session changes nothing.
        await agent2.send_json_to(
            {"type": "sstv_tx_state", "tx_id": draft.pk.hex, "state": "done", "progress": 1}
        )
        await drain(agent2)
        await browser.disconnect()
        await agent2.disconnect()
        return draft, out

    draft, out = asyncio.run(go())
    assert not any(m.get("type") == "sstv_tx_start" for m in out)
    draft.refresh_from_db()
    assert draft.state == TxState.ABORTED and not draft.audio
    assert not SstvImage.objects.exists()


def test_rx_is_paused_for_tx_and_resumed_after(control_agent_auth):
    station, user = setup_station("tx-pause")

    async def go():
        agent, browser = await connect(station, user)
        await started(agent, browser)
        draft = await database_sync_to_async(_draft)(station, user)
        await browser.send_json_to(send(draft))
        seen = []
        frame = await until(agent, lambda m: seen.append(m) or m.get("type") == "sstv_tx_start")
        assert any(is_decoder("decoder_stop")(m) for m in seen[:-1])
        await until(browser, is_state("paused"))
        # The paused decoder's "stopped" must not end the session.
        await agent.send_json_to(
            {"type": "decoder_state", "decoder": "sstv", "slot": 1, "state": "stopped"}
        )
        await agent.send_json_to(
            {"type": "sstv_tx_state", "tx_id": frame["tx_id"], "state": "done", "progress": 1}
        )
        restart = await until(agent, is_decoder("decoder_start"))
        assert 0 < restart["max_runtime_s"] <= 1800
        await until(browser, is_state("starting"))
        await browser.disconnect()
        await agent.disconnect()

    asyncio.run(go())


def test_rx_start_refused_while_sending(ready):
    station, user = ready
    sending = _draft(station, user)
    SstvTransmission.objects.filter(pk=sending.pk).update(state=TxState.SENDING)
    assert sstv_control.start_rx(station, user, 1) == "sstv_busy"


@pytest.mark.parametrize("path", ["lock_release", "operator"])
def test_lock_release_or_operator_stop_aborts_tx(control_agent_auth, path):
    station, user = setup_station(f"tx-stop-{path}")

    async def go():
        agent, browser = await connect(station, user)
        draft = await database_sync_to_async(_draft)(station, user)
        frame = await sent_start(agent, browser, draft)
        if path == "lock_release":
            await browser.send_json_to({"type": "lock_release"})
        else:
            await browser.send_json_to({"type": "sstv_tx_stop", "request_id": "t9"})
        stop = await until(agent, lambda m: m.get("type") == "sstv_tx_stop")
        assert stop["tx_id"] == frame["tx_id"]
        await until(browser, is_tx_state("aborted"))
        await browser.disconnect()
        await agent.disconnect()
        return draft

    draft = asyncio.run(go())
    draft.refresh_from_db()
    assert draft.state == TxState.ABORTED


def test_holder_change_aborts_tx(control_agent_auth):
    station, user = setup_station("tx-transfer")
    other = User.objects.create(username="tx-o", membership_level="staff")

    async def go():
        agent = _agent(station.id)
        await agent.connect()
        draft = await database_sync_to_async(_draft)(station, user)
        await database_sync_to_async(SstvTransmission.objects.filter(pk=draft.pk).update)(
            state=TxState.SENDING
        )
        await lock_hooks.on_lock_acquired(station, other, "preempted", previous_holder_lost=True)
        await until(agent, lambda m: m.get("type") == "sstv_tx_stop")
        await agent.disconnect()
        return draft

    draft = asyncio.run(go())
    draft.refresh_from_db()
    assert draft.state == TxState.ABORTED


def test_sending_counts_as_lock_activity(ready):
    station, user = ready
    then = timezone.now() - timedelta(seconds=600)
    ControlLock.objects.filter(station=station).update(last_activity=then)
    sending = _draft(station, user)
    SstvTransmission.objects.filter(pk=sending.pk).update(state=TxState.SENDING)
    assert lock.sweep_lock(station, timezone.now(), idle_seconds=300) is False


def test_browser_snapshot_shows_a_transmission_on_the_air(control_agent_auth):
    station, user = setup_station("tx-snap")
    draft = _draft(station, user)
    SstvTransmission.objects.filter(pk=draft.pk).update(state=TxState.SENDING)

    async def go():
        browser = _browser(user, station.id)
        await browser.connect()
        got = await until(browser, lambda m: m.get("type") == "sstv_tx_state")
        await browser.disconnect()
        return got

    got = asyncio.run(go())
    assert got["state"] == "sending" and got["id"] == str(draft.pk)


def test_send_needs_sstv_use(control_agent_auth):
    station, user = setup_station("tx-perm", level=User.MembershipLevel.APPLICANT)

    async def go():
        agent, browser = await connect(station, user, acquire=False)
        await browser.send_json_to({"type": "sstv_tx_start", "request_id": "x1"})
        err = await until(browser, lambda m: m.get("type") == "error")
        assert err["code"] == "forbidden"
        await browser.disconnect()
        await agent.disconnect()

    asyncio.run(go())


# -- agent download ----------------------------------------------------------------


def _keyed_station(name):
    from apps.api.models import DeviceKey

    station, user = setup_station(name)
    private_key, _, public_b64 = _make_ed25519_keypair()
    DeviceKey.objects.create(station=station, current_public_key=public_b64)
    return station, user, private_key


def _sending_with_audio(station, user, audio=b"\x01\x02" * 1000):
    draft = _draft(station, user)
    draft.audio.save("a.raw", ContentFile(audio), save=False)
    draft.state = TxState.SENDING
    draft.size = len(audio)
    draft.sha256 = hashlib.sha256(audio).hexdigest()
    draft.save()
    return draft


def test_agent_download_streams_the_audio(client):
    station, user, key = _keyed_station("dl-ok")
    draft = _sending_with_audio(station, user)
    url = reverse("sstv_api:tx_audio", args=[draft.pk])
    assert url.startswith("/api/v1/stations/sstv/")
    resp = client.get(url, **device_auth_headers(key, station.pk))
    assert resp.status_code == 200
    body = b"".join(resp.streaming_content)
    assert hashlib.sha256(body).hexdigest() == draft.sha256
    assert resp["Content-Length"] == str(draft.size)


def test_agent_download_other_station_is_refused(client):
    station, user, _ = _keyed_station("dl-a")
    other, _, other_key = _keyed_station("dl-b")
    draft = _sending_with_audio(station, user)
    url = reverse("sstv_api:tx_audio", args=[draft.pk])
    assert client.get(url, **device_auth_headers(other_key, other.pk)).status_code == 403


@pytest.mark.parametrize("state", [TxState.DONE, TxState.ABORTED, TxState.DRAFT])
def test_agent_download_only_while_sending(client, state):
    station, user, key = _keyed_station(f"dl-{state}")
    draft = _sending_with_audio(station, user)
    SstvTransmission.objects.filter(pk=draft.pk).update(state=state)
    url = reverse("sstv_api:tx_audio", args=[draft.pk])
    assert client.get(url, **device_auth_headers(key, station.pk)).status_code == 403


def test_agent_download_needs_device_auth(client):
    station, user, _ = _keyed_station("dl-anon")
    draft = _sending_with_audio(station, user)
    url = reverse("sstv_api:tx_audio", args=[draft.pk])
    assert client.get(url).status_code in (401, 403)
    client.force_login(user)
    assert client.get(url).status_code in (401, 403)


# -- synthesis ---------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["robot36", "pd90"])
def test_synthesised_audio_decodes_back_to_the_preview(mode):
    # Preview == sent picture: the audio decodes (dev-only numpy decoder) to the preview.
    np = pytest.importorskip("numpy")
    from station_agent.sstv.decoder import End, Row, Start, StreamDecoder

    station, user = setup_station(f"syn-{mode}")
    draft = _draft(station, user, mode=mode)
    audio = tx.synthesise(draft)
    m = MODES[mode]
    assert abs(len(audio) / 2 / 8000 - tx.air_time_s(m)) < 0.01
    dec = StreamDecoder(8000)
    pcm = np.frombuffer(audio + b"\0\0" * 8000, dtype="<i2")
    events = []
    for i in range(0, len(pcm), 160):
        events += dec.feed(pcm[i : i + 160])
    events += dec.flush()
    assert [e.mode for e in events if isinstance(e, Start)] == [m]
    assert [e for e in events if isinstance(e, End)] == [End(complete=True, rows=m.height)]
    rows = b"".join(e.rgb for e in events if isinstance(e, Row))
    got = np.frombuffer(rows, dtype=np.uint8).reshape(m.height, m.width, 3).astype(int)
    assert np.mean(np.abs(got - np.array([10, 200, 10]))) < 12


# -- review round 1: failure paths ---------------------------------------------------


def test_abort_during_synthesis_never_hands_out_audio(ready):
    station, user = ready
    code, claimed = tx.claim(
        station, user, str(_draft(station, user).pk), confirm_no_callsign=False
    )
    assert code is None
    tx.abort_tx(station, "lock released")  # lock lost while the audio was being made
    assert tx.start_sending(station, claimed, b"\0\0" * 100) == "aborted"
    claimed.refresh_from_db()
    assert claimed.state == TxState.ABORTED and not claimed.audio


def test_oversized_audio_ends_the_transmission(ready):
    station, user = ready
    _, claimed = tx.claim(station, user, str(_draft(station, user).pk), confirm_no_callsign=False)
    assert tx.start_sending(station, claimed, b"\0" * (tx.MAX_AUDIO_BYTES + 2)) == "internal_error"
    claimed.refresh_from_db()
    assert claimed.state == TxState.ERROR


def test_synthesis_failure_reports_and_ends(control_agent_auth, monkeypatch):
    station, user = setup_station("tx-synfail")

    def boom(_tx):
        raise ValueError("broken preview")

    monkeypatch.setattr(tx, "synthesise", boom)

    async def go():
        agent, browser = await connect(station, user)
        draft = await database_sync_to_async(_draft)(station, user)
        await browser.send_json_to(send(draft))
        err = await until(browser, lambda m: m.get("type") == "error")
        assert _code(err) == "internal_error"
        assert not any(m.get("type") == "sstv_tx_start" for m in await drain(agent))
        await browser.disconnect()
        await agent.disconnect()
        return draft

    draft = asyncio.run(go())
    draft.refresh_from_db()
    assert draft.state == TxState.ERROR


def test_stale_sending_row_neither_busy_nor_activity(ready):
    station, user = ready
    stale = _draft(station, user)
    then = timezone.now() - timedelta(seconds=sstv_control.TX_MAX_S + 1)
    SstvTransmission.objects.filter(pk=stale.pk).update(state=TxState.SENDING, updated_at=then)
    assert not sstv_control.counts_as_activity(station)
    assert _claim(station, user, _draft(station, user)) is None


def test_lock_loss_aborts_tx_even_if_ending_rx_fails(ready, monkeypatch):
    station, user = ready
    sending = _draft(station, user)
    SstvTransmission.objects.filter(pk=sending.pk).update(state=TxState.SENDING)

    def boom(*a, **kw):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(sstv_control, "stop_rx", boom)
    asyncio.run(sstv_control.on_lock_released(station, "released"))
    sending.refresh_from_db()
    assert sending.state == TxState.ABORTED


def test_gallery_picture_survives_the_transmission_row():
    # Retention deletes transmission rows (and with them the preview blob): the gallery
    # entry of a sent picture must be a copy.
    station, user = setup_station("tx-copy")
    draft = _draft(station, user)
    SstvTransmission.objects.filter(pk=draft.pk).update(state=TxState.SENDING)
    preview_name = draft.preview.name
    tx.on_tx_state(station, {"tx_id": draft.pk.hex, "state": "done", "progress": 1})
    image = SstvImage.objects.get(station=station)
    assert image.image.name != preview_name
    assert not default_storage.exists(preview_name)
    with image.image.open("rb") as f:
        assert Image.open(f).size == (320, 240)
