# tests/test_check_color_tokens.py
import subprocess, sys
from pathlib import Path
SCRIPT = Path(__file__).resolve().parent.parent / "scripts/check_color_tokens.py"

def run_only(tmp_path, content):
    f = tmp_path / "sample.css"; f.write_text(content)
    return subprocess.run([sys.executable, str(SCRIPT), "--only", str(f)],
                          capture_output=True, text=True)

def test_flags_hex(tmp_path):
    assert run_only(tmp_path, ".a{color:#FF8A3D;}").returncode == 1

def test_flags_rgba(tmp_path):
    assert run_only(tmp_path, ".a{border-color:rgba(1,2,3,.3);}").returncode == 1

def test_allows_var_and_colormix(tmp_path):
    ok = ".a{color:var(--accent);background:color-mix(in srgb,var(--danger) 30%,transparent);}"
    assert run_only(tmp_path, ok).returncode == 0

def test_ignores_html_entities(tmp_path):
    f = tmp_path / "t.html"; f.write_text("<span>&#8212; &#8226; &#10003;</span>")
    r = subprocess.run([sys.executable, str(SCRIPT), "--only", str(f)], capture_output=True, text=True)
    assert r.returncode == 0

def test_ignores_theme_color_meta(tmp_path):
    f = tmp_path / "t.html"; f.write_text('<meta name="theme-color" content="#0A1219">')
    r = subprocess.run([sys.executable, str(SCRIPT), "--only", str(f)], capture_output=True, text=True)
    assert r.returncode == 0
