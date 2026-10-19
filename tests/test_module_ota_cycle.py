"""Module-OTA reconcile cycle (backend-agnostic) + crash marker (Task A3)."""

import hashlib
import json
import os

import pytest

from station_agent import module_ota, slot_pause
from station_agent.config import AgentConfig
from station_agent.module_ota import FlashOutcome, Marker, ModuleOta

IMAGE = b"FIRMWARE-IMAGE" * 64
SHA = hashlib.sha256(IMAGE).hexdigest()
UID = "uid-abc"
CHECK = "/api/v1/module-firmware/reconcile/check/"
COMMIT = "/api/v1/module-firmware/reconcile/commit/"
STATUS = "/api/v1/module-firmware/reconcile/7/status/"
DL = "/api/v1/module-firmware/releases/3/download/"


@pytest.fixture(autouse=True)
def _reset_pause():
    slot_pause._reset_for_tests()
    yield
    slot_pause._reset_for_tests()


@pytest.fixture(autouse=True)
def _all_backends_detected(monkeypatch):
    monkeypatch.setattr(module_ota, "detect_backends", lambda cfg: ["dfu", "sim"])


def _cfg(tmp_path):
    return AgentConfig(
        server_url="https://x",
        station_id=1,
        ed25519_key_path="/tmp/k",
        state_dir=str(tmp_path / "state"),
        module_fw_dir=str(tmp_path / "fw"),
        slot_dev_base=str(tmp_path / "dev"),
    )


def _instruction(**over):
    d = {
        "convergence_id": 7,
        "module_uid": UID,
        "slot": "1",
        "module_type": "fm_transceiver",
        "variant": "vhf",
        "target_version": "1.2.0",
        "artifact": "dfu",
        "download_url": DL,
        "checksum_sha256": SHA,
        "size_bytes": len(IMAGE),
    }
    d.update(over)
    return d


class Resp:
    def __init__(self, status_code=200, payload=None, body=b""):
        self.status_code = status_code
        self._payload = payload
        self._body = body
        self.headers = {}

    def json(self):
        return self._payload

    def iter_content(self, chunk_size):
        yield self._body

    def close(self):
        pass


class FakeHttp:
    def __init__(self, instruction=None, image=IMAGE, commit_code=200):
        self.instruction = instruction
        self.image = image
        self.commit_code = commit_code
        self.posts: list[tuple[str, dict | None]] = []
        self.gets: list[str] = []

    def request(self, method, path, json_data=None, timeout=30, stream=False, headers=None):
        if method == "GET":
            self.gets.append(path)
            return Resp(200, body=self.image)
        self.posts.append((path, json_data))
        if path == CHECK:
            if self.instruction is None:
                return Resp(204)
            return Resp(200, payload=self.instruction)
        if path == COMMIT:
            return Resp(self.commit_code, payload={})
        return Resp(200, payload={"status": "ok"})

    def statuses(self):
        return [b["status"] for p, b in self.posts if p.endswith("/status/")]

    def status_bodies(self):
        return [b for p, b in self.posts if p.endswith("/status/")]

    def commits(self):
        return [b for p, b in self.posts if p == COMMIT]


class FakeBackend:
    def __init__(self, name="dfu", settle_s=0.0, outcome=FlashOutcome.OK, recover=None):
        self.name = name
        self.settle_s = settle_s
        self.outcome = outcome
        self.recover_result = recover
        self.flash_calls: list[tuple[int, str]] = []
        self.after: list[tuple[int, bool]] = []
        self.recovered: list[Marker] = []
        self.on_flash = None

    def flash(self, slot, path, marker):
        self.flash_calls.append((slot, path))
        if self.on_flash:
            self.on_flash(slot, path, marker)
        return self.outcome

    def after_verify(self, slot, success):
        self.after.append((slot, success))

    def recover(self, marker):
        self.recovered.append(marker)
        return self.recover_result


class FakeControl:
    def __init__(self, busy=False, entry=None):
        self.busy = busy if callable(busy) else (lambda slot: busy)
        self.entry = entry
        self.events: list[tuple] = []
        self.refreshes = 0
        self.forced_refreshes = 0
        self.forgotten: list[int] = []

    def slot_busy(self, slot):
        return self.busy(slot)

    def last_inventory_entry(self, slot):
        return self.entry

    def send_event(self, slot, module, event, detail):
        self.events.append((slot, module, event, detail))

    def request_inventory_refresh(self, force=False):
        self.refreshes += 1
        if force:
            self.forced_refreshes += 1

    def forget_slot(self, slot):
        self.forgotten.append(slot)


