# station-agent: Boot-Marker + Commit-Gate — Plan

Spec (verbindlich): `OE5XRX/linux-image` Branch `feature/slot-boot-stage2`,
`docs/superpowers/specs/2026-11-04-slot-boot-stage2-overlays-design.md` **§5**.
Dieser PR ist der vorgelagerte Agent-Teil; der linux-image-PR pinnt den Agent danach.

## Kontrakt (aus §5)

Kernel-cmdline (`/proc/cmdline`), gesetzt von Stufe 1/2:

- `oe5xrx.stage2=ok|missing|failed`
- `oe5xrx.overlays=ok:<a>[,<b>…]|failed:<name>`

## Entscheidungen (Implementierungsdetails, Spec lässt sie offen)

- **Neues Modul `station_agent/boot_markers.py`** — eine Quelle für Parser, Gate-Bewertung und
  Metrik-Werte (Health-Check und Metriken importieren es; keine zweite Implementierung).
- **Mehrfaches Vorkommen** eines Markers: das **letzte** gewinnt (Kernel-Semantik; Stufe 2 hängt
  nach Stufe 1 an).
- **Unbekannter / leerer Wert bei vorhandenem Marker** = fail closed (blockiert den Commit):
  ein vorhandener Marker beweist den neuen Bootpfad, ein unverständlicher Wert ist kein „ok".
  `oe5xrx.overlays=ok` / `ok:` (ohne Namen) gilt als ok (leere Overlay-Liste).
- **Marker fehlt ganz** ⇒ alter Bootpfad ⇒ kein Blocker. Kein procfs (Container/Sim) ⇒ ebenfalls
  kein Blocker. procfs da, aber `/proc/cmdline` unlesbar oder versteckt (z.B. `ProcSubset=pid`
  ⇒ ENOENT) ⇒ fail closed — sonst schaltet eine Härtung das Gate still ab.
- **Marker nach `--`** (init-Args) überschreiben nie einen Kernel-seitigen; steht einer nur dort,
  zählt er trotzdem (kein Legacy/fail-open). Tokenizer = Port von `next_arg()`/`parse_args()`.
- **Gate nur im Trial** (`upgrade_available == "1"`): `run_health_checks(..., boot_gate=True)`.
  Im `(0, 0)`-Retry-Fall ist lokal schon committet — dort kein Gate. Fehlerpfad = bestehender
  Health-Check-Fehlerpfad (`rolled_back` + Grund mit „FAIL" in der Message). Commit-Protokoll
  `(upgrade_available, bootcount)` bleibt unverändert.
- **Metriken:** `boot.stage2_ok` (1/0, nur wenn Stage2-Marker da), `boot.overlays_failed`
  (Anzahl Namen in `failed:`, 0 bei `ok`, nur wenn Overlays-Marker da). Gruppe `system` (kein
  neuer `GROUPS`-Eintrag ⇒ kein Server-Code). cmdline ändert sich zur Laufzeit nicht ⇒ einmal
  lesen, cachen.

## Tasks

1. **Parser + Gate** (`boot_markers.py`, `health_check.py`, `agent.py`), TDD:
   `tests/test_agent_boot_markers.py` (alle Varianten, kaputt/fehlend/doppelt), Gate-Matrix
   (trial ja/nein × Marker) in `tests/test_agent_ota_flow.py`.
2. **Metriken** (`metrics.py` Descriptor + Quelle), TDD in `tests/test_agent_metrics.py`.
3. Ruff (`uvx ruff@0.16.8`), volle Testsuite, PR.
