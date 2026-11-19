"""OTA download: network errors are transient, the agent retries with backoff.

On LTE/HAMNET links a timeout, a dropped connection or a 5xx from the proxy is
normal. Reporting ``failed`` for it is terminal on the server and throws the
Range-resumable partial away, so the agent classifies each attempt
(``download_firmware_attempt``) and ``download_with_retry`` keeps resuming
until the download has made no progress for ``stall_limit_s``.
"""

from __future__ import annotations

import hashlib

import pytest

pytest.importorskip(
    "station_agent.ota",
    reason="station_agent deps (requests) not installed in this environment",
)

import requests  # noqa: E402

from station_agent import ota  # noqa: E402
from station_agent.ota import DownloadOutcome, DownloadResult  # noqa: E402

PAYLOAD = bytes(range(256)) * 4  # 1024 bytes
CHECKSUM = hashlib.sha256(PAYLOAD).hexdigest()


class FakeResp:
    def __init__(self, status_code, chunks=(), raise_after=None, content_range=None):
        self.status_code = status_code
        self._chunks = list(chunks)
        self._raise_after = raise_after
        self.headers = {"Content-Range": content_range} if content_range else {}
        self.closed = False

    def iter_content(self, chunk_size):
        yield from self._chunks
        if self._raise_after is not None:
            raise self._raise_after

    def close(self):
        self.closed = True


