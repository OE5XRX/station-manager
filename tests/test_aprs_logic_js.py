"""Runs the Node suite for static/js/aprs-logic.js (skips without node)."""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_FILE = REPO_ROOT / "tests" / "js" / "aprs-logic.test.mjs"


def test_aprs_logic_js():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH — JS suite skipped")
    result = subprocess.run(
        [node, str(TEST_FILE)], cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
