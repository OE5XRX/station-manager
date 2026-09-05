# tests/test_audio_router_module.py
"""audio-router virtual module (§5.6) + its Broker integration seam."""
import asyncio

from station_agent.audio.router_module import MODULE_ID, AudioRouterModule
from station_agent.broker import Broker


def test_descriptor_exposes_streams_and_tx_route():
    m = AudioRouterModule(slot=1000, list_streams=lambda: [{"stream_id": "slot1.rx"}])
    d = m.descriptor()
    names = {c["name"] for c in d["capabilities"]}
    assert names == {"streams", "tx_route"}
    assert d["identity"]["virtual"] is True


def test_state_snapshots_streams_and_route():
    m = AudioRouterModule(list_streams=lambda: [{"stream_id": "op.mic"}])
    st = m.state()
    assert st["streams"] == [{"stream_id": "op.mic"}]
    assert st["tx_route"] is None


def test_tx_route_set_valid_and_hook_fires():
    seen = []
    m = AudioRouterModule(on_tx_route=seen.append)

    async def go():
        return await m.handle_command("set", "tx_route", {"slot": 3, "module": "fm"})

    res = asyncio.run(go())
    assert res == {"ok": True, "value": {"slot": 3, "module": "fm"}}
    assert m.tx_route == {"slot": 3, "module": "fm"}
    assert seen == [{"slot": 3, "module": "fm"}]


def test_tx_route_null_clears():
    m = AudioRouterModule()
    m.tx_route = {"slot": 1, "module": "fm"}
    assert asyncio.run(m.handle_command("set", "tx_route", None))["ok"] is True
    assert m.tx_route is None


def test_tx_route_bad_value_rejected():
    m = AudioRouterModule()
    res = asyncio.run(m.handle_command("set", "tx_route", {"slot": "x"}))
    assert res["ok"] is False
    assert m.tx_route is None


def test_tx_route_wrong_op_rejected():
    m = AudioRouterModule()
    res = asyncio.run(m.handle_command("get", "tx_route", None))
    assert res["ok"] is False


def test_streams_get_returns_list():
    m = AudioRouterModule(list_streams=lambda: [{"stream_id": "slot1.rx"}])
    res = asyncio.run(m.handle_command("get", "streams", None))
    assert res == {"ok": True, "value": [{"stream_id": "slot1.rx"}]}


# --- Broker integration ---------------------------------------------------
def test_broker_inventory_includes_virtual_module():
    sent = []

    async def send(msg):
        sent.append(msg)

    m = AudioRouterModule(slot=1000, list_streams=lambda: [{"stream_id": "slot1.rx"}])
    broker = Broker(send, virtual_modules=[m])
    broker.set_inventory([])  # no physical slots

    asyncio.run(broker.emit_inventory())
    inv = [s for s in sent if s["type"] == "inventory"][-1]
    slot1000 = next(s for s in inv["slots"] if s["slot"] == 1000)
    mod = slot1000["modules"][0]
    assert mod["module"] == MODULE_ID
    assert mod["state"]["streams"] == [{"stream_id": "slot1.rx"}]


def test_broker_routes_tx_route_command_to_virtual_module():
    sent = []

    async def send(msg):
        sent.append(msg)

    m = AudioRouterModule(slot=1000)
    broker = Broker(send, virtual_modules=[m])
    broker.set_inventory([])

    async def go():
        await broker.handle(
            {
                "v": 1,
                "type": "command",
                "request_id": "r1",
                "slot": 1000,
                "module": MODULE_ID,
                "capability": "tx_route",
                "op": "set",
                "value": {"slot": 1, "module": "fm"},
            }
        )

    asyncio.run(go())
    result = next(s for s in sent if s["type"] == "result")
    assert result["ok"] is True and result["request_id"] == "r1"
    assert m.tx_route == {"slot": 1, "module": "fm"}


def test_broker_with_no_virtual_modules_is_unchanged():
    # Regression guard: the default construction registers nothing extra.
    sent = []

    async def send(msg):
        sent.append(msg)

    broker = Broker(send)
    broker.set_inventory([])
    asyncio.run(broker.emit_inventory())
    inv = [s for s in sent if s["type"] == "inventory"][-1]
    assert inv["slots"] == []
