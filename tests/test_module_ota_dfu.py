"""DFU backend for module OTA: port-path addressing + DFU-hang retry (Task A4).

``dfu-util`` is faked by an executable script in tmp_path that logs its argv (JSON lines)
and behaves per a JSON state file (download outcome sequence, whether ``-l`` sees DFU).
"""

import contextlib
import hashlib
import json
import logging
import os
import stat
import subprocess
import sys
import threading

import pytest

from station_agent import devlock, module_ota, module_ota_dfu, slot_pause
from station_agent.config import AgentConfig
from station_agent.module_ota import FlashOutcome, Marker, ModuleOta
from station_agent.module_ota_dfu import DfuBackend, parse_slots_conf

UID = "uid-abc"
PORT = "1-1.2"

FAKE_DFU = r"""#!{python}
import json, os, sys, time
state_path = os.environ["FAKE_DFU_STATE"]
with open(os.environ["FAKE_DFU_LOG"], "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\n")
with open(state_path) as fh:
    state = json.load(fh)
args = sys.argv[1:]
if "-e" in args:
    sys.exit(0)
if "-l" in args:
    if state.get("list_dfu"):
        print("Found DFU: [2fe3:0012] ver=0100, devnum=5, cfg=1, intf=0, path=\"1-1.2\"")
    else:
        print("dfu-util 0.11")
    sys.exit(0)
if "-D" in args:
    seq = state.get("download", ["ok"])
    outcome = seq.pop(0) if len(seq) > 1 else seq[0]
    state["download"] = seq
    with open(state_path, "w") as fh:
        json.dump(state, fh)
    if outcome == "ok":
        print("Download done.")
        sys.exit(0)
    if outcome == "hang":
        print("Error during download get_status", file=sys.stderr)
        sys.exit(74)
    if outcome == "dfuerror":
        print("Download\t[=====     ]  50%")
        print("DFU state(10) = dfuERROR, status(10) = Device's firmware is corrupt. "
              "It cannot return to run-time (non-DFU) operations", file=sys.stderr)
        sys.exit(74)
    if outcome == "clearing_hang":
        # Device left in dfuERROR by a previous hung download: dfu-util clears it first,
        # then the transfer itself hangs again.
        print("Determining device status: state = dfuERROR, status = 10")
        print("dfuERROR, clearing status")
        print("Error during download get_status", file=sys.stderr)
        sys.exit(74)
    if outcome == "notfound":
        print("No DFU capable USB device available", file=sys.stderr)
        sys.exit(74)
    if outcome == "sleep":
        time.sleep(10)
        sys.exit(0)
sys.exit(1)
"""


@pytest.fixture(autouse=True)
def _reset_pause():
    slot_pause._reset_for_tests()
    yield
    slot_pause._reset_for_tests()


class FakeDfu:
    def __init__(self, tmp_path, monkeypatch):
        self.exe = tmp_path / "dfu-util"
        self.exe.write_text(FAKE_DFU.replace("{python}", sys.executable))
        self.exe.chmod(self.exe.stat().st_mode | stat.S_IEXEC)
        self.state = tmp_path / "dfu-state.json"
        self.log = tmp_path / "dfu-log.jsonl"
        self.log.write_text("")
        monkeypatch.setenv("FAKE_DFU_STATE", str(self.state))
        monkeypatch.setenv("FAKE_DFU_LOG", str(self.log))
        self.set(download=["ok"], list_dfu=False)

    def set(self, **state):
        self.state.write_text(json.dumps(state))

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines() if line]

    def downloads(self):
        return [c for c in self.calls() if "-D" in c]


@pytest.fixture
def dfu(tmp_path, monkeypatch):
    return FakeDfu(tmp_path, monkeypatch)


def _cfg(tmp_path, dfu_exe, slots="slot1=1-1.2\n"):
    conf = tmp_path / "slots.conf"
    conf.write_text(slots)
    return AgentConfig(
        server_url="https://x",
        station_id=1,
        ed25519_key_path="/tmp/k",
        state_dir=str(tmp_path / "state"),
        module_fw_dir=str(tmp_path / "fw"),
        slot_dev_base=str(tmp_path / "dev"),
        slots_conf=str(conf),
        dfu_util=str(dfu_exe),
    )


