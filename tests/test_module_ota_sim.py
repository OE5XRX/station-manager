"""native_sim backend for module OTA per the linux-image contract (Task A5).

``D = config.sim_dir`` is a tmp dir; ``systemctl`` is a fake runner that records its argv and
snapshots the directory state at every restart; ``os.chown`` is wrapped (``_chown_root``) and
monkeypatched because the tests do not run as root.
"""

import hashlib
import os
import subprocess

import pytest

from station_agent import module_ota, module_ota_sim, slot_pause
from station_agent.config import AgentConfig
from station_agent.module_ota import FlashOutcome, Marker, ModuleOta
from station_agent.module_ota_sim import SimBackend

UID = "uid-abc"
OLD = b"\x7fELF-old-native-sim" * 32
NEW = b"\x7fELF-new-native-sim" * 32
SVC = "oe5xrx-sim-harness"
FILES = ("native-sim-fm", "native-sim-fm.prev", "native-sim-fm.new", "native-sim-fm.rb", "pending")


@pytest.fixture(autouse=True)
def _reset_pause():
    slot_pause._reset_for_tests()
    yield
    slot_pause._reset_for_tests()


@pytest.fixture(autouse=True)
def chowns(monkeypatch):
    calls = []
    monkeypatch.setattr(module_ota_sim, "_chown_root", lambda fd: calls.append(fd))
    return calls


def _cfg(tmp_path):
    sim = tmp_path / "sim"
    sim.mkdir(exist_ok=True)
    return AgentConfig(
        server_url="https://x",
        station_id=1,
        ed25519_key_path="/tmp/k",
        state_dir=str(tmp_path / "state"),
        module_fw_dir=str(tmp_path / "fw"),
        slot_dev_base=str(tmp_path / "dev"),
        sim_dir=str(sim),
        sim_service=SVC,
    )


def _state(d):
    """{name: (inode, content)} of the contract files present in *d*."""
    out = {}
    for name in FILES:
        p = os.path.join(d, name)
        if os.path.lexists(p):
            with open(p, "rb") as fh:
                out[name] = (os.stat(p).st_ino, fh.read())
    return out


class FakeSystemctl:
    """Records argv + the sim dir state at each call; rc per call from ``rcs`` (last repeats)."""

    def __init__(self, sim_dir, rcs=(0,), raises=None):
        self.sim_dir = sim_dir
        self.rcs = list(rcs)
        self.raises = raises
        self.calls: list[list[str]] = []
        self.states: list[dict] = []
        self.kwargs: list[dict] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        self.kwargs.append(kwargs)
        self.states.append(_state(self.sim_dir))
        if self.raises is not None:
            raise self.raises
        rc = self.rcs.pop(0) if len(self.rcs) > 1 else self.rcs[0]
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr="boom" if rc else "")


def _present(config, slot, module_uid, timeout=3.0):
    return {"uid": module_uid, "version": "1.0.0", "variant": "vhf"}


def _source(tmp_path, data=NEW):
    fw = tmp_path / "fw"
    fw.mkdir(exist_ok=True)
    p = fw / "conv7-1.2.0.native_sim"
    p.write_bytes(data)
    return str(p)


SHA_NEW_IMAGE = hashlib.sha256(NEW).hexdigest()


def _marker(tmp_path, path="", checksum=SHA_NEW_IMAGE):
    return Marker(
        convergence_id=7,
        slot=1,
        module_uid=UID,
        target_version="1.2.0",
        old_version="1.0.0",
        artifact="sim",
        path=path or str(tmp_path / "fw" / "conv7-1.2.0.native_sim"),
        phase="flashing",
        checksum_sha256=checksum,
        state_dir=str(tmp_path / "state"),
    )


def _backend(tmp_path, rcs=(0,), raises=None, read_identity=_present):
    cfg = _cfg(tmp_path)
    run = FakeSystemctl(cfg.sim_dir, rcs=rcs, raises=raises)
    return SimBackend(cfg, run=run, read_identity=read_identity), run, cfg.sim_dir


def _install_old(d):
    p = os.path.join(d, "native-sim-fm")
    with open(p, "wb") as fh:
        fh.write(OLD)
    os.chmod(p, 0o755)
    return os.stat(p).st_ino


