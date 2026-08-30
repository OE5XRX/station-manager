# station-manager Brand-Rebrand (Marine/Cyan) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert the station-manager dark UI fully to the OE5XRX Marine/Cyan brand — Palette C (dark) tokens, self-hosted IBM Plex everywhere, tower mark instead of the "5X" badge, and brand favicon/PWA-icons/theme-color.

**Architecture:** `app.css` is fully tokenized (no hard amber outside `:root`), so the core is a `:root` token remap that cascades through the whole UI. Around it: self-host the brand WOFF2 (drop the Google CDN), swap the `.brand-mark` badge for the inline tower SVG, replace identity asset bytes, and recolor the few remaining amber references in JS and decorative SVGs. Dark-only; tokens are structured so a `[data-theme="light"]` scheme can be added later.

**Tech Stack:** Django 6.0 templates, plain CSS custom properties, vanilla JS (xterm.js terminal), self-hosted WOFF2 fonts. No build step for CSS.

**Spec:** `docs/superpowers/specs/2026-08-30-station-manager-brand-rebrand-design.md`

## Global Constraints

- **Brand source:** `/home/pbuchegger/OE5XRX/branding` (Palette C dark, IBM Plex, tower mark `logo/oe5xrx-mark.svg`, export assets under `export/`).
- **Fonts — brand weights ONLY:** IBM Plex Sans 400/600/700 + IBM Plex Mono 400/600. **No 500.** The branding repo does not ship a 500 weight → do not introduce one. Every existing `font-weight: 500` must be remapped to a brand weight (600 default; 400 for plain body text). Verify `grep -rn 'font-weight: *500' static/ templates/ apps/` → 0.
- **Fonts self-hosted:** no `fonts.googleapis.com` / `fonts.gstatic.com` requests anywhere; no `Bricolage` anywhere.
- **Accent:** Cyan `#3AC6D6` (`--accent`). Amber survives only as `--warn: #FFB84D`.
- **theme-color:** `#0A1219` (brand dark bg) everywhere it currently is `#FF8A3D`.
- **Inline `<script>` comments:** only `/* */`, never `//` (brand convention — minified inline scripts collapse to one line; a `//` comments out the rest). (No new inline scripts are planned here, but honor it if one is added.)
- **Rest-amber scan after all tasks:** `grep -riE 'FF8A3D|FF4E1F|D96418|Bricolage' static/ templates/ apps/` → 0 (or explicitly documented).
- **Process:** feature branch `feat/oe5xrx-brand-rebrand` (already created off `origin/main`); UI work MUST invoke `Skill("frontend-design")`; token-preview PNG render for user look-approval **before** merge; Copilot review loop; **one squash PR** at the end.
- **Verification is grep + `python manage.py check` + visual**, not unit tests (CSS/template rebrand has no meaningful unit surface). Each task's "test" is a check command that fails before and passes after.

---

### Task 1: Self-hosted IBM Plex fonts, drop Google CDN, remove weight 500

**Files:**
- Create: `static/fonts/IBMPlexSans-Regular.woff2`, `IBMPlexSans-SemiBold.woff2`, `IBMPlexSans-Bold.woff2`, `IBMPlexMono-Regular.woff2`, `IBMPlexMono-SemiBold.woff2` (copied from branding)
- Create: `static/css/fonts.css` (`@font-face` block, self-hosted)
- Modify: `templates/base.html:10-14` (remove preconnects + Google CDN `<link>`, add `fonts.css` link before `app.css`)
- Modify: `apps/accounts/templates/accounts/login.html:10-13` (same: remove Google CDN link, add `fonts.css`)
- Modify: `static/css/app.css:57` (`--font-display` → IBM Plex Sans, remove Bricolage)
- Modify: `static/css/app.css` lines 187, 316, 618, 766, 863, 943, 1244, 1668 (`font-weight: 500` → brand weight)

**Interfaces:**
- Produces: self-hosted `@font-face` for `"IBM Plex Sans"` (400/600/700) and `"IBM Plex Mono"` (400/600); `--font-display: "IBM Plex Sans", …`. Later tasks assume no external font requests and no 500 weight anywhere.

- [ ] **Step 1: Copy the five brand WOFF2 into `static/fonts/`**