def _ident(version="1.0.0", uid=UID):
    return {"type": "fm_transceiver", "uid": uid, "variant": "vhf", "version": version}


def _present(config, slot, module_uid, timeout=3.0):
    return _ident()


def _marker(tmp_path, attempts=0):
    image = tmp_path / "fw" / "conv7-1.2.0.bin"
    image.parent.mkdir(exist_ok=True)
    image.write_bytes(b"image")
    return Marker(
        convergence_id=7,
        slot=1,
        module_uid=UID,
        target_version="1.2.0",
        old_version="1.0.0",
        artifact="dfu",
        path=str(tmp_path / "fw" / "conv7-1.2.0.bin"),
        phase="flashing",
        dfu_attempts=attempts,
        checksum_sha256=hashlib.sha256(b"image").hexdigest(),
        state_dir=str(tmp_path / "state"),
    )


def _backend(tmp_path, dfu, read_identity=_present, **kw):
    sleeps = []
    b = DfuBackend(_cfg(tmp_path, dfu.exe, **kw), read_identity=read_identity, sleep=sleeps.append)
    return b, sleeps


# --- slots.conf ---------------------------------------------------------------------


def test_parse_slots_conf(tmp_path, caplog):
    conf = tmp_path / "slots.conf"
    conf.write_text(
        "# slot -> USB port path\n"
        "\n"
        "slot1=1-1.2\n"
        "  slot2 = 1-1.3.4  \n"
        "slot3=1-1;rm -rf /\n"
        "slot4=../1-1\n"
        "garbage line\n"
        "slotX=1-1.5\n"
        "slot5=3-2\n"
        "slot6=1\n"
    )
    with caplog.at_level(logging.WARNING):
        assert parse_slots_conf(str(conf)) == {1: "1-1.2", 2: "1-1.3.4", 5: "3-2"}
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 5


def test_parse_slots_conf_missing_file(tmp_path):
    assert parse_slots_conf(str(tmp_path / "nope.conf")) == {}


# --- flash ----------------------------------------------------------------------------


def test_flash_commands_use_port_path_never_vid_pid(tmp_path, dfu):
    backend, sleeps = _backend(tmp_path, dfu)
    assert backend.name == "dfu"
    assert backend.settle_s == module_ota.SETTLE_S
    marker = _marker(tmp_path)
    assert backend.flash(1, "/fw/image.bin", marker) is FlashOutcome.OK
    calls = dfu.calls()
    assert calls == [
        ["-e", "-p", PORT],
        ["-a", "0", "-p", PORT, "-D", "/fw/image.bin"],
    ]
    for argv in calls:
        assert "-d" not in argv
        assert "-p" in argv and argv[argv.index("-p") + 1] == PORT
    assert sleeps == [module_ota.DETACH_WAIT_S]
    assert marker.dfu_attempts == 0


def test_flash_slot_missing_in_slots_conf_is_transient(tmp_path, dfu):
    backend, _ = _backend(tmp_path, dfu, slots="slot2=1-1.3\n")
    assert backend.flash(1, "/fw/image.bin", _marker(tmp_path)) is FlashOutcome.TRANSIENT
    assert dfu.calls() == []


def test_flash_refuses_when_uid_not_in_slot(tmp_path, dfu):
    seen = []

    def absent(config, slot, module_uid, timeout=3.0):
        seen.append((slot, module_uid))
        return None

    backend, _ = _backend(tmp_path, dfu, read_identity=absent)
    assert backend.flash(1, "/fw/image.bin", _marker(tmp_path)) is FlashOutcome.TRANSIENT
    assert seen == [(1, UID)]
    assert dfu.calls() == []


def test_flash_identity_read_not_under_devlock(tmp_path, dfu):
    """probe_slot takes the (non-reentrant) devlock itself: never call it while holding it."""
    held = []

    def read_identity(config, slot, module_uid, timeout=3.0):
        path = module_ota.control_path(config, slot)
        with devlock.control_device_lock(path, timeout=0.1):
            held.append(False)
        return _ident()

    backend, _ = _backend(tmp_path, dfu, read_identity=read_identity)
    assert backend.flash(1, "/fw/image.bin", _marker(tmp_path)) is FlashOutcome.OK
    assert held == [False]