class FakeAudio:
    def __init__(self, active=False):
        self.active = active

    def slot_active(self, slot):
        return self.active


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _ident(version="1.0.0", uid=UID, variant="vhf"):
    return {"type": "fm_transceiver", "uid": uid, "variant": variant, "version": version}


def _entry(version="1.0.0"):
    return {
        "slot": 1,
        "control": "/dev/oe5xrx/slot1/control",
        "modules": [{"id": "fm", "identity": _ident(version), "capabilities": []}],
    }


def _make(
    tmp_path,
    *,
    http=None,
    backend=None,
    versions=None,
    control=None,
    audio=None,
    clock=None,
):
    """versions: list consumed by read_identity per call (last repeats); None = absent."""
    seq = list(versions if versions is not None else ["1.0.0", "1.2.0"])
    calls = []
    clock = clock or Clock()

    def read_identity(config, slot, module_uid, timeout=3.0):
        calls.append((slot, module_uid, clock()))
        v = seq.pop(0) if len(seq) > 1 else seq[0]
        if v is None:
            return None
        if isinstance(v, dict):
            return v
        return _ident(v)

    http = http or FakeHttp(_instruction())
    backend = backend or FakeBackend()
    ota = ModuleOta(
        _cfg(tmp_path),
        http,
        control=control if control is not None else FakeControl(entry=_entry()),
        audio=audio if audio is not None else FakeAudio(),
        backends={backend.name: backend},
        read_identity=read_identity,
        clock=clock,
        sleep=clock.sleep,
    )
    return ota, http, backend, calls


# --- happy path -----------------------------------------------------------------


def test_success_path_posts_downloading_flashing_verifying_then_commit(tmp_path):
    ota, http, backend, _ = _make(tmp_path)
    assert ota.run_once() == "success"
    assert http.statuses() == ["downloading", "flashing", "verifying"]
    assert http.commits() == [{"convergence_id": 7, "version": "1.2.0"}]
    assert http.gets == [DL]
    assert len(backend.flash_calls) == 1
    slot, path = backend.flash_calls[0]
    assert slot == 1
    assert path == os.path.join(str(tmp_path / "fw"), "conv7-1.2.0.bin")
    assert backend.after == [(1, True)]
    assert not os.path.exists(path)


def test_sim_artifact_url_passed_verbatim_and_native_sim_suffix(tmp_path):
    url = DL + "?artifact=sim"
    http = FakeHttp(_instruction(artifact="sim", download_url=url))
    backend = FakeBackend(name="sim")
    ota, http, backend, _ = _make(tmp_path, http=http, backend=backend)
    assert ota.run_once() == "success"
    assert http.gets == [url]
    assert backend.flash_calls[0][1].endswith("conv7-1.2.0.native_sim")


def test_no_instruction_no_posts(tmp_path):
    ota, http, backend, calls = _make(tmp_path, http=FakeHttp(None))
    assert ota.run_once() is None
    assert http.posts == [(CHECK, {})]
    assert backend.flash_calls == []
    assert calls == []


def test_no_backends_detected_does_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(module_ota, "detect_backends", lambda cfg: [])
    ota, http, _, _ = _make(tmp_path)
    assert ota.run_once() is None
    assert http.posts == []


def test_default_backends_empty_when_nothing_detected(tmp_path, monkeypatch):
    monkeypatch.setattr(module_ota, "detect_backends", lambda cfg: [])
    ota = ModuleOta(_cfg(tmp_path), FakeHttp(_instruction()))
    assert ota.backends == {}


@pytest.mark.parametrize(
    "over",
    [
        {"download_url": "https://evil.example/fw.bin"},
        {"download_url": "/api/v1/other/fw.bin"},
        {"convergence_id": "x"},
        {"slot": "abc"},
        {"checksum_sha256": ""},
        {"target_version": ""},
        {"module_uid": ""},
    ],
)
def test_malformed_instruction_dropped_without_status(tmp_path, over):
    ota, http, backend, _ = _make(tmp_path, http=FakeHttp(_instruction(**over)))
    assert ota.run_once() is None
    assert http.posts == [(CHECK, {})]
    assert backend.flash_calls == []


# --- pre-flash classification ------------------------------------------------------


def test_uid_mismatch_never_flashes(tmp_path):
    ota, http, backend, _ = _make(tmp_path, versions=[_ident(uid="someone-else")])
    assert ota.run_once() == "failed"
    assert backend.flash_calls == []
    assert http.statuses() == ["failed"]
    assert "not found" in http.status_bodies()[0]["error_message"]
    assert http.gets == []


