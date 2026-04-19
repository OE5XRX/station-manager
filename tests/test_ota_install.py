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
