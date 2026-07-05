# Design: Co-located Modul-Simulations-Schicht (D2)

**Datum:** 2026-07-04
**Status:** Design (genehmigt, vor Implementierung)
**Repo (Spec-Heimat):** `station-manager` — cross-cutting; Implementierung v.a. in `linux-image` (+ `station_agent`)
**Bezug:** verfeinert und **ersetzt die Topologie-Entscheidung** aus `2026-06-21-module-platform-sim-bridge-design.md` (§2 „eigene FM-VM" und §5.3 „VM↔VM Serial-over-TCP-Bridge"). Deckt Deliverable **D2** der Roadmap ab; Voraussetzung für D3 (Agent-Broker).

---

## 1. Ziel & Problem

Die Remote-Station soll **ohne echte Hardware** testbar sein — als Fundament der steckbaren Modul-Plattform. Kern-Problem: Auf echter HW hängen die Module per **USB** hinter einem Hub am CM4 (CDC-ACM = Control, USB-Audio = Audio, DFU = Update). `native_sim` hat **keinen USB-Stack** und kann QEMU kein echtes USB-Gerät präsentieren. Die Verbindung muss also **nachgebildet** werden.

**Entscheidung gegenüber der Vorgänger-Spec:** Statt einer getrennten FM-VM mit TCP-Bridge läuft das simulierte Modul **co-located im Pi-Image** (linux-image ist `qemux86-64` → `native_sim` ist ein nativer x86-Prozess im selben Guest/Kernel). Grund: Linux liefert für jede Modul-Schnittstelle ein virtuelles Device — **pty** (Serial), **`snd-aloop`** (Audio), **`i2c-stub`** (I²C) — alle im *einen* Kernel. Damit skaliert der Sim auf **N heterogene Module** durch Hinzufügen von Prozessen/virtuellen Devices, und die späteren Stufen (Audio, DFU-Sim, weitere Modultypen) werden **einfacher**, nicht schwerer. Die 2-VM/TCP-Variante ist serial-zentrisch und trägt weder I²C-Sensoren noch mehrere Module sauber.

## 2. Leitprinzip: Richtung der Wahrheit

> **Die Station entdeckt ihre Module und meldet sie nach oben. Der station-manager konfiguriert nie, was steckt.**

Auf echter HW: Modul physisch einstecken → Agent entdeckt (USB-Topologie + `describe`) → meldet Inventar an den Server. Der Server **zeigt/speichert** nur.
Im Sim gilt dieselbe Richtung — nur das „Einstecken" ist ein **lokales Manifest** statt physischer Stecker.

## 3. Der Slot-Vertrag (Herzstück der sim↔real-Parität)

Der Agent hängt **nicht** von der USB-Topologie ab, sondern von einer stabilen **Slot-Abstraktion** — kanonische Pfade, in Sim **und** Real identisch befüllt:

```
/dev/oe5xrx/slot1/control   /dev/oe5xrx/slot1/audio   /dev/oe5xrx/slot1/dfu
/dev/oe5xrx/slot2/control   …
```

### 3a. Echte HW (Populator = udev, Quelle = USB-Topologie)

Das FM-Modul meldet sich als **USB-Composite** (CDC-ACM + USB-Audio + DFU) hinter dem **FE1.1s-Hub** auf dem BusBoard. Alle drei Interfaces sind **Kinder desselben USB-Geräts** am Hub-Port `1-1.X`. Die vier Hub-Ports sind auf dem BusBoard **fest verdrahtet** → **Port = Slot**, deterministisch (`1-1.1`→slot1 … `1-1.4`→slot4).

Ein **udev-Ruleset in `linux-image`** matcht den **Parent-Port** und legt die Slot-Symlinks an:

```
SUBSYSTEM=="tty",   KERNELS=="1-1.1", SYMLINK+="oe5xrx/slot1/control"   # CDC-ACM
SUBSYSTEM=="sound", KERNELS=="1-1.1", SYMLINK+="oe5xrx/slot1/audio"     # UAC2
SUBSYSTEM=="usb",   KERNELS=="1-1.1", ENV{ID_DFU}=="1", SYMLINK+="oe5xrx/slot1/dfu"
# … analog slot2..4 für 1-1.2 … 1-1.4
```

Eigenschaften:
- **Zwei identische Module** (gleiche VID/PID/Serial) werden **allein über den Port-Pfad** unterschieden — deshalb „Slot = Port", nicht „Slot = Seriennummer".
- **Hot-plug:** Modul ziehen/stecken → udev entfernt/legt die Slot-Symlinks an → der Agent sieht denselben Slot kommen/gehen.
- Das udev-Ruleset (+ die deterministische Port→Slot-Tabelle) ist ein **eigenes Deliverable in `linux-image`** und ist die *echte* Hälfte des Vertrags — gleichwertig zur Sim-Hälfte.

### 3b. Sim (Populator = Harness, Quelle = Manifest)

Ein **Sim-Harness** (im Image, dev-only) legt **exakt dieselben** Symlinks an — sie zeigen auf die `native_sim`-pty (control), `snd-aloop`-Devices (audio) bzw. `i2c-stub` (passive Module). Quelle ist das lokale Manifest (§4).