def test_slot_absent_failed(tmp_path):
    ota, http, backend, _ = _make(tmp_path, versions=[None])
    assert ota.run_once() == "failed"
    assert backend.flash_calls == []


def test_variant_mismatch_rejected(tmp_path):
    ota, http, backend, _ = _make(tmp_path, versions=[_ident(variant="uhf")])
    assert ota.run_once() == "rejected"
    assert http.statuses() == ["rejected"]
    assert backend.flash_calls == []


def test_artifact_backend_missing_is_transient_not_rejected(tmp_path):
    """An agent-local missing backend is no artifact/module mismatch: never quarantine."""
    http = FakeHttp(_instruction(artifact="sim"))
    ota, http, backend, _ = _make(tmp_path, http=http)  # only "dfu" registered
    assert ota.run_once() == "failed"
    assert http.statuses() == ["failed"]
    assert "unavailable" in http.status_bodies()[0]["error_message"]
    assert backend.flash_calls == []


def test_artifact_registered_but_not_detected_is_transient(tmp_path, monkeypatch):
    monkeypatch.setattr(module_ota, "detect_backends", lambda cfg: ["sim"])
    dfu, sim = FakeBackend("dfu"), FakeBackend("sim")
    http = FakeHttp(_instruction())  # dfu instruction; dfu registered but not detected
    ota = ModuleOta(
        _cfg(tmp_path),
        http,
        control=FakeControl(entry=_entry()),
        audio=FakeAudio(),
        backends={"dfu": dfu, "sim": sim},
        read_identity=lambda *a, **k: _ident(),
    )
    assert ota.run_once() == "failed"
    assert http.statuses() == ["failed"]
    assert dfu.flash_calls == [] and sim.flash_calls == []


def test_backend_detected_after_construction_is_built_lazily(tmp_path, monkeypatch):
    detected = []
    monkeypatch.setattr(module_ota, "detect_backends", lambda cfg: list(detected))
    ota = ModuleOta(_cfg(tmp_path), FakeHttp(None))
    assert ota.backends == {} and ota.advertised_backends() == []
    detected.append("sim")  # sim_dir appeared after agent start
    assert ota.advertised_backends() == ["sim"]
    assert ota.run_once() is None  # no instruction; backends re-synced
    assert set(ota.backends) == {"sim"}
    from station_agent.module_ota_sim import SimBackend

    assert isinstance(ota.backends["sim"], SimBackend)
    built = ota.backends["sim"]
    detected.clear()  # sim_dir vanished again
    assert ota.advertised_backends() == []
    ota.run_once()
    assert ota.backends == {}
    detected.append("sim")
    ota.run_once()
    assert ota.backends["sim"] is built  # cached instance reused


def test_injected_backends_are_not_extended(tmp_path):
    # Tests / callers that inject backends get exactly those (intersected with detection).
    ota, _, _, _ = _make(tmp_path)
    assert ota.advertised_backends() == ["dfu"]


def test_already_on_target_commits_without_flash(tmp_path):
    control = FakeControl(entry=_entry())
    ota, http, backend, _ = _make(tmp_path, versions=["1.2.0"], control=control)
    assert ota.run_once() == "success"
    assert backend.flash_calls == []
    assert http.statuses() == []
    assert http.commits() == [{"convergence_id": 7, "version": "1.2.0"}]
    assert control.events == []


# --- idle gate ------------------------------------------------------------------


def test_busy_ptt_skips_without_status(tmp_path):
    control = FakeControl(busy=True, entry=_entry())
    ota, http, backend, _ = _make(tmp_path, control=control)
    assert ota.run_once() == "busy"
    assert http.posts == [(CHECK, {})]
    assert backend.flash_calls == []
    assert control.events == []
    assert not slot_pause.is_paused(1)


def test_busy_audio_skips_without_status(tmp_path):
    ota, http, backend, _ = _make(tmp_path, audio=FakeAudio(active=True))
    assert ota.run_once() == "busy"
    assert http.posts == [(CHECK, {})]
    assert backend.flash_calls == []
    assert not slot_pause.is_paused(1)


def test_busy_after_pause_resumes_and_skips(tmp_path):
    answers = iter([False, True])
    seen_paused = []

    def busy(slot):
        seen_paused.append(slot_pause.is_paused(slot))
        return next(answers)

    control = FakeControl(busy=busy, entry=_entry())
    ota, http, backend, _ = _make(tmp_path, control=control)
    assert ota.run_once() == "busy"
    assert seen_paused == [False, True]  # re-check happens while paused
    assert not slot_pause.is_paused(1)
    assert http.posts == [(CHECK, {})]
    assert control.events == []
    assert backend.flash_calls == []