class ScriptedClient:
    """Returns the scripted responses in order and records the Range headers."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.ranges: list[str | None] = []
        self.timeouts: list = []

    def request(self, method, path, stream=False, headers=None, timeout=None, **kw):
        self.ranges.append((headers or {}).get("Range"))
        self.timeouts.append(timeout)
        resp = self._responses.pop(0)
        if callable(resp):
            return resp(headers or {})
        return resp


def _attempt(client, dest, **kw):
    return ota.download_firmware_attempt(
        http_client=client,
        download_url="/api/v1/deployments/1/download/",
        expected_checksum=kw.pop("expected_checksum", CHECKSUM),
        dest_path=str(dest),
        **kw,
    )


# ---------------------------------------------------------------- classification


class TestAttemptClassification:
    def test_complete_download_is_ok(self, tmp_path):
        dest = tmp_path / "fw.rootfs.bz2"
        result = _attempt(ScriptedClient(FakeResp(200, [PAYLOAD])), dest)
        assert result.outcome is DownloadOutcome.OK
        assert dest.read_bytes() == PAYLOAD

    def test_transport_failure_is_transient(self, tmp_path):
        result = _attempt(ScriptedClient(None), tmp_path / "fw")
        assert result.outcome is DownloadOutcome.TRANSIENT

    @pytest.mark.parametrize(
        "exc",
        [
            requests.exceptions.ChunkedEncodingError("connection broken"),
            requests.exceptions.ConnectionError("read timed out"),
            requests.exceptions.ReadTimeout("read timed out"),
        ],
    )
    def test_mid_stream_network_error_is_transient_and_keeps_partial(self, tmp_path, exc):
        """requests' exceptions subclass OSError — they must NOT hit the
        disk-error branch that deletes the partial."""
        dest = tmp_path / "fw.rootfs.bz2"
        client = ScriptedClient(FakeResp(200, [PAYLOAD[:300]], raise_after=exc))
        result = _attempt(client, dest, expected_size=len(PAYLOAD))
        assert result.outcome is DownloadOutcome.TRANSIENT
        assert dest.read_bytes() == PAYLOAD[:300]

    @pytest.mark.parametrize("code", [500, 502, 503, 504, 408, 429])
    def test_server_and_throttle_errors_are_transient(self, tmp_path, code):
        result = _attempt(ScriptedClient(FakeResp(code)), tmp_path / "fw")
        assert result.outcome is DownloadOutcome.TRANSIENT
        assert str(code) in result.detail

    @pytest.mark.parametrize("code", [400, 401, 403, 404, 410])
    def test_client_errors_are_fatal(self, tmp_path, code):
        """403 = deployment no longer active for this station (cancelled /
        superseded), 404 = image gone — retrying cannot help."""
        result = _attempt(ScriptedClient(FakeResp(code)), tmp_path / "fw")
        assert result.outcome is DownloadOutcome.FATAL
        assert str(code) in result.detail

    def test_server_error_keeps_existing_partial(self, tmp_path):
        dest = tmp_path / "fw.rootfs.bz2"
        dest.write_bytes(PAYLOAD[:100])
        result = _attempt(ScriptedClient(FakeResp(502)), dest, expected_size=len(PAYLOAD))
        assert result.outcome is DownloadOutcome.TRANSIENT
        assert dest.read_bytes() == PAYLOAD[:100]

    def test_short_body_with_known_size_is_transient_and_keeps_partial(self, tmp_path):
        """A connection that closes cleanly before Content-Length must not
        reach the checksum (which would delete the partial and fail)."""
        dest = tmp_path / "fw.rootfs.bz2"
        result = _attempt(
            ScriptedClient(FakeResp(200, [PAYLOAD[:500]])), dest, expected_size=len(PAYLOAD)
        )
        assert result.outcome is DownloadOutcome.TRANSIENT
        assert dest.read_bytes() == PAYLOAD[:500]

    def test_checksum_mismatch_is_fatal(self, tmp_path):
        dest = tmp_path / "fw.rootfs.bz2"
        result = _attempt(
            ScriptedClient(FakeResp(200, [b"X" * len(PAYLOAD)])), dest, expected_size=len(PAYLOAD)
        )
        assert result.outcome is DownloadOutcome.FATAL
        assert "checksum" in result.detail.lower()
        assert not dest.exists()

    def test_disk_write_error_is_fatal(self, tmp_path, monkeypatch):
        dest = tmp_path / "fw.rootfs.bz2"
        real_open = open

        class _Full:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def write(self, data):
                raise OSError(28, "No space left on device")

        def fake_open(path, mode="r", *a, **kw):
            if str(path) == str(dest) and "b" in mode and ("w" in mode or "a" in mode):
                return _Full()
            return real_open(path, mode, *a, **kw)

        monkeypatch.setattr("builtins.open", fake_open)
        result = _attempt(ScriptedClient(FakeResp(200, [PAYLOAD])), dest)
        assert result.outcome is DownloadOutcome.FATAL

    def test_resume_sends_range_and_completes(self, tmp_path):
        dest = tmp_path / "fw.rootfs.bz2"
        dest.write_bytes(PAYLOAD[:400])
        client = ScriptedClient(
            FakeResp(206, [PAYLOAD[400:]], content_range=f"bytes 400-1023/{len(PAYLOAD)}")
        )
        result = _attempt(client, dest, expected_size=len(PAYLOAD))
        assert result.outcome is DownloadOutcome.OK
        assert client.ranges == ["bytes=400-"]
        assert dest.read_bytes() == PAYLOAD

    def test_416_restart_honours_expected_size(self, tmp_path):
        """The 416 restart forwards expected_size: a short body after the
        restart is TRANSIENT with the partial kept, not a checksum failure."""
        dest = tmp_path / "fw.rootfs.bz2"
        dest.write_bytes(b"S" * 100)
        client = ScriptedClient(FakeResp(416), FakeResp(200, [PAYLOAD[:600]]))
        result = _attempt(client, dest, expected_size=len(PAYLOAD))
        assert result.outcome is DownloadOutcome.TRANSIENT
        assert client.ranges == ["bytes=100-", None]
        assert dest.read_bytes() == PAYLOAD[:600]

    def test_content_range_mismatch_restart_honours_expected_size(self, tmp_path):
        dest = tmp_path / "fw.rootfs.bz2"
        dest.write_bytes(PAYLOAD[:100])
        client = ScriptedClient(
            FakeResp(206, [b"G" * 50], content_range=f"bytes 50-99/{len(PAYLOAD)}"),
            FakeResp(200, [PAYLOAD[:600]]),
        )
        result = _attempt(client, dest, expected_size=len(PAYLOAD))
        assert result.outcome is DownloadOutcome.TRANSIENT
        assert client.ranges == ["bytes=100-", None]
        assert dest.read_bytes() == PAYLOAD[:600]

    def test_restart_paths_flag_restarted(self, tmp_path):
        dest = tmp_path / "fw.rootfs.bz2"
        dest.write_bytes(b"S" * 100)
        client = ScriptedClient(FakeResp(416), FakeResp(200, [PAYLOAD[:600]]))
        assert _attempt(client, dest, expected_size=len(PAYLOAD)).restarted is True

        dest.write_bytes(b"S" * (len(PAYLOAD) + 10))  # oversized stale partial
        client = ScriptedClient(FakeResp(502))
        assert _attempt(client, dest, expected_size=len(PAYLOAD)).restarted is True

    def test_plain_resume_and_ignored_range_are_not_restarts(self, tmp_path):
        """A 200 for a Range request is not flagged — otherwise a server that
        ignores Range would count as progress forever."""
        dest = tmp_path / "fw.rootfs.bz2"
        dest.write_bytes(PAYLOAD[:400])
        client = ScriptedClient(FakeResp(200, [PAYLOAD[:300]]))
        assert _attempt(client, dest, expected_size=len(PAYLOAD)).restarted is False

    def test_download_uses_long_read_timeout(self, tmp_path):
        """Slow links stall for longer than the 30 s API default between reads."""
        client = ScriptedClient(FakeResp(200, [PAYLOAD]))
        _attempt(client, tmp_path / "fw")
        assert client.timeouts[0] == ota.DOWNLOAD_TIMEOUT
        assert ota.DOWNLOAD_TIMEOUT > 30

    def test_legacy_bool_wrapper(self, tmp_path):
        assert (
            ota.download_firmware_resumable(
                http_client=ScriptedClient(FakeResp(502)),
                download_url="/x",
                expected_checksum=CHECKSUM,
                dest_path=str(tmp_path / "fw"),
            )
            is False
        )


# ---------------------------------------------------------------- retry loop


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.waits: list[float] = []

    def clock(self):
        return self.now

    def wait(self, seconds):
        """Stand-in for ``shutdown_event.wait``: advance time, no shutdown."""
        self.waits.append(seconds)
        self.now += seconds
        return False


def _retry(attempts, clock, dest, *, stall_limit_s=3600, rng=lambda: 1.0, wait=None, **kw):
    """Run download_with_retry with scripted attempt results.

    Each scripted item is ``(DownloadResult, bytes_on_disk_after)`` — the
    fake attempt writes that many bytes so progress tracking sees them.
    """
    calls = []

    def fake_attempt(**akw):
        result, size = attempts.pop(0)
        calls.append(akw)
        if size is not None:
            dest.write_bytes(b"x" * size)
        clock.now += 1  # an attempt takes some time
        return result

    out = ota.download_with_retry(
        http_client=object(),
        download_url="/x",
        expected_checksum="",
        dest_path=str(dest),
        expected_size=kw.pop("expected_size", 1000),
        stall_limit_s=stall_limit_s,
        wait=wait or clock.wait,
        clock=clock.clock,
        rng=rng,
        attempt=fake_attempt,
        **kw,
    )
    return out, calls


T = DownloadResult(DownloadOutcome.TRANSIENT, "timeout")
OK = DownloadResult(DownloadOutcome.OK)
FATAL = DownloadResult(DownloadOutcome.FATAL, "HTTP 404")


class TestRetryLoop:
    def test_ok_first_try_does_not_wait(self, tmp_path):
        clock = FakeClock()
        out, calls = _retry([(OK, 1000)], clock, tmp_path / "fw")
        assert out.outcome is DownloadOutcome.OK
        assert clock.waits == []
        assert len(calls) == 1

    def test_transient_then_ok(self, tmp_path):
        clock = FakeClock()
        out, calls = _retry([(T, None), (T, None), (OK, 1000)], clock, tmp_path / "fw")
        assert out.outcome is DownloadOutcome.OK
        assert len(calls) == 3
        assert clock.waits == [10, 20]

    def test_fatal_stops_immediately(self, tmp_path):
        clock = FakeClock()
        out, calls = _retry([(T, None), (FATAL, None)], clock, tmp_path / "fw")
        assert out.outcome is DownloadOutcome.FATAL
        assert out.detail == "HTTP 404"
        assert len(calls) == 2
        assert len(clock.waits) == 1

    def test_backoff_doubles_and_caps_at_max(self, tmp_path):
        clock = FakeClock()
        attempts = [(T, None)] * 9 + [(OK, 1000)]
        _retry(attempts, clock, tmp_path / "fw", stall_limit_s=10**6)
        assert clock.waits == [10, 20, 40, 80, 160, 320, 600, 600, 600]

    def test_backoff_does_not_overflow_after_many_stalled_failures(self, tmp_path):
        """With a huge stall limit the loop may fail thousands of times in a
        row; the backoff exponent must not overflow the float."""
        clock = FakeClock()
        attempts = [(T, None)] * 2000 + [(OK, 1000)]
        out, calls = _retry(attempts, clock, tmp_path / "fw", stall_limit_s=10**12)
        assert out.outcome is DownloadOutcome.OK
        assert len(calls) == 2001
        assert max(clock.waits) == 600

    def test_jitter_scales_delay_down(self, tmp_path):
        clock = FakeClock()
        _retry([(T, None), (OK, 1000)], clock, tmp_path / "fw", rng=lambda: 0.0)
        # rng()=0 -> lowest jitter factor (half the base delay)
        assert clock.waits == [5]

    def test_gives_up_after_stall_limit_without_progress(self, tmp_path):
        clock = FakeClock()
        attempts = [(T, None)] * 50
        out, calls = _retry(attempts, clock, tmp_path / "fw", stall_limit_s=300)
        assert out.outcome is DownloadOutcome.TRANSIENT
        assert "timeout" in out.detail
        # 10+20+40+80+160 = 310 > 300 -> stop before sleeping past the limit
        assert sum(clock.waits) <= 300
        assert len(calls) == len(clock.waits) + 1

    def test_progress_resets_backoff_and_stall_window(self, tmp_path):
        """A slow link that drops every few minutes but keeps advancing the
        partial must never give up, and must not back off to 10 min."""
        clock = FakeClock()
        sizes = [100, 200, 300, 400, 500, 600, 700, 800, 900]
        attempts = [(T, s) for s in sizes] + [(OK, 1000)]
        out, calls = _retry(attempts, clock, tmp_path / "fw", stall_limit_s=15)
        assert out.outcome is DownloadOutcome.OK
        assert clock.waits == [10] * len(sizes)

    def test_partial_that_does_not_grow_is_no_progress(self, tmp_path):
        """A server that ignores Range (200 from 0) and drops at the same
        offset each time makes no net progress -> eventually gives up."""
        clock = FakeClock()
        attempts = [(T, 500)] * 50
        out, _ = _retry(attempts, clock, tmp_path / "fw", stall_limit_s=100)
        assert out.outcome is DownloadOutcome.TRANSIENT

    def test_restart_from_zero_measures_progress_from_zero(self, tmp_path):
        """A stale partial discarded by the attempt (416 / Content-Range
        mismatch) must not hide the progress made after the restart."""
        dest = tmp_path / "fw"
        dest.write_bytes(b"x" * 900)
        clock = FakeClock()
        restarted = DownloadResult(DownloadOutcome.TRANSIENT, "drop", restarted=True)
        sizes = [100, 200, 300, 400, 500, 600, 700, 800]
        attempts = [(restarted, sizes[0])] + [(T, s) for s in sizes[1:]] + [(OK, 1000)]
        out, _ = _retry(attempts, clock, dest, stall_limit_s=15)
        assert out.outcome is DownloadOutcome.OK
        assert clock.waits == [10] * len(sizes)

    def test_repeated_forced_restarts_still_hit_stall_limit(self, tmp_path):
        clock = FakeClock()
        restarted = DownloadResult(DownloadOutcome.TRANSIENT, "416 loop", restarted=True)
        out, _ = _retry([(restarted, 300)] * 50, clock, tmp_path / "fw", stall_limit_s=100)
        assert out.outcome is DownloadOutcome.TRANSIENT

    def test_shutdown_during_wait_aborts_without_failure(self, tmp_path):
        clock = FakeClock()
        out, calls = _retry([(T, None), (OK, 1000)], clock, tmp_path / "fw", wait=lambda s: True)
        assert out.outcome is DownloadOutcome.ABORTED
        assert len(calls) == 1

    def test_attempt_args_are_forwarded(self, tmp_path):
        clock = FakeClock()
        _, calls = _retry([(OK, 1000)], clock, tmp_path / "fw", expected_size=1000)
        assert calls[0]["download_url"] == "/x"
        assert calls[0]["dest_path"] == str(tmp_path / "fw")
        assert calls[0]["expected_size"] == 1000

    def test_real_attempt_resumes_after_mid_stream_drop(self, tmp_path):
        """End-to-end through the real attempt: drop at 300 B, 502, then a
        206 from 300 -> complete, verified file, no failure."""
        dest = tmp_path / "fw.rootfs.bz2"
        client = ScriptedClient(
            FakeResp(200, [PAYLOAD[:300]], raise_after=requests.exceptions.ConnectionError("x")),
            FakeResp(502),
            FakeResp(206, [PAYLOAD[300:]], content_range=f"bytes 300-1023/{len(PAYLOAD)}"),
        )
        clock = FakeClock()
        out = ota.download_with_retry(
            http_client=client,
            download_url="/x",
            expected_checksum=CHECKSUM,
            dest_path=str(dest),
            expected_size=len(PAYLOAD),
            stall_limit_s=3600,
            wait=clock.wait,
            clock=clock.clock,
            rng=lambda: 1.0,
        )
        assert out.outcome is DownloadOutcome.OK
        assert client.ranges == [None, "bytes=300-", "bytes=300-"]
        assert dest.read_bytes() == PAYLOAD
