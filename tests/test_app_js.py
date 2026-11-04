"""Runs the Node behavioural suite for static/js/app.js (site-wide script).

Skips if node is not on PATH. Node IS installed in CI, so this must run and
pass there.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_FILES = [
    # Hidden-tab gate for data-poll-visible-only htmx polls (no eval'd filter).
    REPO_ROOT / "tests" / "js" / "app-poll-visibility.test.mjs",
    # [data-tab-link] activates the target tab and moves focus to it.
    REPO_ROOT / "tests" / "js" / "app-tab-link.test.mjs",
]


@pytest.mark.parametrize("test_file", TEST_FILES, ids=lambda p: p.name)
def test_app_js(test_file):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH — JS suite skipped")
    assert test_file.exists(), f"missing {test_file}"
    result = subprocess.run(
        [node, str(test_file)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"{test_file.name} failed (exit {result.returncode})\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