def _p(d, name):
    return os.path.join(d, name)


# --- flash ------------------------------------------------------------------------------


def test_backend_identity():
    assert SimBackend.name == "sim"
    assert SimBackend.settle_s == 0.0


def test_flash_order_prev_before_pending_before_rename(tmp_path, monkeypatch):
    backend, run, d = _backend(tmp_path)
    old_ino = _install_old(d)
    src = _source(tmp_path)

    ops = []
    real_link, real_replace, real_open = os.link, os.replace, os.open

    def link(a, b, *args, **kw):
        ops.append(("link", os.path.basename(a), os.path.basename(b)))
        return real_link(a, b, *args, **kw)

    def replace(a, b, *args, **kw):
        ops.append(("replace", os.path.basename(a), os.path.basename(b)))
        return real_replace(a, b, *args, **kw)

    def open_(path, flags, *args, **kw):
        if os.path.basename(str(path)) == "pending":
            ops.append(("touch", "pending"))
        return real_open(path, flags, *args, **kw)

    monkeypatch.setattr(module_ota_sim.os, "link", link)
    monkeypatch.setattr(module_ota_sim.os, "replace", replace)
    monkeypatch.setattr(module_ota_sim.os, "open", open_)

    assert backend.flash(1, src, _marker(tmp_path, src)) is FlashOutcome.OK

    prev_link = next(i for i, op in enumerate(ops) if op[0] == "link" and op[1] == "native-sim-fm")
    touch = ops.index(("touch", "pending"))
    install = ops.index(("replace", "native-sim-fm.new", "native-sim-fm"))
    assert prev_link < touch < install, ops

    # At the restart (step 5): .prev = old inode, pending set, live = new, .new gone.
    assert run.calls == [["systemctl", "restart", SVC]]
    at_restart = run.states[0]
    assert at_restart["native-sim-fm.prev"] == (old_ino, OLD)
    assert at_restart["native-sim-fm"][1] == NEW
    assert "pending" in at_restart
    assert "native-sim-fm.new" not in at_restart
    # Restart ran synchronously with a bounded timeout.
    assert run.kwargs[0].get("timeout")


def test_flash_installs_root_owned_0755(tmp_path, chowns):
    backend, _, d = _backend(tmp_path)
    src = _source(tmp_path)
    assert backend.flash(1, src, _marker(tmp_path, src)) is FlashOutcome.OK
    mode = os.stat(_p(d, "native-sim-fm")).st_mode & 0o7777
    assert mode == 0o755
    assert len(chowns) == 1 and isinstance(chowns[0], int)  # fd-based (fchown)
    # The downloaded source is left alone (ModuleOta removes it).
    assert os.path.isfile(src)


def test_flash_first_time_removes_stale_prev(tmp_path):
    backend, run, d = _backend(tmp_path)
    with open(_p(d, "native-sim-fm.prev"), "wb") as fh:
        fh.write(b"stale from before a factory reset")
    src = _source(tmp_path)
    assert backend.flash(1, src, _marker(tmp_path, src)) is FlashOutcome.OK
    assert "native-sim-fm.prev" not in run.states[0]
    assert not os.path.lexists(_p(d, "native-sim-fm.prev"))
    assert "pending" in run.states[0]


def test_prev_is_hardlink_same_inode(tmp_path):
    backend, _, d = _backend(tmp_path)
    old_ino = _install_old(d)
    with open(_p(d, "native-sim-fm.prev"), "wb") as fh:
        fh.write(b"older prev")
    src = _source(tmp_path)
    assert backend.flash(1, src, _marker(tmp_path, src)) is FlashOutcome.OK
    st = os.stat(_p(d, "native-sim-fm.prev"))
    assert st.st_ino == old_ino
    with open(_p(d, "native-sim-fm.prev"), "rb") as fh:
        assert fh.read() == OLD


def test_success_removes_pending_keeps_new_binary(tmp_path):
    backend, run, d = _backend(tmp_path)
    _install_old(d)
    src = _source(tmp_path)
    assert backend.flash(1, src, _marker(tmp_path, src)) is FlashOutcome.OK
    backend.after_verify(1, True)
    state = _state(d)
    assert "pending" not in state
    assert state["native-sim-fm"][1] == NEW
    assert state["native-sim-fm.prev"][1] == OLD
    assert len(run.calls) == 1  # no extra restart on success


