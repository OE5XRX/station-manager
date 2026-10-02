# Audio Path Diagnostics (TX chain) — Design

**Status:** Approved-design draft, pre-plan
**Date:** 2026-10-02
**Repo:** station-manager
**Feature family:** `feature/station-control-audio-api` (first concrete piece)

## Problem & Intent

Die operator-TX-Stimme kommt am HF zu leise raus — die Modulation (Hub) ist zu
gering. Der TX-Audiopfad ist mehrstufig (Browser → station-manager → agent →
fm_board → SA818), und mit dem heutigen human-in-the-loop-Vorgehen (PTT drücken,
sprechen, UI ablesen, SSH-Messung) lässt sich **nicht lokalisieren, wo** Pegel
verloren geht.

Ziel: ein **wiederverwendbares, maschinen-/AI-fahrbares Mess- und
Einspeise-Harness**, das an definierten Punkten entlang der Kette den
Signalpegel (dBFS) misst und an definierten Punkten ein kalibriertes
Referenzsignal einspeist. Damit wird der digitale Teil der Kette per
**Signal-Chain-Bisection** jederzeit durchtestbar — nicht nur für diesen Bug,
sondern dauerhaft.

Erfolg =
- Ein einziger „Audio-Path-Diagnostic"-Lauf liefert eine dBFS-Pegeltabelle über
  alle digitalen Messpunkte + die statischen Gains, und lokalisiert den
  Pegelverlust auf eine Stufe (oder spricht die digitale Kette frei).
- Kein Mensch und kein Funkgerät nötig (Referenzton statt Sprechen).
- Wiederverwendbar + AI-fahrbar: uniformes, maschinenlesbares Schema, das ein
  Agent für die Bisection-Schleife selbst fahren kann.

## Die TX-Kette & Messpunkte

```
Browser-Mic (getUserMedia) → Worklet(oe5xrx-mic) → Opus-Encode    [T0, T1]
  → WS → station-manager (DUMB RELAY, byte-identisch) → WS → agent  [transparent]
    → GStreamer TX (opus_bridge.build_tx_argv, KEIN volume-Element)
      → PipeWire output_MONO [48k]                                   [C]
        → Resample 48k→8k → Sink "FM Transceiver Board Mono" (vol 0.40)
          → ALSA (snd-usb-audio) → USB-UAC2                          [D]
            → fm_board FW (pcm16_to_dac Unity) → DAC → SA818 → HF     [E/F: out of scope]
```

| Punkt | Ort | Domäne | In Scope |
|---|---|---|---|
| **T0** | Browser-Mic direkt nach getUserMedia (nach AGC/NS/EC) | Browser JS | ✅ |
| **T1** | Browser vor Opus-Encode (Worklet-Output) | Browser JS | ✅ |
| — | station-manager Relay | server | **freigesprochen** (siehe unten) — kein Tap |
| **C** | gst `output_MONO`, vor Sink-vol | agent/PipeWire | ✅ |
| **D** | nach Sink-vol 0.40, an der ALSA/UAC2-Kante | agent/ALSA | ✅ |
| **E** | DAC-Input in der FW | firmware | ❌ (keine FW-Änderung) |
| **F** | DAC-Out → SA818-Mod-Eingang → Hub | analog/HW | ❌ (kein Analog-Abgriff) |

### Server ist per Konstruktion transparent (kein Tap nötig)
`apps/audio/consumers.py`: „The server is a **dumb relay** … No DSP, no decode,
no re-encode", Uplink-Frames werden **byte-identisch** an den Agent
weitergereicht. Opus rein = Opus raus, Pegel unverändert → der station-manager
ist durch Code-Inspektion freigesprochen und wird **nicht** instrumentiert
(nur dokumentiert). Eine Stufe gratis aus der Suche.

### Was der A–D-Schnitt leisten kann (und was nicht)
- Klärt sicher: „Kommt an der USB/ALSA-Kante (D) digitaler Vollpegel an, und wenn
  nicht, in welcher Stufe fällt er ab?"
- Verliert die Kette schon digital → **digital fixbar** (Makeup-Gain in
  gst/PipeWire, Sink 0.40→1.0, Browser-Constraints). Top-Verdächtige: Browser-AGC
  (T0/T1) und die **Sink-vol 0.40** (≈ −8 dB, zwischen C und D).
- Ist D sauber Vollpegel, HF aber leise → Verlust liegt **analog hinter D**
  (DAC-Out-Pegel vs. SA818-Mod-Empfindlichkeit). Das misst dieses Harness nicht,
  aber es hat das Problem damit auf die analoge Stufe eingegrenzt und die digitale
  Kette freigesprochen. E/F sind ein separater späterer Schritt (FW-Telemetrie +
  Bench-Scope).

## Architektur

### Metrik (einheitlich an allen Punkten)
- **RMS + Peak in dBFS** über ein Messfenster (z.B. 200–500 ms).
- Zusätzlich **statische Gains** als „Nullsignal-Abgriff": PipeWire-Sink-vol,
  Resample-Gain, Browser-getUserMedia-Constraints (autoGainControl/NS/EC-Status)
  und etwaige WebAudio-Gain-Werte. (Oft steht die halbe Antwort schon ohne
  Signal da — z.B. Sink 0.40 + AGC an.)

### Einspeisung (Referenzsignal)
- Kalibriertes Referenzsignal: Sinus (definierte Frequenz, z.B. 1 kHz, definierter
  Pegel in dBFS) und/oder WAV.