def test_busy_check_raising_after_pause_resumes_slot(tmp_path):
    calls = []

    def busy(slot):
        calls.append(slot)
        if len(calls) > 1:
            raise RuntimeError("audio client exploded")
        return False

    control = FakeControl(busy=busy, entry=_entry())
    ota, _, backend, _ = _make(tmp_path, control=control)
    with pytest.raises(RuntimeError):
        ota.run_once()
    assert not slot_pause.is_paused(1)
    assert backend.flash_calls == []


def test_exception_after_pause_before_flash_resumes_slot(tmp_path):
    control = FakeControl(entry=_entry())
    ota, http, backend, _ = _make(tmp_path, control=control)
    real_event = ota._event

    def event(slot, module_id, name, detail):
        if name == "firmware_update_started":
            raise RuntimeError("event path exploded")
        real_event(slot, module_id, name, detail)

    ota._event = event
    assert ota.run_once() == "failed"
    assert not slot_pause.is_paused(1)
    assert backend.flash_calls == []
    assert Marker.load(str(tmp_path / "state")) is None


def test_pause_barrier_takes_devlock(tmp_path, monkeypatch):
    taken = []
    real = module_ota.devlock.control_device_lock

    def spy(path, **kw):
        taken.append((path, slot_pause.is_paused(1)))
        return real(path, **kw)

    monkeypatch.setattr(module_ota.devlock, "control_device_lock", spy)
    ota, _, _, _ = _make(tmp_path)
    assert ota.run_once() == "success"
    assert taken == [(str(tmp_path / "dev" / "slot1" / "control"), True)]


# --- download -------------------------------------------------------------------


def test_sha_mismatch_failed_and_file_removed(tmp_path):
    http = FakeHttp(_instruction(), image=b"X" * len(IMAGE))
    control = FakeControl(entry=_entry())
    ota, http, backend, _ = _make(tmp_path, http=http, control=control)
    assert ota.run_once() == "failed"
    assert http.statuses() == ["downloading", "failed"]
    assert backend.flash_calls == []
    assert os.listdir(tmp_path / "fw") == []
    assert not slot_pause.is_paused(1)
    assert control.events[-1][2] == "firmware_update_finished"
    assert control.events[-1][3]["result"] == "failed"
    # Fast pre-flash failure: the end-of-cycle refresh is forced (the debounced inventory
    # may never have changed, yet the server needs a post-finish frame).
    assert control.forced_refreshes == 1


# --- verify ---------------------------------------------------------------------


def test_old_version_after_settle_rolled_back(tmp_path):
    backend = FakeBackend(settle_s=45.0)
    ota, http, backend, _ = _make(tmp_path, backend=backend, versions=["1.0.0"])
    assert ota.run_once() == "rolled_back"
    assert http.statuses()[-1] == "rolled_back"
    assert "1.0.0" in http.status_bodies()[-1]["error_message"]
    assert http.commits() == []
    assert backend.after == [(1, False)]


def test_version_before_settle_not_trusted(tmp_path):
    # live check, then target seen while still settling, then old after settle.
    backend = FakeBackend(settle_s=45.0)
    clock = Clock()
    seq = ["1.0.0", "1.2.0", "1.2.0", "1.2.0"] + ["1.0.0"] * 100
    ota, http, backend, calls = _make(tmp_path, backend=backend, versions=seq, clock=clock)
    assert ota.run_once() == "rolled_back"
    assert http.commits() == []


def test_target_after_settle_success(tmp_path):
    backend = FakeBackend(settle_s=45.0)
    seq = ["1.0.0"] + [None] * 3 + ["1.2.0"]
    ota, http, backend, calls = _make(tmp_path, backend=backend, versions=seq)
    assert ota.run_once() == "success"
    flash_done = calls[1][2]
    assert calls[-1][2] - flash_done >= 45.0


def test_target_then_revert_within_confirm_window_rolled_back(tmp_path):
    """Trial image fails the module's health gate: target read once, then the old
    version within confirm_s -> rolled_back, never committed."""
    backend = FakeBackend(settle_s=45.0)
    backend.confirm_s = 40.0
    seq = ["1.0.0"] + [None] * 9 + ["1.2.0", "1.2.0"] + ["1.0.0"] * 100
    ota, http, backend, _ = _make(tmp_path, backend=backend, versions=seq)
    assert ota.run_once() == "rolled_back"
    assert http.commits() == []
    assert "1.0.0" in http.status_bodies()[-1]["error_message"]
    assert backend.after == [(1, False)]


