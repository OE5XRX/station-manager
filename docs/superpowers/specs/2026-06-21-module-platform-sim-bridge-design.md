# Design: Steckbare Modul-Plattform & Simulations-Bridge (Firmware → Web)

**Datum:** 2026-06-21
**Status:** Design (genehmigt, vor Implementierung)
**Repo (Spec-Heimat):** `station-manager` — cross-cutting; Implementierung wird auf `FW-RemoteStation`, `station-manager`, `linux-image` aufgeteilt
**Bezug:** baut die Grundlage, in die `FW-RemoteStation` Issue #30 (Audio-Bridge) später einhängt

---

## 1. Ziel & Kontext

### Großes Ziel
Die Remote-Station-Software (CM4 / `linux-image` + `station_agent`) und das FM-Board (`FW-RemoteStation`)
**komplett virtuell** testbar machen — ohne echte Hardware — und dabei eine Architektur legen, die
über das eine FM-Modul hinaus auf eine **steckbare Geräte-Plattform** skaliert (künftig: HF-Radio,
HF-Filter, LTE-Modem, HAMNET-Module …).

### Aktueller Stand (erhoben aus den Repos)
- **`linux-image`** läuft als QEMU-x64-VM (`scripts/run-qemu.sh`): virtio-disk, user-net mit SSH-Forward,
  serielle Konsole (`-serial mon:stdio`). **Kein USB-Device, kein Audio.** Lässt sich auch in Proxmox einbinden;
  A/B-OTA funktioniert darin gut zum Testen.
- **`FW-RemoteStation`** läuft als `native_sim` (Linux-Host-Prozess): SA818-Treiber, GPIO/ADC-Emulatoren,
  UART-PTY. Exponiert eine **Zephyr-Shell** (`fm> `-Prompt) — auf echter HW über USB-CDC-ACM, im Sim über die
  Konsolen-PTY. Kommandobaum `sa818` (power, ptt, powerlevel, at group/volume/rssi/filters/version, status, test tone).
- **`station_agent`** hat heute **nur Control-Plane** (ota, heartbeat, terminal, inventory, bootloader,
  http_client, config, health_check). **Kein** Radio-/Audio-/Serial-Code → die Geräteschicht ist Greenfield.
- **`station-manager`** hat ein **erprobtes Relay-Muster**: `tunnel.TerminalConsumer` (Browser-WS
  `ws/terminal/<station_id>/`, Channel-Group, max 2 Sessions), Agent-seitige Outbound-Ed25519-WS (`terminal.py`),
  `stations.StationStatusConsumer` (Live-Status-Push). **Das übernehmen wir.**
- **Legacy-Hinweis (nicht als Fundament):** Es existieren `ModuleType` (statischer Typ-Katalog mit
  `firmware_flash_method`) und `Station.installed_modules` (M2M, manuell per Checkbox). Sie stammen aus einer
  früheren, weniger ausgereiften Architektur, werden **rein zur Anzeige** genutzt (forms, admin,
  `station_detail.html`) — **keine** Programm-/Firmware-Logik hängt daran. Sie sind **kein** Fundament für die
  Capability-Plattform (M2M-auf-Typen kann keine entdeckten, zustandsbehafteten Modul-*Instanzen* tragen).
  → ersetzen statt erweitern (§5.5). Risikoarm, da nur Display.

### Kern-Erkenntnis (warum diese Architektur)
Auf echter HW hängt der STM32 per **USB** am CM4 (CDC-ACM = Steuerung/Shell, USB-Audio-Class = Audio, DFU = Update).
`native_sim` hat **keinen USB-Stack** und kann QEMU **kein** echtes USB-Gerät präsentieren. Die USB-Verbindung muss
also **nachgebildet** werden. Das FM-Board „läuft" damit **nicht *auf* dem Pi-Image, sondern *daneben* und wird
*hineingebrückt*.**

---

## 2. Getroffene Entscheidungen (Brainstorming-Ergebnis)

