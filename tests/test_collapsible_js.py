"""Runs the Node suite for static/js/collapsible.js (collapsible control-page sections).
Skips without node; CI has node, so it runs there."""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_FILE = REPO_ROOT / "tests" / "js" / "collapsible.test.mjs"


def test_collapsible_js():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH — JS collapsible suite skipped")
    assert TEST_FILE.exists(), f"missing {TEST_FILE}"
    result = subprocess.run(
        [node, str(TEST_FILE)], cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, (
        f"{TEST_FILE.name} failed (exit {result.returncode})\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
