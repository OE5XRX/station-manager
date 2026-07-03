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

Der Agent hängt **nicht** von der USB-Topologie ab, sondern von einer stabilen **Slot-Abstraktion**. Kanonische Pfade, z.B.:

```
/dev/oe5xrx/slot1/control   /dev/oe5xrx/slot1/audio   /dev/oe5xrx/slot1/dfu
/dev/oe5xrx/slot2/control   …
```

- **Echte HW:** udev-Regeln mappen den **physischen Hub-Port-Pfad** (`1-1.1 … 1-1.4`, fest auf dem BusBoard) auf den Slot-Pfad — z.B. `SUBSYSTEM=="tty", KERNELS=="1-1.1", SYMLINK+="oe5xrx/slot1/control"` (Audio/DFU analog übers Parent-USB-Device). Slot = Port-Pfad, deterministisch; **zwei identische Module** werden allein über den Port-Pfad unterschieden.
- **Sim:** ein **Sim-Harness** legt **exakt dieselben** Symlinks an, die auf die `native_sim`-pty / `snd-aloop`-Devices / `i2c-stub` zeigen.

→ **Der Agent scannt nur `/dev/oe5xrx/slot*/` — identischer Code in Sim und Real.** Einziger Unterschied: *wer* die Symlinks füllt (udev vs. Harness). `lsusb -t` bleibt die Quelle auf echter HW (über udev), wird vom Agent aber **nie direkt** aufgerufen → Sim funktioniert identisch, ohne USB zu emulieren.

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

- **Smart / self-describing** (FM, künftig HF): MCU + Zephyr, `describe` über `slotN/control`. Sim = `native_sim`-Instanz (pty [+ Audio]).
- **Passiv** (PowerBoard/INA226): kein `describe`; der **Agent-Treiber** beschreibt + liest (I²C). Sim = `i2c-stub`.

Der Broker (D3) muss beide Klassen tragen (Discovery: Firmware-`describe` vs. Agent-seitige Treiber-Erkennung). → **D3-Thema, hier nur festgehalten.**

## 6. Zukünftige Module / post-camp (zum Abspeichern)

- **Audio:** `snd-aloop` (same-kernel) verbindet `native_sim` ↔ Agent — das ist Issue **#30** (FW-RemoteStation). Kein Netz-Audio nötig.
- **DFU-Sim:** „neue Firmware" = neues `native_sim`-Binary reinlegen + Prozess neu starten (kleiner sim-dfu-Service).
- **HF-Modul:** weitere `native_sim`-Instanz auf einem freien USB-Slot.
- **PowerBoard / INA226:** **eigener Power-Connector, kein USB** → verbraucht **keinen** der 4 Slots und ist **immer präsent** (ohne Strom keine Station). Also **fixe Onboard-Peripherie** auf I²C, außerhalb der Slot-1–4-Logik — im Manifest ein eigener „fixed"-Abschnitt, im Sim `i2c-stub` always-on.

## 7. Scope-Matrix: Camp-Slice vs. später

| Aspekt | Camp-Slice (jetzt) | Post-camp |
|---|---|---|
| Topologie | co-located `native_sim` im Image | unverändert |
| Slot-Vertrag | 1 Slot, FM-Serial; Harness-Default (kein Manifest nötig) | volle 1–4-Slot-Generik + Manifest |
| Transport | Serial/Control (pty) | Audio (`snd-aloop`/#30), DFU-Sim |
| Modultypen | FM (SA818) | HF, PowerBoard/INA226 (fix) |
| Manifest-Quelle | Config-Disk/cloud-init (bzw. Default) | + optionaler station-manager-Generator |

**Unmittelbarer Build-Scope (D2):** co-located `native_sim`-FM + Slot-Vertrag (Harness legt `slot1/control` an) + Agent entdeckt/öffnet es. Audio/DFU/Multi-Slot sind entworfen, aber eigene Deliverables.

## 8. Datenfluss (Camp-Slice)

1. VM bootet; Harness startet `native_sim` (FM), legt `/dev/oe5xrx/slot1/control` (Symlink → pty) an.
2. Agent scannt `/dev/oe5xrx/slot*/`, findet slot1, sendet `describe` → erhält Identity (`fm_transceiver`/`SA818-V`/`2m`) + Capabilities.
3. Agent meldet das Inventar an den station-manager (Muster wie bestehende Outbound-WS).
4. Später: Control-Kommandos (D3+) fließen `slot1/control` → `native_sim` → SA818-Treiber.

## 9. Definition-of-Done (D2 Camp-Slice)

- linux-image startet `native_sim`-FM als Dienst im Guest; Slot-Vertrag `slot1/control` wird materialisiert.
- Agent-Seite kann das Slot-Device öffnen und ein `describe` durchführen (End-to-End gegen `native_sim`, ohne HW).
- Der agent-zugewandte Pfad ist identisch zu dem, den udev auf echter HW liefern würde (sim↔real-Parität dokumentiert/geprüft).
- Doku: 2-Minuten-Anleitung „Sim-Station in Proxmox starten".

## 10. Risiken & Mitigationen

| Risiko | Mitigation |
|---|---|
| `native_sim` landet in Prod-Images | dev-only Image-Feature/Overlay; klar getrennt vom Prod-Build. |
| Slot-Vertrag driftet zwischen udev (real) und Harness (sim) | Der Vertrag (Pfad-Schema) ist **eine** dokumentierte Wahrheit; beide Seiten erfüllen ihn, ein gemeinsamer Test prüft die Pfade. |
| Über-Generalisierung (Multi-Slot/Manifest vor Bedarf) | Camp-Slice = 1 Slot, Harness-Default. Generik erst wenn ein zweites Modul real wird. |
| Passiv-Modul-Discovery unklar | Als D3-Thema markiert, nicht in D2 gelöst. |

## 11. Testing

- **Unit/Integration (native_sim):** Harness legt Slot-Vertrag korrekt an; Agent entdeckt + `describe` liefert erwartete Capabilities. CI-fähig, kein HW.
- **Parität:** ein Test/Doc, der zeigt, dass der Agent-Pfad (`/dev/oe5xrx/slotN/...`) in Sim und Real identisch konsumiert wird.

---

*Diese Spec beschreibt die co-located Modul-Simulations-Schicht bis zur Agent-Anbindung. Der unmittelbare Build-Scope ist der **FM-Serial-Slice auf einem Slot**; Audio, DFU-Sim, Multi-Slot und weitere Modultypen sind hier entworfen, werden aber als eigene Deliverables gebaut. Folgt dem Muster spec → plan → code je Deliverable.*