def test_dfu_error_status_is_rejected(tmp_path, dfu):
    dfu.set(download=["dfuerror"], list_dfu=True)
    backend, _ = _backend(tmp_path, dfu)
    marker = _marker(tmp_path)
    assert backend.flash(1, "/fw/image.bin", marker) is FlashOutcome.REJECTED
    assert len(dfu.downloads()) == 1
    assert marker.dfu_attempts == 0


@pytest.mark.parametrize(
    "output,rejected",
    [
        (
            "DFU state(10) = dfuERROR, status(10) = Device's firmware is corrupt. "
            "It cannot return to run-time (non-DFU) operations",
            True,
        ),
        ("DFU state(10) = dfuERROR, status(3) = Device is unable to write memory.", True),
        ("DFU state(10) = dfuERROR, status(0) = No error condition is present", False),
        ("Determining device status: state = dfuERROR, status = 10", False),
        ("dfuERROR, clearing status", False),
        (
            "Determining device status: state = dfuERROR, status = 10\n"
            "dfuERROR, clearing status\nError during download get_status",
            False,
        ),
        ("Error during download get_status", False),
        ("", False),
    ],
)
def test_is_device_rejection(output, rejected):
    assert module_ota_dfu.is_device_rejection(output) is rejected


def test_dfu_error_clearing_status_on_retry_is_hang_not_rejected(tmp_path, dfu):
    dfu.set(download=["hang", "clearing_hang"], list_dfu=True)
    backend, _ = _backend(tmp_path, dfu)
    marker = _marker(tmp_path)
    assert backend.flash(1, "/fw/image.bin", marker) is FlashOutcome.DFU_STUCK
    assert len(dfu.downloads()) == 3
    assert marker.dfu_attempts == 2


def test_dfu_error_clearing_status_then_ok_recovers(tmp_path, dfu):
    dfu.set(download=["clearing_hang", "ok"], list_dfu=True)
    backend, _ = _backend(tmp_path, dfu)
    assert backend.flash(1, "/fw/image.bin", _marker(tmp_path)) is FlashOutcome.OK
    assert len(dfu.downloads()) == 2


def test_recover_clearing_status_is_hang(tmp_path, dfu):
    dfu.set(download=["clearing_hang"], list_dfu=True)
    backend, _ = _backend(tmp_path, dfu)
    marker = _marker(tmp_path, attempts=1)
    assert backend.recover(marker) is FlashOutcome.DFU_STUCK
    assert len(dfu.downloads()) == 1


def test_device_not_found_is_transient(tmp_path, dfu):
    dfu.set(download=["notfound"], list_dfu=False)
    backend, _ = _backend(tmp_path, dfu)
    marker = _marker(tmp_path)
    assert backend.flash(1, "/fw/image.bin", marker) is FlashOutcome.TRANSIENT
    assert len(dfu.downloads()) == 1
    assert ["-l", "-p", PORT] in dfu.calls()
    assert marker.dfu_attempts == 0


def test_dfu_hang_retries_twice_then_stuck(tmp_path, dfu):
    dfu.set(download=["hang"], list_dfu=True)
    backend, _ = _backend(tmp_path, dfu)
    marker = _marker(tmp_path)
    assert backend.flash(1, "/fw/image.bin", marker) is FlashOutcome.DFU_STUCK
    assert len(dfu.downloads()) == 3
    assert marker.dfu_attempts == module_ota.DFU_MAX_RETRIES
    assert Marker.load(str(tmp_path / "state")).dfu_attempts == 2


def test_dfu_hang_recovers_on_retry(tmp_path, dfu):
    dfu.set(download=["hang", "ok"], list_dfu=True)
    backend, _ = _backend(tmp_path, dfu)
    marker = _marker(tmp_path)
    assert backend.flash(1, "/fw/image.bin", marker) is FlashOutcome.OK
    assert len(dfu.downloads()) == 2
    assert marker.dfu_attempts == 1