### 3c. sim↔real-Symmetrie (die geteilte Wahrheit)

| Schnittstelle | Echte HW | Sim |
|---|---|---|
| Slot-Identität | USB-Hub-Port-Pfad `1-1.X` (fest) | Harness weist zu (Manifest) |
| `slotN/control` | CDC-ACM (`ttyACM*`) via udev | `native_sim`-pty via Harness |
| `slotN/audio` | USB-Audio (UAC2) via udev | `snd-aloop`-Device via Harness |
| `slotN/dfu` | USB-DFU-Interface via udev | Binary-Swap-Service |
| **Populator** | **udev** (aus USB-Topologie) | **Sim-Harness** (aus Manifest) |
| Discovery + Report | Agent scannt Slots, `describe`, meldet | **identisch** |

**Der Agent scannt nur `/dev/oe5xrx/slot*/` — alles unterhalb der „Populator"-Zeile ist geteilter Agent-Code.** `lsusb -t` bleibt die *Quelle* auf echter HW (über udev), wird vom Agent aber **nie direkt** aufgerufen → Sim funktioniert identisch, ohne USB zu emulieren.

## 4. Das Slot-Manifest (lokale physische Realität)

Das Manifest ist das Sim-Äquivalent zum physischen Einstecken — es **lebt lokal an der VM**, nicht am Server (sonst würde die Richtung der Wahrheit umgekehrt).

**Lieferung:** per **Config-Disk / cloud-init (NoCloud)** — beim Anlegen der Proxmox-VM angehängt; der Harness liest sie beim Boot. Beispiel:

```yaml
slots:
  1: { type: fm_transceiver, variant: 2m }
  2: { type: fm_transceiver, variant: 70cm }   # gleiches Modul, anderer Slot
  3: { type: power_board }                       # INA226, siehe §6
  4: empty
```

