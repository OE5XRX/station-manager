"""Agent-side sequential carrier scanner (repeater-scanner spec §7, plan Task 7).

Unit tests drive :class:`station_agent.scanner.Scanner` with a fake clock/sleep and a scripted
carrier timeline; broker tests at the bottom run it end-to-end over the pty fake firmware.
No pytest-asyncio — every scenario runs inside ``asyncio.run``.
"""

import asyncio

from station_agent import slot_pause
from station_agent.audio.router_module import AudioRouterModule
from station_agent.broker import Broker
from station_agent.scanner import Scanner, parse_entries
from station_agent.slot_control import SlotControl
from tests.fake_fw import FakeFirmware


def _entry(ref, slot=1, module="fm", rx=145.6, tx=145.0, rx_tone="none", tx_tone="77.0"):
    return {
        "ref": ref,
        "label": ref.upper(),
        "slot": slot,
        "module": module,
        "rx_frequency": rx,
        "tx_frequency": tx,
        "rx_tone": rx_tone,
        "tx_tone": tx_tone,
    }


class Rig:
    """Fake clock + device. ``carrier(ref, t) -> bool`` scripts the carrier timeline."""

    def __init__(self, carrier=None, *, busy=None, apply_error=None, read_error=None):
        self.t = 0.0
        self.carrier = carrier or (lambda ref, t: False)
        self.current = None  # ref of the entry last applied
        self.applied = []  # [(ref, values)]
        self.routes = []
        self.sent = []
        self.apply_error = apply_error or (lambda ref: None)
        self.read_error = read_error or (lambda ref: None)
        self.busy_fn = busy or (lambda rig: False)  # PTT keyed anywhere? (gets the rig)
        self.paused = set()
        self._by_freq = {}

    def register(self, entries):
        for e in entries:
            self._by_freq[(e["slot"], e["rx_frequency"])] = e["ref"]

    async def apply_preset(self, slot, module, values):
        ref = self._by_freq.get((slot, values["rx_frequency"]))
        err = self.apply_error(ref)
        if err:
            return {"tx_frequency": {"ok": False, "error": {"code": err, "msg": ""}}}
        self.current = ref
        self.applied.append((ref, dict(values)))
        await asyncio.sleep(0)
        return {k: {"ok": True, "value": v} for k, v in values.items()}

    async def read_carrier(self, slot, module):
        err = self.read_error(self.current)
        if err:
            return {"ok": False, "error": err}
        await asyncio.sleep(0)
        return {"ok": True, "value": bool(self.carrier(self.current, self.t))}

    async def set_tx_route(self, slot, module):
        self.routes.append((slot, module))

    async def send(self, msg):
        self.sent.append(msg)

    async def sleep(self, d):
        self.t = round(self.t + d, 6)
        await asyncio.sleep(0)

    def now(self):
        return self.t

    def ptt_active(self):
        return self.busy_fn(self)

    def scanner(self):
        return Scanner(
            self.apply_preset,
            self.read_carrier,
            self.set_tx_route,
            self.send,
            sleep=self.sleep,
            now=self.now,
            poll_s=0.1,
            ptt_active=self.ptt_active,
            paused=lambda: self.paused,
            held_check_s=0.001,
        )

    def states(self):
        return [(m["state"], m["ref"]) for m in self.sent if m["type"] == "scan_state"]

    def visits(self):
        return [ref for ref, _ in self.applied]


async def _until(cond, steps=20000):
    for _ in range(steps):
        if cond():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not reached")


def _run(coro):
    return asyncio.run(coro)


ABC = [_entry("a", rx=145.6), _entry("b", rx=145.7), _entry("c", rx=145.8)]


def test_no_carrier_advances_after_listen_window():
    async def go():
        rig = Rig()
        rig.register(ABC)
        s = rig.scanner()
        await s.start("s1", ABC, 2.0, 5.0)
        await _until(lambda: len(rig.applied) >= 2)
        # Entry b applied only once a's whole 2 s listen window passed.
        assert rig.visits()[:2] == ["a", "b"]
        assert 2.0 <= rig.t < 2.3
        await s.stop("operator")

    _run(go())


