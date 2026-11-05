"""Runs the Node pure-logic suite for static/js/metrics-history-logic.js.

Skips if node is not on PATH. Node IS installed in CI, so this must run and
pass there. TZ is pinned to UTC so the local-time label assertions are
deterministic.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_FILE = REPO_ROOT / "tests" / "js" / "metrics-history-logic.test.mjs"


def test_metrics_history_logic_js():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH — JS pure-logic suite skipped")
    assert TEST_FILE.exists(), f"missing {TEST_FILE}"
    result = subprocess.run(
        [node, str(TEST_FILE)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "TZ": "UTC"},
    )
    assert result.returncode == 0, (
        f"{TEST_FILE.name} failed (exit {result.returncode})\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
