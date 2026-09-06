# tests/test_broker_virtual_telemetry.py
"""Regression: telemetry polling a *virtual* control-plane module (the audio-router on
its synthetic slot) must NOT build a serial transport.

Live bug (RC#2): the server subscribes the ``audio-router`` ``streams`` telemetry cap on
the synthetic slot 1000. ``_poll_loop`` called ``_execute`` unconditionally, which built a
``SlotControl`` for ``_control_path(1000) -> None`` and did ``os.open(None)`` — raising
``TypeError: open: path should be string, bytes or os.PathLike, not NoneType`` once per
poll tick (~1×/s). The command path and inventory path already special-cased virtual
modules; the telemetry poll did not.
"""

import asyncio

from station_agent.audio.router_module import AudioRouterModule
from station_agent.broker import Broker


class Collector:
    def __init__(self):
        self.sent = []

    async def __call__(self, msg):
        self.sent.append(msg)


def _run(coro):
    return asyncio.run(coro)


_STREAMS = [
    {
        "stream_id": "slot1.rx",
        "slot": 1,
        "module": "fm",
        "direction": "rx",
        "format": {"rate": 8000, "channels": 1},
        "codec": "opus",
        "stream_ref": 0,
    }
]


def _broker_with_router(transport_factory):
    col = Collector()
    router = AudioRouterModule(slot=1000, list_streams=lambda: list(_STREAMS))
    b = Broker(
        col,
        transport_factory=transport_factory,
        telemetry_min_floor_ms=10,
        telemetry_default_interval_ms=20,
        now=lambda: 1.0,
        virtual_modules=[router],
    )
    # No physical slots at all: the synthetic router slot has no control device.
    b.set_inventory([])
    return b, col


def test_virtual_telemetry_poll_never_builds_a_transport():
    """A telemetry tick on the virtual router must not touch the serial transport factory
    (which for the synthetic slot would receive a None path and os.open(None))."""
    factory_calls = []

    def spy_factory(path):
        factory_calls.append(path)
        raise AssertionError(f"transport built for a virtual module (path={path!r})")

    b, col = _broker_with_router(spy_factory)

    async def scenario():
        await b.handle_subscribe(
            {
                "slot": 1000,
                "module": "audio-router",
                "capabilities": ["streams"],
                "interval_ms": 10,
            }
        )
        await asyncio.sleep(0.05)  # let a few poll ticks fire
        await b.stop()

    _run(scenario())
    assert factory_calls == [], f"transport factory called for virtual slot: {factory_calls}"


def test_virtual_telemetry_poll_emits_state_value():
    """The poll must emit the router's own ``streams`` state, proving it routed to the
    virtual handler instead of failing on a serial open."""

    def dead_factory(path):  # would only be hit for a physical slot
        raise AssertionError(f"unexpected transport for path={path!r}")

    b, col = _broker_with_router(dead_factory)

    async def scenario():
        await b.handle_subscribe(
            {
                "slot": 1000,
                "module": "audio-router",
                "capabilities": ["streams"],
                "interval_ms": 10,
            }
        )
        await asyncio.sleep(0.05)
        await b.stop()

    _run(scenario())
    states = [
        m
        for m in col.sent
        if m.get("type") == "state"
        and m.get("slot") == 1000
        and "streams" in (m.get("values") or {})
    ]
    assert states, f"no streams state emitted for the virtual router; sent={col.sent}"
    assert states[-1]["values"]["streams"] == _STREAMS