def test_stable_target_through_confirm_window_commits(tmp_path):
    backend = FakeBackend(settle_s=45.0)
    backend.confirm_s = 40.0
    seq = ["1.0.0"] + [None] * 9 + ["1.2.0"]
    ota, http, backend, calls = _make(tmp_path, backend=backend, versions=seq)
    assert ota.run_once() == "success"
    assert http.commits() == [{"convergence_id": 7, "version": "1.2.0"}]
    first_target = next(t for (_, _, t), v in zip(calls, seq, strict=False) if v == "1.2.0")
    assert calls[-1][2] - first_target >= 40.0
    assert backend.after == [(1, True)]


def test_target_gone_before_confirm_until_deadline_rolled_back(tmp_path):
    backend = FakeBackend(settle_s=45.0)
    backend.confirm_s = 40.0
    seq = ["1.0.0"] + [None] * 9 + ["1.2.0"] + [None] * 100
    ota, http, backend, _ = _make(tmp_path, backend=backend, versions=seq)
    assert ota.run_once() == "rolled_back"
    assert http.commits() == []
    assert "confirmed" in http.status_bodies()[-1]["error_message"]


@pytest.mark.parametrize("nones", [35, 36])  # first target read at t=175 / t=180 (deadline)
def test_late_first_target_read_still_gets_full_confirm_window(tmp_path, nones):
    """Slow re-enumeration: the first trusted target read lands just before / at the
    verify deadline -> the confirm window still runs its full confirm_s."""
    backend = FakeBackend(settle_s=45.0)
    backend.confirm_s = 40.0
    seq = ["1.0.0"] + [None] * nones + ["1.2.0"]
    ota, http, backend, calls = _make(tmp_path, backend=backend, versions=seq)
    assert ota.run_once() == "success"
    flash_done = calls[1][2]
    first_target = calls[nones + 1][2]
    assert first_target - flash_done >= module_ota.VERIFY_TIMEOUT_S - module_ota.VERIFY_POLL_S
    assert calls[-1][2] - first_target >= 40.0
    assert http.commits() == [{"convergence_id": 7, "version": "1.2.0"}]


def test_late_first_target_read_then_revert_rolled_back(tmp_path):
    backend = FakeBackend(settle_s=45.0)
    backend.confirm_s = 40.0
    seq = ["1.0.0"] + [None] * 35 + ["1.2.0", "1.2.0"] + ["1.0.0"] * 100
    ota, http, backend, _ = _make(tmp_path, backend=backend, versions=seq)
    assert ota.run_once() == "rolled_back"
    assert http.commits() == []
    assert "1.0.0" in http.status_bodies()[-1]["error_message"]


def test_late_first_target_read_then_gone_rolled_back(tmp_path):
    backend = FakeBackend(settle_s=45.0)
    backend.confirm_s = 40.0
    seq = ["1.0.0"] + [None] * 35 + ["1.2.0"] + [None] * 100
    ota, http, backend, _ = _make(tmp_path, backend=backend, versions=seq)
    assert ota.run_once() == "rolled_back"
    assert http.commits() == []
    assert "confirmed" in http.status_bodies()[-1]["error_message"]


def test_late_first_target_read_probe_miss_at_window_end_still_commits(tmp_path):
    """First target read at t=175 -> confirm_until=215. A single unreadable probe
    exactly at the window end must not turn the good flash into rolled_back: the
    next read (within CONFIRM_GRACE_S) is the target -> commit."""
    backend = FakeBackend(settle_s=45.0)
    backend.confirm_s = 40.0
    seq = ["1.0.0"] + [None] * 35 + ["1.2.0"] + [None] * 7 + [None] + ["1.2.0"]
    ota, http, backend, calls = _make(tmp_path, backend=backend, versions=seq)
    assert ota.run_once() == "success"
    first_target = calls[36][2]
    assert calls[-2][2] - first_target == 40.0  # the miss landed on confirm_until
    assert http.commits() == [{"convergence_id": 7, "version": "1.2.0"}]
    assert backend.after == [(1, True)]