def test_retry_attempt_persisted_before_download(tmp_path, dfu):
    """marker.save() with the incremented counter happens BEFORE the retry download runs."""
    dfu.set(download=["hang", "ok"], list_dfu=True)
    state_dir = str(tmp_path / "state")
    persisted_at_download = []

    def run(argv, **kw):
        if "-D" in argv:
            m = Marker.load(state_dir)
            persisted_at_download.append(None if m is None else m.dfu_attempts)
        return subprocess.run(argv, **kw)

    cfg = _cfg(tmp_path, dfu.exe)
    backend = DfuBackend(cfg, run=run, sleep=lambda s: None, read_identity=_present)
    assert backend.flash(1, "/fw/image.bin", _marker(tmp_path)) is FlashOutcome.OK
    assert persisted_at_download == [None, 1]


def test_dfu_retry_budget_persists_across_restart(tmp_path, dfu):
    # Initial run: first download (not a retry) + 1 retry, then the agent "crashes".
    dfu.set(download=["hang"], list_dfu=True)
    state_dir = str(tmp_path / "state")
    downloads_before_crash = []

    def crash_on_second(argv, **kw):
        if "-D" in argv:
            downloads_before_crash.append(argv)
            if len(downloads_before_crash) == 2:
                result = subprocess.run(argv, **kw)
                raise KeyboardInterrupt(result)  # agent dies mid-retry
        return subprocess.run(argv, **kw)

    cfg = _cfg(tmp_path, dfu.exe)
    backend = DfuBackend(cfg, run=crash_on_second, sleep=lambda s: None, read_identity=_present)
    with pytest.raises(KeyboardInterrupt):
        backend.flash(1, "/fw/image.bin", _marker(tmp_path))
    persisted = Marker.load(state_dir)
    assert persisted.dfu_attempts == 1

    # Restart: recover() — every download is a retry; one left in the budget.
    backend2, _ = _backend(tmp_path, dfu)
    assert backend2.recover(persisted) is FlashOutcome.DFU_STUCK
    assert len(dfu.downloads()) == 3  # never more than 1 + DFU_MAX_RETRIES in total
    assert Marker.load(state_dir).dfu_attempts == 2

    # Second restart with the budget exhausted: no download at all.
    backend3, _ = _backend(tmp_path, dfu)
    assert backend3.recover(Marker.load(state_dir)) is FlashOutcome.DFU_STUCK
    assert len(dfu.downloads()) == 3


def test_recover_exhausted_budget_no_download(tmp_path, dfu):
    dfu.set(download=["ok"], list_dfu=True)
    backend, _ = _backend(tmp_path, dfu)
    assert backend.recover(_marker(tmp_path, attempts=2)) is FlashOutcome.DFU_STUCK
    assert dfu.downloads() == []


def test_recover_one_attempt_left_downloads_once(tmp_path, dfu):
    dfu.set(download=["hang"], list_dfu=True)
    backend, _ = _backend(tmp_path, dfu)
    marker = _marker(tmp_path, attempts=1)
    assert backend.recover(marker) is FlashOutcome.DFU_STUCK
    assert len(dfu.downloads()) == 1
    assert marker.dfu_attempts == 2


def test_recover_in_dfu_download_ok_returns_none(tmp_path, dfu):
    dfu.set(download=["ok"], list_dfu=True)
    backend, _ = _backend(tmp_path, dfu)
    marker = _marker(tmp_path)
    assert backend.recover(marker) is None
    assert dfu.downloads() == [["-a", "0", "-p", PORT, "-D", marker.path]]
    assert marker.dfu_attempts == 1
    assert all("-d" not in c and "-e" not in c for c in dfu.calls())


def test_recover_missing_image_is_stuck_without_download(tmp_path, dfu, caplog):
    dfu.set(download=["ok"], list_dfu=True)
    backend, _ = _backend(tmp_path, dfu)
    marker = _marker(tmp_path)
    marker.path = str(tmp_path / "fw" / "gone.bin")
    with caplog.at_level(logging.ERROR):
        assert backend.recover(marker) is FlashOutcome.DFU_STUCK
    assert dfu.downloads() == []
    assert marker.dfu_attempts == 0
    assert "gone.bin" in caplog.text