def test_failure_rolls_back_to_prev(tmp_path):
    backend, run, d = _backend(tmp_path)
    _install_old(d)
    src = _source(tmp_path)
    assert backend.flash(1, src, _marker(tmp_path, src)) is FlashOutcome.OK
    backend.after_verify(1, False)
    state = _state(d)
    assert state["native-sim-fm"][1] == OLD
    assert state["native-sim-fm"][0] == state["native-sim-fm.prev"][0]  # .prev kept
    for name in ("pending", "native-sim-fm.new", "native-sim-fm.rb"):
        assert name not in state
    assert run.calls == [["systemctl", "restart", SVC]] * 2
    # R2 restart ran after R1 restored the old binary.
    assert run.states[1]["native-sim-fm"][1] == OLD
    assert "pending" in run.states[1]


def test_failure_without_prev_removes_override(tmp_path):
    backend, run, d = _backend(tmp_path)
    src = _source(tmp_path)
    assert backend.flash(1, src, _marker(tmp_path, src)) is FlashOutcome.OK
    backend.after_verify(1, False)
    assert _state(d) == {}
    assert len(run.calls) == 2
    assert "native-sim-fm" not in run.states[1]


def test_rollback_idempotent_rerun(tmp_path, monkeypatch):
    backend, run, d = _backend(tmp_path)
    _install_old(d)
    src = _source(tmp_path)
    assert backend.flash(1, src, _marker(tmp_path, src)) is FlashOutcome.OK

    # Interrupt after R1: the R2 restart "crashes" the agent.
    class Crash(BaseException):
        pass

    def crash(argv, **kw):
        raise Crash()

    backend._run = crash
    with pytest.raises(Crash):
        backend.rollback()
    after_r1 = _state(d)
    assert after_r1["native-sim-fm"][1] == OLD
    assert "pending" in after_r1  # still marked: the rollback will run again

    backend._run = run
    assert backend.rollback() is True
    assert backend.rollback() is True  # and again: still a no-op-safe re-run
    state = _state(d)
    assert state["native-sim-fm"][1] == OLD
    assert state["native-sim-fm.prev"][1] == OLD
    assert "pending" not in state and "native-sim-fm.rb" not in state


def test_rollback_replaces_stale_rb(tmp_path):
    backend, _, d = _backend(tmp_path)
    _install_old(d)
    os.link(_p(d, "native-sim-fm"), _p(d, "native-sim-fm.prev"))
    os.unlink(_p(d, "native-sim-fm"))  # .prev keeps the old inode
    with open(_p(d, "native-sim-fm"), "wb") as fh:
        fh.write(NEW)
    with open(_p(d, "native-sim-fm.rb"), "wb") as fh:
        fh.write(b"leftover rb")
    open(_p(d, "pending"), "w").close()
    assert backend.rollback() is True
    state = _state(d)
    assert state["native-sim-fm"][1] == OLD
    assert "native-sim-fm.rb" not in state and "pending" not in state


def test_pending_removed_last_in_rollback(tmp_path, monkeypatch):
    backend, _, d = _backend(tmp_path)
    _install_old(d)
    src = _source(tmp_path)
    assert backend.flash(1, src, _marker(tmp_path, src)) is FlashOutcome.OK
    # Leave R3 debris so every rm -f has something to do.
    with open(_p(d, "native-sim-fm.new"), "wb") as fh:
        fh.write(b"x")

    removed = []
    real_unlink = os.unlink

    def unlink(path, *args, **kw):
        removed.append(os.path.basename(str(path)))
        return real_unlink(path, *args, **kw)

    monkeypatch.setattr(module_ota_sim.os, "unlink", unlink)
    assert backend.rollback() is True
    assert removed[-1] == "pending"
    assert "native-sim-fm.new" in removed


def test_rollback_restart_failure_keeps_pending(tmp_path):
    backend, run, d = _backend(tmp_path, rcs=(0, 1))
    _install_old(d)
    src = _source(tmp_path)
    assert backend.flash(1, src, _marker(tmp_path, src)) is FlashOutcome.OK
    assert backend.rollback() is False
    assert os.path.exists(_p(d, "pending"))  # rollback will be re-run


