import os
import subprocess

import pytest

from station_agent.decoders.units import DecoderUnitError, DecoderUnits


class FakeRun:
    def __init__(self, rc=0, stdout="", stderr="", exc=None):
        self.calls = []
        self.rc, self.stdout, self.stderr, self.exc = rc, stdout, stderr, exc

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(argv, self.rc, self.stdout, self.stderr)


ENV = {"OE5XRX_SLOT": "1", "OE5XRX_RX_NODE": "alsa_input.x"}


def test_instance_name(tmp_path):
    assert DecoderUnits(env_dir=str(tmp_path)).instance("aprs", 1) == "aprs-slot1"


def test_start_writes_env_then_starts_unit(tmp_path):
    run = FakeRun()
    DecoderUnits(run=run, env_dir=str(tmp_path / "d")).start("aprs", 1, ENV)
    env_file = tmp_path / "d" / "aprs-slot1.env"
    assert env_file.read_text() == "OE5XRX_SLOT=1\nOE5XRX_RX_NODE=alsa_input.x\n"
    assert oct(env_file.stat().st_mode & 0o777) == "0o644"
    assert run.calls == [["systemctl", "start", "oe5xrx-decoder@aprs-slot1.service"]]


@pytest.mark.parametrize(
    "env",
    [
        {"OE5XRX_X": "a\nb"},
        {"OE5XRX_X": "a b"},
        {"OE5XRX_X": ""},
        {"BAD": "x"},
        {"OE5XRX_x": "x"},
    ],
)
def test_start_rejects_bad_env(tmp_path, env):
    run = FakeRun()
    with pytest.raises(DecoderUnitError, match="bad env"):
        DecoderUnits(run=run, env_dir=str(tmp_path)).start("aprs", 1, env)
    assert run.calls == [] and list(tmp_path.iterdir()) == []


def test_trailing_newline_rejected(tmp_path):
    run = FakeRun()
    with pytest.raises(DecoderUnitError, match="bad env"):
        DecoderUnits(run=run, env_dir=str(tmp_path)).start("aprs", 1, {"OE5XRX_X": "node\n"})
    with pytest.raises(DecoderUnitError):
        DecoderUnits(run=run, env_dir=str(tmp_path)).instance("aprs\n", 1)
    assert run.calls == []


def test_start_nonzero_raises(tmp_path):
    with pytest.raises(DecoderUnitError, match="no such unit"):
        DecoderUnits(run=FakeRun(rc=5, stderr="no such unit\n"), env_dir=str(tmp_path)).start(
            "aprs", 1, ENV
        )


def test_start_timeout_raises(tmp_path):
    run = FakeRun(exc=subprocess.TimeoutExpired("systemctl", 15))
    with pytest.raises(DecoderUnitError):
        DecoderUnits(run=run, env_dir=str(tmp_path)).start("aprs", 1, ENV)


def test_stop_removes_env(tmp_path):
    run = FakeRun()
    u = DecoderUnits(run=run, env_dir=str(tmp_path))
    u.start("aprs", 1, ENV)
    u.stop("aprs", 1)
    assert run.calls[-1] == ["systemctl", "stop", "oe5xrx-decoder@aprs-slot1.service"]
    assert not (tmp_path / "aprs-slot1.env").exists()


def test_stop_nonzero_raises_but_env_removed(tmp_path):
    (tmp_path / "aprs-slot1.env").write_text("x")
    with pytest.raises(DecoderUnitError):
        DecoderUnits(run=FakeRun(rc=1), env_dir=str(tmp_path)).stop("aprs", 1)
    assert not (tmp_path / "aprs-slot1.env").exists()


def test_state_values(tmp_path):
    u = DecoderUnits(run=FakeRun(rc=3, stdout="inactive\n"), env_dir=str(tmp_path))
    assert u.state("aprs", 1) == "inactive"


def test_state_timeout_returns_unknown(tmp_path):
    run = FakeRun(exc=subprocess.TimeoutExpired("systemctl", 15))
    assert DecoderUnits(run=run, env_dir=str(tmp_path)).state("aprs", 1) == "unknown"


def test_state_oserror_returns_unknown(tmp_path):
    assert DecoderUnits(run=FakeRun(exc=OSError()), env_dir=str(tmp_path)).state("aprs", 1) == (
        "unknown"
    )


def test_stop_all_globs_and_cleans(tmp_path):
    (tmp_path / "aprs-slot1.env").write_text("x")
    (tmp_path / "aprs-slot2.env").write_text("x")
    (tmp_path / "keep.txt").write_text("x")
    run = FakeRun()
    DecoderUnits(run=run, env_dir=str(tmp_path)).stop_all()
    assert run.calls == [["systemctl", "stop", "oe5xrx-decoder@*.service"]]
    assert sorted(os.listdir(tmp_path)) == ["keep.txt"]


def test_stop_all_without_env_dir_ok(tmp_path):
    DecoderUnits(run=FakeRun(), env_dir=str(tmp_path / "missing")).stop_all()


@pytest.mark.parametrize("name", ["../x", "APRS", "", "a b", "1aprs", "a" * 17])
def test_bad_decoder_name_rejected(tmp_path, name):
    run = FakeRun()
    u = DecoderUnits(run=run, env_dir=str(tmp_path))
    with pytest.raises(DecoderUnitError):
        u.start(name, 1, ENV)
    with pytest.raises(DecoderUnitError):
        u.instance(name, 1)
    assert run.calls == []