@pytest.mark.parametrize(
    "content, checksum",
    [
        (b"\0" * 5, None),  # zero-filled after power loss
        (b"ima", None),  # truncated
        (b"image", ""),  # marker without a known checksum: unverifiable
    ],
)
def test_recover_unverifiable_image_is_stuck_without_download(
    tmp_path, dfu, caplog, content, checksum
):
    dfu.set(download=["ok"], list_dfu=True)
    backend, _ = _backend(tmp_path, dfu)
    marker = _marker(tmp_path)
    if checksum is not None:
        marker.checksum_sha256 = checksum
    (tmp_path / "fw" / "conv7-1.2.0.bin").write_bytes(content)
    with caplog.at_level(logging.ERROR):
        assert backend.recover(marker) is FlashOutcome.DFU_STUCK
    assert dfu.downloads() == []
    assert marker.dfu_attempts == 0
    assert "re-verification" in caplog.text


def test_marker_checksum_roundtrip_and_legacy_default(tmp_path):
    marker = _marker(tmp_path)
    marker.save()
    assert Marker.load(marker.state_dir).checksum_sha256 == marker.checksum_sha256
    path = os.path.join(marker.state_dir, "module_ota.json")
    with open(path) as fh:
        data = json.load(fh)
    data.pop("checksum_sha256")
    with open(path, "w") as fh:
        json.dump(data, fh)
    assert Marker.load(marker.state_dir).checksum_sha256 == ""


def test_flash_cycle_marker_carries_instruction_checksum(tmp_path, dfu, monkeypatch):
    monkeypatch.setattr(module_ota, "detect_backends", lambda cfg: ["dfu"])
    cfg = _cfg(tmp_path, dfu.exe)
    seen = []
    ota = ModuleOta(
        cfg,
        _Http(),
        control=_Control(),
        read_identity=_present,
        clock=lambda: 0.0,
        sleep=lambda s: 0,
    )
    ota._sync_backends()
    backend = ota.backends["dfu"]

    def flash(slot, path, marker):
        seen.append(Marker.load(cfg.state_dir).checksum_sha256)
        return FlashOutcome.TRANSIENT

    monkeypatch.setattr(backend, "flash", flash)
    monkeypatch.setattr(ota, "_sync_backends", lambda: {"dfu"})
    ota.run_once()
    assert seen == [SHA]


def test_parse_slots_conf_rejects_non_ascii_digits(tmp_path):
    conf = tmp_path / "slots.conf"
    conf.write_text("slot1=\u0661-\u0661\nslot2=1-1\n", encoding="utf-8")
    assert parse_slots_conf(str(conf)) == {2: "1-1"}


def test_recover_not_in_dfu_falls_through(tmp_path, dfu):
    dfu.set(download=["ok"], list_dfu=False)
    backend, _ = _backend(tmp_path, dfu)
    assert backend.recover(_marker(tmp_path)) is None
    assert dfu.downloads() == []
    assert dfu.calls() == [["-l", "-p", PORT]]


def test_after_verify_is_noop(tmp_path, dfu):
    backend, _ = _backend(tmp_path, dfu)
    assert backend.after_verify(1, True) is None
    assert backend.after_verify(1, False) is None
    assert dfu.calls() == []


def test_detach_holds_devlock(tmp_path, dfu):
    cfg = _cfg(tmp_path, dfu.exe)
    path = module_ota.control_path(cfg, 1)
    acquired = {}

    def run(argv, **kw):
        if "-e" in argv:

            def try_acquire():
                try:
                    with devlock.control_device_lock(path, timeout=0.2):
                        acquired["detach"] = True
                except TimeoutError:
                    acquired["detach"] = False

            t = threading.Thread(target=try_acquire)
            t.start()
            t.join()
        if "-D" in argv:
            with devlock.control_device_lock(path, timeout=0.2):
                acquired["download"] = True
        return subprocess.run(argv, **kw)

    sleeps_locked = []

    def sleep(s):
        def probe():
            try:
                with devlock.control_device_lock(path, timeout=0.1):
                    sleeps_locked.append(False)
            except TimeoutError:
                sleeps_locked.append(True)

        t = threading.Thread(target=probe)
        t.start()
        t.join()

    backend = DfuBackend(cfg, run=run, sleep=sleep, read_identity=_present)
    assert backend.flash(1, "/fw/image.bin", _marker(tmp_path)) is FlashOutcome.OK
    assert acquired == {"detach": False, "download": True}
    assert sleeps_locked == [True]  # DETACH_WAIT_S is spent under the lock