```bash
cd /home/pbuchegger/OE5XRX/station-manager
mkdir -p static/fonts
cp /home/pbuchegger/OE5XRX/branding/type/fonts/IBMPlexSans-Regular.woff2 \
   /home/pbuchegger/OE5XRX/branding/type/fonts/IBMPlexSans-SemiBold.woff2 \
   /home/pbuchegger/OE5XRX/branding/type/fonts/IBMPlexSans-Bold.woff2 \
   /home/pbuchegger/OE5XRX/branding/type/fonts/IBMPlexMono-Regular.woff2 \
   /home/pbuchegger/OE5XRX/branding/type/fonts/IBMPlexMono-SemiBold.woff2 \
   static/fonts/
ls static/fonts/
```
Expected: 5 files listed.

- [ ] **Step 2: Create `static/css/fonts.css` (paths relative to `static/css/`)**

```css
/* OE5XRX Brand — self-hosted IBM Plex (OFL). No external CDN (GDPR).
   Brand weights only: Sans 400/600/700, Mono 400/600. No 500. */
@font-face { font-family: "IBM Plex Sans"; font-style: normal; font-weight: 400;
  font-display: swap; src: url("../fonts/IBMPlexSans-Regular.woff2") format("woff2"); }
@font-face { font-family: "IBM Plex Sans"; font-style: normal; font-weight: 600;
  font-display: swap; src: url("../fonts/IBMPlexSans-SemiBold.woff2") format("woff2"); }
@font-face { font-family: "IBM Plex Sans"; font-style: normal; font-weight: 700;
  font-display: swap; src: url("../fonts/IBMPlexSans-Bold.woff2") format("woff2"); }
@font-face { font-family: "IBM Plex Mono"; font-style: normal; font-weight: 400;
  font-display: swap; src: url("../fonts/IBMPlexMono-Regular.woff2") format("woff2"); }
@font-face { font-family: "IBM Plex Mono"; font-style: normal; font-weight: 600;
  font-display: swap; src: url("../fonts/IBMPlexMono-SemiBold.woff2") format("woff2"); }
```

- [ ] **Step 3: In `templates/base.html`, remove the two `<link rel="preconnect">` lines and the Google CDN stylesheet `<link>`; add the local fonts stylesheet before `app.css`**

Replace the block (currently lines 10–17) so it reads:

```html
  <link rel="stylesheet" href="{% static 'css/fonts.css' %}" nonce="{{ csp_nonce }}">
  <link rel="stylesheet" href="{% static 'css/app.css' %}" nonce="{{ csp_nonce }}">
```
(Delete both `preconnect` lines and the `fonts.googleapis.com/css2?...` `<link>`.)

- [ ] **Step 4: In `apps/accounts/templates/accounts/login.html`, do the same** — remove the preconnects + Google CDN `<link>` (around lines 10–13), add `<link rel="stylesheet" href="{% static 'css/fonts.css' %}" nonce="{{ csp_nonce }}">` before its `app.css` link. (Keep whatever `{% load static %}` / nonce pattern the file already uses.)

- [ ] **Step 5: In `static/css/app.css:57`, swap the display font off Bricolage**

```css
  --font-display: "IBM Plex Sans", ui-sans-serif, system-ui, sans-serif;
```

- [ ] **Step 6: Remap every `font-weight: 500` to a brand weight**

Rule: headings / labels / emphasized UI text → `600`; plain body/secondary text → `400`. Apply per site:
- `app.css:187`, `:316`, `:618`, `:766`, `:863`, `:943` → inspect each selector; use `600` for labels/nav/buttons/headings, `400` for muted/secondary paragraph text.
- `app.css:1244` (`.summary-val.mono`) → `600`.
- `app.css:1668` (`.page-title .t-muted`) → `400` (muted subtitle).

Do them all; none may remain.

- [ ] **Step 7: Verify no CDN, no Bricolage, no weight-500, fonts resolvable**

