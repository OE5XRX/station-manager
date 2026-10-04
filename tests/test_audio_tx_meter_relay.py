"""Server relay of agent tx_meter frames to browsers (sanitized, per-station)."""

import asyncio
import math

import pytest
from channels.testing import WebsocketCommunicator

from apps.accounts.models import User
from apps.audio.tx_meter import sanitize
from apps.stations.models import Station
from config.asgi import application

GOOD = {
    "v": 1,
    "type": "tx_meter",
    "slot": 1,
    "active": True,
    "peak_dbfs": -12.0,
    "rms_dbfs": -18.2,
    "gain_reduction_db": 3.0,
    "limiting": True,
    "dsp": "full",
    "ceiling_dbfs": -12.0,
}


def test_good_frame_passes_through():
    assert sanitize(GOOD) == GOOD


def test_inactive_frame_minimal():
    assert sanitize({"type": "tx_meter", "slot": 2, "active": False}) == {
        "v": 1,
        "type": "tx_meter",
        "slot": 2,
        "active": False,
    }


@pytest.mark.parametrize(
    "field,bad",
    [
        ("peak_dbfs", float("nan")),
        ("peak_dbfs", 1e9),
        ("peak_dbfs", "loud"),
        ("rms_dbfs", float("inf")),
        ("gain_reduction_db", -5.0),
        ("gain_reduction_db", 1e9),
        ("ceiling_dbfs", "x"),
    ],
)
def test_bad_numbers_become_none_or_clamped(field, bad):
    out = sanitize({**GOOD, field: bad})
    v = out[field]
    assert v is None or (isinstance(v, float) and math.isfinite(v) and -120.0 <= v <= 120.0)
    if field == "gain_reduction_db":
        assert 0.0 <= v <= 60.0


def test_unknown_dsp_and_extra_keys_dropped():
    out = sanitize({**GOOD, "dsp": "<script>", "evil": 1})
    assert out["dsp"] == "off" and "evil" not in out


@pytest.mark.parametrize("mode", ["off", "full", "degraded", "failed"])
def test_known_dsp_modes_pass(mode):
    assert sanitize({**GOOD, "dsp": mode})["dsp"] == mode


def test_initial_frame_with_none_levels_stays_none():
    out = sanitize({**GOOD, "peak_dbfs": None, "rms_dbfs": None})
    assert out["peak_dbfs"] is None and out["rms_dbfs"] is None
    assert out["gain_reduction_db"] == 3.0


@pytest.mark.parametrize(
    "bad", [None, [], "x", {"type": "tx_meter"}, {**GOOD, "slot": "1"}, {**GOOD, "slot": True}]
)
def test_unroutable_frames_rejected(bad):
    assert sanitize(bad) is None


def test_limiting_coerced_to_bool():
    assert sanitize({**GOOD, "limiting": 1})["limiting"] is True


def _agent_comm(station_id):
    return WebsocketCommunicator(
        application, f"/ws/agent/audio/{station_id}/?signature=x&timestamp=0"
    )


def _browser(user, station_id):
    comm = WebsocketCommunicator(application, f"/ws/audio/{station_id}/")
    comm.scope["user"] = user
    return comm


async def _next_tx_meter(comm, timeout=0.5):
    """Return the next tx_meter frame, skipping other frames; None on silence."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return None
        try:
            data = await asyncio.wait_for(comm.receive_json_from(), timeout=remaining)
        except (TimeoutError, ValueError):
            return None
        if isinstance(data, dict) and data.get("type") == "tx_meter":
            return data


@pytest.mark.django_db(transaction=True)
def test_agent_tx_meter_relayed_sanitized_and_scoped(audio_agent_auth):
    station = Station.objects.create(name="txm1", status="online")
    other = Station.objects.create(name="txm2", status="online")
    user = User.objects.create(username="member_txm", membership_level=User.MembershipLevel.MEMBER)

    async def scenario():
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
        browser = _browser(user, station.id)
        assert (await browser.connect())[0] is True
        other_browser = _browser(user, other.id)
        assert (await other_browser.connect())[0] is True
        await asyncio.sleep(0.1)

        # Unroutable frame is dropped; the first tx_meter seen is the good one.
        await agent.send_json_to({**GOOD, "slot": "x"})
        await agent.send_json_to({**GOOD, "evil": "<script>", "dsp": "bogus"})
        got = await _next_tx_meter(browser)
        assert got == {**GOOD, "dsp": "off"}

        await agent.send_json_to(GOOD)
        assert await _next_tx_meter(browser) == GOOD

        # A different station's browser never sees this station's meter.
        assert await _next_tx_meter(other_browser, timeout=0.3) is None

        await browser.disconnect()
        await other_browser.disconnect()
        await agent.disconnect()

    asyncio.run(scenario())