def test_detach_lock_timeout_is_transient(tmp_path, dfu, monkeypatch):
    @contextlib.contextmanager
    def busy_lock(path, *, timeout=15.0):
        assert timeout == 15
        raise TimeoutError("busy")
        yield

    monkeypatch.setattr(module_ota_dfu.devlock, "control_device_lock", busy_lock)
    backend, _ = _backend(tmp_path, dfu)
    assert backend.flash(1, "/fw/image.bin", _marker(tmp_path)) is FlashOutcome.TRANSIENT
    assert dfu.calls() == []


def test_download_timeout_treated_as_failure(tmp_path, dfu, monkeypatch):
    monkeypatch.setattr(module_ota_dfu, "DFU_DOWNLOAD_TIMEOUT_S", 0.5)
    dfu.set(download=["sleep"], list_dfu=False)
    backend, _ = _backend(tmp_path, dfu)
    assert backend.flash(1, "/fw/image.bin", _marker(tmp_path)) is FlashOutcome.TRANSIENT
    assert len(dfu.downloads()) == 1


def test_download_timeout_in_dfu_mode_retries(tmp_path, dfu, monkeypatch):
    seen = []

    def run(argv, **kw):
        if "-D" in argv:
            seen.append(kw.get("timeout"))
            if len(seen) == 1:
                raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
        return subprocess.run(argv, **kw)

    dfu.set(download=["ok"], list_dfu=True)
    backend = DfuBackend(
        _cfg(tmp_path, dfu.exe), run=run, sleep=lambda s: None, read_identity=_present
    )
    assert backend.flash(1, "/fw/image.bin", _marker(tmp_path)) is FlashOutcome.OK
    assert seen == [module_ota.DFU_DOWNLOAD_TIMEOUT_S] * 2


def test_dfu_util_missing_is_transient(tmp_path, dfu):
    cfg = _cfg(tmp_path, tmp_path / "does-not-exist")
    backend = DfuBackend(cfg, sleep=lambda s: None, read_identity=_present)
    assert backend.flash(1, "/fw/image.bin", _marker(tmp_path)) is FlashOutcome.TRANSIENT


# --- registration -----------------------------------------------------------------------


def test_default_backends_registers_dfu_when_detected(tmp_path, dfu, monkeypatch):
    seen = []

    def read_identity(config, slot, module_uid, timeout=3.0):
        seen.append(slot)
        return None

    monkeypatch.setattr(module_ota, "detect_backends", lambda cfg: ["dfu"])
    ota = ModuleOta(_cfg(tmp_path, dfu.exe), http_client=None, read_identity=read_identity)
    assert isinstance(ota.backends["dfu"], DfuBackend)
    # The backend shares ModuleOta's identity reader (wrong-module guard).
    assert ota.backends["dfu"].flash(1, "/x", _marker(tmp_path)) is FlashOutcome.TRANSIENT
    assert seen == [1]

    monkeypatch.setattr(module_ota, "detect_backends", lambda cfg: [])
    assert ModuleOta(_cfg(tmp_path, dfu.exe), http_client=None).backends == {}


# --- E2E with the A3 cycle -----------------------------------------------------------------

IMAGE = b"FIRMWARE-IMAGE" * 64
SHA = hashlib.sha256(IMAGE).hexdigest()
CHECK = "/api/v1/module-firmware/reconcile/check/"
DL = "/api/v1/module-firmware/releases/3/download/"


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
            return _Resp(200, body=IMAGE)
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
                    "artifact": "dfu",
                    "download_url": DL,
                    "checksum_sha256": SHA,
                    "size_bytes": len(IMAGE),
                },
            )
        return _Resp(200, payload={})


class _Control:
    def __init__(self):
        self.events = []
        self.forgotten = []

    def forget_slot(self, slot):
        self.forgotten.append(slot)

    def slot_busy(self, slot):
        return False

    def last_inventory_entry(self, slot):
        return {"slot": 1, "modules": [{"id": "fm", "identity": _ident(), "capabilities": []}]}

    def send_event(self, slot, module, event, detail):
        self.events.append((slot, module, event, detail))

    def request_inventory_refresh(self, force=False):
        pass