```bash
cd /home/pbuchegger/OE5XRX/station-manager
grep -rniE 'fonts\.googleapis|fonts\.gstatic|Bricolage' static/ templates/ apps/ ; echo "cdn/bricolage exit=$?"
grep -rn 'font-weight: *500' static/ templates/ apps/ ; echo "weight500 exit=$?"
python manage.py check
python manage.py collectstatic --dry-run --no-input | grep -iE 'fonts/IBMPlex|css/fonts.css' | head
```
Expected: first two greps print nothing (`exit=1`), `check` passes, collectstatic sees the new fonts + `fonts.css`.

- [ ] **Step 8: Commit**

```bash
git add static/fonts/ static/css/fonts.css static/css/app.css templates/base.html apps/accounts/templates/accounts/login.html
git commit -m "brand: self-host IBM Plex, drop Google CDN, remove font-weight 500"
```

---

### Task 2: Remap color tokens in `:root` to Palette C (dark)

**Files:**
- Modify: `static/css/app.css:7-58` (the `:root` custom-property block — surfaces, lines, ink, accent, semantic, shadow)

**Interfaces:**
- Consumes: nothing.
- Produces: `--accent: #3AC6D6` (cyan), `--warn: #FFB84D` (amber lives here), marine surfaces/ink. Every downstream `var(--…)` in the UI recolors automatically; later tasks (mark, decorative SVGs) point their `color`/`fill` at these tokens.

- [ ] **Step 1: Invoke `Skill("frontend-design")`** before touching UI values.

- [ ] **Step 2: Replace the color tokens in `:root` with Palette C (dark)**

Set exactly (keep the existing property names; only values change):

```css
  /* Surfaces — marine dark ramp (bg #0A1219 / surface #0F1C28) */
  --bg-0:#070C11; --bg-1:#0A1219; --bg-2:#0F1C28; --bg-3:#16283A; --bg-hover:#1E3A50;
  /* Lines (border #1A2D3D) */
  --line:#1A2D3D; --line-bright:#2C4A63; --line-hot:#3E6588;
  /* Ink (text #D8ECF5, muted #7AAFCA) */
  --ink-0:#D8ECF5; --ink-1:#A9C4D6; --ink-2:#7AAFCA; --ink-3:#4E6B7E;
  /* Accent = brand cyan */
  --accent:#3AC6D6; --accent-soft:#7FE0EB; --accent-deep:#1E9EAD;
  --accent-glow:rgba(58,198,214,.22); --accent-tint:rgba(58,198,214,.08);
  /* Semantic (brand dark) — amber survives as warn */
  --signal:#4DB870; --signal-soft:rgba(77,184,112,.14);
  --warn:#FFB84D;   --warn-soft:rgba(255,184,77,.14);
  --danger:#F06060; --danger-soft:rgba(240,96,96,.15);
  /* Status kept distinct from the cyan accent */
  --cyan:#5FBFE0;   --cyan-soft:rgba(95,191,224,.12);
  --violet:#9B6BFF; --violet-soft:rgba(155,107,255,.14);
  --shadow-glow:0 0 32px -4px var(--accent-glow);
```
Match the exact property names already present in the file (e.g. if a token is named `--accent-glow`, keep that spelling). Do NOT rename tokens — only revalue. If the file has additional accent-derived tokens (e.g. `--focus-ring`) that hardcode the old amber hex, point them at `var(--accent)` / the new values too.

- [ ] **Step 3: Confirm no hard amber remains inside `:root` and none of the old hexes are used as literals in the stylesheet**

```bash
cd /home/pbuchegger/OE5XRX/station-manager
grep -niE 'FF8A3D|FF4E1F|D96418' static/css/app.css ; echo "exit=$?"
```
Expected: prints nothing (`exit=1`). (The `.brand-mark` gradient using `#FF4E1F` is handled in Task 3; if it still shows here, that is the only allowed remaining hit until Task 3 — note it and continue.)

- [ ] **Step 4: `python manage.py check`** → passes.

- [ ] **Step 5: Commit**

```bash
git add static/css/app.css
git commit -m "brand: remap :root color tokens to Palette C marine/cyan (dark)"
```

---

### Task 3: Tower mark replaces the "5X" badge

**Files:**
- Modify: `templates/includes/sidebar.html:4` (`<div class="brand-mark">5X</div>` → tower SVG)
- Modify: `apps/accounts/templates/accounts/login.html:22` (same swap)
- Modify: `static/css/app.css:264-276` (`.brand-mark` — drop amber gradient, make it a transparent SVG container tinted `var(--accent)`)

