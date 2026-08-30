# Color-Token Centralization + Palette C Light — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `static/css/tokens.css` the single source of every color; every other file (CSS/templates/JS) references only tokens; add a CI guard enforcing it; complete the Palette C **light** theme with full dark↔light token parity anchored to the branding repo; center the logout icon.

**Architecture:** Extract both `:root` token blocks into a dedicated `tokens.css`. Replace every raw color literal elsewhere with `var(--token)` or `color-mix(in srgb, var(--token) N%, transparent)`. The xterm.js terminal reads tokens via `getComputedStyle` and re-themes on toggle. A Python guard (mirroring the existing `check_template_comments.py`) fails CI on any raw color outside `tokens.css`, on broken dark↔light parity, or on anchor tokens drifting from `branding/color/tokens.json` (pinned git submodule @ v0.2.5).

**Tech Stack:** Django 6 templates, plain CSS custom properties + `color-mix`, vanilla JS (xterm.js), Python 3.14 guard script, git submodule, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-08-30-color-token-centralization-design.md`

## Global Constraints

- **Single source of color:** `static/css/tokens.css` is the ONLY file allowed to contain raw color literals (`#hex`, `rgb()/rgba()/hsl()/hsla()`). Everywhere else: `var(--…)`, `color-mix(…)`, `currentColor`, `transparent`, `inherit`, `none` only.
- **Branding is the source, pinned + enforced:** `OE5XRX/branding` git submodule pinned to **`v0.2.5`**; anchor tokens MUST equal `branding/color/tokens.json` per theme (mapping table below).
- **Full parity:** every custom property defined in the `:root` (dark) block is also defined in the `:root[data-theme="light"]` block, and vice-versa. Both blocks live in `tokens.css`.
- **`color-mix` for transparency**, never raw rgba, outside `tokens.css`. Browser baseline (Chrome/Edge 111, FF 113, Safari 16.2) accepted.
- **Process:** feature branch `feat/color-token-centralization` (exists), UI work invokes `Skill("frontend-design")`, token-preview (both themes) for user look-approval before merge, Copilot loop, ONE squash PR, deploy via `servers` `main.yml` workflow_dispatch. Commit trailer `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- Verification is guard + `python manage.py check` + `collectstatic --dry-run` + visual (both themes); use the venv at `.venv/bin/python`. `manage.py check` prints an OIDC-key warning + "1 silenced" — that is pre-existing/OK.

### New tokens (added in BOTH theme blocks — dark value / light value)

| token | dark | light | purpose |
|---|---|---|---|
| `--primary` | `#3AC6D6` | `#123B54` | branding `primary` anchor (marine in light; = accent in dark) |
| `--shadow-color` | `#000000` | `#0D1A24` | base for shadows/scrims via color-mix |
| `--sheen-color` | `#FFFFFF` | `#FFFFFF` | base for inset highlights via color-mix |
| `--on-accent-ink` | `#08131A` | `#F4F7F9` | text/glyphs on accent/warn fills |
| `--term-bg` | `#000000` | `#000000` | xterm console background (black both themes) |

### Canonical color-literal → replacement map (used by every sweep task)

Chromatic rgba triplets → token (keep the original alpha `A`, write `color-mix(in srgb, var(--TOKEN) {A×100}%, transparent)`):

| rgb triplet | token | | rgb triplet | token |
|---|---|---|---|---|
| `58,198,214` | `--accent` | | `255,184,77` | `--warn` |
| `77,184,112` | `--signal` | | `240,96,96` | `--danger` |
| `95,191,224` | `--cyan` | | `155,107,255` | `--violet` |
| `0,0,0` | `--shadow-color` | | `255,255,255` | `--sheen-color` |

Hex → replacement:

| hex | replacement |
|---|---|
| `#08131A`, `#0C0D10`, `#08090C` | `var(--on-accent-ink)` |
| `#000`, `#000000` (surface/mask/bg) | `var(--shadow-color)`; for the xterm container bg use `var(--term-bg)` |
| `#fff`, `#ffffff` | `var(--sheen-color)` |
| `#ccc` (print border) | `var(--line)` |
| `#1C8058` (progress "ok" start) | `color-mix(in srgb, var(--signal) 70%, var(--shadow-color))` |
| `#C78A1E` (progress "warn" start) | `color-mix(in srgb, var(--warn) 70%, var(--shadow-color))` |
| `#D23250` (progress "danger" start) | `color-mix(in srgb, var(--danger) 70%, var(--shadow-color))` |
| `#5B3CB8` (violet deep) | `color-mix(in srgb, var(--violet) 70%, var(--shadow-color))` |
| `#3AC6D6` (SVG accent) | `var(--accent)` |

In SVG presentation attributes, alpha goes in a separate `*-opacity` attribute (a plain number, not a color): `fill="var(--accent)" fill-opacity="0.5"`, `stroke="var(--accent)" stroke-opacity="0.3"`, `stop-color="var(--accent)" stop-opacity="0.35"`.

### Branding anchor mapping (guard check c)

| branding key | station token | dark | light |
|---|---|---|---|
| `bg` | `--bg-1` | `#0A1219` | `#F4F7F9` |
| `surface` | `--bg-2` | `#0F1C28` | `#E8EFF4` |
| `text` | `--ink-0` | `#D8ECF5` | `#0D1A24` |
| `muted` | `--ink-2` | `#7AAFCA` | `#526070` |
| `primary` | `--primary` | `#3AC6D6` | `#123B54` |
| `accent` | `--accent` | `#3AC6D6` | `#0F7A87` |
| `border` | `--line` | `#1A2D3D` | `#C8D6E0` |
| `success` | `--signal` | `#4DB870` | `#1B6B35` |
| `warn` | `--warn` | `#FFB84D` | `#8A5200` |
| `error` | `--danger` | `#F06060` | `#B02020` |

---

### Task 1: Pin branding as a git submodule @ v0.2.5

**Files:**
- Create: `.gitmodules`
- Create: `vendor/branding` (submodule, pinned to tag `v0.2.5`)

**Interfaces:**
- Produces: `vendor/branding/color/tokens.json` readable at a fixed version (guard consumes it in Task 2).

- [ ] **Step 1: Add the submodule at the pinned tag**

```bash
cd /home/pbuchegger/OE5XRX/station-manager
git submodule add https://github.com/OE5XRX/branding.git vendor/branding
git -C vendor/branding fetch --tags --depth 1 origin tag v0.2.5
git -C vendor/branding checkout v0.2.5
git add .gitmodules vendor/branding
```

- [ ] **Step 2: Verify the pin + the tokens file is present**

```bash
git submodule status vendor/branding      # expect the commit for tag v0.2.5, prefixed
.venv/bin/python -c "import json; d=json.load(open('vendor/branding/color/tokens.json'))['color']; print(d['light']['bg'], d['dark']['primary'])"
```
Expected: prints `#F4F7F9 #3AC6D6`.

- [ ] **Step 3: Commit**

```bash
git commit -m "build: pin OE5XRX/branding submodule @ v0.2.5 (color token source)"
```

---

### Task 2: Color-token guard script + unit tests (the completeness oracle)

**Files:**
- Create: `scripts/check_color_tokens.py`
- Test: `tests/test_check_color_tokens.py`

**Interfaces:**
- Produces: `python scripts/check_color_tokens.py` → exit 0 clean, exit 1 + `file:line` findings. Flags: `--only <path>` (scan a single file). Used as the verification oracle by Tasks 4–8.
- Consumes: `vendor/branding/color/tokens.json` (Task 1) for anchor check.

- [ ] **Step 1: Write the guard script**