# --- recover ----------------------------------------------------------------------------


def test_recover_with_pending_rolls_back(tmp_path):
    backend, run, d = _backend(tmp_path)
    _install_old(d)
    src = _source(tmp_path)
    assert backend.flash(1, src, _marker(tmp_path, src)) is FlashOutcome.OK
    # Agent crashes between steps 5 and 7; a fresh backend picks it up.
    fresh = SimBackend(_cfg(tmp_path), run=run, read_identity=_present)
    assert fresh.recover(_marker(tmp_path, src)) is FlashOutcome.ROLLED_BACK
    state = _state(d)
    assert state["native-sim-fm"][1] == OLD
    assert "pending" not in state
    assert len(run.calls) == 2


def test_recover_without_pending_falls_through(tmp_path):
    backend, run, d = _backend(tmp_path)
    _install_old(d)
    assert backend.recover(_marker(tmp_path)) is None
    assert run.calls == []
    assert _state(d)["native-sim-fm"][1] == OLD


def test_startup_rolls_back_sim_pending_without_marker(tmp_path):
    """Contract: ``$D/pending`` on agent startup → rollback, even with no crash marker."""
    backend, run, d = _backend(tmp_path)
    _install_old(d)
    src = _source(tmp_path)
    assert backend.flash(1, src, _marker(tmp_path, src)) is FlashOutcome.OK
    assert Marker.load(str(tmp_path / "state")) is None
    fresh = SimBackend(_cfg(tmp_path), run=run, read_identity=_present)
    ota = ModuleOta(_cfg(tmp_path), http_client=None, backends={"sim": fresh})
    assert ota.recover_on_startup() is None
    state = _state(d)
    assert state["native-sim-fm"][1] == OLD
    assert "pending" not in state
    assert len(run.calls) == 2  # flash restart + rollback restart


def test_startup_no_pending_no_rollback(tmp_path):
    backend, run, d = _backend(tmp_path)
    _install_old(d)
    before = _state(d)
    ota = ModuleOta(_cfg(tmp_path), http_client=None, backends={"sim": backend})
    assert ota.recover_on_startup() is None
    assert run.calls == []
    assert _state(d) == before


def test_startup_marker_recovery_runs_before_backend_hook(tmp_path, monkeypatch):
    # Hooks run for the backends detected at startup (re-synced like every cycle).
    monkeypatch.setattr(module_ota, "detect_backends", lambda cfg: ["dfu"])
    calls = []

    class Hooked:
        name = "dfu"
        settle_s = 0.0

        def startup_recover(self):
            calls.append("hook")

    ota = ModuleOta(_cfg(tmp_path), http_client=None, backends={"dfu": Hooked()})
    ota._finish_from_marker = lambda marker: calls.append("marker") or "failed"
    m = _marker(tmp_path)
    m.save()
    assert ota.recover_on_startup() == "failed"
    assert calls == ["marker", "hook"]


def test_startup_hook_exception_isolated(tmp_path):
    class Broken:
        def startup_recover(self):
            raise OSError("boom")

    backend, run, d = _backend(tmp_path)
    _install_old(d)
    src = _source(tmp_path)
    assert backend.flash(1, src, _marker(tmp_path, src)) is FlashOutcome.OK
    ota = ModuleOta(_cfg(tmp_path), http_client=None, backends={"dfu": Broken(), "sim": backend})
    assert ota.recover_on_startup() is None
    assert "pending" not in _state(d)


# --- transient failures -----------------------------------------------------------------


def test_link_failure_before_pending_is_transient(tmp_path, monkeypatch):
    """Any failure before step 3 → TRANSIENT, no pending, live override untouched."""
    backend, run, d = _backend(tmp_path)
    old_ino = _install_old(d)
    src = _source(tmp_path)
    real_link = os.link

    def failing_link(a, b, *args, **kw):
        if os.path.basename(a) == "native-sim-fm":
            raise OSError(28, "No space left on device")
        return real_link(a, b, *args, **kw)

    monkeypatch.setattr(module_ota_sim.os, "link", failing_link)
    assert backend.flash(1, src, _marker(tmp_path, src)) is FlashOutcome.TRANSIENT
    state = _state(d)
    assert "pending" not in state
    assert state["native-sim-fm"] == (old_ino, OLD)
    assert "native-sim-fm.new" not in state
    assert run.calls == []