**Interfaces:**
- Consumes: `--accent` (Task 2).
- Produces: `.brand-mark` renders the inline tower SVG in cyan at 30px (sidebar) / 40px (auth). `.brand-mark svg { width:100%; height:100% }`.

- [ ] **Step 1: Invoke `Skill("frontend-design")`.**

- [ ] **Step 2: Replace the sidebar badge markup** in `templates/includes/sidebar.html` — swap `<div class="brand-mark" aria-hidden="true">5X</div>` for the inline tower (keep the `brand-mark` class + `aria-hidden`):

```html
    <div class="brand-mark" aria-hidden="true">
      <svg viewBox="0 0 128 128" xmlns="http://www.w3.org/2000/svg">
        <g fill="none" stroke="currentColor" stroke-width="5" stroke-linecap="round" stroke-linejoin="round">
          <path d="M52 43 a11 11 0 0 0 0 15"/>
          <path d="M43 37 a18 18 0 0 0 0 28"/>
          <path d="M76 43 a11 11 0 0 1 0 15"/>
          <path d="M85 37 a18 18 0 0 1 0 28"/>
          <path d="M64 52 L43 108"/>
          <path d="M64 52 L85 108"/>
          <path d="M41 108 L87 108"/>
          <path d="M55.8 74 L72.2 74"/>
          <path d="M55.8 74 L85 108"/>
          <path d="M72.2 74 L43 108"/>
        </g>
        <circle cx="64" cy="51" r="3.8" fill="currentColor"/>
        <path d="M67 14 L60 28 L65 28 L61 40 L73 26 L67 26 Z" fill="currentColor"/>
      </svg>
    </div>
```

- [ ] **Step 3: Replace the login badge markup** at `apps/accounts/templates/accounts/login.html:22` with the identical `<div class="brand-mark" aria-hidden="true">…same SVG…</div>` block.

- [ ] **Step 4: Restyle `.brand-mark`** in `static/css/app.css` (replace the amber-gradient badge with a plain tinted SVG container):

```css
.brand-mark {
  width: 30px; height: 30px;
  display: grid; place-items: center;
  color: var(--accent);
  flex-shrink: 0;
}
.brand-mark svg { width: 100%; height: 100%; display: block; }
```
(Remove `background`, `border-radius`, `box-shadow`, `font-*`, and the `#0C0D10` text color — the SVG carries the color now.) Keep the auth/mobile size overrides at `:1138` and `:1695`, but drop their now-irrelevant `border-radius`/`font-size` props if they only sized the text badge (leave `width`/`height`).

- [ ] **Step 5: Verify the "5X" text and amber gradient are gone**

```bash
cd /home/pbuchegger/OE5XRX/station-manager
grep -rn '>5X<' templates/ apps/ ; echo "5X exit=$?"
grep -niE 'FF4E1F' static/css/app.css ; echo "amber-grad exit=$?"
python manage.py check
```
Expected: both greps empty (`exit=1`), check passes.

- [ ] **Step 6: Commit**

```bash
git add templates/includes/sidebar.html apps/accounts/templates/accounts/login.html static/css/app.css
git commit -m "brand: replace 5X badge with cyan tower mark"
```

---

### Task 4: Identity assets — favicon, PWA icons, apple-touch, theme-color

**Files:**
- Modify (bytes): `static/favicon.ico` ← `branding/export/favicon.ico`
- Modify (bytes): `static/webpush/icon-192.png` ← `branding/export/pwa-192.png`; `static/webpush/icon-512.png` ← `branding/export/pwa-512.png`
- Modify: `templates/base.html:23` (`theme-color` `#FF8A3D` → `#0A1219`)
- Modify: `apps/webpush/views.py:38` (`"theme_color": "#FF8A3D"` → `"#0A1219"`)
- Modify: `apps/accounts/templates/accounts/login.html` + `templates/oauth2_provider/authorize.html` (`theme-color` meta if present → `#0A1219`)

**Interfaces:**
- Consumes: nothing (byte + string swaps).
- Produces: brand favicon/PWA icons at the existing paths (no code path changes — SW/manifest reference the same filenames).