Behavior:
- **Scan set:** `static/css/*.css` except `static/css/tokens.css`; `templates/**/*.html`; `apps/**/templates/**/*.html`; `static/js/*.js`.
- **Check (a) — no raw color:** flag any `#` hex color (`#([0-9A-Fa-f]{3,4}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})\b`) or `rgb(`/`rgba(`/`hsl(`/`hsla(` token. **Ignore:** HTML numeric entities `&#\d+;?` and `&#x[0-9A-Fa-f]+;?`; the exact substring `<meta name="theme-color"` line; occurrences inside `var(...)`/`color-mix(...)` are not hex/rgba so they pass naturally.
- **Check (b) — parity:** parse the two blocks in `tokens.css` (`:root {…}` and `:root[data-theme="light"] {…}`); the set of `--custom-property` names must be identical. Report missing-in-light / missing-in-dark.
- **Check (c) — anchor == branding:** for each row of the anchor mapping (embed the mapping in the script), assert `tokens.css` defines the station token with the branding value from `vendor/branding/color/tokens.json` for each theme. Allow ONE documented exception: light `--accent` may differ from branding `accent` (`#0F7A87`) IF a same-file comment `/* accent-light-aa-override: <hex> */` is present (AA-contrast carve-out); still assert it is a valid hex.
- Print `path:line: message` per finding; exit 1 if any; else print `color-tokens: OK` and exit 0.

```python
#!/usr/bin/env python3
"""Guard: all colors live in static/css/tokens.css; anchors match branding; dark/light parity.
Mirrors scripts/check_template_comments.py. Run in CI (Lint) and locally."""
import json, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOKENS = ROOT / "static/css/tokens.css"
BRANDING = ROOT / "vendor/branding/color/tokens.json"

SCAN_GLOBS = ["static/css/*.css", "templates/**/*.html",
              "apps/**/templates/**/*.html", "static/js/*.js"]
HEX = re.compile(r"#(?:[0-9A-Fa-f]{8}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{3,4})\b")
FUNC = re.compile(r"\b(?:rgba?|hsla?)\s*\(")
ENTITY = re.compile(r"&#(?:x[0-9A-Fa-f]+|\d+);?")
ANCHORS = {  # branding key -> (station token, )
    "bg": "--bg-1", "surface": "--bg-2", "text": "--ink-0", "muted": "--ink-2",
    "primary": "--primary", "accent": "--accent", "border": "--line",
    "success": "--signal", "warn": "--warn", "error": "--danger",
}

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
                findings.append(f"{p.relative_to(ROOT)}:{i}: raw color literal — use var(--token)/color-mix")
    return findings

def parse_block(text, selector):
    m = re.search(re.escape(selector) + r"\s*\{(.*?)\}", text, re.S)
    return set(re.findall(r"(--[a-z0-9-]+)\s*:", m.group(1))) if m else set()

def check_parity(text):
    dark = parse_block(text, ":root")
    light = parse_block(text, ':root[data-theme="light"]')
    out = []
    for t in sorted(dark - light): out.append(f"tokens.css: {t} defined in :root but missing in light")
    for t in sorted(light - dark): out.append(f"tokens.css: {t} defined in light but missing in :root")
    return out

def token_value(text, selector, token):
    m = re.search(re.escape(selector) + r"\s*\{(.*?)\}", text, re.S)
    if not m: return None
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
                    out.append(f"tokens.css: light --accent must equal documented override {aa.group(1)}")
                continue
            if got != want:
                out.append(f"tokens.css: {selector} {token}={got!r} must equal branding {key}={want!r}")
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
            findings += check_parity(text) + check_anchors(text)
    if findings:
        print("\n".join(findings)); print(f"\ncolor-tokens: {len(findings)} finding(s)"); sys.exit(1)
    print("color-tokens: OK"); sys.exit(0)

if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Write unit tests on fixtures**

```python
# tests/test_check_color_tokens.py
import subprocess, sys, textwrap
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
```

- [ ] **Step 3: Run the unit tests**

Run: `.venv/bin/python -m pytest tests/test_check_color_tokens.py -v`
Expected: all PASS.

- [ ] **Step 4: Baseline the real repo (expected to fail now)**

Run: `.venv/bin/python scripts/check_color_tokens.py; echo "exit=$?"`
Expected: exit=1 with many findings (tokens.css doesn't exist yet + literals everywhere). This confirms the oracle detects the current state. Do NOT wire into CI yet (Task 9).

- [ ] **Step 5: Commit**

```bash
git add scripts/check_color_tokens.py tests/test_check_color_tokens.py
git commit -m "build: color-token guard (raw-color + parity + branding-anchor checks) with unit tests"
```

---

### Task 3: Extract `tokens.css` (dark) + new tokens + link before app.css

**Files:**
- Create: `static/css/tokens.css`
- Modify: `static/css/app.css` (remove lines 7–83, the two `:root` blocks)
- Modify: `templates/base.html:10` (add tokens.css link before app.css)
- Modify: `apps/accounts/templates/accounts/login.html:9` (same)

**Interfaces:**
- Consumes: nothing.
- Produces: `tokens.css` with the full `:root` (dark) + `:root[data-theme="light"]` blocks; new dark tokens `--primary/--shadow-color/--sheen-color/--on-accent-ink/--term-bg`; dark derived alpha tokens as `color-mix`. (The light block is moved as-is here; Task 8 rewrites it to Palette C.)

- [ ] **Step 1: Create `static/css/tokens.css`** — move the entire current `:root {…}` (dark, app.css lines 7–56) and `:root[data-theme="light"] {…}` (lines 59–83) into it verbatim, then:
  - Add to the **dark** `:root`: `--primary:#3AC6D6; --shadow-color:#000000; --sheen-color:#FFFFFF; --on-accent-ink:#08131A; --term-bg:#000000;`
  - Convert the dark derived alpha tokens to `color-mix` (visually identical):
    ```css
    --accent-glow: color-mix(in srgb, var(--accent) 22%, transparent);
    --accent-tint: color-mix(in srgb, var(--accent)  8%, transparent);
    --signal-soft: color-mix(in srgb, var(--signal) 14%, transparent);
    --warn-soft:   color-mix(in srgb, var(--warn)   14%, transparent);
    --danger-soft: color-mix(in srgb, var(--danger) 15%, transparent);
    --cyan-soft:   color-mix(in srgb, var(--cyan)   12%, transparent);
    --violet-soft: color-mix(in srgb, var(--violet) 14%, transparent);
    ```
  - Convert the dark shadow tokens off raw black: `--shadow-sm/-md/-lg` use `color-mix(in srgb, var(--shadow-color) N%, transparent)` instead of `rgba(0,0,0,N)` (sm .4→40%, md .8→80%, lg .9→90%).
  - Keep non-color tokens (radius/fonts/metrics/motion) in the dark block.
  - Leave the `:root[data-theme="light"]` block with its CURRENT values verbatim — Task 8 rewrites it to Palette C with the full parity set. The new tokens are added ONLY to the dark `:root` here; parity/anchor are not checked until Task 8/9 (Tasks 4–7 verify with `--only`, which scans raw colors only). Light mode may look slightly off between Task 3 and Task 8 — acceptable mid-plan.