def test_short_over_inside_window_detected():
    async def go():
        rig = Rig(lambda ref, t: ref == "a" and 1.45 <= t <= 1.55)
        rig.register(ABC)
        s = rig.scanner()
        await s.start("s1", ABC, 2.0, 5.0)
        await _until(lambda: ("active", "a") in rig.states())
        await s.stop("operator")

    _run(go())


def test_hang_then_advance():
    async def go():
        rig = Rig(lambda ref, t: ref == "a" and t < 1.0)
        rig.register(ABC)
        s = rig.scanner()
        await s.start("s1", ABC, 2.0, 5.0)
        await _until(lambda: len(rig.applied) >= 2)
        assert rig.states()[:4] == [
            ("scanning", "a"),
            ("active", "a"),
            ("hang", "a"),
            ("scanning", "b"),
        ]
        # Carrier dropped at t=1.0 → 5 s hang → next entry at ~6 s.
        assert 6.0 <= rig.t < 6.3
        await s.stop("operator")

    _run(go())


def test_carrier_returns_within_hang_stays_active():
    async def go():
        on = lambda ref, t: ref == "a" and (t < 1.0 or 3.0 <= t < 4.0)  # noqa: E731
        rig = Rig(on)
        rig.register(ABC)
        s = rig.scanner()
        await s.start("s1", ABC, 2.0, 5.0)
        await _until(lambda: len(rig.applied) >= 2)
        assert rig.states()[:6] == [
            ("scanning", "a"),
            ("active", "a"),
            ("hang", "a"),
            ("active", "a"),
            ("hang", "a"),
            ("scanning", "b"),
        ]
        assert 9.0 <= rig.t < 9.3
        await s.stop("operator")

    _run(go())


def test_wraps_at_end():
    async def go():
        rig = Rig()
        rig.register(ABC)
        s = rig.scanner()
        await s.start("s1", ABC, 2.0, 5.0)
        await _until(lambda: len(rig.applied) >= 5)
        assert rig.visits()[:5] == ["a", "b", "c", "a", "b"]
        await s.stop("operator")

    _run(go())


def test_update_adopted_next_step():
    async def go():
        rig = Rig()
        d = _entry("d", rx=146.0)
        new = [ABC[0], d, ABC[2]]
        rig.register(ABC + [d])
        s = rig.scanner()
        await s.start("s1", ABC, 2.0, 5.0)
        await _until(lambda: len(rig.applied) >= 1)
        await s.update("s1", new)
        # Not adopted mid-entry: a keeps listening; the next step continues after a in the new
        # list (d), not b.
        await _until(lambda: len(rig.applied) >= 3)
        assert rig.visits()[:3] == ["a", "d", "c"]
        assert s.active_slots() == {1}
        await s.stop("operator")

    _run(go())


def test_update_wrong_scan_id_ignored():
    async def go():
        rig = Rig()
        rig.register(ABC)
        s = rig.scanner()
        await s.start("s1", ABC, 2.0, 5.0)
        await s.update("other", [ABC[2]])
        await _until(lambda: len(rig.applied) >= 2)
        assert rig.visits()[:2] == ["a", "b"]
        await s.stop("operator")

    _run(go())


def test_skip_hold_resume_stop():
    async def go():
        rig = Rig(lambda ref, t: ref == "a")  # a is busy forever
        rig.register(ABC)
        s = rig.scanner()
        await s.start("s1", ABC, 2.0, 5.0)
        await _until(lambda: ("active", "a") in rig.states())
        await s.control("s1", "skip")
        await _until(lambda: rig.visits()[-1:] == ["b"])
        await s.control("s1", "hold")
        assert rig.states()[-1] == ("held", "b")
        n = len(rig.applied)
        t0 = rig.t
        for _ in range(200):
            await asyncio.sleep(0)
        assert len(rig.applied) == n and rig.t == t0  # held: no device activity, no polling
        await s.control("s1", "resume")
        await _until(lambda: rig.states()[-1] == ("scanning", "b") and len(rig.applied) > n)
        assert rig.visits()[n] == "b"  # resume restarts the held entry
        await s.control("s1", "stop")
        stopped = [m for m in rig.sent if m["state"] == "stopped"]
        assert len(stopped) == 1 and stopped[0]["reason"] == "operator"
        assert not s.running and s.active_slots() == set()

    _run(go())