- **Zwei Inject-Anker**, die die ganze digitale Kette bisektieren:
  - **T1 (Browser)**: Mic-Quelle durch Referenz-Oszillator/WAV ersetzen → testet
    Encode → Wire → Agent → gst → ALSA.
  - **C (Agent/gst)**: Stream-Quelle durch `audiotestsrc`/WAV ersetzen → testet
    nur die Station-Teilkette gst → Sink → ALSA.
- Vergleich „Inject@T1 vs Inject@C" trennt Browser/Wire-Verluste von
  Station-Verlusten.

### RF-Sicherheit (nicht verhandelbar)
Alle A–D-Messungen sind **rein digital** (im gst/PipeWire-Graph bzw. an der
ALSA-Kante). Das Messen/Einspeisen bis D darf den **SA818 NICHT keyen** und
**kein HF erzeugen** — Pegel an D ist der digitale Samplestrom Richtung USB,
unabhängig vom PTT/Keying-Zustand. Der Diagnostic-Lauf fordert kein Keying an.

### Instrumentierung pro Schicht
- **Browser (`static/js/audio-panel.js` + Mic-Worklet `oe5xrx-mic`)**: der Worklet
  rechnet bereits RMS (`levels[]`). Erweitern um On-Demand-dBFS-Report an T0
  (MediaStreamSource nach getUserMedia) und T1 (Worklet-Output vor Encoder) +
  Inject-Modus (Mic-Quelle ↔ Referenz). Statische Capture-Constraints mitmelden.
- **Agent (`station_agent`, gst-TX-Pipeline)**: `level`-Elemente an `output_MONO`
  (C) und am Sink-Input/ALSA-Kante (D) einfügen (native RMS/Peak-Messages, kein
  pw-record-Node-ID-Jagen) + Inject via `audiotestsrc`/`tee`-Umschaltung.
  Ergebnis über den bestehenden Agent↔Server-Kanal.
- **station-manager (Orchestrator)**: koordiniert einen „Diagnostic-Run" —
  kommandiert Browser-Taps/Inject + Agent-Taps/Inject, sammelt die Messungen,
  baut die **dBFS-Delta-Tabelle** + statische Gains + ein Lokalisierungs-Urteil
  („Verlust zwischen C und D: Sink 0.40" / „schon bei T1 niedrig: Browser-AGC").
  Exponiert als REST/WS-Diagnose-Endpoint (fügt sich in die control+audio-API-
  Scope-Erweiterung ein; auth/scope wie die übrige API).

### AI-fahrbares Schema
- Tap-Report: `{point, format{rate,channels}, rms_dbfs, peak_dbfs, window_ms, static_gains?}`.
- Inject-Request: `{point, signal:{kind: "sine"|"wav", freq_hz?, level_dbfs, duration_ms}}`.
- Diagnostic-Run-Report: Liste der Tap-Reports + per-Stufe-Deltas + Verdikt.
  Uniform → ein Agent kann „Referenz rein @T1 → alle Taps lesen → Delta-Tabelle →
  Stufe mit unerwartetem Verlust" autonom fahren.

## Komponenten-Schnitt

| Unit | Zweck | Schicht |
|------|-------|---------|
| Mic-Worklet-Erweiterung (T0/T1-Metrik + Inject) | dBFS messen / Referenz einspeisen im Browser | Browser JS |
| audio-panel.js Diagnostic-Hooks | On-Demand-Messung/Inject + Constraints melden | Browser JS |
| gst-Pipeline `level`-Taps (C/D) + Inject-Tee | dBFS messen / Referenz einspeisen auf der Station | station_agent |
| Agent↔Server Diagnostic-Command | Mess-/Inject-Kommando + Report | agent + server |
| Diagnostic-Orchestrator + Report | Run koordinieren, Delta-Tabelle + Verdikt | station-manager |
| REST/WS-Diagnose-Endpoint | AI-/Skript-Zugang, auth/scope | station-manager |

## Testing
- **Browser**: dBFS-Berechnung + Inject-Logik (Logik extrahieren / Worklet-Harness).
- **Agent**: `level`-Readout + Inject, **validiert auf der HIL-Bench (littleone)** —
  Punkt D fällt exakt mit der HIL-raw-ALSA@8k-USB-Kante zusammen (siehe Memory
  `architecture/audio-stack`): ein eingespeister Referenzton muss an D den
  erwarteten dBFS zeigen.
- **Server**: Orchestrierung/Report-Assembly + Permission-Gating.
- **E2E**: Referenz @T1 → Report zeigt plausible Deltas inkl. der 0.40-Sink-Stufe.

## Non-Goals (YAGNI)
- Kein Umbau des Audio-Streamings (bleibt WS/Opus Echtzeit).
- Keine FW-Telemetrie (E), keine analoge Messung (F) — separater späterer Schritt.
- **Keine automatische Pegelkorrektur**: das Harness *misst*. Der eigentliche Fix
  (Makeup-Gain / Sink 1.0 / Browser-Constraints) ist eine separate, von der
  Messung informierte Änderung.
- Kein Control-Write (key/unkey) hier — das ist der andere Teil der
  control+audio-API (eigenes Feature/Phase).

## Offene Punkte
- Trigger-Kanal Agent↔Server im Detail (Terminal/Tunnel vs. Heartbeat-Command vs.
  eigener WS) — im Plan festzulegen; Orchestrierung sitzt am station-manager.
- Genaues Referenz-Level/-Frequenz + Fenstergröße als Kalibrier-Konvention.
