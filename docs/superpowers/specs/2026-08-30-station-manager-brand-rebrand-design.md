# station-manager Brand-Rebrand (Marine/Cyan) — Design

**Datum:** 2026-08-30
**Status:** Design (approved, Spec-Review offen)
**Branch:** `feat/oe5xrx-brand-rebrand` (off `origin/main`)
**Quelle der Brand-Werte:** OE5XRX Palette C (dark), IBM Plex, Turm-Marke (branding-Repo)

## Zusammenfassung

station-manager (Django, dark-only UI, aktuell „VU-meter amber"-Design) vollständig
auf das OE5XRX-Brand umstellen: Marine/Cyan-**Dark**-Palette (Cyan-Akzent), IBM Plex
überall (self-hosted), Turm-Marke statt „5X"-Badge, Brand-Favicon/PWA-Icons/theme-color.

Weil `app.css` **vollständig tokenisiert** ist (kein hartes Amber außerhalb `:root`),
ist der Kern ein **`:root`-Token-Remap**, der durch die gesamte UI cascadet.

**Scope:** nur die Dark-Umstellung. **Ein helles Theme ist bewusst NICHT in Scope** —
die Tokens werden aber so strukturiert, dass ein Light-Scheme (Brand Palette C light)
später als Drop-in via `[data-theme]` ergänzt werden kann.

## Kontext / Ausgangslage

- `static/css/app.css` (1880 Z.): `:root` mit Surfaces (`--bg-0..3`), Lines, Ink,
  Signal-Palette (`--accent` amber, `--signal`, `--cyan`, `--warn`, `--danger`,
  `--violet`), Shadows, Fonts. UI nutzt durchgängig `var(--…)`. Kein hartes Amber
  außerhalb `:root`.
- Fonts: **Google-CDN** (Bricolage Grotesque + IBM Plex Sans + IBM Plex Mono) in
  `templates/base.html`.
- Brand-Marke: `<div class="brand-mark">5X</div>` in `templates/includes/sidebar.html`
  und in der Auth-/Login-Seite (`apps/accounts/templates/accounts/login.html`),
  gestylt als Amber-Gradient-Badge.
- Identität: `templates/base.html` `favicon.ico` + `theme-color #FF8A3D` +
  `apple-touch-icon webpush/icon-192.png`; PWA-Manifest (`apps/webpush/views.py`) +
  Service-Worker (`apps/webpush/templates/webpush/sw.js`) nutzen `webpush/icon-192/512.png`.
  Amber `#FF8A3D` auch in `templates/oauth2_provider/authorize.html` + `login.html`.
- **Konvention (CLAUDE.md):** station-manager = Feature-Branch + **ein Squash-PR**,
  Copilot-Review-Loop. UI-Arbeit → `frontend-design`-Skill.

## Entscheidungen (aus Brainstorming)

| Thema | Entscheidung |
|-------|--------------|
| Richtung | **Voll umstellen** auf Brand-Dark (Cyan-Akzent). |
| Akzent | **Cyan `#3AC6D6`** (Brand-Dark-Primary; Marine `#123B54` zu dunkel für Akzent auf near-black). Amber überlebt als `--warn`. |
| Typografie | **IBM Plex überall** (Bricolage raus), `--font-display` → IBM Plex Sans. |
| Font-Hosting | **Self-hosted WOFF2** (kein Google-CDN), wie alle anderen Consumer. |
| Font-Gewichte | **Nur Brand-Gewichte** (Sans 400/600/700, Mono 400/600). **Kein 500** — vorhandene `font-weight:500` auf 600/400 remappen. |
| Marke | Turm-Marke (inline SVG) statt „5X"-Badge. |
| Light-Theme | **Nicht in Scope**; Tokens für spätere `[data-theme="light"]`-Ergänzung strukturieren. |

## Komponenten

### 1. Farb-Tokens (`static/css/app.css` `:root`)
Remap auf Brand-Dark (Palette C dark). Konkrete Zielwerte:
```css
/* Surfaces — marine-getönter Dark-Ramp (bg #0A1219 / surface #0F1C28) */
--bg-0:#070C11; --bg-1:#0A1219; --bg-2:#0F1C28; --bg-3:#16283A; --bg-hover:#1E3A50;
/* Lines (border #1A2D3D) */
--line:#1A2D3D; --line-bright:#2C4A63; --line-hot:#3E6588;
/* Ink (text #D8ECF5, muted #7AAFCA) */
--ink-0:#D8ECF5; --ink-1:#A9C4D6; --ink-2:#7AAFCA; --ink-3:#4E6B7E;
/* Accent = Brand-Cyan */
--accent:#3AC6D6; --accent-soft:#7FE0EB; --accent-deep:#1E9EAD;
--accent-glow:rgba(58,198,214,.22); --accent-tint:rgba(58,198,214,.08);
/* Semantik (Brand dark) — Amber lebt als warn weiter */
--signal:#4DB870; --signal-soft:rgba(77,184,112,.14);
--warn:#FFB84D;   --warn-soft:rgba(255,184,77,.14);
--danger:#F06060; --danger-soft:rgba(240,96,96,.15);
/* Status distinkt vom Cyan-Akzent halten */
--cyan:#5FBFE0;   --cyan-soft:rgba(95,191,224,.12);   /* info/tunnel, blauer als accent */
--violet:#9B6BFF; --violet-soft:rgba(155,107,255,.14);/* deploy — unverändert */
--shadow-glow:0 0 32px -4px var(--accent-glow);
```
Selektion (`::selection`) + Fokus-Ringe erben `--accent` → automatisch cyan.
Beim Umsetzen visuell auf Rest-Amber und Kontrast (Text auf bg ≥ 4.5:1) prüfen.

### 2. Typografie (self-hosted, **nur Brand-Gewichte**)
- **Das branding-Repo ist maßgeblich.** Es liefert genau **IBM Plex Sans 400/600/700**
  + **IBM Plex Mono 400/600**. **Kein 500** — also wird 500 NICHT eingeführt.
- Genau diese 5 WOFF2 aus `branding/type/fonts/` nach `static/fonts/` kopieren.
- **Gewicht 500 entfernen:** station nutzt aktuell `font-weight: 500` an mehreren
  Stellen. Diese im Zuge des Umbaus auf ein Brand-Gewicht remappen — Standard **600**
  (nächstliegendes „medium"), bei reinem Fließtext ggf. **400**. Kein Laden eines
  500er-WOFF2, kein Fallback-500.
- `@font-face`-Block (in `app.css` oder neuer `static/css/fonts.css`) auf die lokalen
  WOFF2 (nur 400/600/700 Sans, 400/600 Mono); `base.html`: Google-CDN-`<link>` +
  Preconnects **entfernen**.
- `:root` `--font-display` → `"IBM Plex Sans", …` (Bricolage entfernt).

### 3. Brand-Marke (Turm)
- `templates/includes/sidebar.html` + `apps/accounts/templates/accounts/login.html`:
  `<div class="brand-mark">5X</div>` → inline Turm-SVG (aus `branding/logo/oe5xrx-mark.svg`,
  `currentColor`), Container-`color:var(--accent)` → Cyan. `.brand-mark`-CSS
  (Amber-Gradient) → schlichter Container/transparent, SVG trägt die Farbe.

### 4. Identitäts-Assets
- `static/favicon.ico` ← `branding/export/favicon.ico`.
- `static/webpush/icon-192.png` / `icon-512.png` ← `branding/export/pwa-192.png` /
  `pwa-512.png` (Weiß auf Marine; PWA/SW/Manifest referenzieren diese Namen → nur Bytes
  tauschen, keine Code-Änderung).
- `apple-touch-icon`: bleibt `webpush/icon-192.png` (jetzt Brand) oder auf
  `branding/export/apple-touch-icon-180.png` als eigenes Static — im Plan festlegen.
- `theme-color`: `#FF8A3D` → **`#0A1219`** (Brand-Dark-bg; für Mobil-Chrome der App)
  in `base.html`, `oauth2_provider/authorize.html`, `accounts/login.html`.

## Was NICHT geändert wird
- Semantik der Status-Farben (online/offline/updating/deploy) bleibt funktional; nur
  Werte auf Brand-Semantik gezogen.
- Layout, Komponenten-Struktur, JS-Verhalten. `static/js/app.js` nur falls es Farb-
  Hardcodes hat (z.B. Chart-Farbe `--accent`/mono-font — Font-Ref ist schon IBM Plex).
- Kein Light-Theme (separates Folge-Vorhaben).

## Testing / Verifikation
- **Vorab (Controller):** Token-Preview — Sample-Komponenten (Sidebar-Brand, Card,
  Button, Pills online/warn/danger, Fokus-Ring) mit neuen Tokens als statisches HTML,
  zu PNG gerendert (SFTP/Datei) → Look-Abnahme durch Nutzer VOR Merge.
- **Rest-Amber-Scan:** nach Umbau `grep -riE 'FF8A3D|FF4E1F|D96418|Bricolage'` über
  `static/ templates/ apps/` → 0 (außer ggf. bewusst dokumentiert).
- **Font-Check:** kein `fonts.googleapis`/`Bricolage` mehr; WOFF2 laden lokal.
- **Kontrast:** `--ink-0/1` auf `--bg-1/2` ≥ 4.5:1; `--accent` auf `--bg` lesbar.
- **Django-Checks:** `python manage.py check`; `collectstatic --dry-run` findet neue
  Fonts/Assets; Templates rendern (Smoke).
- **Echte Abnahme:** am laufenden/deployten App-UI (Sidebar-Logo, Farben, Fonts,
  Favicon, PWA-Icon, theme-color) durch den Nutzer.
- **Agent-Team-Flow (CLAUDE.md):** UI via `frontend-design`-Skill; Watcher (audit)
  auf die geänderten Files; `probe` E2E-Smoke; Copilot-Review-Loop; **ein Squash-PR**.

## Risiken
- **Laufende App / SSO-Hub:** großflächige Optik-Änderung. Mitigation: Token-Preview
  vorab, PR-Review, kein Direct-to-main.
- **Cyan-Akzent vs. Cyan-Info-Status:** bewusst getrennt gehalten (`--cyan` blauer als
  `--accent`) — beim Umbau visuell prüfen, dass Links/Buttons vs. Info-Pills
  unterscheidbar bleiben.
- **Font-Gewichte:** branding ist maßgeblich (400/600/700, kein 500). Alle
  `font-weight: 500` in station remappen (→ 600, ggf. 400) — sonst Fallback. Verify:
  `grep -rn 'font-weight: *500' static/ templates/` → 0 nach Umbau.
- **Rest-Amber in JS/Charts:** `app.js` prüfen (Chart-Serienfarben o.ä.).

## Explizit NICHT in Scope
- Helles Theme + Umschalter (Folge-Vorhaben; Tokens werden dafür vorbereitet).
- internal-web (hat noch kein Frontend).
- Layout-/Feature-Änderungen jenseits des Rebrands.