Ablauf: Manifest editieren (= „einstecken") → Harness materialisiert Slot-Devices → Agent **entdeckt** (`describe`) → **meldet** Inventar an den station-manager. Der Server diktiert nie; optionaler späterer Komfort: der station-manager *generiert* ein Manifest-Snippet zum Download (Quelle der Wahrheit bleibt lokal + gemeldet).

## 5. Modul-Klassen

- **Smart / self-describing** (FM, künftig HF): MCU + Zephyr, `describe` über `slotN/control`. Real = USB-Composite; Sim = `native_sim`-Instanz (pty [+ Audio]).
- **Passiv** (PowerBoard/INA226): kein `describe`; der **Agent-Treiber** beschreibt + liest (I²C). Real = I²C am CM4/Bus; Sim = `i2c-stub`.

Der Broker (D3) muss beide Klassen tragen (Discovery: Firmware-`describe` vs. Agent-seitige Treiber-Erkennung). → **D3-Thema, hier nur festgehalten.**

## 6. Zukünftige Module / post-camp (zum Abspeichern)

- **Audio:** `snd-aloop` (same-kernel) verbindet `native_sim` ↔ Agent — das ist Issue **#30** (FW-RemoteStation). Kein Netz-Audio nötig. (Real: UAC2 über USB.)
- **DFU-Sim:** „neue Firmware" = neues `native_sim`-Binary reinlegen + Prozess neu starten (kleiner sim-dfu-Service). (Real: USB-DFU.)
- **HF-Modul:** weitere `native_sim`-Instanz auf einem freien USB-Slot. (Real: einstecken.)
- **PowerBoard / INA226:** **eigener Power-Connector, kein USB** → verbraucht **keinen** der 4 Slots und ist **immer präsent** (ohne Strom keine Station). Also **fixe Onboard-Peripherie** auf I²C, außerhalb der Slot-1–4-Logik — im Manifest ein eigener „fixed"-Abschnitt, im Sim `i2c-stub` always-on. Auf echter HW entsprechend eine feste udev/Discovery-Regel, kein Hub-Port.

## 7. Scope-Matrix: Camp-Slice vs. später

| Aspekt | Camp-Slice (jetzt) | Post-camp |
|---|---|---|
| Topologie | co-located `native_sim` im Image | unverändert |
| Slot-Vertrag | 1 Slot, FM-Serial | volle 1–4-Slot-Generik, identische Module, Hot-plug |
| Sim-Populator | Harness-Default (kein Manifest nötig) | Harness + Manifest |
| **Real-Populator** | **1 FM-Modul: Agent öffnet `slot1` (bzw. direkt `ttyACM0`)** | **volles udev-Ruleset (Port→Slot 1–4)** |
| Transport | Serial/Control (pty) | Audio (`snd-aloop`/#30), DFU-Sim |
| Modultypen | FM (SA818) | HF, PowerBoard/INA226 (fix) |
| Manifest-Quelle | Config-Disk/cloud-init (bzw. Default) | + optionaler station-manager-Generator |

**Symmetrisches Staging:** Camp = *ein* FM-Modul auf **beiden** Seiten (Sim: Harness-Default; Real: das eine `ttyACM`/`slot1`). Die volle Slot-Generik (udev-Ruleset ↔ Manifest, 1–4, identische Module, Hot-plug) ist **auf beiden Seiten** post-camp.

**Unmittelbarer Build-Scope (D2):** co-located `native_sim`-FM + Slot-Vertrag (Harness legt `slot1/control` an) + Agent entdeckt/öffnet es. Das Real-HW-udev-Ruleset ist mit-entworfen (§3a), wird aber als eigenes `linux-image`-Deliverable gebaut. Audio/DFU/Multi-Slot sind entworfen, aber eigene Deliverables.

## 8. Datenfluss (Camp-Slice)

1. VM bootet; Harness startet `native_sim` (FM), legt `/dev/oe5xrx/slot1/control` (Symlink → pty) an.
2. Agent scannt `/dev/oe5xrx/slot*/`, findet slot1, sendet `module list` → erhält die Modul-IDs (`{"modules":["fm", …]}`), dann pro ID `module <id> describe` → Identity (`fm_transceiver`/`SA818-V`/`2m`) + Capabilities. Keine hartkodierte Modul-ID — die Firmware ist self-describing.
3. Agent meldet das Inventar an den station-manager (Muster wie bestehende Outbound-WS).
4. Später: Control-Kommandos (D3+) fließen `slot1/control` → `native_sim` → SA818-Treiber.

*Auf echter HW ist der Ablauf ab Schritt 2 identisch — nur `slot1/control` wird von udev statt vom Harness angelegt.*

## 9. Definition-of-Done (D2 Camp-Slice)

- linux-image startet `native_sim`-FM als Dienst im Guest; Slot-Vertrag `slot1/control` wird materialisiert.
- Agent-Seite kann das Slot-Device öffnen und ein `describe` durchführen (End-to-End gegen `native_sim`, ohne HW).
- Der agent-zugewandte Pfad ist identisch zu dem, den udev auf echter HW liefert (sim↔real-Parität dokumentiert/geprüft; das udev-Ruleset für den Real-Fall existiert bzw. ist als Deliverable spezifiziert).
- Doku: 2-Minuten-Anleitung „Sim-Station in Proxmox starten".

## 10. Risiken & Mitigationen

| Risiko | Mitigation |
|---|---|
| `native_sim` landet in Prod-Images | dev-only Image-Feature/Overlay; klar getrennt vom Prod-Build. |
| Slot-Vertrag driftet zwischen udev (real) und Harness (sim) | Der Vertrag (Pfad-Schema) ist **eine** dokumentierte Wahrheit; beide Seiten erfüllen ihn, ein gemeinsamer Test prüft die Pfade. |
| Real-udev-Ruleset bleibt hinter der Sim-Seite zurück | Beide Hälften im selben Design (§3a/§3b); DoD verlangt die Real-Seite als Deliverable, nicht nur die Sim. |
| Über-Generalisierung (Multi-Slot/Manifest vor Bedarf) | Camp-Slice = 1 Slot, Harness-Default. Generik erst wenn ein zweites Modul real wird. |
| Passiv-Modul-Discovery unklar | Als D3-Thema markiert, nicht in D2 gelöst. |

## 11. Testing

- **Unit/Integration (native_sim):** Harness legt Slot-Vertrag korrekt an; Agent entdeckt + `describe` liefert erwartete Capabilities. CI-fähig, kein HW.
- **Real-Seite:** udev-Ruleset gegen die erwarteten Port→Slot-Symlinks prüfen (z.B. mit `udevadm test` / einem simulierten sysfs-Pfad) — ohne echtes Modul verifizierbar.
- **Parität:** ein Test/Doc, der zeigt, dass der Agent-Pfad (`/dev/oe5xrx/slotN/...`) in Sim und Real identisch konsumiert wird.

## 12. Implementierung (Prozess)

Umsetzung folgt dem superpowers-/CLAUDE.md-Fluss — der Design-Schritt lief bereits über `superpowers:brainstorming` (dieses Dokument):

1. `superpowers:writing-plans` — Implementierungsplan (Slot-Vertrag, Sim-Harness, udev-Ruleset, Agent-Discovery) mit Task-Checkboxen.
2. `superpowers:subagent-driven-development` (Default; forge-Agent für linux-image/Yocto + Infra, gateway für die Agent-Seite).
3. `superpowers:test-driven-development` — native_sim-Integrationstests + `udevadm test` für die Real-Seite.
4. `superpowers:verification-before-completion`, dann PR + copilot-loop. `Closes #23`.

Repos: v.a. `linux-image` (Sim-Harness, udev-Ruleset, dev-only Image-Feature), Agent-Discovery in `station_agent`. Ein Deliverable-Branch, ein PR.

---

*Diese Spec beschreibt die co-located Modul-Simulations-Schicht bis zur Agent-Anbindung — mit gleichwertiger Real-HW- und Sim-Hälfte des Slot-Vertrags. Der unmittelbare Build-Scope ist der **FM-Serial-Slice auf einem Slot**; Audio, DFU-Sim, Multi-Slot und weitere Modultypen sind hier entworfen, werden aber als eigene Deliverables gebaut. Folgt dem Muster spec → plan → code je Deliverable.*
