# Color-Token Centralization + Palette C Light Theme — Design

**Datum:** 2026-08-30
**Status:** Design (Spec-Review offen)
**Branch:** `feat/color-token-centralization` (off `origin/main` @ 87ead1c)
**Quelle der Brand-Werte:** OE5XRX Palette C (dark + **light**), branding-Repo `color/tokens.json`

## Zusammenfassung

Zwei zusammenhängende Ziele:

1. **Eine einzige Quelle für Farben.** Alle Farbwerte (Hex/rgba) leben ausschließlich
   in **einem** Design-Token-File (`static/css/tokens.css`). Kein roher Farbcode mehr
   irgendwo sonst — nicht in `app.css`/`control-panel.css`-Regeln, nicht in Template-
   SVGs/HTML, nicht in `app.js`. Alles referenziert nur Tokens (`var(--…)`,
   `color-mix(… var(--…) …)`, `currentColor`). Ein **CI-Guard** hält das dauerhaft.
2. **Light-Theme voll auf Palette C Light.** Der bereits existierende
   `[data-theme="light"]`-Block + Topbar-Toggle werden auf die maßgeblichen Brand-
   Light-Werte umgestellt, mit **vollständiger Token-Parität** zum Dark-Block.

Nebenbei: **Logout-Icon zentrieren** (kleiner CSS-Bug, `.btn-icon`).

**Motivation (Nutzer):** „Ich habe keine Ahnung von Frontend — was ich definitiv nicht
mehr will, ist dass irgendwo im HTML-Code Farbcodes versteckt sind. Am besten so
strukturiert wie möglich." → Farbänderungen dürfen künftig **nur** in `tokens.css`
passieren; alles andere zieht automatisch nach (Hell/Dunkel + alle Komponenten).

## Kontext / Ausgangslage

- `static/css/app.css` (~1880 Z.): `:root`-Block (dark) + `:root[data-theme="light"]`-
  Block (Z. 7–83) enthalten die Tokens. **Außerhalb** dieser Blöcke liegen noch ~20+
  hartkodierte Status/Cyan-rgba-Literale (Pill-Borders, Fokus-Glows, Keyframes) auf
  **Dark**-Werten → im Light-Mode falsch.
- `static/css/control-panel.css`: nutzt schon 176× `var(--…)`, hat aber ~10 hartkodierte
  Status-rgba + neutrale `rgba(0,0,0,…)`/`rgba(255,255,255,…)`.
- **Template-SVGs** (`accounts/login.html`, `oauth2_provider/authorize.html`): dekorative
  Grafiken mit hartkodierten `#3AC6D6`/rgba + `<stop stop-color="…">`. Das ist „Farbcode
  im HTML".
- `static/js/app.js`: xterm.js-Terminal-Theme mit Hardcodes (`#000000`, `#D8ECF5`,
  `#3AC6D6`, `rgba(58,198,214,0.3)`).
- Toggle: Topbar-Button `data-theme-toggle` + `app.js` setzt `data-theme` auf `<html>`
  und persistiert in `localStorage`. **Existiert bereits** (nicht Teil dieses Vorhabens,
  wird nur konsumiert).
- Light-Block heute: eigene Nicht-Brand-Werte (bg `#F3F4F6`, accent `#1E9EAD`, …) und
  **unvollständige Parität** (z. B. `*-soft` fehlen → Fallback auf Dark-Werte).

## Entscheidungen (aus Brainstorming)

| Thema | Entscheidung |
|-------|--------------|
| Single source of truth | **Alle Tokens in `static/css/tokens.css`** (dark + light Block). Einziges File mit rohen Farbwerten. |
| Transparenzen | **`color-mix(in srgb, var(--token) N%, transparent)`** statt hartkodierter rgba außerhalb der Tokens. |
| Neutrale Overlays | Shadows/Scrims/Sheens über Tokens (`--shadow-color`, `--sheen-color`) + `color-mix` — auch `rgba(0,0,0,…)` verschwindet aus Komponenten. |
| Template-SVGs | `fill/stroke/stop-color="var(--token)"` (+ `*-opacity`-Zahl für Alpha). Kein Farbcode im HTML. |
| JS-Terminal | Farbwerte via `getComputedStyle(document.documentElement).getPropertyValue('--…')` lesen; bei Theme-Toggle neu setzen. |
| Light-Werte | **Palette C Light** (`branding/color/tokens.json`), volle Parität zum Dark-Block. |
| Guard | CI-Check: **kein** `#hex`/`rgb()/rgba()/hsl()` außerhalb `tokens.css`; + **Token-Parität** dark↔light. |
| Prozess | Feature-Branch → **ein Squash-PR** → Copilot-Loop → Deploy via `servers`-Workflow. |

## Architektur