def test_module_ota_dfu_stuck_reports_rolled_back_and_event(tmp_path, dfu, monkeypatch):
    monkeypatch.setattr(module_ota, "detect_backends", lambda cfg: ["dfu"])
    monkeypatch.setattr(module_ota_dfu, "DETACH_WAIT_S", 0.0)  # skip the real 3 s wait
    dfu.set(download=["hang"], list_dfu=True)
    cfg = _cfg(tmp_path, dfu.exe)
    http, control = _Http(), _Control()
    ota = ModuleOta(
        cfg,
        http,
        control=control,
        read_identity=_present,
        clock=lambda: 0.0,
        sleep=lambda s: None,
    )
    assert ota.run_once() == "rolled_back"

    statuses = [b for p, b in http.posts if p.endswith("/status/")]
    assert [b["status"] for b in statuses] == ["downloading", "flashing", "rolled_back"]
    assert statuses[-1]["error_message"] == module_ota.DFU_STUCK_MSG
    names = [e[2] for e in control.events]
    assert names == ["firmware_update_started", "firmware_dfu_stuck", "firmware_update_finished"]
    assert control.events[1][3] == {"msg": module_ota.DFU_STUCK_MSG}
    assert control.events[2][3] == {"result": "rolled_back", "target_version": "1.2.0"}
    assert len(dfu.downloads()) == 3
    assert all("-d" not in c for c in dfu.calls())
    assert Marker.load(cfg.state_dir) is None
    assert not os.listdir(cfg.module_fw_dir)


def test_dfu_backend_is_stuck_probes_port(tmp_path, dfu):
    backend = DfuBackend(_cfg(tmp_path, dfu.exe))
    dfu.set(list_dfu=True)
    assert backend.is_stuck(1) is True
    dfu.set(list_dfu=False)
    assert backend.is_stuck(1) is False
    assert backend.is_stuck(9) is False  # no port in slots.conf


def test_dfu_stuck_notice_survives_next_cycle(tmp_path, dfu, monkeypatch):
    """Next cycle after DFU_STUCK: no CDC (still in DFU) -> re-report DFU-stuck, never
    'not found' (which would overwrite the dashboard notice). The re-report is a
    transient ``failed`` (keeps the DFU-stuck message, spends no attempt)."""
    monkeypatch.setattr(module_ota, "detect_backends", lambda cfg: ["dfu"])
    monkeypatch.setattr(module_ota_dfu, "DETACH_WAIT_S", 0.0)
    dfu.set(download=["hang"], list_dfu=True)
    cfg = _cfg(tmp_path, dfu.exe)
    http, control = _Http(), _Control()
    present = {"on": True}

    def identity(config, slot, module_uid, timeout=3.0):
        return _present(config, slot, module_uid, timeout) if present["on"] else None

    ota = ModuleOta(
        cfg, http, control=control, read_identity=identity, clock=lambda: 0.0, sleep=lambda s: 0
    )
    assert ota.run_once() == "rolled_back"
    present["on"] = False  # the module hangs in DFU mode: no CDC control device
    http.posts.clear()
    control.events.clear()
    downloads = len(dfu.downloads())

    for _ in range(2):  # consecutive stuck cycles: none of them counts an attempt
        http.posts.clear()
        control.events.clear()
        assert ota.run_once() == "failed"
        statuses = [b for p, b in http.posts if p.endswith("/status/")]
        assert statuses == [{"status": "failed", "error_message": module_ota.DFU_STUCK_MSG}]
        assert [e[2] for e in control.events] == ["firmware_dfu_stuck"]
        assert control.events[0][1] == "fm"
        # Each re-report drops the stale debounced identity again (review #168 r4).
        assert control.forgotten[-1] == 1
    assert len(dfu.downloads()) == downloads  # never flashes again

    dfu.set(list_dfu=False)  # power-cycled but gone: plain "not found" again
    http.posts.clear()
    assert ota.run_once() == "failed"
    statuses = [b for p, b in http.posts if p.endswith("/status/")]
    assert "not found" in statuses[0]["error_message"]