- [ ] **Step 1: Locate the current webpush icon paths and confirm export sources exist**

```bash
cd /home/pbuchegger/OE5XRX/station-manager
ls -la static/favicon.ico static/webpush/icon-192.png static/webpush/icon-512.png
ls -la /home/pbuchegger/OE5XRX/branding/export/favicon.ico \
       /home/pbuchegger/OE5XRX/branding/export/pwa-192.png \
       /home/pbuchegger/OE5XRX/branding/export/pwa-512.png \
       /home/pbuchegger/OE5XRX/branding/export/apple-touch-icon-180.png
```
Expected: destination files and all four export sources exist. (If the webpush icons live elsewhere, use the real path from `apps/webpush/views.py` / `sw.js`.)

- [ ] **Step 2: Overwrite the identity asset bytes**

```bash
cd /home/pbuchegger/OE5XRX/station-manager
cp /home/pbuchegger/OE5XRX/branding/export/favicon.ico static/favicon.ico
cp /home/pbuchegger/OE5XRX/branding/export/pwa-192.png static/webpush/icon-192.png
cp /home/pbuchegger/OE5XRX/branding/export/pwa-512.png static/webpush/icon-512.png
```

- [ ] **Step 3: Add a dedicated brand apple-touch icon** and point `base.html` at it (cleaner than reusing the PWA icon):

```bash
cp /home/pbuchegger/OE5XRX/branding/export/apple-touch-icon-180.png static/apple-touch-icon.png
```
In `templates/base.html`, change the apple-touch line to:

```html
  <link rel="apple-touch-icon" href="{% static 'apple-touch-icon.png' %}">
```

- [ ] **Step 4: Change `theme-color` to the brand dark bg everywhere**

- `templates/base.html:23`: `<meta name="theme-color" content="#0A1219">`
- `apps/webpush/views.py:38`: `"theme_color": "#0A1219",`
- If `apps/accounts/templates/accounts/login.html` or `templates/oauth2_provider/authorize.html` contain a `theme-color` meta, set it to `#0A1219` as well. (The `#FF8A3D` hits in those two files that are `<stop>`/`fill` belong to decorative SVGs — Task 5, not here.)

- [ ] **Step 5: Verify theme-color swapped and no code refers to a missing asset**

```bash
cd /home/pbuchegger/OE5XRX/station-manager
grep -rniE 'theme.color[^>]*FF8A3D|theme_color.*FF8A3D' static/ templates/ apps/ ; echo "themecolor exit=$?"
python manage.py check
python manage.py collectstatic --dry-run --no-input | grep -iE 'apple-touch-icon|icon-192|icon-512|favicon' | head
```
Expected: theme-color grep empty (`exit=1`), check passes, collectstatic sees the assets.

- [ ] **Step 6: Commit**

```bash
git add static/favicon.ico static/webpush/icon-192.png static/webpush/icon-512.png static/apple-touch-icon.png templates/base.html apps/webpush/views.py apps/accounts/templates/accounts/login.html templates/oauth2_provider/authorize.html
git commit -m "brand: swap favicon/PWA/apple-touch assets + theme-color #0A1219"
```

---

### Task 5: Recolor remaining amber — terminal + decorative SVGs

**Files:**
- Modify: `static/js/app.js:297-298` (xterm.js theme `cursor`/`selection` amber → cyan)
- Modify: `templates/oauth2_provider/authorize.html:22-46` (SVG `stop-color`/`fill` `#FF8A3D` → `#3AC6D6`)
- Modify: `apps/accounts/templates/accounts/login.html:84-93` (SVG `stop-color`/`fill` `#FF8A3D` → `#3AC6D6`)

**Interfaces:**
- Consumes: brand cyan `#3AC6D6` (matches `--accent`).
- Produces: no amber left anywhere in JS or decorative SVGs.

- [ ] **Step 1: Invoke `Skill("frontend-design")`** (decorative SVG recolor is UI work).

- [ ] **Step 2: Recolor the xterm.js terminal theme** in `static/js/app.js` (lines ~297–298):

```javascript
      theme: { background: "#000000", foreground: "#D8ECF5", cursor: "#3AC6D6",
               selection: "rgba(58, 198, 214, 0.3)" },
```
(`foreground` moved to brand ink `--ink-0`; cursor/selection to cyan.)