def test_late_first_target_grace_is_bounded(tmp_path):
    """No trusted read within CONFIRM_GRACE_S after the window -> rolled_back."""
    backend = FakeBackend(settle_s=45.0)
    backend.confirm_s = 40.0
    seq = ["1.0.0"] + [None] * 35 + ["1.2.0"] + [None] * 100
    ota, http, backend, calls = _make(tmp_path, backend=backend, versions=seq)
    assert ota.run_once() == "rolled_back"
    first_target = calls[36][2]
    assert calls[-1][2] - first_target == 40.0 + module_ota.CONFIRM_GRACE_S


def test_dfu_and_sim_confirm_windows():
    from station_agent.module_ota_dfu import DfuBackend
    from station_agent.module_ota_sim import SimBackend

    assert DfuBackend.confirm_s == module_ota.CONFIRM_S == 40.0
    assert SimBackend.confirm_s == 0.0
    assert module_ota.SETTLE_S + module_ota.CONFIRM_S < module_ota.VERIFY_TIMEOUT_S


def test_timeout_rolled_back(tmp_path):
    clock = Clock()
    ota, http, backend, calls = _make(tmp_path, versions=["1.0.0", None], clock=clock)
    start = clock()
    assert ota.run_once() == "rolled_back"
    assert "180" in http.status_bodies()[-1]["error_message"]
    assert backend.after == [(1, False)]
    assert 180.0 <= clock() - start <= 180.0 + module_ota.VERIFY_POLL_S + 1


def test_commit_409_reported_as_rolled_back(tmp_path):
    ota, http, backend, _ = _make(tmp_path, http=FakeHttp(_instruction(), commit_code=409))
    assert ota.run_once() == "rolled_back"
    assert len(http.commits()) == 1


# --- outcomes -------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome,result",
    [
        (FlashOutcome.REJECTED, "rejected"),
        (FlashOutcome.TRANSIENT, "failed"),
        (FlashOutcome.ROLLED_BACK, "rolled_back"),
        (FlashOutcome.DFU_STUCK, "rolled_back"),
    ],
)
def test_flash_outcome_mapping(tmp_path, outcome, result):
    control = FakeControl(entry=_entry())
    ota, http, backend, _ = _make(tmp_path, backend=FakeBackend(outcome=outcome), control=control)
    assert ota.run_once() == result
    assert http.statuses() == ["downloading", "flashing", result]
    names = [e[2] for e in control.events]
    if outcome is FlashOutcome.DFU_STUCK:
        assert "firmware_dfu_stuck" in names
        stuck = [e for e in control.events if e[2] == "firmware_dfu_stuck"][0]
        assert stuck[3] == {"msg": module_ota.DFU_STUCK_MSG}
        assert http.status_bodies()[-1]["error_message"] == module_ota.DFU_STUCK_MSG
        # The stale debounced identity is dropped so the post-resume emit omits the
        # slot instead of telling the server the module is back (review #168 r4).
        assert control.forgotten == [1]
    else:
        assert "firmware_dfu_stuck" not in names
        assert control.forgotten == []


# --- pause / events / marker ---------------------------------------------------------


def test_slot_paused_during_flash_and_resumed_after(tmp_path):
    seen = {}
    backend = FakeBackend()

    def on_flash(slot, path, marker):
        seen["paused"] = slot_pause.is_paused(slot)
        seen["entry"] = slot_pause.updating_entry(slot)

    backend.on_flash = on_flash
    ota, _, _, _ = _make(tmp_path, backend=backend)
    assert ota.run_once() == "success"
    assert seen["paused"] is True
    assert seen["entry"]["modules"][0]["status"] == "updating"
    assert not slot_pause.is_paused(1)


def test_slot_resumed_and_marker_cleared_on_exception(tmp_path):
    backend = FakeBackend()

    def boom(slot, path, marker):
        raise RuntimeError("kaboom")

    backend.on_flash = boom
    control = FakeControl(entry=_entry())
    ota, http, _, _ = _make(tmp_path, backend=backend, control=control)
    assert ota.run_once() == "failed"
    assert not slot_pause.is_paused(1)
    assert Marker.load(str(tmp_path / "state")) is None
    assert http.statuses()[-1] == "failed"
    assert control.events[-1][3]["result"] == "failed"
    assert os.listdir(tmp_path / "fw") == []


@pytest.mark.parametrize("entry", [None, {"slot": 1, "modules": []}])
def test_no_inventory_entry_for_module_is_busy(tmp_path, entry):
    """No last-known entry (or none carrying the module): never pause with a ghost
    module id ''. Busy, no status POST, retry next cycle."""
    control = FakeControl(entry=entry)
    ota, http, backend, _ = _make(tmp_path, control=control)
    assert ota.run_once() == "busy"
    assert http.posts == [(CHECK, {})]
    assert backend.flash_calls == []
    assert control.events == []
    assert not slot_pause.is_paused(1)