def test_stale_pending_rollback_restart_failure_is_transient(tmp_path):
    """The pre-flash rollback of a stale pending fails at its restart → TRANSIENT, nothing new."""
    backend, run, d = _backend(tmp_path, rcs=(1,))
    old_ino = _install_old(d)
    os.link(_p(d, "native-sim-fm"), _p(d, "native-sim-fm.prev"))  # crash before step 4
    open(_p(d, "pending"), "w").close()
    src = _source(tmp_path)
    assert backend.flash(1, src, _marker(tmp_path, src)) is FlashOutcome.TRANSIENT
    state = _state(d)
    assert state["native-sim-fm"][0] == old_ino
    assert "pending" in state  # rollback not finished: still re-run next time
    assert "native-sim-fm.new" not in state
    assert len(run.calls) == 1


def test_restart_failure_after_pending_rolls_back(tmp_path):
    backend, run, d = _backend(tmp_path, rcs=(1, 0))
    _install_old(d)
    src = _source(tmp_path)
    assert backend.flash(1, src, _marker(tmp_path, src)) is FlashOutcome.ROLLED_BACK
    state = _state(d)
    assert state["native-sim-fm"][1] == OLD
    assert "pending" not in state
    assert len(run.calls) == 2


def test_restart_timeout_after_pending_rolls_back(tmp_path):
    backend, run, d = _backend(tmp_path, raises=subprocess.TimeoutExpired("systemctl", 60))
    _install_old(d)
    src = _source(tmp_path)
    assert backend.flash(1, src, _marker(tmp_path, src)) is FlashOutcome.ROLLED_BACK
    assert _state(d)["native-sim-fm"][1] == OLD
    # The rollback restart failed too: pending stays so the rollback is re-run.
    assert os.path.exists(_p(d, "pending"))


def test_verify_failure_without_pending_keeps_verified_override(tmp_path):
    """Crash before step 3 → marker recovery → verify fails: stale .prev must NOT go live."""
    backend, run, d = _backend(tmp_path)
    with open(_p(d, "native-sim-fm.prev"), "wb") as fh:
        fh.write(b"v0.9")
    with open(_p(d, "native-sim-fm"), "wb") as fh:
        fh.write(b"v1.0-verified")
    before = _state(d)
    assert backend.recover(_marker(tmp_path)) is None
    backend.after_verify(1, False)
    assert _state(d) == before
    assert run.calls == []


def test_verify_failure_without_pending_and_prev_keeps_override(tmp_path):
    backend, run, d = _backend(tmp_path)
    old_ino = _install_old(d)
    backend.after_verify(1, False)
    assert _state(d) == {"native-sim-fm": (old_ino, OLD)}
    assert run.calls == []


def test_symlink_at_new_is_not_followed(tmp_path):
    backend, _, d = _backend(tmp_path)
    outside = tmp_path / "outside"
    outside.write_bytes(b"do not touch")
    os.symlink(outside, _p(d, "native-sim-fm.new"))
    src = _source(tmp_path)
    assert backend.flash(1, src, _marker(tmp_path, src)) is FlashOutcome.OK
    assert outside.read_bytes() == b"do not touch"
    live = _p(d, "native-sim-fm")
    assert os.path.isfile(live) and not os.path.islink(live)
    with open(live, "rb") as fh:
        assert fh.read() == NEW


def test_symlink_live_is_hardlinked_not_followed(tmp_path):
    backend, _, d = _backend(tmp_path)
    outside = tmp_path / "outside"
    outside.write_bytes(b"target")
    os.symlink(outside, _p(d, "native-sim-fm"))
    src = _source(tmp_path)
    assert backend.flash(1, src, _marker(tmp_path, src)) is FlashOutcome.OK
    assert os.path.islink(_p(d, "native-sim-fm.prev"))  # the link itself, not its target
    assert os.stat(outside).st_nlink == 1


def test_wrong_module_guard_is_transient(tmp_path):
    backend, run, d = _backend(tmp_path, read_identity=lambda *a, **k: None)
    old_ino = _install_old(d)
    src = _source(tmp_path)
    assert backend.flash(1, src, _marker(tmp_path, src)) is FlashOutcome.TRANSIENT
    assert _state(d) == {"native-sim-fm": (old_ino, OLD)}
    assert run.calls == []