def test_dfu_stuck_rereport_without_inventory_entry_skips_event(tmp_path, dfu, monkeypatch):
    """The debouncer dropped the CDC-less slot: no module id -> status only, no event
    with module '' (browsers could not match it to a card)."""
    monkeypatch.setattr(module_ota, "detect_backends", lambda cfg: ["dfu"])
    dfu.set(list_dfu=True)
    cfg = _cfg(tmp_path, dfu.exe)
    http, control = _Http(), _Control()
    control.last_inventory_entry = lambda slot: None
    ota = ModuleOta(
        cfg,
        http,
        control=control,
        read_identity=lambda *a, **k: None,
        clock=lambda: 0.0,
        sleep=lambda s: 0,
    )
    assert ota.run_once() == "failed"
    statuses = [b for p, b in http.posts if p.endswith("/status/")]
    assert statuses == [{"status": "failed", "error_message": module_ota.DFU_STUCK_MSG}]
    assert control.events == []


# --- marker validation (review #168 r3) ------------------------------------------------


def _write_marker(tmp_path, **overrides):
    m = _marker(tmp_path)
    m.save()
    path = tmp_path / "state" / "module_ota.json"
    data = json.loads(path.read_text())
    data.update(overrides)
    path.write_text(json.dumps(data))
    return m


@pytest.mark.parametrize(
    "overrides",
    [
        {"checksum_sha256": 123},
        {"checksum_sha256": ["a"]},
        {"checksum_sha256": "abc"},
        {"checksum_sha256": "g" * 64},
        {"path": 7},
        {"module_uid": None},
        {"artifact": {"x": 1}},
        {"dfu_attempts": "1"},
        {"snapshot": "slot"},
    ],
)
def test_marker_load_rejects_bad_types_and_checksum(tmp_path, overrides):
    _write_marker(tmp_path, **overrides)
    state_dir = str(tmp_path / "state")
    assert Marker.load(state_dir, str(tmp_path / "fw")) is None
    assert not os.path.exists(os.path.join(state_dir, "module_ota.json"))  # discarded


@pytest.mark.parametrize("path", ["/etc/passwd", "{fw}/../evil.bin", "{fw}/sub/x.bin", ""])
def test_marker_load_rejects_path_outside_module_fw_dir(tmp_path, path):
    fw = tmp_path / "fw"
    _write_marker(tmp_path, path=path.format(fw=fw))
    assert Marker.load(str(tmp_path / "state"), str(fw)) is None


def test_marker_load_rejects_symlink_escaping_module_fw_dir(tmp_path):
    fw = tmp_path / "fw"
    fw.mkdir(exist_ok=True)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"image")
    link = fw / "conv7-1.2.0.bin.lnk"
    link.symlink_to(outside)
    _write_marker(tmp_path, path=str(link))
    assert Marker.load(str(tmp_path / "state"), str(fw)) is None


def test_marker_load_valid_marker_normalises_checksum(tmp_path):
    m = _write_marker(tmp_path, checksum_sha256=hashlib.sha256(b"image").hexdigest().upper())
    loaded = Marker.load(str(tmp_path / "state"), str(tmp_path / "fw"))
    assert loaded is not None
    assert loaded.checksum_sha256 == m.checksum_sha256  # lowercase
    assert loaded.path == m.path


def test_recover_never_flashes_path_outside_module_fw_dir(tmp_path, dfu):
    """Defence in depth: even a Marker built in memory (not via load) pointing outside
    module_fw_dir with a matching hash is never handed to dfu-util."""
    dfu.set(download=["ok"], list_dfu=True)
    backend, _ = _backend(tmp_path, dfu)
    outside = tmp_path / "evil.bin"
    outside.write_bytes(b"image")
    marker = _marker(tmp_path)
    marker.path = str(outside)
    assert backend.recover(marker) is FlashOutcome.DFU_STUCK
    assert dfu.downloads() == []


def test_recover_non_str_checksum_is_dfu_stuck_not_attribute_error(tmp_path, dfu):
    dfu.set(download=["ok"], list_dfu=True)
    backend, _ = _backend(tmp_path, dfu)
    marker = _marker(tmp_path)
    marker.checksum_sha256 = 123
    assert backend.recover(marker) is FlashOutcome.DFU_STUCK
    assert dfu.downloads() == []
