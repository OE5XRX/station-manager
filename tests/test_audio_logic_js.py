"""Runs the Node pure-logic suite for static/js/audio-logic.js.

Skips if node is not on PATH. Node IS installed in CI, so this must run and
pass there — the audio wire logic (§5.3 frame codec, presets, mixer, jitter/
seq-loss) is the correctness foundation the Alpine audio component relies on
and can only be exercised outside a browser via Node.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_FILES = [
    REPO_ROOT / "tests" / "js" / "audio-logic.test.mjs",
    # Behavioural: TX-meter dispatch/watchdog + capture-gain wiring in audio-panel.js.
    REPO_ROOT / "tests" / "js" / "audio-panel-txmeter.test.mjs",
]


@pytest.mark.parametrize("test_file", TEST_FILES, ids=lambda p: p.name)
def test_audio_logic_js(test_file):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH — JS pure-logic suite skipped")
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