def test_control_wrong_scan_id_ignored():
    async def go():
        rig = Rig()
        rig.register(ABC)
        s = rig.scanner()
        await s.start("s1", ABC, 2.0, 5.0)
        await s.control("nope", "stop")
        assert s.running
        await s.stop("operator")

    _run(go())


def test_ptt_holds():
    async def go():
        rig = Rig()
        rig.register(ABC)
        s = rig.scanner()
        await s.start("s1", ABC, 2.0, 5.0)
        await _until(lambda: len(rig.applied) >= 1)
        await s.hold("ptt")
        held = [m for m in rig.sent if m["state"] == "held"]
        assert held[-1]["reason"] == "ptt" and held[-1]["ref"] == "a"
        # Held on an applied entry → tx_route points at its module.
        assert rig.routes[-1] == (1, "fm")
        n = len(rig.applied)
        for _ in range(200):
            await asyncio.sleep(0)
        assert len(rig.applied) == n
        await s.stop("operator")

    _run(go())


def test_busy_slot_at_apply_holds_without_applying():
    async def go():
        rig = Rig(busy=lambda r: r.t >= 1.0)
        entries = [_entry("a", rx=145.6), _entry("b", slot=2, rx=433.0)]
        rig.register(entries)
        s = rig.scanner()
        await s.start("s1", entries, 2.0, 5.0)
        await _until(lambda: rig.states()[-1:] == [("held", "a")])
        assert rig.visits() == ["a"]  # b never applied while its slot is keyed
        # Held is reported on (and tx_route points at) the entry the radio is still tuned to.
        held = [m for m in rig.sent if m["state"] == "held"][-1]
        assert (held["slot"], held["module"], held["index"]) == (1, "fm", 0)
        assert rig.routes == []  # a PTT is keyed: tx_route is not moved under it
        await s.stop("operator")

    _run(go())


def test_hold_between_entries_reports_tuned_entry():
    """A hold landing after _index advanced but before the next apply holds the tuned entry."""

    async def go():
        entries = [_entry("a", rx=145.6), _entry("b", slot=2, module="fm2", rx=433.0)]
        rig = Rig()
        rig.register(entries)
        gate = asyncio.Event()
        inner = rig.send

        async def send(msg):
            await inner(msg)
            if (msg["state"], msg["ref"]) == ("scanning", "b"):
                await gate.wait()  # suspend inside the `scanning b` emit step

        rig.send = send
        s = rig.scanner()
        await s.start("s1", entries, 0.3, 5.0)
        await _until(lambda: rig.states()[-1:] == [("scanning", "b")])
        hold = asyncio.ensure_future(s.hold("ptt"))
        await asyncio.sleep(0)
        gate.set()
        await hold
        held = [m for m in rig.sent if m["state"] == "held"][-1]
        assert (held["ref"], held["slot"], held["module"]) == ("a", 1, "fm")
        assert rig.visits() == ["a"] and rig.routes == [(1, "fm")]
        await s.control("s1", "resume")
        await _until(lambda: len(rig.applied) >= 2)
        assert rig.visits()[1] == "a"  # resume restarts the held (tuned) entry
        await s.stop("operator")

    _run(go())


def _gated_sleep_rig(carrier):
    """Rig whose sleep can be frozen, to land hold+skip/resume while the runner sleeps."""
    rig = Rig(carrier)
    rig.freeze = None
    rig.sleeping = 0
    inner = rig.sleep

    async def sleep(d):
        if rig.freeze is not None:
            rig.sleeping += 1
            await rig.freeze.wait()
        await inner(d)

    rig.sleep = sleep
    return rig


def test_hold_skip_while_runner_sleeps_moves_on_cleanly():
    async def go():
        rig = _gated_sleep_rig(lambda ref, t: ref == "a")
        rig.register(ABC)
        s = rig.scanner()
        await s.start("s1", ABC, 2.0, 5.0)
        await _until(lambda: ("active", "a") in rig.states())
        rig.freeze = asyncio.Event()
        await _until(lambda: rig.sleeping > 0)
        await s.hold("manual")
        await s.control("s1", "skip")
        rig.freeze.set()
        rig.freeze = None
        await _until(lambda: len(rig.applied) >= 2)
        # The stale visit of `a` is abandoned: b is tuned next, no frame claims b while the
        # radio still sits on a, and nothing is skipped over.
        assert rig.visits()[:2] == ["a", "b"]
        assert rig.states()[-2:] == [("held", "a"), ("scanning", "b")]
        await s.stop("operator")

    _run(go())


