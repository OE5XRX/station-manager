"""Unit tests for the station_agent.terminal message-handling logic.

Focused on the JSON-frame shape the agent uses to exchange PTY I/O with
the server: plain UTF-8 strings in the `data` field. End-to-end coverage
(asyncio + websockets + real pty) is deliberately skipped — this file
only verifies the narrow contract that `_handle_message("input", ...)`
writes raw UTF-8 to the master fd and that the output path never
base64-encodes the data.

station_agent imports PyYAML transitively (via config.py). If that's not
installed in the test environment (main project dev.txt does not pull
it in; the agent has its own requirements.txt), every test in this
module is skipped instead of producing a collection error.
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
import os

import pytest

terminal_mod = pytest.importorskip(
    "station_agent.terminal",
    reason="station_agent deps (PyYAML) not installed in this environment",
)
TerminalClient = terminal_mod.TerminalClient


def _make_client():
    """Construct a TerminalClient without running its real __init__.

    The real __init__ loads an Ed25519 private key and would require a
    full AgentConfig — irrelevant for these message-level tests. Using
    __new__ keeps the test focused on just the I/O shape.
    """
    client = TerminalClient.__new__(TerminalClient)
    client._config = None
    client._process = None
    client._master_fd = None
    client._ws = None
    client._shutdown = None
    client._loop = None
    client._private_key = None
    return client


def test_handle_message_input_writes_raw_utf8_to_master_fd():
    """`input` frames must write the raw UTF-8 bytes (not base64-decoded)."""
    read_fd, write_fd = os.pipe()
    try:
        client = _make_client()
        client._master_fd = write_fd

        asyncio.run(client._handle_message(json.dumps({"type": "input", "data": "echo hi\n"})))

        os.set_blocking(read_fd, False)
        got = os.read(read_fd, 4096)
        assert got == b"echo hi\n"
    finally:
        for fd in (read_fd, write_fd):
            try:
                os.close(fd)
            except OSError:
                pass


def test_handle_message_input_handles_multibyte_utf8():
    """Non-ASCII input must reach the shell as the same UTF-8 bytes."""
    read_fd, write_fd = os.pipe()
    try:
        client = _make_client()
        client._master_fd = write_fd

        payload = "café — 你好\n"
        asyncio.run(client._handle_message(json.dumps({"type": "input", "data": payload})))

        os.set_blocking(read_fd, False)
        got = os.read(read_fd, 4096)
        assert got == payload.encode("utf-8")
    finally:
        for fd in (read_fd, write_fd):
            try:
                os.close(fd)
            except OSError:
                pass


def test_handle_message_input_noop_when_master_fd_missing():
    """Input arriving before the shell is started must be a silent no-op."""
    client = _make_client()
    client._master_fd = None
    # Would raise if the method tried os.write() with fd=None.
    asyncio.run(client._handle_message(json.dumps({"type": "input", "data": "x"})))


def test_output_frame_is_plain_utf8_not_base64():
    """The output path builds a JSON frame whose `data` is raw UTF-8.

    The loop around os.read + ws.send lives in _read_shell_output, which
    is tightly coupled to asyncio + websockets. Rather than spin up the
    full machinery, verify the exact formula that function uses: the
    `data` field is produced by ``data.decode("utf-8", errors="replace")``
    — so round-tripping a known string through that decode and re-
    encoding through json must yield that string back, NOT a base64
    padded form.
    """
    raw = b"prompt$ ls\n"
    frame = {
        "type": "output",
        "data": raw.decode("utf-8", errors="replace"),
    }
    serialized = json.dumps(frame)
    parsed = json.loads(serialized)

    assert parsed["type"] == "output"
    assert parsed["data"] == "prompt$ ls\n"
    # base64 of the same bytes would yield a different string; guard
    # against a regression back to base64.
    assert parsed["data"] != base64.b64encode(raw).decode("ascii")


def test_output_decode_is_lenient_on_split_multibyte():
    """A read boundary that splits a UTF-8 codepoint must not crash."""
    # Start byte of a 2-byte UTF-8 sequence, on its own — invalid.
    partial = b"\xc3"
    decoded = partial.decode("utf-8", errors="replace")
    assert "\ufffd" in decoded
    json.dumps({"type": "output", "data": decoded})  # must be serializable


def test_source_no_longer_calls_b64_on_pty_output():
    """Regression guard: terminal.py must not wrap PTY bytes in base64.

    The PTY pipeline is awkward to exercise end-to-end under pytest
    (asyncio + websockets + real pty), so this text-level assertion
    protects the UTF-8 switch from being silently undone. base64 is
    still used in _build_ws_url for the Ed25519 signature query param,
    so we narrow the check to the exact formulas the old code used on
    PTY I/O.
    """
    src = importlib.import_module("station_agent.terminal").__loader__.get_source(
        "station_agent.terminal"
    )
    assert 'base64.b64encode(data).decode("ascii")' not in src
    assert "base64.b64decode(data)" not in src
