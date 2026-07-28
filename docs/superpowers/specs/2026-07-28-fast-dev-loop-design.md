# Fast-Dev-Loop — station-manager (Agent + Server + Sim)

**Datum:** 2026-07-28
**Branch:** `feat/fast-dev-loop`
**Scope-Hälfte:** Agent-seitige + Server-seitige Änderungen. Die Image-/Mount-/QEMU-/Guard-Hälfte liegt in `linux-image/docs/superpowers/specs/2026-07-28-fast-dev-loop-design.md` (gemeinsamer Branch-Name, ein PR pro Repo).

## Problem

Jede Änderung am `station_agent` läuft heute durch: Image-Build (GitHub) → station-manager-Import → guestfish-Key-Injection → OTA-Deploy. ~30 Min pro Test-Runde. Dazu ist das Rootfs read-only, also keine Live-Edits am Gerät. Ergebnis: tagelanges Trial-and-Error, weil die Feedback-Schleife so lang ist, dass Hypothesen (bis hin zu Kernel-Verdacht) sich stapeln, bevor sich eine widerlegen lässt.

Verschärfend: **Der Simulator lügt am Serial-Boundary.** Ein `open()`/`read()`/`write()`-Zugriff auf das Modul-Device war gegen das QEMU-**PTY** grün, gegen den echten CM4-**UART** aber rot (kein termios/Baud/Raw-Mode). Der Bug versteckte sich eine Woche, weil der Sim ihn nicht aufdeckte. (Git-Spur: `refactor(agent): drive slot control serial with pyserial + drain-before-send`.)

## Grundprinzip — Iterations-Tiers

Änderungen nach Blast-Radius trennen, jede Klasse durch ihren schnellsten Weg:

| Tier | Was sich ändert | Fehler-Klassen | Weg |
|------|-----------------|----------------|-----|
| 0 | Agent-Python | Verbindet nicht, Module nicht gefunden/lesbar, Control-Knopf fehlt, OTA-Logik | Live-Mount + `systemctl restart` (~10 s) |
| 1 | Config, Server-Code | Routing, Control-UI-Rendering, Heartbeat-Format | docker-compose Auto-Reload (Sek.) |
| 2 | Kernel, Bootloader, Partition | Kernel-Panic, Boot-Hang, Slot-Write | Voller Build + Reflash (selten; `linux-image`-Spec) |

Diese Spec adressiert Tier 0 (Agent) und Tier 1 (Server). Tier 2 bleibt bewusst am vollen Weg.

## Komponenten (dieses Repo)

### 1. `station_agent selftest serial` — Serial-Contract-Test

Neues CLI-Frontend in `station_agent/__main__.py` (argparse; Default ohne Subcommand = `agent.run()`, rückwärtskompatibel):

- `python -m station_agent selftest serial [--slot N] [--json]`
- Öffnet das Slot-Control-Device **exakt wie der Broker im Betrieb** — gleiche pyserial-Konfiguration (Baudrate, Raw-/termios-Mode, VMIN/VTIME, drain-before-send). Keine Zweit-Implementierung: die Öffnungslogik wird aus `broker.py`/`slot_discovery.py` extrahiert in eine gemeinsame Funktion (z.B. `serial_io.open_slot_control()`), die Broker *und* Selftest nutzen. So kann der Test niemals grün sein, während der Betriebspfad rot ist.
- Führt einen `describe`-Round-Trip aus und **hexdumpt TX und RX** byteweise.
- Exit-Code spiegelt Erfolg (0 = Round-Trip ok, ≠0 = Timeout/Mismatch).
- Läuft identisch gegen echten CM4-UART und gegen das Sim-PTY → Byte-Level-Diff Sim-vs-HW bei Divergenz.

### 2. `--trace-serial` — Rohbyte-Trace im Betrieb

Flag/Config-Option, die in `broker.py` und `slot_discovery.py` jeden TX/RX auf dem Modul-Serial als Hexdump ins Log schreibt (Level DEBUG). Bei „Modul nicht gefunden" sieht man die echten Bytes statt zu raten. Aktivierbar per Config (`trace_serial: true`) und CLI.

### 3. Sim-PTY termios-Treue

Der/die Simulator(en), die am Serial-Boundary dazwischenhängen, müssen ein **raw-termios-fähiges PTY** präsentieren (openpty + cfmakeraw), damit ein file-open-artiger Bug auch im Sim rot wird statt still grün. Bekannte Lücken, die das PTY *nicht* nachbilden kann (echte Baud-Timing-Effekte etc.), werden im Sim-Doc explizit als „nicht abgedeckt — nur auf HW verifizierbar" vermerkt.

