# tests/test_audio_diag_ws.py
"""AudioClient._dispatch: diag_command routes to on_diag_command and sends diag_result."""

import asyncio

from station_agent.audio.ws_client import AudioClient


class _Cfg:
    server_url = "https://x"
    station_id = 1
    ed25519_key_path = "/nonexistent"


def test_dispatch_routes_diag_command(monkeypatch):
    # Build a client without loading a key (patch load_private_key).
    import station_agent.audio.ws_client as m

    monkeypatch.setattr(m, "load_private_key", lambda p: object())
    c = AudioClient(_Cfg())

    class FakeEngine:
        async def on_diag_command(self, msg):
            return {"v": 1, "type": "diag_result", "request_id": msg["request_id"]}

    c._engine = FakeEngine()
    out = []

    async def fake_emit(m):
        out.append(m)

    c._send_json = fake_emit

    async def scenario():
        await c._dispatch(
            '{"v":1,"type":"diag_command","request_id":"r9","anchor":"C","slot":1,"signal":{}}'
        )

    asyncio.run(scenario())
    assert out and out[0]["type"] == "diag_result" and out[0]["request_id"] == "r9"


def test_dispatch_does_not_send_when_diag_returns_none(monkeypatch):
    """Test that diag_command returning None sends nothing (async U run)."""
    import station_agent.audio.ws_client as m

    monkeypatch.setattr(m, "load_private_key", lambda p: object())
    c = AudioClient(_Cfg())

    class FakeEngine:
        async def on_diag_command(self, msg):
            return None  # U accepted, async result comes later via emit_json

    c._engine = FakeEngine()
    out = []

    async def fake_emit(m):
        out.append(m)

    c._send_json = fake_emit

    async def scenario():
        await c._dispatch(
            '{"v":1,"type":"diag_command","request_id":"r1","anchor":"U","slot":1,"signal":{}}'
        )

    asyncio.run(scenario())
    assert out == []  # nothing sent synchronously


def test_dispatch_sends_when_diag_returns_dict(monkeypatch):
    """Test that diag_command returning a dict sends it (sync C run)."""
    import station_agent.audio.ws_client as m

    monkeypatch.setattr(m, "load_private_key", lambda p: object())
    c = AudioClient(_Cfg())

    class FakeEngine:
        async def on_diag_command(self, msg):
            return {"type": "diag_result", "request_id": msg["request_id"]}

    c._engine = FakeEngine()
    out = []

    async def fake_emit(m):
        out.append(m)

    c._send_json = fake_emit

    async def scenario():
        await c._dispatch(
            '{"v":1,"type":"diag_command","request_id":"r2","anchor":"C","slot":1,"signal":{}}'
        )

    asyncio.run(scenario())
    assert out == [{"type": "diag_result", "request_id": "r2"}]