- [ ] **Step 2: Remove the two `:root` blocks from `app.css`** (lines 7–83) so `app.css` begins at the reset comment. Leave everything else untouched.

- [ ] **Step 3: Link tokens.css before app.css** in both `templates/base.html` and `apps/accounts/templates/accounts/login.html`:

```html
  <link rel="stylesheet" href="{% static 'css/fonts.css' %}" nonce="{{ csp_nonce }}">
  <link rel="stylesheet" href="{% static 'css/tokens.css' %}" nonce="{{ csp_nonce }}">
  <link rel="stylesheet" href="{% static 'css/app.css' %}" nonce="{{ csp_nonce }}">
```

- [ ] **Step 4: Verify dark render unchanged + static wiring**

```bash
cd /home/pbuchegger/OE5XRX/station-manager
.venv/bin/python manage.py check
.venv/bin/python manage.py collectstatic --dry-run --no-input | grep -i 'css/tokens.css'
.venv/bin/python scripts/check_color_tokens.py --only static/css/tokens.css; echo "tokens raw-scan exit=$? (tokens.css is exempt so this scans nothing → OK)"
```
Expected: check OK; collectstatic sees tokens.css.

- [ ] **Step 5: Commit**

```bash
git add static/css/tokens.css static/css/app.css templates/base.html apps/accounts/templates/accounts/login.html
git commit -m "refactor(css): extract tokens.css; add primary/shadow/sheen/on-accent-ink/term tokens; dark alphas via color-mix"
```