def test_hold_resume_while_runner_sleeps_reapplies_entry():
    async def go():
        rig = _gated_sleep_rig(lambda ref, t: ref == "a")
        rig.register(ABC)
        s = rig.scanner()
        await s.start("s1", ABC, 2.0, 5.0)
        await _until(lambda: ("active", "a") in rig.states())
        rig.freeze = asyncio.Event()
        await _until(lambda: rig.sleeping > 0)
        await s.hold("manual")
        await s.control("s1", "resume")
        rig.freeze.set()
        rig.freeze = None
        # Resume after a (possibly manual) retune restarts the entry: a is applied again.
        await _until(lambda: len(rig.applied) >= 2)
        assert rig.visits()[:2] == ["a", "a"]
        await s.stop("operator")

    _run(go())


def test_stop_during_runner_side_hold_waits_for_held_frame():
    async def go():
        # Keyed until the held frame went out (released while it is in flight), so the hold
        # also routes tx_route afterwards — both must complete before stop() cancels.
        rig = Rig(busy=lambda r: r.t >= 0.2 and not any(m["state"] == "held" for m in r.sent))
        entries = [_entry("a", rx=145.6), _entry("b", slot=2, rx=433.0)]
        rig.register(entries)
        gate = asyncio.Event()
        inner = rig.send

        async def send(msg):
            await inner(msg)
            if msg["state"] == "held":
                await gate.wait()

        rig.send = send
        s = rig.scanner()
        await s.start("s1", entries, 0.3, 5.0)
        await _until(lambda: rig.states()[-1:] == [("held", "a")])
        stop = asyncio.ensure_future(s.stop("operator"))
        for _ in range(50):
            await asyncio.sleep(0)
        assert not stop.done()  # stop waits for the in-flight held frame + route
        gate.set()
        await stop
        assert rig.routes == [(1, "fm")]
        assert rig.states()[-1][0] == "stopped"

    _run(go())


def test_self_stop_does_not_wipe_new_scan():
    """A scan_start while the runner's own `stopped` frame is in flight keeps the new scan."""

    async def go():
        rig = Rig(apply_error=lambda ref: "timeout" if ref == "a" else None)
        rig.register(ABC)
        gate = asyncio.Event()
        inner = rig.send

        async def send(msg):
            await inner(msg)
            if msg["state"] == "stopped" and msg["scan_id"] == "s1":
                await gate.wait()

        rig.send = send
        s = rig.scanner()
        await s.start("s1", ABC[:1], 2.0, 5.0)
        await _until(lambda: any(m["state"] == "stopped" for m in rig.sent))
        await s.start("s2", ABC[1:], 2.0, 5.0)
        gate.set()
        await _until(lambda: len(rig.applied) >= 2)
        assert s.running and s.scan_id == "s2"
        assert rig.visits()[:2] == ["b", "c"]
        await s.stop("operator")

    _run(go())


def test_resume_while_ptt_keyed_stays_held():
    """Resuming while a PTT is still keyed (any slot) must not retune or move tx_route."""

    async def go():
        keyed = {"on": False}
        rig = Rig(lambda ref, t: ref == "b", busy=lambda r: keyed["on"])
        entries = [_entry("a", rx=145.6), _entry("b", slot=2, module="fm2", rx=433.0)]
        rig.register(entries)
        s = rig.scanner()
        await s.start("s1", entries, 0.3, 5.0)
        await _until(lambda: len(rig.applied) >= 1)
        keyed["on"] = True
        await s.hold("ptt", keyed=(2, "fm2"))
        n, routes = len(rig.applied), list(rig.routes)
        await s.control("s1", "resume")
        await _until(lambda: [m["state"] for m in rig.sent].count("held") >= 2)
        for _ in range(200):
            await asyncio.sleep(0)
        assert len(rig.applied) == n and rig.routes == routes
        keyed["on"] = False
        await s.control("s1", "resume")
        await _until(lambda: ("active", "b") in rig.states())
        await s.stop("operator")

    _run(go())


