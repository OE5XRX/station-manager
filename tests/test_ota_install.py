"""Unit tests for station_agent.ota.install_to_slot.

Verifies that install_to_slot streams a .wic.bz2 file chunk-by-chunk into
a destination file (simulating a block device). The key behaviours:

1. Bytes written match the decompressed payload (round-trip correctness).
2. The reader is invoked multiple times rather than slurping the entire
   compressed file at once — confirming the streaming contract.

station_agent.ota pulls in `requests` (via .http_client); when that dep
isn't installed in the test environment, the importorskip below keeps
this module from erroring out at collection time.
"""

from __future__ import annotations

import bz2
import os

import pytest

pytest.importorskip(
    "station_agent.ota",
    reason="station_agent deps (requests) not installed in this environment",
)


def test_install_to_slot_decompresses_bz2_and_writes_bytes(tmp_path):
    from station_agent.ota import install_to_slot

    payload = b"hello world" * 4096
    src = tmp_path / "image.wic.bz2"
    src.write_bytes(bz2.compress(payload))

    target = tmp_path / "fake-slot.bin"
    target.write_bytes(b"\x00" * len(payload))  # pre-size, simulate block device

    install_to_slot(src, str(target))

    actual = target.read_bytes()
    assert actual[: len(payload)] == payload


def test_install_to_slot_writes_in_chunks(tmp_path, monkeypatch):
    """Confirm the implementation streams (doesn't read everything into memory).

    Uses os.urandom so the compressed file is ~incompressible and stays
    roughly 4 MiB — otherwise (e.g. `b"x" * N`) bz2 shrinks the input to
    ~50 bytes and the streaming loop only gets a single non-empty read,
    which wouldn't actually exercise the chunked contract.
    """
    from station_agent import ota

    payload = os.urandom(4 << 20)  # 4 MiB, incompressible
    src = tmp_path / "image.wic.bz2"
    src.write_bytes(bz2.compress(payload))

    target = tmp_path / "fake-slot.bin"
    target.write_bytes(b"\x00" * len(payload))

    original_read = ota._stream_read
    call_count = {"n": 0}

    def counted_read(fh, n):
        call_count["n"] += 1
        return original_read(fh, n)

    monkeypatch.setattr(ota, "_stream_read", counted_read)

    ota.install_to_slot(src, str(target))
    assert call_count["n"] >= 4


def test_download_resumes_on_partial(tmp_path):
    """When a .part file exists, the next download pass sends Range."""
    from station_agent import ota

    dest = tmp_path / "image.wic.bz2"
    partial = bytes(range(100))
    dest.write_bytes(partial)

    captured = {}

    class FakeResp:
        status_code = 206
        headers = {}

        def __init__(self, tail):
            self._tail = tail

        def iter_content(self, chunk_size):
            yield self._tail

        def close(self):
            pass

    class FakeClient:
        def request(self, method, path, stream=False, headers=None, **kw):
            captured["headers"] = headers or {}
            tail = bytes(range(100, 200))
            return FakeResp(tail)

    ok = ota.download_firmware_resumable(
        http_client=FakeClient(),
        download_url="/path",
        expected_checksum="",  # skip checksum for this unit — covered elsewhere
        dest_path=str(dest),
        resume=True,
    )
    assert ok is True
    assert dest.read_bytes() == bytes(range(200))
    assert captured["headers"].get("Range", "").startswith("bytes=100-")


def test_inventory_reports_current_version(tmp_path, monkeypatch):
    import station_agent.inventory as inv

    os_release = tmp_path / "os-release"
    os_release.write_text(
        'NAME="Poky"\nPRETTY_NAME="OE5XRX Remote Station v1-beta"\nOE5XRX_RELEASE="v1-beta"\n'
    )
    monkeypatch.setattr(inv, "_OS_RELEASE_PATH", str(os_release))
    assert inv.get_current_version() == "v1-beta"