def test_events_started_finished(tmp_path):
    control = FakeControl(entry=_entry())
    ota, _, _, _ = _make(tmp_path, control=control)
    assert ota.run_once() == "success"
    assert control.events == [
        (1, "fm", "firmware_update_started", {"target_version": "1.2.0", "convergence_id": 7}),
        (1, "fm", "firmware_update_finished", {"result": "success", "target_version": "1.2.0"}),
    ]
    assert control.refreshes == 2
    assert control.forced_refreshes == 1  # only the end-of-cycle refresh is forced


def test_marker_written_before_flash_and_cleared_after(tmp_path):
    seen = {}
    backend = FakeBackend()
    state = str(tmp_path / "state")

    def on_flash(slot, path, marker):
        loaded = Marker.load(state)
        seen["marker"] = loaded
        seen["file_exists"] = os.path.exists(path)

    backend.on_flash = on_flash
    ota, _, _, _ = _make(tmp_path, backend=backend)
    assert ota.run_once() == "success"
    m = seen["marker"]
    assert m.phase == "flashing"
    assert (m.convergence_id, m.slot, m.module_uid) == (7, 1, UID)
    assert (m.target_version, m.old_version, m.artifact) == ("1.2.0", "1.0.0", "dfu")
    assert seen["file_exists"] is True
    assert Marker.load(state) is None


# --- recovery -------------------------------------------------------------------


def _marker(tmp_path, phase, **kw):
    fw = tmp_path / "fw"
    fw.mkdir(exist_ok=True)
    path = fw / "conv7-1.2.0.bin"
    path.write_bytes(IMAGE)
    m = Marker(
        convergence_id=7,
        slot=1,
        module_uid=UID,
        target_version="1.2.0",
        old_version="1.0.0",
        artifact="dfu",
        path=str(path),
        phase=phase,
        snapshot=_entry(),
        state_dir=str(tmp_path / "state"),
        **kw,
    )
    m.save()
    return m


def test_marker_roundtrip(tmp_path):
    m = _marker(tmp_path, "flashing", dfu_attempts=1)
    loaded = Marker.load(str(tmp_path / "state"))
    assert loaded == m
    m.clear()
    assert Marker.load(str(tmp_path / "state")) is None


def test_recover_phase_downloaded_reports_failed_not_rolled_back(tmp_path):
    m = _marker(tmp_path, "downloaded")
    control = FakeControl(entry=None)
    ota, http, backend, _ = _make(tmp_path, versions=["1.0.0"], control=control)
    assert ota.recover_on_startup() == "failed"
    assert http.statuses() == ["failed"]
    assert "restarted before flashing" in http.status_bodies()[0]["error_message"]
    assert backend.flash_calls == [] and backend.recovered == []
    assert Marker.load(str(tmp_path / "state")) is None
    assert not os.path.exists(m.path)
    assert not slot_pause.is_paused(1)
    assert control.events[-1][2] == "firmware_update_finished"
    assert control.forced_refreshes == 1  # marker recovery also forces the post-finish emit


def test_recover_phase_downloaded_on_target_commits(tmp_path):
    _marker(tmp_path, "downloaded")
    ota, http, _, _ = _make(tmp_path, versions=["1.2.0"])
    assert ota.recover_on_startup() == "success"
    assert http.commits() == [{"convergence_id": 7, "version": "1.2.0"}]


def test_recover_phase_flashing_classifies_target_as_success(tmp_path):
    _marker(tmp_path, "flashing")
    seen = {}
    ota, http, backend, _ = _make(tmp_path, versions=["1.2.0"])
    backend.recover_result = None
    orig = backend.recover

    def rec(marker):
        seen["paused"] = slot_pause.is_paused(1)
        return orig(marker)

    backend.recover = rec
    assert ota.recover_on_startup() == "success"
    assert seen["paused"] is True
    assert len(backend.recovered) == 1
    assert http.commits() == [{"convergence_id": 7, "version": "1.2.0"}]
    assert backend.after == [(1, True)]
    assert Marker.load(str(tmp_path / "state")) is None
    assert not slot_pause.is_paused(1)


def test_recover_phase_flashing_backend_outcome_reported(tmp_path):
    _marker(tmp_path, "flashing")
    backend = FakeBackend(recover=FlashOutcome.DFU_STUCK)
    control = FakeControl(entry=None)
    ota, http, _, _ = _make(tmp_path, backend=backend, control=control)
    assert ota.recover_on_startup() == "rolled_back"
    assert "firmware_dfu_stuck" in [e[2] for e in control.events]
    assert control.events[0][1] == "fm"  # module id from the marker's snapshot
    assert control.forgotten == [1]