---

### Task 4: Sweep `app.css` literals → tokens (+ logout icon fix)

**Files:**
- Modify: `static/css/app.css` (all raw color literals; `.btn-icon` at lines ~716 and ~1509)

**Interfaces:**
- Consumes: tokens from Task 3.
- Produces: `app.css` with zero raw color literals; `.btn-icon` centered.

- [ ] **Step 1: Replace every raw color literal** in `app.css` per the canonical map (Global Constraints). Specifics for the known sites:
  - `.pill-*` `border-color: rgba(…)` → `color-mix(in srgb, var(--TOKEN) 30%, transparent)` (accent pill uses 35%); TOKEN per the pill's semantic (`--signal/--danger/--cyan/--warn/--accent/--violet`).
  - `@keyframes` pulses `rgba(77,184,112,0.6|0)` → `color-mix(in srgb, var(--signal) 60%|0%, transparent)`.
  - `--btn-fg: #08131A` / `color:#08131A` / `#0C0D10` / `#08090C` → `var(--on-accent-ink)`.
  - `--btn-border: rgba(240,96,96,0.35)` → `color-mix(in srgb, var(--danger) 35%, transparent)`.
  - progress-bar gradient starts `#1C8058/#C78A1E/#D23250` → the `color-mix(... 70%, var(--shadow-color))` forms from the map.
  - `box-shadow ... rgba(77,184,112,0.35)` → `color-mix(in srgb, var(--signal) 35%, transparent)`.
  - atmosphere gradients `rgba(95,191,224,0.05|0.06)` → `color-mix(in srgb, var(--cyan) 5%|6%, transparent)`; grid `rgba(255,255,255,0.012|0.015|0.02)` → `color-mix(in srgb, var(--sheen-color) …%, transparent)`; mask `#000` → `var(--shadow-color)`.
  - `background:#000` (xterm container ~1093) → `var(--term-bg)`.
  - overlays `rgba(0,0,0,0.6|0.85)` → `color-mix(in srgb, var(--shadow-color) 60%|85%, transparent)`.
  - `.pager-links .current color:#0C0D10` → `var(--on-accent-ink)`.
  - print `#ccc` → `var(--line)`.

- [ ] **Step 2: Fix logout icon centering** — add `justify-content: center;` to the `.btn-icon` rule (~line 716) and to the desktop `.btn-icon { min-width:40px; … }` rule (~line 1509).

- [ ] **Step 3: Verify app.css is color-clean**

Run: `.venv/bin/python scripts/check_color_tokens.py --only static/css/app.css; echo exit=$?`
Expected: `color-tokens: OK`, exit=0.

- [ ] **Step 4: Commit**

```bash
git add static/css/app.css
git commit -m "refactor(css): tokenize all app.css color literals; center .btn-icon"
```

---

### Task 5: Sweep `control-panel.css` literals → tokens

**Files:**
- Modify: `static/css/control-panel.css`

**Interfaces:**
- Consumes: tokens from Task 3. Produces: `control-panel.css` color-clean.

- [ ] **Step 1: Replace every raw literal** per the canonical map. All chromatic rgba (`58,198,214 / 77,184,112 / 95,191,224 / 255,184,77 / 240,96,96`) → `color-mix(in srgb, var(--TOKEN) {alpha%}, transparent)`; neutral `rgba(0,0,0,α)` → `color-mix(in srgb, var(--shadow-color) {α%}, transparent)`; `rgba(255,255,255,α)` → `color-mix(in srgb, var(--sheen-color) {α%}, transparent)`; any hex per the map.

- [ ] **Step 2: Verify**

Run: `.venv/bin/python scripts/check_color_tokens.py --only static/css/control-panel.css; echo exit=$?`
Expected: `color-tokens: OK`.