- [ ] **Step 3: Recolor the authorize-page SVG** — in `templates/oauth2_provider/authorize.html`, replace every `#FF8A3D` (the two `<stop stop-color>`, the two `<text fill>`, and the `<circle fill>`) with `#3AC6D6`.

- [ ] **Step 4: Recolor the login-page SVG** — in `apps/accounts/templates/accounts/login.html`, replace every `#FF8A3D` (the two `<stop stop-color>` and the `<g fill>`) with `#3AC6D6`.

- [ ] **Step 5: Full rest-amber scan → 0**

```bash
cd /home/pbuchegger/OE5XRX/station-manager
grep -riE 'FF8A3D|FF4E1F|D96418|Bricolage' static/ templates/ apps/ ; echo "exit=$?"
python manage.py check
```
Expected: grep prints nothing (`exit=1`); check passes.

- [ ] **Step 6: Commit**

```bash
git add static/js/app.js templates/oauth2_provider/authorize.html apps/accounts/templates/accounts/login.html
git commit -m "brand: recolor terminal + decorative SVGs to cyan"
```

---

### Task 6: Token-preview render, full verification, review & PR

**Files:**
- Create (temporary, not committed): `/tmp/oe5xrx-token-preview.html` for the look-approval render

**Interfaces:**
- Consumes: all prior tasks.
- Produces: user-approved look; green Django checks; one squash PR.

- [ ] **Step 1: Build a static token-preview page** — a standalone HTML that pulls `static/css/fonts.css` + `static/css/app.css` and shows: sidebar brand (tower mark), a card, primary/secondary buttons, status pills (online `--signal`, warn `--warn`, danger `--danger`, info `--cyan`, deploy `--violet`), a focus ring, and a mono value. Render it to PNG for the user (write to a file / serve over SFTP). This is the pre-merge look-approval gate.

- [ ] **Step 2: Present the PNG to the user and get explicit look-approval.** Do not merge before approval. Check specifically: cyan accent vs. cyan-info-status (`--cyan`) remain distinguishable; text contrast `--ink-0`/`--ink-1` on `--bg-1`/`--bg-2` ≥ 4.5:1.

- [ ] **Step 3: Full static verification sweep**

```bash
cd /home/pbuchegger/OE5XRX/station-manager
echo "== rest-amber/bricolage =="; grep -riE 'FF8A3D|FF4E1F|D96418|Bricolage' static/ templates/ apps/ ; echo "exit=$?"
echo "== weight 500 =="; grep -rn 'font-weight: *500' static/ templates/ apps/ ; echo "exit=$?"
echo "== external fonts =="; grep -rniE 'fonts\.googleapis|fonts\.gstatic' static/ templates/ apps/ ; echo "exit=$?"
echo "== django check =="; python manage.py check
echo "== collectstatic =="; python manage.py collectstatic --dry-run --no-input | tail -3
```
Expected: the three greps each print nothing (`exit=1`); check passes; collectstatic clean.

- [ ] **Step 4: Watcher pass (CLAUDE.md flow)** — dispatch `audit` on the changed files and `probe` for an E2E smoke (login page renders, sidebar renders, terminal page loads, manifest + sw served, PWA icons 200). Fix any findings.

- [ ] **Step 5: Push the feature branch and open ONE PR (squash-merge)**

```bash
cd /home/pbuchegger/OE5XRX/station-manager
git push -u origin feat/oe5xrx-brand-rebrand
```
Open the PR (title e.g. `brand: full OE5XRX marine/cyan rebrand`), summarizing the token remap, self-hosted fonts, tower mark, identity assets, and the deferred light-theme note.

- [ ] **Step 6: Copilot review loop** — run the `~/.claude/skills/copilot-loop/` cycle (4 min initial, 1 min poll, 10 min total; Opus for code-quality). Address rounds until clean, then squash-merge.

- [ ] **Step 7: Live sign-off** — after deploy, the user verifies on the running app: sidebar tower mark, cyan accents, IBM Plex fonts loading locally (no CDN in network tab), brand favicon, PWA icon, mobile theme-color. Deferred (separate follow-up): light theme via `[data-theme="light"]`.
