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
        # Honest Content-Range matching the Range we requested — the
        # agent now validates the reported start against existing_len
        # before appending, so the header has to be present.
        headers = {"Content-Range": "bytes 100-199/200"}

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


def test_download_skips_network_when_partial_matches_expected_size(tmp_path):
    """If the on-disk partial already matches expected_size, the agent
    must skip the HTTP round-trip and just verify the checksum — the
    recovery path for "downloaded once, died before advancing to
    INSTALLING" should not re-fetch a multi-hundred-MB image."""
    import hashlib

    from station_agent import ota

    payload = b"FULL_IMAGE_BYTES" * 64  # 1024 bytes
    dest = tmp_path / "image.wic.bz2"
    dest.write_bytes(payload)

    calls = []

    class FakeClient:
        def request(self, *args, **kwargs):
            calls.append(kwargs)
            raise AssertionError("HTTP request must not fire when partial is already complete")

    ok = ota.download_firmware_resumable(
        http_client=FakeClient(),
        download_url="/path",
        expected_checksum=hashlib.sha256(payload).hexdigest(),
        dest_path=str(dest),
        resume=True,
        expected_size=len(payload),
    )
    assert ok is True
    assert calls == []
    # File untouched.
    assert dest.read_bytes() == payload


def test_download_discards_oversized_stale_partial(tmp_path):
    """A partial larger than expected_size is stale from a previous,
    bigger release. Discard without sending a Range that would 416."""
    from station_agent import ota

    dest = tmp_path / "image.wic.bz2"
    dest.write_bytes(b"X" * 500)

    calls = []

    class FakeResp:
        status_code = 200
        headers: dict[str, str] = {}

        def iter_content(self, chunk_size):
            yield b"FRESH" * 40

        def close(self):
            pass

    class FakeClient:
        def request(self, method, path, stream=False, headers=None, **kw):
            calls.append(dict(headers or {}))
            return FakeResp()

    ok = ota.download_firmware_resumable(
        http_client=FakeClient(),
        download_url="/path",
        expected_checksum="",
        dest_path=str(dest),
        resume=True,
        expected_size=200,
    )
    assert ok is True
    assert len(calls) == 1
    # No Range header — we dropped the stale partial and did a fresh GET.
    assert "Range" not in calls[0]
    assert dest.read_bytes() == b"FRESH" * 40


def test_install_to_slot_rejects_truncated_bz2(tmp_path):
    """A truncated .wic.bz2 must raise, not silently write a partial
    image that would brick the next boot."""
    from station_agent.ota import install_to_slot

    payload = b"hello world" * 4096
    src = tmp_path / "image.wic.bz2"
    full = bz2.compress(payload)
    # Chop off the last 8 bytes to simulate a .wic.bz2 that reached the
    # installer intact-looking (e.g. the expected checksum was computed
    # over the same truncated object and happens to match). SHA-256 can't
    # catch that — install_to_slot must independently require the bz2
    # stream to reach EOF before calling the write a success.
    src.write_bytes(full[:-8])

    target = tmp_path / "fake-slot.bin"
    target.write_bytes(b"\x00" * len(payload))

    with pytest.raises(ValueError, match="truncated"):
        install_to_slot(src, str(target))


def test_download_rejects_mismatched_content_range(tmp_path):
    """If the server/proxy returns 206 but with a start offset that
    doesn't match our partial size, the partial must be discarded and
    the download restarted from 0 — otherwise we'd silently append
    the wrong bytes and fail only at the final checksum."""
    from station_agent import ota

    dest = tmp_path / "image.wic.bz2"
    partial = bytes(range(100))
    dest.write_bytes(partial)

    calls = []

    class FakeResp:
        def __init__(self, status, body=b"", content_range=None):
            self.status_code = status
            self._body = body
            self.headers = {"Content-Range": content_range} if content_range else {}

        def iter_content(self, chunk_size):
            if self._body:
                yield self._body

        def close(self):
            pass

    class FakeClient:
        def request(self, method, path, stream=False, headers=None, **kw):
            calls.append(dict(headers or {}))
            # First call: resumed, but server lies about the offset.
            if "Range" in (headers or {}):
                return FakeResp(206, body=b"GARBAGE" * 10, content_range="bytes 50-149/200")
            # Second call (restart): fresh, no Range.
            return FakeResp(200, body=b"FRESH" * 40)

    ok = ota.download_firmware_resumable(
        http_client=FakeClient(),
        download_url="/path",
        expected_checksum="",
        dest_path=str(dest),
        resume=True,
    )
    assert ok is True
    # Two calls: first with Range (rejected), second without.
    assert len(calls) == 2
    assert "Range" in calls[0]
    assert "Range" not in calls[1]
    # File was rewritten from scratch — old partial + lying tail are gone.
    assert dest.read_bytes() == b"FRESH" * 40


def test_download_recovers_from_416_on_stale_partial(tmp_path):
    """A stale partial larger than the current object produces a 416 on
    the server. Agent must drop the partial and retry without Range —
    one retry, not a hard failure."""
    from station_agent import ota

    dest = tmp_path / "image.wic.bz2"
    # Pretend a stale partial is left over from a previous release.
    dest.write_bytes(b"STALE" * 100)

    calls = []

    class FakeResp:
        def __init__(self, status_code, body=b""):
            self.status_code = status_code
            self.headers = {}
            self._body = body

        def iter_content(self, chunk_size):
            if self._body:
                yield self._body

        def close(self):
            pass

    class FakeClient:
        def request(self, method, path, stream=False, headers=None, **kw):
            calls.append(dict(headers or {}))
            if "Range" in (headers or {}):
                return FakeResp(416)
            return FakeResp(200, body=b"FRESH" * 20)

    ok = ota.download_firmware_resumable(
        http_client=FakeClient(),
        download_url="/path",
        expected_checksum="",
        dest_path=str(dest),
        resume=True,
    )
    assert ok is True
    # Two requests: first with Range (got 416), second without.
    assert len(calls) == 2
    assert "Range" in calls[0]
    assert "Range" not in calls[1]
    # File was rewritten from scratch.
    assert dest.read_bytes() == b"FRESH" * 20


def test_inventory_reports_current_version(tmp_path, monkeypatch):
    import station_agent.inventory as inv

    os_release = tmp_path / "os-release"
    os_release.write_text(
        'NAME="Poky"\nPRETTY_NAME="OE5XRX Remote Station v1-beta"\nOE5XRX_RELEASE="v1-beta"\n'
    )
    monkeypatch.setattr(inv, "_OS_RELEASE_PATH", str(os_release))
    assert inv.get_current_version() == "v1-beta"