- [ ] **Step 3: Commit**

```bash
git add static/css/control-panel.css
git commit -m "refactor(css): tokenize all control-panel.css color literals"
```

---

### Task 6: Sweep template SVGs + inline `<style>` → tokens

**Files:**
- Modify: `templates/oauth2_provider/authorize.html`
- Modify: `apps/accounts/templates/accounts/login.html`
- Modify: `apps/sso/templates/sso/application_detail.html`
- Modify: `apps/sso/templates/sso/tag_detail.html`

**Interfaces:**
- Consumes: tokens from Task 3. Produces: those templates color-clean (theme-color meta exempt).

- [ ] **Step 1: `authorize.html`** — decorative SVG + inline `<style>`:
  - `<stop stop-color="#3AC6D6" stop-opacity="X"/>` → `stop-color="var(--accent)" stop-opacity="X"`.
  - `stroke="rgba(58,198,214,A)"` → `stroke="var(--accent)" stroke-opacity="A"`; `fill="rgba(58,198,214,A)"`/`<g fill="rgba(…)">` → `fill="var(--accent)" fill-opacity="A"`.
  - `fill="#0A1219"` (RP/IDP box) → `fill="var(--bg-1)"`; `fill="#3AC6D6"` (text/circle) → `fill="var(--accent)"`; `stroke="#08131A"` (check) → `stroke="var(--on-accent-ink)"`.
  - inline `<style>`: `linear-gradient(135deg, var(--violet), #5B3CB8)` → second stop `color-mix(in srgb, var(--violet) 70%, var(--shadow-color))`; `color:#fff` → `var(--sheen-color)`; `color:#08131A` → `var(--on-accent-ink)`; `box-shadow ... rgba(155,107,255,0.4)` → `color-mix(in srgb, var(--violet) 40%, transparent)`; `rgba(58,198,214,0.45)` → `color-mix(in srgb, var(--accent) 45%, transparent)`; scanline `rgba(255,255,255,0.015)` → `color-mix(in srgb, var(--sheen-color) 1.5%, transparent)`.

- [ ] **Step 2: `login.html`** — decorative SVG: grid `stroke="rgba(58,198,214,0.08)"` → `stroke="var(--accent)" stroke-opacity="0.08"`; `<stop stop-color="#3AC6D6" stop-opacity="X"/>` → `var(--accent)`; orbit circles `stroke="rgba(58,198,214,A)"` → `stroke="var(--accent)" stroke-opacity="A"`; `<g fill="#3AC6D6">` → `fill="var(--accent)"`; `<g stroke="rgba(58,198,214,0.3)">` → `stroke="var(--accent)" stroke-opacity="0.3"`.

- [ ] **Step 3: `application_detail.html` + `tag_detail.html`** — inline `style="…box-shadow:0 0 8px rgba(77,184,112,0.5)"` → `color-mix(in srgb, var(--signal) 50%, transparent)`; `application_detail.html` inline `<style>`: `color:#08131A` → `var(--on-accent-ink)`; `box-shadow ... rgba(58,198,214,0.35)` → `color-mix(in srgb, var(--accent) 35%, transparent)`.

- [ ] **Step 4: Verify the four templates are color-clean**

```bash
for f in templates/oauth2_provider/authorize.html apps/accounts/templates/accounts/login.html apps/sso/templates/sso/application_detail.html apps/sso/templates/sso/tag_detail.html; do
  .venv/bin/python scripts/check_color_tokens.py --only "$f" || echo "FAIL $f"
done; echo done
```
Expected: `color-tokens: OK` for each.

- [ ] **Step 5: Commit**

```bash
git add templates/oauth2_provider/authorize.html apps/accounts/templates/accounts/login.html apps/sso/templates/sso/application_detail.html apps/sso/templates/sso/tag_detail.html
git commit -m "refactor(templates): tokenize decorative SVG + inline-style colors"
```

---

### Task 7: xterm.js terminal reads tokens + follows theme toggle

**Files:**
- Modify: `static/js/app.js` (terminal theme block ~297; theme `applyTheme` ~11)