def test_recover_barrier_timeout_keeps_marker(tmp_path):
    """A busy control device during recovery must not lose the crash marker (DFU-stuck)."""
    m = _marker(tmp_path, "flashing")
    control = FakeControl(entry=None)
    ota, http, backend, _ = _make(tmp_path, control=control)

    def barrier(slot):
        raise TimeoutError

    ota._barrier = barrier
    assert ota.recover_on_startup() == "busy"
    assert Marker.load(str(tmp_path / "state")) == m
    assert os.path.exists(m.path)
    assert not slot_pause.is_paused(1)
    assert http.posts == []
    assert backend.recovered == []
    assert "firmware_update_finished" not in [e[2] for e in control.events]
    # Next cycle the barrier is free again: recovery completes normally.
    del ota._barrier
    backend.recover_result = None
    ota_result = ota.run_once()
    assert ota_result in ("success", "rolled_back")
    assert Marker.load(str(tmp_path / "state")) is None


def test_recover_barrier_crash_resumes_slot(tmp_path):
    """Any exception after the recovery pause still resumes the slot (no stuck pause)."""
    _marker(tmp_path, "flashing")
    control = FakeControl(entry=None)
    ota, _, backend, _ = _make(tmp_path, control=control)

    def barrier(slot):
        raise RuntimeError("devlock exploded")

    ota._barrier = barrier
    assert ota.recover_on_startup() == "failed"
    assert not slot_pause.is_paused(1)
    assert backend.recovered == []


def test_run_once_recovers_marker_before_check(tmp_path):
    _marker(tmp_path, "downloaded")
    ota, http, _, _ = _make(tmp_path, versions=["1.0.0"])
    assert ota.run_once() == "failed"
    assert CHECK not in [p for p, _ in http.posts]


def test_recover_discards_marker_with_path_outside_module_fw_dir(tmp_path):
    """ModuleOta loads the marker with module_fw_dir: a tampered marker pointing at an
    arbitrary file is discarded before any recovery/flash runs."""
    m = _marker(tmp_path, "flashing")
    m.path = "/etc/passwd"
    m.save()
    ota, http, backend, _ = _make(tmp_path)
    assert ota.recover_on_startup() is None
    assert backend.recovered == [] and backend.flash_calls == []
    assert http.posts == []
    assert Marker.load(str(tmp_path / "state")) is None  # deleted


def test_recover_without_marker_noop(tmp_path):
    ota, http, _, _ = _make(tmp_path)
    assert ota.recover_on_startup() is None
    assert http.posts == []


def test_corrupt_marker_ignored(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "module_ota.json").write_text("{not json")
    assert Marker.load(str(state)) is None
    assert not (state / "module_ota.json").exists()
    (state / "module_ota.json").write_text(json.dumps({"convergence_id": 1}))
    assert Marker.load(str(state)) is None
    ota, http, _, _ = _make(tmp_path, http=FakeHttp(None))
    assert ota.recover_on_startup() is None


def test_status_post_failure_is_best_effort(tmp_path):
    class FlakyHttp(FakeHttp):
        def request(self, method, path, **kw):
            if method == "POST" and path.endswith("/status/"):
                self.posts.append((path, kw.get("json_data")))
                return None
            return super().request(method, path, **kw)

    ota, http, _, _ = _make(tmp_path, http=FlakyHttp(_instruction()))
    assert ota.run_once() == "success"
    assert Marker.load(str(tmp_path / "state")) is None


def test_read_slot_identity_matches_uid(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    seen = {}

    def probe(path, timeout=3.0):
        seen["args"] = (path, timeout)
        return [
            {"id": "a", "identity": {"uid": "other", "version": "9"}, "capabilities": []},
            {"id": "fm", "identity": _ident("1.0.0"), "capabilities": []},
        ]

    monkeypatch.setattr(module_ota.slot_discovery, "probe_slot", probe)
    assert module_ota.read_slot_identity(cfg, 1, UID)["version"] == "1.0.0"
    assert seen["args"] == (str(tmp_path / "dev" / "slot1" / "control"), 3.0)
    assert module_ota.read_slot_identity(cfg, 1, "missing") is None
    monkeypatch.setattr(module_ota.slot_discovery, "probe_slot", lambda p, timeout=3.0: None)
    assert module_ota.read_slot_identity(cfg, 1, UID) is None