def test_copy_sha_mismatch_is_transient(tmp_path, monkeypatch):
    backend, run, d = _backend(tmp_path)
    old_ino = _install_old(d)
    src = _source(tmp_path)
    real = module_ota_sim._sha256_file

    def corrupt(path):
        digest = real(path)
        return "0" * 64 if path.endswith(".new") else digest

    monkeypatch.setattr(module_ota_sim, "_sha256_file", corrupt)
    assert backend.flash(1, src, _marker(tmp_path, src)) is FlashOutcome.TRANSIENT
    assert _state(d) == {"native-sim-fm": (old_ino, OLD)}
    assert run.calls == []


@pytest.mark.parametrize("checksum", ["", "0" * 64, "zz" * 32, None])
def test_stage_requires_server_pinned_checksum(tmp_path, checksum):
    """The staged copy is checked against the marker's server-pinned sha256, not a
    fresh hash of the source: an image that changed after the download check (or a
    missing/malformed pin) is never installed — TRANSIENT, ``.new`` removed."""
    backend, run, d = _backend(tmp_path)
    old_ino = _install_old(d)
    src = _source(tmp_path)
    assert backend.flash(1, src, _marker(tmp_path, src, checksum=checksum)) is (
        FlashOutcome.TRANSIENT
    )
    assert _state(d) == {"native-sim-fm": (old_ino, OLD)}
    assert run.calls == []


def test_stage_rejects_source_changed_after_download_check(tmp_path):
    backend, run, d = _backend(tmp_path)
    old_ino = _install_old(d)
    src = _source(tmp_path, data=b"\x7fELF-tampered" * 8)  # pin is still sha256(NEW)
    assert backend.flash(1, src, _marker(tmp_path, src)) is FlashOutcome.TRANSIENT
    assert _state(d) == {"native-sim-fm": (old_ino, OLD)}
    assert run.calls == []


def test_stage_accepts_uppercase_pin(tmp_path):
    backend, run, d = _backend(tmp_path)
    _install_old(d)
    src = _source(tmp_path)
    marker = _marker(tmp_path, src, checksum=SHA_NEW_IMAGE.upper())
    assert backend.flash(1, src, marker) is FlashOutcome.OK


def test_missing_source_is_transient(tmp_path):
    backend, run, d = _backend(tmp_path)
    assert backend.flash(1, str(tmp_path / "nope"), _marker(tmp_path)) is FlashOutcome.TRANSIENT
    assert _state(d) == {}
    assert run.calls == []


def test_chown_failure_is_transient(tmp_path, monkeypatch):
    def deny(fd):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(module_ota_sim, "_chown_root", deny)
    backend, run, d = _backend(tmp_path)
    src = _source(tmp_path)
    assert backend.flash(1, src, _marker(tmp_path, src)) is FlashOutcome.TRANSIENT
    assert _state(d) == {}


def test_stale_pending_is_rolled_back_before_next_flash(tmp_path):
    """A pending left by a failed rollback is resolved before a new flash refreshes .prev."""
    backend, run, d = _backend(tmp_path)
    _install_old(d)
    os.link(_p(d, "native-sim-fm"), _p(d, "native-sim-fm.prev"))
    os.unlink(_p(d, "native-sim-fm"))
    with open(_p(d, "native-sim-fm"), "wb") as fh:
        fh.write(b"unverified")
    os.chmod(_p(d, "native-sim-fm"), 0o755)
    open(_p(d, "pending"), "w").close()

    src = _source(tmp_path)
    assert backend.flash(1, src, _marker(tmp_path, src)) is FlashOutcome.OK
    # .prev must be the verified OLD, never the unverified binary.
    assert _state(d)["native-sim-fm.prev"][1] == OLD
    assert _state(d)["native-sim-fm"][1] == NEW


# --- registration + E2E with the A3 cycle -----------------------------------------------