**Interfaces:**
- Consumes: `--term-bg/--ink-0/--accent/--on-accent-ink` from tokens. Produces: terminal theme built from tokens; re-applied on toggle; `app.js` color-clean.

- [ ] **Step 1: Add a token reader + theme-change event.** Near the top helpers add:

```javascript
  function tok(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }
  function termThemeFromTokens() {
    return { background: tok("--term-bg"), foreground: tok("--ink-0"),
             cursor: tok("--accent"), cursorAccent: tok("--on-accent-ink"),
             selectionBackground: tok("--accent-tint") };
  }
```
In `applyTheme`, after setting `data-theme`, notify listeners:
```javascript
    document.dispatchEvent(new CustomEvent("oe5xrx:themechange"));
```

- [ ] **Step 2: Use tokens for the terminal + re-theme on toggle.** Replace the hardcoded `theme: { … }` in the `new Terminal({…})` call with `theme: termThemeFromTokens()`, and after `term.open(host);` add:
```javascript
    document.addEventListener("oe5xrx:themechange", function () {
      term.options.theme = termThemeFromTokens();
    });
```

- [ ] **Step 2b: Verify color-clean**

Run: `.venv/bin/python scripts/check_color_tokens.py --only static/js/app.js; echo exit=$?`
Expected: `color-tokens: OK`.