### 1. `static/css/tokens.css` — die einzige Farbquelle
Die beiden `:root`-Blöcke (Z. 7–83 in `app.css`) werden **vollständig** hierher
verschoben (alle Custom Properties: Farben, Shadows, Radius, Fonts, Metrics, Motion).
`app.css` beginnt danach beim Reset. Lade-Reihenfolge (in `base.html` **und**
`accounts/login.html`): `fonts.css` → **`tokens.css`** → `app.css` (→ `control-panel.css`).

**Regel:** `tokens.css` ist das **einzige** File, in dem rohe Farbwerte (`#hex`,
`rgb/rgba/hsl`) stehen dürfen.

### 2. Neutrale Overlay-Tokens
Neu (in beiden Blöcken, damit Komponenten kein `rgba(0,0,0/255…)` mehr brauchen):
```
--shadow-color   /* dark: #000000  · light: #0D1A24  — für Schatten/Scrims       */
--sheen-color    /* dark: #FFFFFF  · light: #FFFFFF   — für Inset-Highlights      */
```
Komponenten nutzen `color-mix(in srgb, var(--shadow-color) N%, transparent)` statt
`rgba(0,0,0,N)`. Die vorhandenen `--shadow-sm/-md/-lg` bleiben Tokens (leben in
`tokens.css`) und werden intern auf `--shadow-color` umgestellt.

### 3. Derivations-Tokens (Alpha) via `color-mix`
Bestehende Alpha-Tokens werden aus ihrem Basis-Token abgeleitet, damit sie Theme-Wechsel
automatisch folgen und in beiden Blöcken identisch stehen können:
```
--accent-glow: color-mix(in srgb, var(--accent) 22%, transparent);
--accent-tint: color-mix(in srgb, var(--accent)  8%, transparent);
--signal-soft: color-mix(in srgb, var(--signal) 14%, transparent);
--warn-soft:   color-mix(in srgb, var(--warn)   14%, transparent);
--danger-soft: color-mix(in srgb, var(--danger) 15%, transparent);
--cyan-soft:   color-mix(in srgb, var(--cyan)   12%, transparent);
--violet-soft: color-mix(in srgb, var(--violet) 14%, transparent);
--accent-soft, --accent-deep: eigenständige Hue-Werte je Theme (keine Ableitung).
```

### 4. Vollständiger Light-Block (Palette C Light) — Parität zum Dark-Block
Zielwerte (Anker aus `branding/color/tokens.json`: bg `#F4F7F9`, surface `#E8EFF4`,
text `#0D1A24`, muted `#526070`, primary `#123B54`, accent `#0F7A87`, border `#C8D6E0`,
success `#1B6B35`, warn `#8A5200`, error `#B02020`). Vorschlag (Implementer verifiziert
**WCAG AA ≥ 4.5:1** für Text/Muted/Accent-als-Text + Live-Look, justiert innerhalb
Palette C):
```
--bg-0:#E9EEF2; --bg-1:#F4F7F9; --bg-2:#FFFFFF; --bg-3:#E8EFF4; --bg-hover:#DDE7EE;
--line:#D5E0E8; --line-bright:#C8D6E0; --line-hot:#A9BDCB;
--ink-0:#0D1A24; --ink-1:#334657; --ink-2:#526070; --ink-3:#8497A6;
--accent:#0E7580; --accent-soft:#3AA7B3; --accent-deep:#0B5C66;   /* AA als Text prüfen */
--signal:#1B6B35; --warn:#8A5200; --danger:#B02020; --cyan:#0B6E8C; --violet:#5B3CB8;
--shadow-color:#0D1A24; --sheen-color:#FFFFFF; --on-accent-ink:#F4F7F9;
```
Dark-Block behält seine Werte; erhält zusätzlich `--shadow-color:#000000`,
`--sheen-color:#FFFFFF`, `--on-accent-ink:#08131A` (das heute hartkodierte kühle
Near-Black auf Akzent/Warn).

**Parität:** Jedes in einem Block definierte Token existiert auch im anderen.

### 5. Tokenisierung der Rest-Literale
- **`app.css` / `control-panel.css`:** jede rohe rgba/hex außerhalb `tokens.css` →
  `var(--token)` bzw. `color-mix(in srgb, var(--token) N%, transparent)` (Alpha aus dem
  rgba übernommen). Neutrale `rgba(0,0,0/255…)` → `color-mix(… var(--shadow-color|--sheen-color) …)`.
  Near-Black-Textfarben (`#08131A`, `#0C0D10`) → `var(--on-accent-ink)`.
- **Template-SVGs + Inline-`<style>`** (`login.html`, `authorize.html`,
  `sso/application_detail.html`, `sso/tag_detail.html`): `#3AC6D6`/rgba →
  `fill/stroke/stop-color="var(--token)"`; Alpha via `fill-opacity`/`stroke-opacity`/
  `stop-opacity` (reine Zahl, kein Farbcode). Farbwerte in Inline-`<style>`-Blöcken →
  `var(--token)`/`color-mix`. `tower_mark.html` nutzt bereits `currentColor` (unverändert).