def test_default_backends_registers_sim_when_detected(tmp_path, monkeypatch):
    seen = []

    def read_identity(config, slot, module_uid, timeout=3.0):
        seen.append(slot)
        return None

    monkeypatch.setattr(module_ota, "detect_backends", lambda cfg: ["sim"])
    ota = ModuleOta(_cfg(tmp_path), http_client=None, read_identity=read_identity)
    assert isinstance(ota.backends["sim"], SimBackend)
    assert "dfu" not in ota.backends
    assert ota.backends["sim"].flash(1, "/x", _marker(tmp_path)) is FlashOutcome.TRANSIENT
    assert seen == [1]


SHA_NEW = hashlib.sha256(NEW).hexdigest()
CHECK = "/api/v1/module-firmware/reconcile/check/"
COMMIT = "/api/v1/module-firmware/reconcile/commit/"
DL = "/api/v1/module-firmware/releases/3/download/?artifact=sim"


class _Resp:
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


class _Http:
    def __init__(self):
        self.posts = []

    def request(self, method, path, json_data=None, timeout=30, stream=False, headers=None):
        if method == "GET":
            return _Resp(200, body=NEW)
        self.posts.append((path, json_data))
        if path == CHECK:
            return _Resp(
                200,
                payload={
                    "convergence_id": 7,
                    "module_uid": UID,
                    "slot": "1",
                    "variant": "vhf",
                    "target_version": "1.2.0",
                    "artifact": "sim",
                    "download_url": DL,
                    "checksum_sha256": SHA_NEW,
                    "size_bytes": len(NEW),
                },
            )
        return _Resp(200, payload={})

    def statuses(self):
        return [b["status"] for p, b in self.posts if p.endswith("/status/")]


def _harness_identity(sim_dir):
    """The 'module' reports the version of whatever native_sim binary is installed."""

    def read_identity(config, slot, module_uid, timeout=3.0):
        live = os.path.join(sim_dir, "native-sim-fm")
        version = "1.0.0"
        if os.path.exists(live):
            with open(live, "rb") as fh:
                version = "1.2.0" if fh.read() == NEW else "1.0.0"
        return {"uid": module_uid, "version": version, "variant": "vhf"}

    return read_identity


class _E2eControl:
    """Control plane with a last-known inventory entry for slot 1 (pause snapshot)."""

    def slot_busy(self, slot):
        return False

    def last_inventory_entry(self, slot):
        ident = {"uid": UID, "version": "1.0.0", "variant": "vhf"}
        return {"slot": 1, "modules": [{"id": "fm", "identity": ident, "capabilities": []}]}

    def send_event(self, slot, module, event, detail):
        pass

    def request_inventory_refresh(self, force=False):
        pass


@pytest.mark.parametrize("comes_back", ["target", "old"])
def test_e2e_cycle_with_sim_backend(tmp_path, monkeypatch, comes_back):
    monkeypatch.setattr(module_ota, "detect_backends", lambda cfg: ["sim"])
    cfg = _cfg(tmp_path)
    d = cfg.sim_dir
    _install_old(d)
    run = FakeSystemctl(d)
    real_identity = _harness_identity(d)
    flashed = []

    def identity(config, slot, module_uid, timeout=3.0):
        ident = real_identity(config, slot, module_uid, timeout)
        if comes_back == "old" and flashed:
            ident = dict(ident, version="1.0.0")
        return ident

    backend = SimBackend(cfg, run=run, read_identity=identity)
    real_flash = backend.flash

    def flash(slot, path, marker):
        out = real_flash(slot, path, marker)
        flashed.append(out)
        return out

    backend.flash = flash
    http = _Http()
    ota = ModuleOta(
        cfg,
        http,
        control=_E2eControl(),
        backends={"sim": backend},
        read_identity=identity,
        sleep=lambda s: None,
    )
    result = ota.run_once()
    state = _state(d)
    assert "pending" not in state
    if comes_back == "target":
        assert result == "success"
        assert state["native-sim-fm"][1] == NEW
        assert [b for p, b in http.posts if p == COMMIT] == [
            {"convergence_id": 7, "version": "1.2.0"}
        ]
        assert len(run.calls) == 1
    else:
        assert result == "rolled_back"
        assert state["native-sim-fm"][1] == OLD
        assert len(run.calls) == 2
    assert http.statuses()[:3] == ["downloading", "flashing", "verifying"]
    assert os.listdir(cfg.module_fw_dir) == []