| Frage | Entscheidung | Begründung |
|---|---|---|
| **Topologie** | FM-Board (`native_sim`) läuft in **eigener Linux-VM**, verbunden mit der Pi-VM. Nicht im Pi-Guest, nicht am Host, keine echte USB-Emulation. | Entspricht dem realen Zwei-Geräte-Aufbau und ist Proxmox-nativ. `native_sim` ist nur ein x86-Linux-Prozess. |
| **Build-Scope (jetzt)** | **Nur Serial/Control-Bridge + Agent-Abstraktion.** Audio bewusst vertagt. | Audio über VM-Grenze ist der teure Teil; die Web-Anbindung ist Aufwand genug. |
| **Capability-Quelle** | Kleines, **komplett generisches** Self-Description-Kommando in der Firmware. | Zukunft = ganz andere Geräteklassen (HF, Filter, LTE). So generisch wie möglich. |
| **Wo sitzt die Abstraktion** | **Echter generischer Broker im Agent**; Firmware bleibt dünn (self-describe + execute). | Plattform-Logik nicht in jede Firmware duplizieren; Agent wird zentral per OTA aktualisiert; neuer Modultyp → null Broker-Änderung. |
| **Schema-Ambition** | Generisches Descriptor-Schema entwerfen, **gegen genau ein echtes Modul (SA818) end-to-end validieren**. | Beweist Generizität ohne Scope-Explosion; ein realer Datenpunkt deckt Schema-Lücken auf. |
| **TX-Exklusivität** | **Einfacher Exklusiv-Lock** (first-come), andere read-only/Warteschlange. Kein Kalender. | Reicht für DX-Camp-MVP, einfach zu bauen. |
| **Web-UI-Rendering** | **Generischer Renderer** aus Capability-Descriptor als Basis; spätere optionale Skins. | Neues Modul erscheint automatisch ohne UI-Code; skaliert mit der Modul-Vision. |
| **Audio (Referenz)** | Über VM-Grenze braucht es einen Netz-Audio-Transport (PCM/RTP) statt `snd-aloop`. Späteres Deliverable. | `snd-aloop` (Issue #30) wirkt nur innerhalb *eines* Kernels. |

---

## 3. Leitprinzip

> **Jede Schicht spricht so generisch wie möglich; Geräte-Spezifika leben so weit unten wie möglich.**

Konkret heißt das: Server und Web-UI sind **geräteagnostisch** (kennen kein „SA818", kein „FM"). Das einzige
modul-spezifische Stück ist der **SA818-Treiber im Agent**. Die Firmware ist die *Ground Truth* dessen, was
physisch steckt, und beschreibt sich selbst.

---

## 4. Gesamtarchitektur (Firmware → Web)

```
┌─ Browser (Operator) ─────────────────────────────────────────────┐
│  Web-UI: rendert Controls GENERISCH aus Capability-Descriptor     │
│  (Freq-Input mit Range, PTT-Button, Power-Enum, RSSI-Meter)       │
└───────────── ws/control/<station>  +  ws/status (Telemetry) ──────┘
                         │  semantisches Kommando {module, capability, op, value}
┌─ station-manager (Django) ── geräteagnostisch ───────────────────┐
│  • ControlConsumer (Browser-Seite) ──relay──► Agent-WS            │
│  • Capability-Registry: neues StationModule (entdeckte Instanz    │
│    + Capability-JSON je Station; UI rendert auch wenn offline)    │
│  • Access-Control: Topology + Membership; TX-Exklusiv-Lock        │
└───────── Outbound Ed25519-WS (Muster terminal.py) ───────────────┘
                         │  generisches typisiertes Kommando
┌─ station-agent (Pi-VM) ── DER BROKER, generisch, einmal geschrieben┐
│  • Discovery: fragt Modul-Descriptor ab, cached, meldet nach oben │
│  • validiert Kommando gegen Descriptor (Typ/Range/Zugriff)        │
│  • übersetzt semantisch → konkrete Modul-Syntax                   │
│  • SA818-Treiber = das EINZIGE modul-spezifische Stück            │
└───────── Serial-over-TCP (VM↔VM Bridge) ─────────────────────────┘
                         │  fm> Shell-Kommandos
┌─ Firmware (FM-VM, native_sim) ── dünn & ehrlich ──────────────────┐
│  • generisches Self-Description-Kommando (Identity + Capabilities)│
│  • führt generische Set/Action/Read-Kommandos aus → sa818 + GPIO  │
└───────────────────────────────────────────────────────────────────┘
```

---

## 5. Komponenten im Detail

### 5.1 Module-Capability-Protokoll (der Vertrag)
Das gemeinsame, geräteunabhängige Schema. Jedes Modul beantwortet eine Discovery-Abfrage mit einem **Descriptor**:

- **Identity:** `type`, `model`, `version` (z.B. `fm_transceiver` / `SA818-V` / `2m`)
- **Capabilities:** Liste typisierter Einträge, je mit:
  - `name` (z.B. `frequency`)
  - `kind`: `action` | `setting` | `telemetry`
  - `type`: `bool` | `int` | `float` | `enum` | `string`
  - `constraints`: `range` (min/max/step) | `enum`-Werte | `unit`
  - `access`: z.B. `operator` | `admin` (optional)

**Beispiel FM-Modul (SA818-V, 2m):**
```
frequency    setting   float  MHz  range 144.0–148.0
ptt          action    bool
power_level  setting   enum   [low, high]
rssi         telemetry int    dBm  readonly
volume       setting   int    1–8
bandwidth    setting   enum   [12.5, 25] kHz
```
**Beispiele für künftige Module (nur als Schema-Beleg, nicht zu bauen):**
- HF-Filter: `band_select: enum`, `bypass: bool`
- LTE-Modem: `apn: string (setting)`, `signal: int (telemetry)`, `connect: action`

> Erprobtes Muster (Home-Assistant-Entities, Matter-Cluster, Firmata, Redfish, USB-Descriptors) — schlank adaptiert.
> **Validierungs-Disziplin:** Das Schema gilt erst als „generisch", wenn ein realer Datenpunkt (z.B. `frequency`)
> end-to-end durch alle Schichten gelaufen ist.

### 5.2 Firmware (`FW-RemoteStation`) — dünn & ehrlich
- **Neu:** ein generisches Self-Description-Kommando (z.B. `module describe`) liefert Identity + Capability-Liste
  strukturiert (maschinenlesbar, nicht der Mensch-Prompt).
- **Neu:** generisches Ausführungs-Mapping: ein eingehendes typisiertes Kommando wird auf den vorhandenen
  `sa818`-Treiber + GPIO abgebildet. Die `sa818`-Shell darf intern erhalten bleiben; entscheidend ist die
  maschinenlesbare, generische Schnittstelle nach außen.
- **Kein** Plattform-Wissen, **keine** Capability-Persistenz, **kein** Zugriffsmodell in der Firmware.

### 5.3 VM↔VM Serial-Bridge (`linux-image` / Infra)
- `native_sim`-Konsolen-PTY in der FM-VM → TCP-Endpoint (z.B. `socat`/`ser2net`).
- Pi-VM verbindet sich und exponiert das als lokales Device/Stream für den Agent.
- DFU im 2-VM-Modell entfällt praktisch: „neue Firmware" = neues `native_sim`-Binary in die FM-VM deployen.

### 5.4 Agent-Broker (`station-manager` / `station_agent`) — Herzstück, generisch
- **Discovery:** beim Connect Descriptor abfragen, cachen, an Server melden.
- **Validierung:** eingehende semantische Kommandos gegen den Descriptor prüfen (Typ, Range, Zugriff).
- **Übersetzung:** semantisch → konkrete Modul-Syntax. **Nur der SA818-Treiber** kennt `sa818 at group …`.
- **Treiber-Form:** generisches Interface nach oben, SA818-konkreter Serial-Treiber darunter. Neuer Modultyp =
  neuer kleiner Treiber, **null** Broker-Änderung. (Eine generische *Firmware*-Shell wäre jetzt YAGNI.)
- Anbindung an den Server über die etablierte **Outbound-Ed25519-WS** (Muster `terminal.py`).

### 5.5 Server (`station-manager`) — geräteagnostische Registry + Router
- **Registry (neu, ersetzt Legacy):** ein neues **`StationModule`**-Model (Instanz, FK→Station) als
  Source-of-Truth: Identity-Felder + **Capability-Descriptor als JSON** + `online`/`last_seen`/Version,
  **vom Agent-Discovery befüllt** (nicht manuell). UI rendert daraus auch bei Offline-Station.
  Das Legacy-`ModuleType`/`installed_modules` (statischer Typ-Katalog, M2M, nur Display) wird **nicht erweitert**;
  Entscheidung „als dünner Namens-/Doku-Katalog behalten **oder** entfernen" fällt im Server-Deliverable
  (risikoarm, da keine Programm-Abhängigkeit). `firmware_flash_method` ist im Agent/OTA-Modell vermutlich obsolet.
- **Control-Channel:** neuer `ControlConsumer` (Browser-Seite, `ws/control/<station_id>/`) + Relay zum Agent
  über Channel-Layer-Group — exakt dem `TerminalConsumer`-Muster folgend.
- **Telemetrie:** periodischer Push (z.B. RSSI) Agent → Server → Browser, Muster `StationStatusConsumer`.
- **Access-Control:** Topology-Zuweisung (User ↔ Station) + Membership-Level entscheiden, wer bedienen darf.
- **TX-Exklusiv-Lock:** ein Operator hält exklusiven Control-Lock (first-come); andere read-only/Warteschlange.

### 5.6 Web-UI (`station-manager` / `internal-web`) — generischer Renderer
- Widgets werden aus `kind`+`type` generiert: `range → Slider/Input`, `enum → Dropdown`, `bool → Toggle`,
  `telemetry → Meter`. Neues Modul erscheint automatisch ohne UI-Code.
- HTMX + WebSocket für Live-Telemetrie und Kommando-Versand; xterm.js ist bereits vorhanden.
- Spätere optionale „Skins" (handpoliertes FM-Panel) ohne den generischen Kern aufzugeben.
- (Pixel-Agent muss für UI-Arbeit `Skill("frontend-design")` invoken — siehe CLAUDE.md.)

---

## 6. Datenfluss-Beispiel: „Operator stellt 145.500 MHz ein"

1. Browser sendet über `ws/control/<station>` → `{module: "fm0", capability: "frequency", op: "set", value: 145.500}`.
2. Server prüft Access-Control + TX-/Control-Lock, relayt das semantische Kommando an die Agent-WS.
3. Agent-Broker validiert gegen den gecachten Descriptor (float, 144.0–148.0).
4. SA818-Treiber übersetzt → `sa818 at group …` und schickt es über die Serial-Bridge in die FM-VM.
5. Firmware-Shell führt aus (sa818-Treiber + AT), antwortet.
6. Agent meldet Ack/Resultat zurück; Server pusht Status-Update an alle Viewer.

---

## 7. Scope-Matrix: jetzt vs. später

| Schicht | Jetzt (Build-Scope) | Später |
|---|---|---|
| Bridge | Serial-over-TCP VM↔VM | Audio-Bridge (Issue #30, Netz-Audio-Transport) |
| Firmware | generisches `describe` + SA818-Mapping | weitere Module |
| Agent | Broker + SA818-Treiber, Schema gegen SA818 validiert | weitere Treiber, Audio-IO |
| Server | Design dokumentiert (Legacy `ModuleType` nicht Fundament) | neues `StationModule` + Control-Consumer + TX-Lock; Legacy-Entscheid |
| UI | Design dokumentiert | generischer Renderer + Live-Telemetrie |

> Diese Spec beschreibt das **Gesamtbild bis zum Web-Interface**. Der *unmittelbare* Build-Scope ist
> **Bridge + Agent-Abstraktion** (inkl. minimalem Firmware-`describe`). Server- und UI-Schicht sind hier
> entworfen, damit die Agent-Abstraktion sie sauber aufnehmen kann, werden aber als eigene Deliverables gebaut.

---

## 8. Schnittstellen-Verträge (Definition-of-Done für die Abstraktion)

- **Firmware ↔ Agent:** maschinenlesbares `describe`-Format + typisiertes Kommando-Format über die Shell/Serial.
- **Agent ↔ Server:** generisches Capability-/Command-/Telemetry-JSON über die Ed25519-WS.
- **Server ↔ Browser:** dasselbe geräteagnostische JSON über `ws/control` + `ws/status`.
- Jeder Vertrag ist gegen `frequency` (FM/SA818) end-to-end validiert, bevor er „generisch" heißt.

---

## 9. Risiken & Mitigationen

| Risiko | Mitigation |
|---|---|
| Schema-Über-Engineering (zu abstrakt, nie validiert) | Strikt gegen SA818 end-to-end validieren; andere Module nur als Schema-Beispiele. |
| `native_sim`-Shell-Parsing brüchig (Mensch-Prompt vs. Maschine) | Maschinenlesbares `describe`/Command-Format definieren, nicht den `fm> `-Prompt parsen. |
| Audio-Annahmen schleichen in den Serial-Scope | Audio ist explizit out-of-scope; nur als Referenz dokumentiert. |
| TX-Lock-Races (zwei Operatoren) | Server-seitiger Exklusiv-Lock, serialisiert (Muster wie bestehende Lock-Logik in `accounts`/`stations`). |
| Cross-Repo-Drift (3 Repos) | Verträge in dieser Meta-Spec zentral; per-Repo-PRs referenzieren sie. |

---

## 10. Implementierungs-Aufteilung (grob, eigene Sessions/PRs/Issues)

1. **Meta:** diese Spec (`station-manager`, cross-cutting). ← *hier*
2. **FW-RemoteStation:** generisches `describe`-Kommando + typisiertes Command-Mapping auf den SA818-Treiber.
3. **linux-image / Infra:** VM↔VM Serial-over-TCP-Bridge + Doku (zweite Linux-VM für `native_sim`).
4. **station-manager (Agent):** generischer Broker (Discovery/Validierung/Übersetzung) + SA818-Treiber.
5. **station-manager (Server):** neues `StationModule`-Instanz-Model (entdeckt, Capability-JSON), `ControlConsumer`,
   TX-Lock; Legacy-`ModuleType`/`installed_modules` entsorgen oder zu dünnem Katalog reduzieren. *(später)*
6. **station-manager/internal-web (UI):** generischer Renderer + Live-Telemetrie. *(später)*

Jedes Folge-Deliverable durchläuft seinen eigenen spec→plan→code-Zyklus, referenziert aber die Verträge aus
Abschnitt 8.
