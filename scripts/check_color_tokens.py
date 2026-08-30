#!/usr/bin/env python3
"""Guard: all colors live in static/css/tokens.css; anchors match branding; dark/light parity.
Mirrors scripts/check_template_comments.py. Run in CI (Lint) and locally."""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOKENS = ROOT / "static/css/tokens.css"
BRANDING = ROOT / "vendor/branding/color/tokens.json"

SCAN_GLOBS = [
    "static/css/*.css",
    "templates/**/*.html",
    "apps/**/templates/**/*.html",
    "static/js/*.js",
]
HEX = re.compile(r"#(?:[0-9A-Fa-f]{8}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{3,4})\b")
FUNC = re.compile(r"\b(?:rgba?|hsla?)\s*\(")
ENTITY = re.compile(r"&#(?:x[0-9A-Fa-f]+|\d+);?")
ANCHORS = {  # branding key -> (station token, )
    "bg": "--bg-1",
    "surface": "--bg-2",
    "text": "--ink-0",
    "muted": "--ink-2",
    "primary": "--primary",
    "accent": "--accent",
    "border": "--line",
    "success": "--signal",
    "warn": "--warn",
    "error": "--danger",
}


def rel(p):
    """Path relative to ROOT for display; raw path if outside ROOT (e.g. --only tmp files)."""
    try:
        return p.relative_to(ROOT)
    except ValueError:
        return p


def scan_raw_colors(only=None):
    findings = []
    files = [only] if only else [p for g in SCAN_GLOBS for p in ROOT.glob(g)]
    for path in files:
        p = Path(path)
        if p.resolve() == TOKENS.resolve():
            continue
        for i, line in enumerate(p.read_text().splitlines(), 1):
            if 'name="theme-color"' in line:
                continue
            stripped = ENTITY.sub("", line)
            if HEX.search(stripped) or FUNC.search(stripped):
                findings.append(f"{rel(p)}:{i}: raw color literal — use var(--token)/color-mix")
    return findings


def parse_block(text, selector):
    m = re.search(re.escape(selector) + r"\s*\{(.*?)\}", text, re.S)
    return set(re.findall(r"(--[a-z0-9-]+)\s*:", m.group(1))) if m else set()


def check_parity(text):
    dark = parse_block(text, ":root")
    light = parse_block(text, ':root[data-theme="light"]')
    out = []
    for t in sorted(dark - light):
        out.append(f"tokens.css: {t} defined in :root but missing in light")
    for t in sorted(light - dark):
        out.append(f"tokens.css: {t} defined in light but missing in :root")
    return out


def token_value(text, selector, token):
    m = re.search(re.escape(selector) + r"\s*\{(.*?)\}", text, re.S)
    if not m:
        return None
    v = re.search(re.escape(token) + r"\s*:\s*([^;]+);", m.group(1))
    return v.group(1).strip().lower() if v else None


def check_anchors(text):
    brand = json.loads(BRANDING.read_text())["color"]
    out = []
    for selector, theme in ((":root", "dark"), (':root[data-theme="light"]', "light")):
        aa = re.search(r"/\*\s*accent-light-aa-override:\s*(#[0-9A-Fa-f]{6})\s*\*/", text)
        for key, token in ANCHORS.items():
            want = brand[theme][key].lower()
            got = token_value(text, selector, token)
            if token == "--accent" and theme == "light" and aa:
                if got != aa.group(1).lower():
                    out.append(
                        f"tokens.css: light --accent must equal documented override {aa.group(1)}"
                    )
                continue
            if got != want:
                out.append(
                    f"tokens.css: {selector} {token}={got!r} must equal branding {key}={want!r}"
                )
    return out


def main():
    only = None
    if len(sys.argv) == 3 and sys.argv[1] == "--only":
        only = sys.argv[2]
    findings = scan_raw_colors(only)
    if not only:
        if not TOKENS.exists():
            findings.append("static/css/tokens.css: missing (extract tokens here)")
        else:
            text = TOKENS.read_text()
            findings += check_parity(text)
            if not BRANDING.exists():
                findings.append(
                    "vendor/branding/color/tokens.json: missing — run "
                    "`git submodule update --init` (branding submodule provides the anchor values)"
                )
            else:
                findings += check_anchors(text)
    if findings:
        print("\n".join(findings))
        print(f"\ncolor-tokens: {len(findings)} finding(s)")
        sys.exit(1)
    print("color-tokens: OK")
    sys.exit(0)


if __name__ == "__main__":
    main()