- [ ] **Step 3: Manual smoke** — open a station terminal page, toggle theme: background/foreground/cursor update (dark = black/#D8ECF5/cyan). Note in the report (no automated JS test in this repo for xterm).

- [ ] **Step 4: Commit**

```bash
git add static/js/app.js
git commit -m "refactor(js): xterm theme from CSS tokens; re-theme on toggle"
```

---

### Task 8: Palette C light block (full parity, anchored to branding)

**Files:**
- Modify: `static/css/tokens.css` (the `:root[data-theme="light"]` block)

**Interfaces:**
- Consumes: branding submodule (Task 1), guard (Task 2). Produces: light block = Palette C with full parity; guard checks (b)+(c) pass.

- [ ] **Step 1: Invoke `Skill("frontend-design")`** (light-theme color values).

- [ ] **Step 2: Rewrite the light block** with a value for EVERY token the dark block defines (parity), anchored per the mapping table. Proposed values (adjust the derived ones for WCAG AA + live look, keep anchors exact):
```css
:root[data-theme="light"] {
  --bg-1:#F4F7F9; --bg-2:#E8EFF4; --bg-0:#E9EEF2; --bg-3:#FFFFFF; --bg-hover:#DDE7EE;
  --line:#C8D6E0; --line-bright:#B4C6D3; --line-hot:#9DB3C3;
  --ink-0:#0D1A24; --ink-2:#526070; --ink-1:#334657; --ink-3:#8497A6;
  --primary:#123B54;
  --accent:#0E7580; --accent-soft:#3AA7B3; --accent-deep:#0B5C66;   /* accent-light-aa-override: #0E7580 */
  --accent-glow: color-mix(in srgb, var(--accent) 22%, transparent);
  --accent-tint: color-mix(in srgb, var(--accent)  8%, transparent);
  --signal:#1B6B35; --signal-soft: color-mix(in srgb, var(--signal) 14%, transparent);
  --warn:#8A5200;   --warn-soft:   color-mix(in srgb, var(--warn)   14%, transparent);
  --danger:#B02020; --danger-soft: color-mix(in srgb, var(--danger) 15%, transparent);
  --cyan:#0B6E8C;   --cyan-soft:   color-mix(in srgb, var(--cyan)   12%, transparent);
  --violet:#5B3CB8; --violet-soft: color-mix(in srgb, var(--violet) 14%, transparent);
  --shadow-color:#0D1A24; --sheen-color:#FFFFFF; --on-accent-ink:#F4F7F9; --term-bg:#000000;
  --shadow-sm: 0 1px 0 0 color-mix(in srgb, var(--shadow-color) 6%, transparent), 0 0 0 1px var(--line);
  --shadow-md: 0 6px 24px -12px color-mix(in srgb, var(--shadow-color) 14%, transparent), 0 0 0 1px var(--line);
  --shadow-lg: 0 32px 80px -40px color-mix(in srgb, var(--shadow-color) 20%, transparent), 0 0 0 1px var(--line);
  --shadow-glow: 0 0 32px -4px var(--accent-glow);
}
```
If `#0E7580` still fails AA as body/link text on `--bg-1`, darken `--accent` further and update the `accent-light-aa-override` comment to the new hex (guard honors it).

- [ ] **Step 3: Verify parity + anchors + contrast**

```bash
.venv/bin/python scripts/check_color_tokens.py; echo "full-guard exit=$?"
```
Expected: `color-tokens: OK` (raw-color clean across repo + parity + anchors). Then manually verify (frontend-design) AA ≥ 4.5:1 for `--ink-0/--ink-1/--ink-2` and `--accent`-as-text on `--bg-1/--bg-2` in light; record ratios in the report.

- [ ] **Step 4: Commit**

```bash
git add static/css/tokens.css
git commit -m "feat(css): Palette C light theme — full parity, branding-anchored"
```

---

### Task 9: Wire the guard into CI (completeness gate)

**Files:**
- Modify: `.github/workflows/ci.yml` (Lint job)

**Interfaces:**
- Consumes: guard (Task 2), all sweeps (Tasks 3–8). Produces: CI fails on any future raw color / parity / drift.

- [ ] **Step 1: Add `submodules: true` to the Lint job checkout** and a guard step after the template-comment check:

```yaml
      - uses: actions/checkout@v6
        with:
          submodules: true
      # … existing ruff + template-comment steps …
      - name: Check color tokens
        run: python scripts/check_color_tokens.py
```

- [ ] **Step 2: Full local guard + Django checks**

```bash
cd /home/pbuchegger/OE5XRX/station-manager
.venv/bin/python scripts/check_color_tokens.py; echo "guard exit=$?"
.venv/bin/python -m pytest tests/test_check_color_tokens.py -q
.venv/bin/python manage.py check
.venv/bin/python manage.py collectstatic --dry-run --no-input | tail -2
```
Expected: guard OK (exit 0); unit tests pass; check OK; collectstatic clean.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: enforce color-token guard in Lint (checkout submodules)"
```

---

### Task 10: Token-preview (both themes) + verification + PR

**Files:**
- Create (temporary, not committed): `/tmp/token-preview.*`

**Interfaces:**
- Consumes: all prior tasks. Produces: user look-approval (both themes); one squash PR.

- [ ] **Step 1: Build a two-theme preview.** Serve `static/` via `python3 -m http.server` (detached, as before) with a standalone `static/_preview.html` that links `css/fonts.css` + `css/tokens.css` + `css/app.css` and shows the sidebar tower, cards, all status pills, buttons, focus ring, mono values — with a button toggling `document.documentElement.dataset.theme`. Give the user LAN + Tailscale URLs. (Remove `_preview.html` before the PR.)

- [ ] **Step 2: User look-approval — BOTH themes.** Confirm: light = Palette C (marine/teal on `#F4F7F9`), dark unchanged; accent vs info-cyan distinguishable; contrast fine. Do not merge before approval.

- [ ] **Step 3: Watcher + full verification**

```bash
.venv/bin/python scripts/check_color_tokens.py; echo "guard exit=$?"
.venv/bin/python -m pytest tests/test_check_color_tokens.py -q
.venv/bin/python manage.py check
```
Dispatch `audit` on the diff; `probe` E2E smoke (login renders both themes, terminal loads, manifest/sw served). Fix findings.

- [ ] **Step 4: Push + ONE squash PR + Copilot loop.** `git push -u origin feat/color-token-centralization`; open PR summarizing token centralization + Palette C light + guard; run the Copilot loop; squash-merge.

- [ ] **Step 5: Deploy + live check.** Trigger `OE5XRX/servers` `main.yml` (workflow_dispatch); after success verify `remote.oe5xrx.org` in both themes; confirm the guard is green in CI.