def test_start_while_keyed_holds_naming_no_entry():
    async def go():
        rig = Rig(busy=lambda r: True)
        rig.register(ABC)
        s = rig.scanner()
        await s.start("s1", ABC, 2.0, 5.0)
        await _until(lambda: any(m["state"] == "held" for m in rig.sent))
        held = rig.sent[-1]
        assert (held["ref"], held["slot"], held["module"]) == (None, None, None)
        assert rig.applied == [] and rig.routes == []
        await s.stop("operator")

    _run(go())


def test_stop_between_entries_names_tuned_entry():
    async def go():
        rig = Rig()
        rig.register(ABC)
        gate = asyncio.Event()
        inner = rig.send

        async def send(msg):
            await inner(msg)
            if (msg["state"], msg["ref"]) == ("scanning", "b"):
                await gate.wait()

        rig.send = send
        s = rig.scanner()
        await s.start("s1", ABC, 0.3, 5.0)
        await _until(lambda: rig.states()[-1:] == [("scanning", "b")])
        stop = asyncio.ensure_future(s.stop("operator"))
        await asyncio.sleep(0)
        gate.set()
        await stop
        assert rig.visits() == ["a"]
        last = rig.sent[-1]
        assert (last["state"], last["ref"], last["index"]) == ("stopped", "a", 0)

    _run(go())


def test_ptt_on_other_module_does_not_move_tx_route():
    async def go():
        rig = Rig()
        rig.register(ABC)
        s = rig.scanner()
        await s.start("s1", ABC, 2.0, 5.0)
        await _until(lambda: len(rig.applied) >= 1)
        await s.hold("ptt", keyed=(2, "fm2"))  # keying a module the scan is not tuned on
        assert rig.states()[-1] == ("held", "a")
        assert rig.routes == []
        await s.stop("operator")

    _run(go())


def test_drain_while_held_stops_scan():
    async def go():
        rig = Rig()
        rig.register(ABC)
        s = rig.scanner()
        await s.start("s1", ABC, 2.0, 5.0)
        await _until(lambda: len(rig.applied) >= 1)
        await s.hold("operator")
        rig.paused = {1}
        await _until(lambda: not s.running)
        assert rig.sent[-1]["state"] == "stopped"
        assert rig.sent[-1]["reason"] == "module_update"

    _run(go())


def test_drain_of_other_scanned_slot_stops_scan_mid_entry():
    async def go():
        rig = Rig(lambda ref, t: ref == "a")  # parked active on slot 1
        entries = [_entry("a", rx=145.6), _entry("b", slot=2, rx=433.0)]
        rig.register(entries)
        s = rig.scanner()
        await s.start("s1", entries, 2.0, 5.0)
        await _until(lambda: ("active", "a") in rig.states())
        rig.paused = {2}  # slot 2 (scanned, not the current one) starts a firmware update
        await _until(lambda: not s.running)
        assert rig.sent[-1]["reason"] == "module_update"

    _run(go())


def test_manual_set_holds_then_applies():
    """hold() returns only once the scanner is quiescent: no apply may follow it."""

    async def go():
        rig = Rig()
        rig.register(ABC)
        s = rig.scanner()
        await s.start("s1", ABC, 0.3, 5.0)
        await _until(lambda: len(rig.applied) >= 2)
        await s.hold("manual")
        n = len(rig.applied)
        for _ in range(500):
            await asyncio.sleep(0)
        assert len(rig.applied) == n
        assert rig.states()[-1][0] == "held"
        await s.stop("operator")

    _run(go())


def test_all_entries_error_stops():
    async def go():
        rig = Rig(apply_error=lambda ref: "timeout")
        rig.register(ABC)
        s = rig.scanner()
        await s.start("s1", ABC, 2.0, 5.0)
        await _until(lambda: not s.running)
        stopped = [m for m in rig.sent if m["state"] == "stopped"]
        assert stopped[-1]["reason"] == "no_reachable_entries"

    _run(go())


def test_one_erroring_entry_is_skipped():
    async def go():
        rig = Rig(read_error=lambda ref: "unknown_slot" if ref == "b" else None)
        rig.register(ABC)
        s = rig.scanner()
        await s.start("s1", ABC, 2.0, 5.0)
        await _until(lambda: len(rig.applied) >= 5)
        assert rig.visits()[:5] == ["a", "b", "c", "a", "b"]
        assert s.running
        await s.stop("operator")

    _run(go())


