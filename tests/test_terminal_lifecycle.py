"""Unit tests for the station_agent.terminal shell-lifecycle helpers.

Covers the decoupled shell lifecycle introduced so the web terminal can
survive transient browser disconnects: ``_shell_alive`` detection,
idempotent ``_ensure_shell`` (spawn only when dead), ``_restart_shell``
(stop then fresh spawn), and dispatch of the ``ensure``/``restart``
control messages in ``_handle_message``.

This repo has NO ``pytest-asyncio``. Every coroutine under test is driven
via ``asyncio.run(...)`` inside a plain sync test function, matching the
established pattern in ``tests/test_terminal_agent.py``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

terminal_mod = pytest.importorskip(
    "station_agent.terminal",
    reason="station_agent deps (PyYAML) not installed in this environment",
)
TerminalClient = terminal_mod.TerminalClient

from station_agent.config import AgentConfig  # noqa: E402


def _client():
    cfg = MagicMock(spec=AgentConfig)
    cfg.ed25519_key_path = "/nonexistent"
    cfg.terminal_shell = "/bin/sh"
    # Bypass key loading in __init__.
    with patch("station_agent.terminal.load_private_key", return_value=MagicMock()):
        return TerminalClient(cfg)


def test_shell_alive_false_when_no_process():
    c = _client()
    assert c._shell_alive() is False


def test_shell_alive_true_when_process_running():
    c = _client()
    proc = MagicMock()
    proc.poll.return_value = None  # still running
    c._process = proc
    c._master_fd = 5
    assert c._shell_alive() is True


def test_shell_alive_false_when_process_exited():
    c = _client()
    proc = MagicMock()
    proc.poll.return_value = 0  # exited
    c._process = proc
    c._master_fd = 5
    assert c._shell_alive() is False


async def _noop_reader(*_args, **_kwargs):
    return None


def test_ensure_shell_spawns_when_dead():
    c = _client()
    c._ws = MagicMock()
    started = {"n": 0}

    def fake_start():
        started["n"] += 1
        proc = MagicMock()
        proc.poll.return_value = None
        return 7, proc

    async def scenario():
        with (
            patch.object(c, "_start_shell", side_effect=fake_start),
            patch.object(c, "_read_shell_output", side_effect=_noop_reader),
        ):
            await c._ensure_shell()
            await c._ensure_shell()  # second call: shell alive -> no-op

    asyncio.run(scenario())
    assert started["n"] == 1


def test_restart_shell_stops_then_starts():
    c = _client()
    c._ws = MagicMock()
    calls = []
    proc = MagicMock()
    proc.poll.return_value = None
    c._process = proc
    c._master_fd = 7

    async def fake_stop():
        calls.append("stop")
        c._process = None
        c._master_fd = None

    def fake_start():
        calls.append("start")
        p = MagicMock()
        p.poll.return_value = None
        return 8, p

    async def scenario():
        with (
            patch.object(c, "_stop_shell", side_effect=fake_stop),
            patch.object(c, "_start_shell", side_effect=fake_start),
            patch.object(c, "_read_shell_output", side_effect=_noop_reader),
        ):
            await c._restart_shell()

    asyncio.run(scenario())
    assert calls == ["stop", "start"]


def test_handle_message_dispatches_ensure_and_restart():
    c = _client()
    counters = {"ensure": 0, "restart": 0}

    async def fake_ensure():
        counters["ensure"] += 1

    async def fake_restart():
        counters["restart"] += 1

    async def scenario():
        with (
            patch.object(c, "_ensure_shell", side_effect=fake_ensure),
            patch.object(c, "_restart_shell", side_effect=fake_restart),
        ):
            await c._handle_message('{"type":"ensure"}')
            await c._handle_message('{"type":"restart"}')

    asyncio.run(scenario())
    assert counters["ensure"] == 1 and counters["restart"] == 1