- **`app.js`:** Terminal-Theme baut aus `getComputedStyle`-gelesenen Tokens
  (`--bg-0`→background, `--ink-0`→foreground, `--accent`→cursor, cursorAccent/selection
  via Token). Beim Theme-Toggle (bestehender Handler in `app.js`) `term.options.theme`
  neu setzen, damit auch das Terminal Hell/Dunkel folgt.

### 6. Logout-Icon zentrieren
`.btn` hat `align-items:center`, aber kein `justify-content`. `.btn-icon` (und die
Desktop-40×40-Variante) bekommen `justify-content:center` → Icon zentriert.

### 7. CI-Guard: `scripts/check_color_tokens.py` (im `Lint`-Job)
Fail-closed, wenn:
- **(a)** ein `#hex` (3/4/6/8-stellig) oder `rgb()/rgba()/hsl()/hsla()`-Literal
  **außerhalb** `static/css/tokens.css` auftaucht — gescannt: `static/css/*.css` (außer
  `tokens.css`), `templates/**/*.html`, `apps/**/templates/**/*.html`, `static/js/*.js`.
  Erlaubt überall: `var(--…)`, `color-mix(…)`, `currentColor`, `transparent`, `inherit`,
  `none`, sowie reine Opacity-Zahlen (`*-opacity="0.3"`).
- **(b)** Token-Parität verletzt ist: die Property-Menge in `:root` ≠ die in
  `:root[data-theme="light"]` (in `tokens.css`).

Fehlermeldungen mit Datei:Zeile. Analog zum bestehenden Django-Template-Comment-Guard.
Wird in `.github/workflows/ci.yml` (`Lint`) aufgerufen; lokal ausführbar.

## Was NICHT geändert wird
- Der Toggle-Mechanismus (`data-theme-toggle` + `localStorage`) selbst — nur konsumiert.
- Layout, Komponenten-Struktur, JS-Verhalten (außer Terminal-Farbquelle).
- Marken-Bild-Assets (favicon/PWA/apple-touch — PNG/ICO, nicht themebar).
- Nicht-Farb-Tokens (Radius/Fonts/Metrics/Motion) — ziehen nur ins `tokens.css` um,
  Werte unverändert.

## Testing / Verifikation
- **Guard grün:** `scripts/check_color_tokens.py` → 0 Findings (kein Farbcode außerhalb
  `tokens.css`; Parität ok). Der Guard ist selbst der primäre Vollständigkeits-Check.
- **Kontrast:** `--ink-0/1/2` + `--accent`-als-Text auf `--bg-0/1/2` ≥ 4.5:1 in **beiden**
  Themes (Implementer misst; justiert Light-Werte innerhalb Palette C).
- **Django:** `manage.py check`; `collectstatic --dry-run` findet `tokens.css`.
- **Live-Abnahme (beide Modi):** Toggle Hell/Dunkel — Sidebar, Cards, Pills
  (online/warn/danger/info/deploy), Buttons, Fokus-Ring, Login-/Consent-Deko-SVGs,
  Terminal. Token-Preview-Seite (statisch, beide Themes) → PNG/Server für Nutzer-Abnahme
  **vor** Merge (wie beim Rebrand).
- **Logout-Icon** in Sidebar zentriert (Desktop + Mobile).
- **Agent-Team-Flow (CLAUDE.md):** UI via `frontend-design`; Watcher (audit) auf die
  geänderten Files; Copilot-Loop; ein Squash-PR.

## Risiken
- **`color-mix`-Browser-Support:** Baseline seit 2023 (Chrome/Edge 111, Firefox 113,
  Safari 16.2). Für ein internes Ops-Tool auf aktuellen Browsern akzeptiert (die Codebase
  nutzt bereits `aspect-ratio`, moderne Custom Properties). Bewusste Entscheidung.
- **Große mechanische Umstellung:** viele Literale → Regressionsrisiko bei Farbwahl.
  Mitigation: Guard erzwingt Vollständigkeit, Token-Preview + Live-Abnahme in beiden
  Themes, Copilot-Loop, PR-Review.
- **Light-Kontrast:** `--accent` (Teal) als Text kann unter AA fallen → Implementer
  darkened innerhalb Palette C bis AA erfüllt ist (Verifikation Pflicht).
- **SVG `var()` in Presentation-Attributen:** von allen Ziel-Browsern unterstützt; die
  Topbar-Waveform nutzt es bereits (`fill="var(--accent)"`) → Muster erprobt.

## Explizit NICHT in Scope
- Neuer Toggle/UX fürs Theme (existiert schon).
- Änderung der Brand-Werte selbst (nur Zentralisierung + Light-Vervollständigung).
- Andere Consumer-Apps (branding-Repo, HW-Docs etc. — separater Brand-Layer).