def test_updating_slot_stops_scan():
    async def go():
        rig = Rig(apply_error=lambda ref: "updating" if ref == "b" else None)
        rig.register(ABC)
        s = rig.scanner()
        await s.start("s1", ABC, 2.0, 5.0)
        await _until(lambda: not s.running)
        assert [m for m in rig.sent if m["state"] == "stopped"][-1]["reason"] == "module_update"

    _run(go())


def test_tx_route_set_on_active():
    async def go():
        entries = [_entry("a", rx=145.6), _entry("b", slot=2, module="fm2", rx=433.0)]
        rig = Rig(lambda ref, t: ref == "b")
        rig.register(entries)
        s = rig.scanner()
        await s.start("s1", entries, 2.0, 5.0)
        await _until(lambda: ("active", "b") in rig.states())
        assert rig.routes == [(2, "fm2")]  # only on entering active, not while scanning a
        await s.stop("operator")

    _run(go())


def test_states_only_on_transition():
    async def go():
        rig = Rig(lambda ref, t: ref == "a" and t < 3.0)
        rig.register(ABC[:1])
        s = rig.scanner()
        await s.start("s1", ABC[:1], 2.0, 5.0)
        await _until(lambda: len(rig.applied) >= 3)
        await s.stop("operator")
        states = [m["state"] for m in rig.sent]
        # ~30 carrier polls while active, but one frame per transition; a single-entry list
        # re-entering `scanning` on the same entry after a no-carrier window emits nothing.
        assert states == ["scanning", "active", "hang", "scanning", "stopped"]
        frame = rig.sent[0]
        assert frame["type"] == "scan_state" and frame["v"] == 1
        assert {"scan_id", "state", "index", "ref", "slot", "module", "ts"} <= set(frame)

    _run(go())


def test_start_replaces_running_scan():
    async def go():
        rig = Rig()
        rig.register(ABC)
        s = rig.scanner()
        await s.start("s1", ABC, 2.0, 5.0)
        await s.start("s2", ABC[1:], 2.0, 5.0)
        stopped = [m for m in rig.sent if m["state"] == "stopped"]
        assert stopped == [stopped[0]] and stopped[0]["scan_id"] == "s1"
        assert stopped[0]["reason"] == "replaced"
        await _until(lambda: any(m["scan_id"] == "s2" for m in rig.sent))
        await s.stop("operator")

    _run(go())


def test_parse_entries_validation():
    assert parse_entries([_entry("a")]) == [_entry("a")]
    assert parse_entries([]) == []
    for bad in (
        None,
        "x",
        [{**_entry("a"), "slot": True}],
        [{**_entry("a"), "slot": "1"}],
        [{**_entry("a"), "rx_frequency": "145.6"}],
        [{**_entry("a"), "tx_tone": 77}],
        [{**_entry("a"), "module": ""}],
        [{**_entry("a"), "module": "m" * 65}],
        [_entry("a"), _entry("a", rx=146.0)],  # duplicate ref
        [{**_entry("a"), "rx_tone": "1" * 17}],
        [{k: v for k, v in _entry("a").items() if k != "ref"}],
    ):
        assert parse_entries(bad) is None, bad
    # label is optional (defaults to "")
    e = {k: v for k, v in _entry("a").items() if k != "label"}
    assert parse_entries([e])[0]["label"] == ""


# --- broker integration (pty fake firmware) ------------------------------------------------

_F = {"name": "f", "kind": "setting", "type": "float", "ranges": [{"min": 100.0, "max": 500.0}]}
FM_CAPS = [
    {**_F, "name": "frequency"},
    {**_F, "name": "tx_frequency"},
    {**_F, "name": "rx_frequency"},
    {"name": "tx_tone", "kind": "setting", "type": "string"},
    {"name": "rx_tone", "kind": "setting", "type": "string"},
    {"name": "bandwidth", "kind": "setting", "type": "string"},
    {"name": "volume", "kind": "setting", "type": "int"},
    {"name": "ptt", "kind": "action", "type": "bool"},
    {"name": "carrier", "kind": "telemetry", "type": "bool", "readonly": True},
]
FM = {"schema": 1, "module": "fm", "identity": {"type": "fm_transceiver"}, "capabilities": FM_CAPS}


class Collector:
    def __init__(self):
        self.sent = []

    async def __call__(self, msg):
        self.sent.append(msg)

    def scan_states(self):
        return [m for m in self.sent if m["type"] == "scan_state"]


