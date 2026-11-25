"""Runs the Node suites of the control-page SSTV section: pure helpers
(static/js/sstv-logic.js), the sstvPanel component with the control-panel bridge and
the voice-PTT block, and the audio panel's mic block. Skips without node; CI has node."""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SUITES = ["sstv-logic.test.mjs", "sstv-panel.test.mjs", "audio-panel-sstv.test.mjs"]


@pytest.mark.parametrize("suite", SUITES)
def test_sstv_js(suite):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH — JS SSTV suites skipped")
    test_file = REPO_ROOT / "tests" / "js" / suite
    assert test_file.exists(), f"missing {test_file}"
    result = subprocess.run(
        [node, str(test_file)], cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, (
        f"{suite} failed (exit {result.returncode})\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
