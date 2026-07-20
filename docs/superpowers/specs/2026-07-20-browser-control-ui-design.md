# Design: Browser-Control-UI / generischer Renderer (D5)

**Datum:** 2026-07-20
**Status:** Design (genehmigt, vor Implementierung)
**Repo:** `station-manager` (Django · Bootstrap 5 · HTMX · **neu: Alpine.js**).
**Bezug:** das sichtbare Ende der Control-Kette. Konsumiert D4 (`ws/control/<id>/`, StationModule-Registry) + den D3-§7-Vertrag + die D1-Descriptoren. Verfeinert Parent-Spec §5.6.

> **Scope-Grenze:** D5 **steuert + keyt (PTT) + zeigt Live-Telemetrie** übers Web. Das echte **Voice-Audio** (Browser-Mic → TX, RX → Speaker) ist die **Audio-Phase D6–D9** und NICHT Teil von D5. Nach D5: erstmals **Browser → Agent → Radio** end-to-end (steuern + keyen + zuhören-was-die-Telemetrie-sagt).

---

## 1. Ziel & Prinzipien

Ein **generisches, modul-agnostisches** Control-Panel: es rendert, was der Descriptor sagt — **kein `fm`/`frequency`-Hardcode**. Ein neues Modul/eine neue Capability erscheint automatisch, ohne UI-Code. Multi-Viewer: alle sehen Live-Zustand + wer steuert; nur der Lock-Halter kommandiert/keyt.

## 2. Architektur

- **Generischer Renderer = Django-Template**, iteriert den `capability_descriptor` der `StationModule`-Registry → Bootstrap-Widgets. Server-gerendert → **Offline-Render aus `last_state` gratis**.
- **Alpine.js-Insel** (additiv, ~15 KB, `x-data` scoped auf das Panel) — bindet Live-Werte reaktiv, hält Lock-/Verbindungs-/TX-State. **Bestehendes Vanilla-JS/HTMX/xterm.js bleibt unangetastet.** Leitlinie: Alpine = reaktive Live-Daten-UIs, HTMX = Request/Response, Vanilla = Glue.
- **WS-Client** zu `ws/control/<station_id>/` (D4). Empfängt `inventory`/`state`/`result`/`event` + Lock-Status; sendet `command`/Lock-Aktionen/`subscribe`/`ptt_keepalive`.
- **Ort:** dedizierte **Control-Seite `/stations/<id>/control/`** (Platz für Multi-Modul-Karten + Live-Panel); `station_detail` verlinkt drauf.

## 3. Widget-Mapping (aus dem Descriptor)

| kind + type | Widget |
|---|---|
| `setting` + `float`/`int` (+ Range) | Number-Input **+ Step-Buttons** (bounded aus benannten Ranges/min/max/step); Slider optional |
| `setting` + `enum` | Select / Segmented |
| `setting` + `bool` | Toggle |
| `action` + `bool` (ptt) | **Push-and-hold-Button** (§5) |
| `action` (sonstige) | Button |
| `telemetry` | Meter / Readout (live, §6) |
| `readonly` | reine Anzeige |

Mehrere Module/Slots → je eine **Karte**. Optionale handpolierte „Skins" später, ohne den generischen Kern aufzugeben.

**Frequenz-Eingabe (Spezialfall):** Number-Input + Step-Buttons aus dem Descriptor-`step`. **⚠️ DE-Locale-Falle:** `<input type="number">` interpretiert in `de_AT` Komma als Dezimaltrenner → Floats round-trippen falsch (dokumentiert in CLAUDE.md). **Forced dot-decimal** (`lang="en"` am Input / Step-Pattern / Text-Input mit Validierung). Slider-Drag **debouncen** (nicht pro Pixel senden).

## 4. Lock-UX

- **Banner:** „🔒 <User> steuert" / „frei". Bei Nicht-Holder: **read-only** (Widgets disabled, Live-Werte sichtbar) + „Control anfragen/übernehmen". Holder = Operator-Modus. Station-Admin: „übernehmen" (Preempt).
- **Lock-Verlust *während* des Operierens** (Admin-Preempt oder `T_idle`): UI schaltet **sofort auf read-only + unkeyt eine laufende PTT + Hinweis „Kontrolle verloren"**.
- Hand-off-Aktionen (`acquire`/`release`/`request`/`grant`/`preempt`) laufen über die Control-WS (D4).

## 5. PTT (push-and-hold, Maus + Tastatur)

- **Zwei Eingabewege, gleiche Semantik:** Maus/Touch (`mousedown`/`touchstart` → key, loslassen → unkey) **und** Tastatur (Taste halten = TX). **Default-Taste Spacebar, per User konfigurierbar** (MVP: Präferenz in `localStorage`; fußschalter-tauglich, da eine Taste emittiert wird). Beide spiegeln denselben visuellen Zustand.
- **Ablauf:** key → `command ptt set true` + **Keepalive-Loop** (< `T_ptt`); Loslassen/`mouseleave`/**Blur**/WS-Drop → `set false` + Keepalive stoppen. Agent-lokaler **Dead-Man ist der Backstop**.
- **Guards:** nur Lock-Halter · **nicht während Tippen in einem Feld** · **Key-Repeat ignorieren** · `preventDefault` (Spacebar-Scroll) · Pointer-Capture beim Ziehen.
- **Confirmed-TX vs. angefordert:** die UI zeigt **„keying…" (Button gedrückt) → „TX ON" (Agent meldet keyed per `state`/`event`)** — der Operator sieht über flakiges Netz, *dass* wirklich gekeyed ist, bevor er redet.