def _broker(router=None):
    fw = FakeFirmware({"fm": FM})
    fw.start()
    col = Collector()
    b = Broker(
        col,
        transport_factory=lambda p: SlotControl(p, timeout=2.0),
        dead_man_timeout=5.0,
        virtual_modules=[router] if router else None,
        scan_poll_s=0.01,
    )
    b.set_inventory(
        [
            {
                "slot": 1,
                "control": fw.control_path,
                "modules": [{"id": "fm", "identity": FM["identity"], "capabilities": FM_CAPS}],
            }
        ]
    )
    return fw, b, col


def _scan_start(entries, listen_s=0.05, hang_s=0.05, scan_id="s1"):
    return {
        "v": 1,
        "type": "scan_start",
        "scan_id": scan_id,
        "entries": entries,
        "listen_s": listen_s,
        "hang_s": hang_s,
    }


def _sets(fw, cap):
    return [ln for ln in map(bytes.decode, fw.received) if f" set {cap} " in ln]


async def _wait(cond, timeout=3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timeout waiting for condition")
        await asyncio.sleep(0.005)


BENTRIES = [_entry("a", rx=145.6, tx=145.0), _entry("b", rx=145.7, tx=145.1)]


def test_broker_scans_and_reports_active():
    fw, b, col = _broker()
    fw.carrier = lambda: fw.state["fm"].get("rx_frequency") == "145.7"

    async def go():
        await b.handle(_scan_start(BENTRIES, listen_s=0.1))
        await _wait(lambda: any(m["state"] == "active" for m in col.scan_states()))
        active = [m for m in col.scan_states() if m["state"] == "active"][0]
        assert active["ref"] == "b" and active["slot"] == 1 and active["module"] == "fm"
        await b.handle({"v": 1, "type": "scan_control", "scan_id": "s1", "action": "stop"})
        assert col.scan_states()[-1]["state"] == "stopped"

    try:
        asyncio.run(go())
    finally:
        fw.stop()


def test_broker_tx_route_follows_active_entry():
    router = AudioRouterModule()
    fw, b, col = _broker(router)
    fw.carrier = True

    async def go():
        await b.handle(_scan_start(BENTRIES[:1]))
        await _wait(lambda: router.tx_route == {"slot": 1, "module": "fm"})
        # The route change is mirrored to the server like a tx_route command's state frame.
        assert any(
            m["type"] == "state"
            and m["slot"] == router.slot
            and m["values"] == {"tx_route": {"slot": 1, "module": "fm"}}
            for m in col.sent
        )
        await b.stop()

    try:
        asyncio.run(go())
    finally:
        fw.stop()


def test_broker_ptt_holds_scan_before_keying():
    fw, b, col = _broker()

    async def go():
        await b.handle(_scan_start(BENTRIES))
        await _wait(lambda: len(_sets(fw, "rx_frequency")) >= 3)
        await b.handle(
            {
                "v": 1,
                "type": "command",
                "request_id": "k1",
                "slot": 1,
                "module": "fm",
                "capability": "ptt",
                "op": "do",
                "value": True,
            }
        )
        held = [m for m in col.scan_states() if m["state"] == "held"]
        assert held and held[-1]["reason"] == "ptt"
        n = len(_sets(fw, "rx_frequency"))
        await asyncio.sleep(0.2)
        assert len(_sets(fw, "rx_frequency")) == n  # no retune while keyed
        await b.on_disconnect()

    try:
        asyncio.run(go())
    finally:
        fw.stop()


def test_broker_manual_set_holds_then_applies():
    fw, b, col = _broker()

    async def go():
        await b.handle(_scan_start(BENTRIES))
        await _wait(lambda: len(_sets(fw, "rx_frequency")) >= 2)
        await b.handle(
            {
                "v": 1,
                "type": "command",
                "request_id": "m1",
                "slot": 1,
                "module": "fm",
                "capability": "rx_frequency",
                "op": "set",
                "value": 433.5,
            }
        )
        assert [m for m in col.scan_states() if m["state"] == "held"][-1]["reason"] == "manual"
        await asyncio.sleep(0.2)
        # The manual value is the last word on the wire: the scanner never retunes after it.
        assert _sets(fw, "rx_frequency")[-1].endswith("433.5")
        assert fw.state["fm"]["rx_frequency"] == "433.5"
        await b.stop()

    try:
        asyncio.run(go())
    finally:
        fw.stop()


def test_broker_unrelated_set_does_not_hold():
    fw, b, col = _broker()

    async def go():
        await b.handle(_scan_start(BENTRIES))
        await _wait(lambda: len(_sets(fw, "rx_frequency")) >= 1)
        await b.handle(
            {
                "v": 1,
                "type": "command",
                "request_id": "v1",
                "slot": 1,
                "module": "fm",
                "capability": "volume",
                "op": "set",
                "value": 5,
            }
        )
        assert not any(m["state"] == "held" for m in col.scan_states())
        await b.stop()

    try:
        asyncio.run(go())
    finally:
        fw.stop()


def test_broker_apply_preset_holds_scan():
    fw, b, col = _broker()

    async def go():
        await b.handle(_scan_start(BENTRIES))
        await _wait(lambda: len(_sets(fw, "rx_frequency")) >= 1)
        await b.handle(
            {
                "v": 1,
                "type": "apply_preset",
                "request_id": "p1",
                "slot": 1,
                "module": "fm",
                "values": {"tx_frequency": 145.2, "rx_frequency": 145.8},
            }
        )
        assert [m for m in col.scan_states() if m["state"] == "held"][-1]["reason"] == "manual"
        await asyncio.sleep(0.2)
        assert fw.state["fm"]["rx_frequency"] == "145.8"
        await b.stop()

    try:
        asyncio.run(go())
    finally:
        fw.stop()


def test_disconnect_stops():
    fw, b, col = _broker()

    async def go():
        await b.handle(_scan_start(BENTRIES))
        await _wait(lambda: len(_sets(fw, "rx_frequency")) >= 1)
        await b.on_disconnect()
        assert col.scan_states()[-1]["state"] == "stopped"
        assert col.scan_states()[-1]["reason"] == "disconnect"
        n = len(fw.received)
        await asyncio.sleep(0.2)
        assert len(fw.received) == n  # nothing touches the line after a disconnect
        assert b.scanner.active_slots() == set()

    try:
        asyncio.run(go())
    finally:
        fw.stop()


def test_firmware_drain_stops_scan():
    fw, b, col = _broker()

    async def go():
        await b.handle(_scan_start(BENTRIES))
        await _wait(lambda: len(_sets(fw, "rx_frequency")) >= 1)
        slot_pause.pause(1, None)
        try:
            await _wait(lambda: not b.scanner.running)
        finally:
            slot_pause.resume(1)
        assert col.scan_states()[-1]["reason"] == "module_update"

    try:
        asyncio.run(go())
    finally:
        slot_pause._reset_for_tests()
        fw.stop()


def test_broker_invalid_scan_start_reports_stopped():
    fw, b, col = _broker()

    async def go():
        await b.handle(_scan_start([{"ref": "x"}]))
        assert col.scan_states() == [
            {
                "v": 1,
                "type": "scan_state",
                "scan_id": "s1",
                "state": "stopped",
                "index": 0,
                "ref": None,
                "slot": None,
                "module": None,
                "ts": col.scan_states()[0]["ts"],
                "reason": "invalid_scan",
            }
        ]
        await b.handle(_scan_start([]))
        assert col.scan_states()[-1]["reason"] == "empty_scan_list"
        assert not b.scanner.running
        # A frame without a usable scan_id is dropped without a reply.
        n = len(col.sent)
        await b.handle({**_scan_start(BENTRIES), "scan_id": ""})
        assert len(col.sent) == n

    try:
        asyncio.run(go())
    finally:
        fw.stop()


def test_broker_scan_update_routed():
    fw, b, col = _broker()

    async def go():
        await b.handle(_scan_start(BENTRIES[:1], listen_s=0.05))
        await _wait(lambda: len(_sets(fw, "rx_frequency")) >= 1)
        await b.handle({"v": 1, "type": "scan_update", "scan_id": "s1", "entries": BENTRIES[1:]})
        await _wait(lambda: fw.state["fm"].get("rx_frequency") == "145.7")
        await b.handle({"v": 1, "type": "scan_update", "scan_id": "s1", "entries": []})
        assert col.scan_states()[-1]["reason"] == "empty_scan_list"

    try:
        asyncio.run(go())
    finally:
        fw.stop()
