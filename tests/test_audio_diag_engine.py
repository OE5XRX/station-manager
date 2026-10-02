# tests/test_audio_diag_engine.py
"""AudioEngine.on_diag_command: validation, off-thread dispatch, result envelope."""

import asyncio

from station_agent.audio.engine import AudioEngine


class FakeBackend:
    def list_audio_slots(self):
        return [1]

    def resolve_node(self, slot, direction):
        return "oe5xrx.slot1.tx"

    def tx_sink_node(self, slot):
        return "FM.Mono"

    def get_volume(self, node):
        return 0.40


def _engine():
    sent = []

    async def emit_json(m):
        sent.append(m)

    def emit_binary(b):
        pass

    eng = AudioEngine(FakeBackend(), emit_json=emit_json, emit_binary=emit_binary)
    return eng, sent


def test_on_diag_command_rejects_bad_anchor():
    eng, _ = _engine()

    async def scenario():
        return await eng.on_diag_command(
            {"anchor": "Z", "slot": 1, "request_id": "r1", "signal": {"level_dbfs": -20.0}}
        )

    res = asyncio.run(scenario())
    assert res["type"] == "diag_result" and res["request_id"] == "r1"
    assert "error" in res


def test_on_diag_command_rejects_non_int_slot():
    eng, _ = _engine()

    async def scenario():
        return await eng.on_diag_command(
            {"anchor": "C", "slot": "x", "request_id": "r2", "signal": {"level_dbfs": -20.0}}
        )

    res = asyncio.run(scenario())
    assert "error" in res
    assert res["request_id"] == "r2"


def test_on_diag_command_rejects_bool_slot():
    eng, _ = _engine()

    async def scenario():
        return await eng.on_diag_command(
            {"anchor": "C", "slot": True, "request_id": "r4", "signal": {"level_dbfs": -20.0}}
        )

    res = asyncio.run(scenario())
    assert "error" in res
    assert res["request_id"] == "r4"


def test_on_diag_command_anchor_c_returns_report(monkeypatch):
    from station_agent.audio import diagnostics

    eng, _ = _engine()
    monkeypatch.setattr(
        diagnostics,
        "run_diagnostic",
        lambda **kw: {"anchor": "C", "taps": [], "static_gains": {}},
    )

    async def scenario():
        return await eng.on_diag_command(
            {"anchor": "C", "slot": 1, "request_id": "r3", "signal": {"level_dbfs": -20.0}}
        )

    res = asyncio.run(scenario())
    assert res["type"] == "diag_result" and res["request_id"] == "r3"
    assert res["anchor"] == "C"


def test_on_diag_command_refuses_while_tx_active(monkeypatch):
    """BUG4 — RF safety: diagnostic inject must be refused when a TX bridge is up."""
    from station_agent.audio import diagnostics

    eng, _ = _engine()

    # Simulate an active TX bridge (PTT / mic up)
    eng._tx = {"bridge": object(), "slot": 0, "module": "fm"}

    # If run_diagnostic is called it will raise to make the test fail visibly
    def _should_not_be_called(**kw):
        raise AssertionError("run_diagnostic must NOT be called while TX is active")

    monkeypatch.setattr(diagnostics, "run_diagnostic", _should_not_be_called)

    async def scenario():
        return await eng.on_diag_command(
            {"anchor": "C", "slot": 1, "request_id": "rf1", "signal": {"level_dbfs": -20.0}}
        )

    res = asyncio.run(scenario())
    assert res["type"] == "diag_result"
    assert res["request_id"] == "rf1"
    assert "error" in res
    assert "TX active" in res["error"]
