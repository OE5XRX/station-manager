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


# ---------------------------------------------------------------------------
# Anchor U: non-blocking orchestration, busy guards, routing, teardown
# ---------------------------------------------------------------------------


class _FakeDiagBridge:
    def __init__(self):
        self.fed = []
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def feed_opus(self, p):
        self.fed.append(p)

    def read_measurement(self, nbytes, timeout):
        return b"\x00\x10" * (nbytes // 2)

    def stop(self):
        self.stopped = True


class _FakeFactoryWithDiag:
    def __init__(self, diag):
        self._diag = diag

    def make_rx(self, *a, **k):
        raise AssertionError("unused")

    def make_tx(self, *a, **k):
        class _B:
            def start(self):
                pass

            def feed_opus(self, p):
                pass

            def stop(self):
                pass

        return _B()

    def make_diag_u(self, node, rate):
        return self._diag


class _FakeBackendWithTx:
    def list_audio_slots(self):
        return [0]

    def resolve_node(self, slot, direction):
        return "tx.node"

    def tx_sink_node(self, slot):
        return "sink.node"

    def get_volume(self, node):
        return 0.40


def _engine_with_diag(diag):
    emitted = []

    async def emit_json(m):
        emitted.append(m)

    eng = AudioEngine(
        _FakeBackendWithTx(),
        emit_json=emit_json,
        emit_binary=lambda b: None,
        bridge_factory=_FakeFactoryWithDiag(diag),
    )
    return eng, emitted


def test_u_diag_is_nonblocking_and_emits_result():
    from station_agent.audio import frame

    diag = _FakeDiagBridge()
    eng, emitted = _engine_with_diag(diag)

    # Use a per-run diag_ref in the reserved high band (≥ 0x8000).
    diag_ref = 0x8ABC

    async def scenario():
        ret = await eng.on_diag_command(
            {"request_id": "r1", "anchor": "U", "slot": 0, "signal": {}, "diag_ref": diag_ref}
        )
        assert ret is None  # non-blocking: no synchronous result
        assert diag.started is True
        # feed a reference frame tagged with the per-run ref → routed to bridge
        f = frame.pack_frame(
            stream_ref=diag_ref,
            seq=0,
            ts=0,
            flags=0,
            payload=b"\xfc\xff",
        )
        await eng.on_media_frame(f)
        assert diag.fed == [b"\xfc\xff"]
        # let the background measurement task finish
        await eng._diag_task
        assert diag.stopped is True
        res = [m for m in emitted if m.get("type") == "diag_result"]
        assert res and res[0]["request_id"] == "r1" and res[0]["anchor"] == "U"
        assert {t["point"] for t in res[0]["taps"]} == {"C", "D"}

    asyncio.run(scenario())


def test_u_refused_when_tx_active_is_busy():
    diag = _FakeDiagBridge()
    eng, _ = _engine_with_diag(diag)

    async def scenario():
        eng._tx = {"bridge": object(), "slot": 0, "module": "fm"}  # simulate PTT up
        ret = await eng.on_diag_command(
            {"request_id": "r2", "anchor": "U", "slot": 0, "signal": {}}
        )
        assert ret["busy"] is True and "TX active" in ret["error"]
        assert diag.started is False

    asyncio.run(scenario())


def test_second_u_while_running_is_busy():
    diag = _FakeDiagBridge()
    eng, _ = _engine_with_diag(diag)

    async def scenario():
        await eng.on_diag_command({"request_id": "r3", "anchor": "U", "slot": 0, "signal": {}})
        ret = await eng.on_diag_command(
            {"request_id": "r4", "anchor": "U", "slot": 0, "signal": {}}
        )
        assert ret["busy"] is True
        await eng._diag_task

    asyncio.run(scenario())


def test_diag_ref_frame_ignored_when_no_diag_active():
    from station_agent.audio import frame

    diag = _FakeDiagBridge()
    eng, _ = _engine_with_diag(diag)

    async def scenario():
        # A frame in the high-band ref range with no active diag → silently dropped.
        f = frame.pack_frame(stream_ref=0x8ABC, seq=0, ts=0, flags=0, payload=b"x")
        await eng.on_media_frame(f)  # no diag running → silently ignored, no crash
        assert diag.fed == []

    asyncio.run(scenario())


def test_stop_tears_down_inflight_diag():
    import threading

    diag = _FakeDiagBridge()
    eng, _ = _engine_with_diag(diag)

    async def scenario():
        # make the measurement block so the run is still in-flight at stop()
        gate = threading.Event()
        diag.read_measurement = lambda n, t: (gate.wait(5), b"\x00\x10" * (n // 2))[1]
        await eng.on_diag_command({"request_id": "r5", "anchor": "U", "slot": 0, "signal": {}})
        await eng.stop()
        gate.set()
        assert diag.stopped is True

    asyncio.run(scenario())


def test_mic_ptt_preempts_inflight_diag():
    """RF safety: operator PTT must abort an in-flight diagnostic before TX comes up."""
    import threading

    diag = _FakeDiagBridge()
    eng, _ = _engine_with_diag(diag)

    async def scenario():
        # build the stream registry so op.mic is registered for the TX path
        await eng.start()
        # keep the measurement in-flight so self._diag is genuinely set when PTT lands
        gate = threading.Event()
        diag.read_measurement = lambda n, t: (gate.wait(5), b"\x00\x10" * (n // 2))[1]
        await eng.on_diag_command({"request_id": "r6", "anchor": "U", "slot": 0, "signal": {}})
        assert eng._diag is not None and diag.started is True
        await eng.on_mic_state(active=True, tx_slot=0, tx_module="fm")
        # diagnostic torn down (tone source stopped) BEFORE the TX bridge is up
        assert diag.stopped is True
        assert eng._diag is None
        assert eng._tx is not None
        gate.set()  # let the measurement thread drain
        await eng._teardown_tx()

    asyncio.run(scenario())


def test_mic_ptt_preempts_diag_even_when_resolve_node_returns_none():
    """Fix 1: PTT teardown must happen BEFORE resolve_node — even when it returns None.

    The old ordering resolved the TX node first; if resolve_node returned None the
    early-return skipped _teardown_diag entirely. The diagnostic tone would keep
    feeding the sink while the operator was keying. This test asserts that:
    - the diagnostic bridge is stopped (diag.stopped is True)
    - self._diag is cleared (None)
    - self._tx stays None (TX did not start, because the node was unresolvable)
    """
    import threading

    diag = _FakeDiagBridge()

    class _NullTxBackend(_FakeBackendWithTx):
        """resolve_node always returns None (unresolvable TX node)."""

        def resolve_node(self, slot, direction):
            return None

    emitted = []

    async def emit_json(m):
        emitted.append(m)

    eng = AudioEngine(
        _NullTxBackend(),
        emit_json=emit_json,
        emit_binary=lambda b: None,
        bridge_factory=_FakeFactoryWithDiag(diag),
    )

    async def scenario():
        await eng.start()
        gate = threading.Event()
        diag.read_measurement = lambda n, t: (gate.wait(5), b"\x00\x10" * (n // 2))[1]
        # Manually install the diag (backend can't start U via on_diag_command because
        # resolve_node returns None; inject directly to simulate mid-run state)
        diag.started = True  # mark as started for clarity
        diag.stopped = False
        eng._diag = {"bridge": diag, "slot": 0}
        # PTT arrives — resolve_node will return None, so TX won't start
        await eng.on_mic_state(active=True, tx_slot=0, tx_module="fm")
        # Diagnostic must be torn down regardless of resolve_node result
        assert diag.stopped is True, (
            "diag bridge must be stopped on PTT even if TX node unresolvable"
        )
        assert eng._diag is None, "self._diag must be cleared on PTT"
        assert eng._tx is None, "TX must NOT be started when resolve_node returns None"
        gate.set()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Finding A — per-run diag_ref: cross-run frame isolation
# ---------------------------------------------------------------------------


def test_frame_with_run_ref_is_fed_to_bridge():
    """A frame tagged with the active run's diag_ref IS routed to the bridge."""
    from station_agent.audio import frame

    diag = _FakeDiagBridge()
    eng, _ = _engine_with_diag(diag)
    diag_ref = 0x8001

    async def scenario():
        await eng.on_diag_command(
            {"request_id": "rx1", "anchor": "U", "slot": 0, "signal": {}, "diag_ref": diag_ref}
        )
        f = frame.pack_frame(stream_ref=diag_ref, seq=0, ts=0, flags=0, payload=b"\xaa\xbb")
        await eng.on_media_frame(f)
        assert b"\xaa\xbb" in diag.fed
        await eng._diag_task

    asyncio.run(scenario())


def test_frame_with_different_ref_is_dropped():
    """A frame tagged with a DIFFERENT ref while a run is active is NOT routed to the bridge."""
    from station_agent.audio import frame

    diag = _FakeDiagBridge()
    eng, _ = _engine_with_diag(diag)
    diag_ref = 0x8001
    other_ref = diag_ref ^ 1  # any different high-band value

    async def scenario():
        await eng.on_diag_command(
            {"request_id": "rx2", "anchor": "U", "slot": 0, "signal": {}, "diag_ref": diag_ref}
        )
        # Frame carrying the stale/wrong ref must be dropped.
        f = frame.pack_frame(stream_ref=other_ref, seq=0, ts=0, flags=0, payload=b"\xcc\xdd")
        await eng.on_media_frame(f)
        assert diag.fed == [], "frame with wrong diag_ref must not reach the bridge"
        await eng._diag_task

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Finding C — anchor C must honour the "diagnostic running" busy gate
# ---------------------------------------------------------------------------


def test_anchor_c_refused_while_diag_active(monkeypatch):
    """A C request while self._diag is set must return busy=True and not run."""
    from station_agent.audio import diagnostics

    diag = _FakeDiagBridge()
    eng, _ = _engine_with_diag(diag)

    def _must_not_run(**kw):
        raise AssertionError("run_diagnostic must NOT be called while a diag is active")

    monkeypatch.setattr(diagnostics, "run_diagnostic", _must_not_run)

    async def scenario():
        # Plant an active diagnostic so _diag is set.
        eng._diag = {"bridge": diag, "slot": 0, "ref": 0x8001}
        ret = await eng.on_diag_command(
            {"request_id": "rc1", "anchor": "C", "slot": 0, "signal": {}}
        )
        assert ret is not None
        assert ret["busy"] is True
        assert "diagnostic is already running" in ret["error"]

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Finding B — _diag_lock: TX start must wait for diag bridge stop to complete
# ---------------------------------------------------------------------------


def test_tx_start_waits_for_diag_bridge_stop():
    """on_mic_state must not start the TX bridge until the in-flight diagnostic
    bridge stop() has fully returned.

    Strategy: use a fake diag bridge whose stop() appends to a shared timeline
    list, and a factory that records when make_tx is called.  Assert that
    make_tx is only called AFTER the diag bridge's stop() has returned.
    """
    import threading

    timeline: list[str] = []

    class _BlockingDiagBridge:
        def __init__(self, gate: threading.Event):
            self._gate = gate
            self.started = False
            self.stopped = False

        def start(self):
            self.started = True

        def feed_opus(self, p):
            pass

        def read_measurement(self, nbytes, timeout):
            # Block so the measurement task is still in-flight when PTT arrives.
            self._gate.wait(10)
            return b"\x00\x10" * (nbytes // 2)

        def stop(self):
            timeline.append("diag_stop_returned")
            self.stopped = True

    class _RecordingFactory:
        """Factory that records make_tx calls and provides a blocking diag bridge."""

        def __init__(self, diag):
            self._diag = diag

        def make_rx(self, *a, **k):
            raise AssertionError("unused")

        def make_tx(self, node, rate):
            timeline.append("make_tx_called")

            class _SimpleTx:
                def start(self):
                    pass

                def feed_opus(self, p):
                    pass

                def stop(self):
                    pass

            return _SimpleTx()

        def make_diag_u(self, node, rate):
            return self._diag

    stop_gate = threading.Event()
    diag = _BlockingDiagBridge(stop_gate)

    emitted = []

    async def emit_json(m):
        emitted.append(m)

    eng = AudioEngine(
        _FakeBackendWithTx(),
        emit_json=emit_json,
        emit_binary=lambda b: None,
        bridge_factory=_RecordingFactory(diag),
    )

    async def scenario():
        await eng.start()
        # Start the U diagnostic (measurement blocks on stop_gate).
        await eng.on_diag_command({"request_id": "rb1", "anchor": "U", "slot": 0, "signal": {}})
        assert eng._diag is not None and diag.started

        # Release the gate so stop() can complete when teardown is called.
        stop_gate.set()
        # Fire PTT — _teardown_diag (lock-guarded) must fully stop the diag bridge
        # before make_tx is called.
        await eng.on_mic_state(active=True, tx_slot=0, tx_module="fm")

        # After on_mic_state returns:
        assert eng._diag is None, "self._diag must be cleared"
        assert diag.stopped, "diag bridge must be stopped"
        assert eng._tx is not None, "TX bridge must have started"

        # Ordering: diag stop must have completed BEFORE make_tx was called.
        assert "diag_stop_returned" in timeline, "diag stop must have been called"
        assert "make_tx_called" in timeline, "make_tx must have been called"
        diag_stop_idx = timeline.index("diag_stop_returned")
        make_tx_idx = timeline.index("make_tx_called")
        assert diag_stop_idx < make_tx_idx, (
            f"diag bridge stop ({diag_stop_idx}) must complete before make_tx ({make_tx_idx}); "
            f"timeline={timeline}"
        )

        await eng._teardown_tx()

    asyncio.run(scenario())