### 4. `manage.py seed_dev_station` — Ein-Kommando-Setup

Idempotenter Management-Command:
- Legt eine Dev-Station + einen **statischen, langlebigen** `DeviceKey` an (kein Rotations-Tanz, kein guestfish).
- Druckt die fertige Agent-`config.yml` (server_url = Dev-Server, station_id, ed25519_key_path) auf stdout.
- Re-Run darf nichts duplizieren (get-or-create auf einer festen Dev-Station-Kennung).
- **Nur** unter `config.settings.dev` erlaubt — hart abgesichert, damit kein statischer Key je in Prod landet.

### 5. Dev-Server (bereits vorhanden, nur nutzen)

`docker-compose.yml` mountet schon `.:/app` und fährt `runserver` → Server-Code + Channels-WS reloaden automatisch (Tier 1 ist praktisch geschenkt). Keine Änderung nötig außer ggf. Doku. Der CM4 im selben LAN erreicht den Dev-Server über die LAN-IP des Dev-Rechners (`web` bindet `0.0.0.0:8000`).

### 6. Agent-Dev-Config + Prod-Switch

Ein Config-Template (`station_agent/config.dev.example.yml`) zeigt auf den Dev-Server. Umschalten auf Prod für den finalen Smoke-Test ist ein Einzeiler (server_url + key-Pfad). Dev und Prod werden **nie verschmolzen** — Local-Dev zum Iterieren, ein Prod-Smoke-Test vor dem Ausliefern.

### 7. justfile (station-manager)

Dünne Recipes, echte Logik in `scripts/*.sh`. Hosts/Secrets aus ungetrackter `.env` mit Env-Override. Recipes:
- `dev-server` — `docker compose up -d web` + `seed_dev_station`
- `seed` — nur `seed_dev_station`
- `selftest host=$cm4_host` — `ssh root@{{host}} 'python -m station_agent selftest serial'`
- `test` — pytest

(Cross-Repo-Orchestrierung — der volle CM4-Loop, der auch den Mount berührt — kommt später ins Umbrella-Repo. Der Mount+Restart selbst wird in der `linux-image`-Spec als Recipe verortet.)

## Ehrlichkeits-Regel (verbindlich)

Ein Bug am Serial-/Modul-Boundary gilt erst als gefixt, wenn `selftest serial` auf **echtem CM4** grün ist. Sim-grün ist notwendig, nicht hinreichend. Diese Regel wird in `CLAUDE.md` (station-manager) verankert.

## Datenfluss (Tier-0-Loop)

```
Dev-Rechner: Editor speichert station_agent/*.py
        │  (sshfs live-mount, siehe linux-image-Spec)
        ▼
CM4/QEMU:  systemctl restart station-agent  →  läuft neuer Code aus /mnt/dev/station_agent
        │
        ├─ Heartbeat/Control ──► Dev-Server (docker compose, Auto-Reload)
        └─ Serial ◄──► FM-Modul (echter UART / Sim-PTY)  ──trace-serial──► Log
```

> **Hinweis:** „QEMU" meint hier das **Dev-Image lokal auf QEMU**. Die `qemux86-64`-**Prod**-Sim-Station auf Proxmox (`native_sim`-FM) ist ein Produktions-Target und fährt das Prod-Image — der Dev-Loop berührt sie nicht. `selftest`/`--trace-serial` sind reine Zusatz-Features und damit auch auf der Prod-Sim-Station gefahrlos vorhanden.

## Testing

- `selftest serial` **ist** der Boundary-Test (auf HW + Sim).
- Unit-Test, der prüft, dass das Sim-PTY raw-termios liefert (regressions-fest gegen „Sim wird wieder file-open-tolerant").
- `seed_dev_station`-Test: idempotent, verweigert Ausführung außerhalb `settings.dev`.
- Bestehende OTA-Unit-Tests bleiben unberührt.

## Bewusst NICHT in dieser Spec (YAGNI / Folge-Schritte)

- Golden-Inventory-Fixture, Protocol-Record/Replay — verworfen (Simulatoren decken das ab).
- `just doctor` (Preflight-Healthcheck) — benannter Folge-Schritt.
- Umbrella-Orchestrierungs-Repo — eigener Schritt, keine Submodules.

## Prod-Sicherheit

Alle Neuerungen hier sind prod-safe: `selftest`/`--trace-serial` sind reine Zusatz-Features; `seed_dev_station` und die statische Key-Config sind auf `settings.dev` beschränkt. Der Live-Mount + systemd-Override (der eigentliche Prod-Risiko-Punkt) lebt ausschließlich in der `linux-image`-Spec, gebunden ans Dev-Image + CI-Guard.
