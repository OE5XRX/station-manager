"""Runs the Node pure-logic suite for static/js/control-logic.js.

Skips if node is not on PATH. Node IS installed in CI, so this must run and
pass there — the JS control logic (DE-locale parse, PTT state machine, telemetry
percent, lock derivation) is the correctness foundation the Alpine component
relies on and can only be exercised outside a browser via Node.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_FILE = REPO_ROOT / "tests" / "js" / "control-logic.test.mjs"


def test_control_logic_js():
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
    )
    # Surface the Node output so a failing assertion is legible in pytest output.
    assert result.returncode == 0, (
        f"control-logic.test.mjs failed (exit {result.returncode})\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