## 6. Live-Telemetrie

- **`subscribe` beim Panel-Öffnen** (auch für reine Viewer — laut D4 ist `subscribe` **access-gated**, nicht Lock-gated), `unsubscribe` beim Schließen. Rate/`min_interval` aus dem Descriptor (D3).
- Meter/Readouts updaten live aus `state`. RSSI ist `raw` → generischer Meter im MVP (S-Meter-Skin später).

## 7. Command-Flow & Feedback

- Kommando senden → **pending** (Widget markiert) → **ack/error** aus `result` → der `state`-Push bestätigt den **Ist-Wert** (kein Blind-Optimismus; Multi-Viewer bleibt konsistent).
- Strukturierte Fehler (D3 §10) je Widget anzeigen (z.B. `out_of_range`).

## 8. Verbindungs-Status & Robustheit

- **WS-Disconnect/Reconnect-Indikator** klar sichtbar. Bei Drop: **alle Controls disabled**, PTT **fail-safe** (Dead-Man unkeyt agent-seitig), Reconnect mit Backoff.
- **Offline-Station/Modul:** read-only + „offline"-Indikator, Render aus `last_state`; kein Lock/Command.

## 9. Scope

- **DRIN (D5):** dedizierte Control-Seite, generischer Django-Renderer, Alpine-Reaktiv-Insel + WS-Client, Widget-Mapping, Lock-UX inkl. Verlust, PTT (Maus+Tastatur, confirmed-TX, Guards), Live-Telemetrie-Subscription, Command-Feedback, Verbindungs-/Offline-States, DE-Locale-Fix.
- **DRAUSSEN:** **Voice-Audio (Mic/Speaker) = D6–D9.** Per-Modul-Lock / Lehrer-Schüler / Lizenz-Bit / Control-Präsenz-Tracking (#97) = spätere Erweiterungen. Server-Seite = D4 (fertig).

## 10. Testing

- **Generizität:** Panel rendert korrekt aus verschiedenen Descriptoren; ein zweites fiktives Modul erscheint ohne UI-Code; kein „fm"-String in der Renderer-Logik.
- **Widgets:** range→Number+Step (bounded), enum→Select, bool→Toggle, telemetry→Meter (live-Update aus `state`).
- **Lock:** Viewer read-only + Live-Werte · anfragen/übernehmen · Verlust während Operierens → read-only+unkey+Hinweis.
- **PTT:** Maus press/release · Tastatur (Spacebar/konfiguriert) hold/release · Guards (nicht beim Tippen, Key-Repeat, Blur→unkey) · confirmed-TX-Anzeige aus `state`.
- **Robustheit:** WS-Drop → Controls disabled + PTT fail-safe + Reconnect · Offline-Render aus `last_state`.
- **DE-Locale:** Frequenz-Float round-trippt korrekt (dot-decimal).

## 11. Implementierung (Prozess)

superpowers-/CLAUDE.md-Fluss (Brainstorming = dieses Dokument):
1. `superpowers:writing-plans` — Plan (Control-Seite/Route, Django-Renderer-Template, Alpine-Einbindung + WS-Client, Widget-Partials, Lock-UX, PTT-Handler, Telemetrie-Subscription, Command-Feedback, Connection/Offline) mit Task-Checkboxen.
2. **`Skill("frontend-design")` ist Pflicht für alle UI-Arbeit** (CLAUDE.md) — visuelle Qualität/Layout.
3. `superpowers:subagent-driven-development` (pixel für UI + gateway für die Server-/Template-Seite).
4. `superpowers:test-driven-development` — Renderer-Generizität + Interaktion (Django/JS-Tests, ggf. Playwright für PTT/Lock/WS-Flows).
5. `superpowers:verification-before-completion`, dann PR + copilot-loop.

Repo: `station-manager`. Ein Deliverable-Branch, ein PR.

---

*Diese Spec definiert das generische, modul-agnostische Browser-Control-Panel: Django-Renderer aus dem Registry-Descriptor + Alpine-Reaktiv-Insel + Control-WS; Widget-Mapping, Lock-UX (inkl. Verlust), PTT push-and-hold (Maus+Tastatur, confirmed-TX, Dead-Man-Backstop), Live-Telemetrie-Subscription, robuste Verbindungs-/Offline-States, DE-Locale-Fix. **Voice-Audio bleibt D6–D9.** Folgt dem Muster spec → plan → code; UI-Arbeit über die frontend-design-Skill.*
