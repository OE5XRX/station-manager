# Design: Agent-Broker & Agent↔Server-Control-Protokoll (D3)

**Datum:** 2026-07-05
**Status:** Design (genehmigt, vor Implementierung)
**Repo (Spec-Heimat):** `station-manager` — Implementierung Agent-Seite in `station_agent`; das hier definierte **Protokoll** ist der Vertrag, den D4 (Server) und D5 (UI) konsumieren.
**Bezug:** baut auf D1 (generisches FW-Modul-Interface: `module list|describe|set|get|do`) und D2 (Slot-Discovery über `/dev/oe5xrx/slotN/control`). Verfeinert die Parent-Spec `2026-06-21-module-platform-sim-bridge-design.md` §5.4 (Broker) + §8 (Verträge).

> **Anspruch:** Dies ist der langlebige Plattform-Control-Vertrag. Bewusst gründlich, descriptor-getrieben und erweiterbar entworfen, damit spätere Module/Capabilities **keine** Protokoll-Änderung erzwingen.

---

## 1. Ziel & Kontext

Der Agent entdeckt bereits Module (D2). **D3** macht daraus den **Broker**: er nimmt semantische Kommandos vom Server, **validiert** sie gegen den gecachten Descriptor, **übersetzt** sie in die konkrete Modul-Syntax, führt sie aus und meldet **Ergebnis + Ist-Zustand + Telemetrie** nach oben. Der SA818-Agent-Treiber ist das *einzige* modul-spezifische Stück; Broker, Server und UI sind geräteagnostisch.

## 2. Leitprinzipien

- **Ein Vokabular von der Firmware bis zum Browser.** Die FW definiert es schon (`iface.h`): `Kind{Setting,Action,Telemetry}`, `Op{Set,Get,Do}`, `ValueType{Bool,Int,Float,Enum,String}`, typisierte `FieldSpec`. Das Protokoll spiegelt es 1:1 nach oben — kein paralleles Vokabular im Agent/Server.
- **Generische Adressierung `(slot, module, capability)`.** `slot` aus dem D2-Slot-Vertrag, `module` aus `module list`, `capability` aus dem Descriptor. **`fm` wird nie hardcoded** — der Agent enumeriert `list → describe`. Zwei identische Module (2 Slots) sind über den Slot unterscheidbar.
- **Descriptor-getrieben = „nie wieder ändern".** Kommandos tragen `(op, capability, value)`, validiert gegen den Descriptor; die Inventory trägt den Descriptor. Ein neues Modul / eine neue Capability / ein neuer ValueType = **null Protokoll-Änderung**.

## 3. Drei-Schichten-Datenmodell

| Schicht | Was | Nachricht | Kadenz |
|---|---|---|---|
| **Constraint / Descriptor-Metadata** | Typ, Unit, benannte Ranges (Bänder), Enum-Optionen, readonly (`FieldSpec`) | `inventory` | statisch (per Build); bei Connect/Topologie-Änderung |
| **Setting-Wert** | aktueller Wert einer Setting-Capability (z.B. `frequency`) | `state` | event-driven (nach Command) |
| **Telemetrie-Wert** | read-only, ändert sich von allein (z.B. `rssi`) | `state` | **subscription-basiert** (siehe §6) |

„Supported bands / Frequenzbereich" ist **Schicht 1** (die benannten Ranges der `frequency`-Capability), *kein* Wert — es fließt als Teil des Descriptors im `inventory`.

## 4. Broker-Pipeline (Agent-Seite)

```
Discovery   module list → module <id> describe → Descriptor cachen, Inventory melden   (aus D2)
Validierung eingehendes Command gegen gecachten Descriptor prüfen (Existenz/Typ/Range/Enum/Kind/Op)
Übersetzung generisch → konkrete Modul-Syntax  (module <id> set|get|do <cap> [value])
Ausführung  über den Slot-Control-Kanal /dev/oe5xrx/slotN/control  → MODULE-RESULT
Meldung     result (Ack) + state (Ist) nach oben; Telemetrie per Subscription
```
Der **SA818-Agent-Treiber** kapselt die konkrete Serial-/Shell-Interaktion; der Broker selbst kennt keine Modul-Spezifika.

## 5. Control-Modell: Hybrid

Imperative Kommandos **+** voller Ist-Zustand-Push. Nach jedem Command meldet der Agent den relevanten Ist-Zustand (`state`), plus Voll-Snapshot bei Connect (`inventory`). → Multi-Viewer & Reconnect sind konsistent, **ohne** Reconcile-Loop. Kein Desired-State-Abgleich (schlecht für PTT-Latenz, unnötig komplex).

## 6. Telemetrie: Subscription + `min_interval`

- Settings sind **event-driven** — kein periodisches Neu-Senden.
- Telemetrie ist **abonnierbar**: der Server sendet `subscribe {slot, module, capabilities[], interval_ms}`; der Agent streamt `state` für diese Capabilities in dieser Rate, **geclamped auf ein `min_interval`**, das der Descriptor pro Telemetrie-Capability deklariert.
- **Kein Abonnent → kein Stream → kein Polling** (SA818/FW wird nicht gehämmert). Idle = still.
- Kette: UI subscribed @ R → Server → Agent pollt FW (`module <id> get <cap>`) @ R (≥ min_interval) → pusht `state`.
- **FW-Implikation (kleine D1-Erweiterung):** die Telemetrie-`FieldSpec` bekommt ein `min_interval`/Default-Feld, damit die Rate descriptor-getrieben ist.

## 7. Nachrichten (Envelope `{ "v": 1, "type": …, … }`)

**Agent → Server**
| type | Felder | Zweck |
|---|---|---|
| `inventory` | `slots:[{slot, modules:[{module, identity, capabilities:[<descriptor>], state:{cap:value}}]}]` | Voll-Snapshot bei Connect + Topologie-Änderung (D2 plug/unplug) |
| `state` | `slot, module, values:{cap:value}, ts` | Ist-Werte nach Command / Telemetrie-Tick; Server merged nach (slot,module,cap) |
| `result` | `request_id, ok, value? , error?:{code,msg}` | Antwort auf ein Command |
| `event` | `slot, module, event, detail` | async (z.B. `ptt_auto_unkey`, `module_error`, `module_added/removed`) |

**Server → Agent**
| type | Felder | Zweck |
|---|---|---|
| `command` | `request_id, slot, module, capability, op(set\|get\|do), value?` | validieren → übersetzen → ausführen → `result` |
| `subscribe` | `slot, module, capabilities[], interval_ms` | Telemetrie-Stream starten (≥ min_interval) |
| `unsubscribe` | `slot, module, capabilities[]` | Stream stoppen |
| `ptt_keepalive` | `slot, module` | Dead-Man-Futter während aktivem TX (§8) |

## 8. Sicherheit

- **TX-Exklusiv-Lock — server-seitig (D4).** Ein Operator hält den Control-Lock (first-come); der Server relayt Kommandos nur vom Lock-Halter. Der Agent führt aus, was der Server relayt (kein Lock-Wissen nötig).
- **PTT-Dead-Man — agent-lokal (fail-safe).** `set ptt true` startet TX **und** den Dead-Man-Timer. Der Operator muss `ptt_keepalive` innerhalb `T` senden; **lapst es (Timeout ODER WS-Disconnect) → der Agent unkeyt sofort lokal** und meldet `event: ptt_auto_unkey`. Lokal, weil im Zweifel auch der Server unerreichbar ist. `set ptt false` unkeyt explizit. `T` konservativ (z.B. ~1–2 s), damit ein Dauerträger physisch unmöglich ist.

## 9. Transport

Ein **Control-WS pro Station**, über das etablierte **Outbound-Ed25519-WS-Muster** (`station_agent/terminal.py` ↔ `tunnel`/`stations`-Consumer). Der Agent baut die Verbindung nach außen auf (kein Inbound). Auth/Signatur wie beim Terminal-Kanal.

## 10. Versionierung & Fehler

- **Zwei getrennte Versionen:** Envelope `v` (Protokoll) und Descriptor `schema` (Modell). Unabhängig entwickelbar.
- **Strukturierte Fehler:** `error:{code, msg}`. Codes umfassen FW-Fehler (aus `MODULE-RESULT`, z.B. `unknown_module`, `out_of_range`, `read_only`) plus Transport-/Broker-Fehler (`unknown_slot`, `not_locked`, `validation_failed`, `timeout`).

## 11. Scope

- **DRIN (D3, Agent-Seite):** Broker-Pipeline (Discovery→Validierung→Übersetzung→Execute→Meldung), SA818-Agent-Treiber, der Agent-seitige Teil aller §7-Nachrichten, Subscription-Handling, PTT-Dead-Man, `inventory`/`state`/`result`/`event`-Emission, Control-WS-Anbindung. **Die Protokoll-Definition (§7/§8/§10) ist der verbindliche Vertrag.**
- **DRAUSSEN:** Server-Seite (`ControlConsumer`, TX-Lock-Enforcement, Registry-Persistenz) = **D4**; UI-Renderer = **D5**; Audio = D6–D9. Kleine D1-Erweiterung (`min_interval` in Telemetrie-FieldSpec) als eigener FW-Task.

## 12. Testing

- **Broker gegen `native_sim`:** Command validiert/übersetzt/ausgeführt; `result` + `state` korrekt; ungültige Werte (Range/Enum/Kind/Op) werden **vor** der FW abgelehnt.
- **Subscription:** Rate wird auf `min_interval` geclamped; ohne Abonnent kein Polling; Unsubscribe stoppt.
- **Dead-Man:** PTT ohne Keepalive → Auto-Unkey innerhalb `T`; WS-Disconnect während TX → Unkey; `event` gemeldet.
- **Generizität:** kein „fm"-String im Broker; ein zweites (fiktives) Modul im `native_sim`-Describe fließt ohne Broker-Änderung durch.

## 13. Implementierung (Prozess)

superpowers-/CLAUDE.md-Fluss (Brainstorming = dieses Dokument):
1. `superpowers:writing-plans` — Plan (Broker-Pipeline, SA818-Agent-Treiber, Message-Handling, Subscription, Dead-Man, Control-WS) mit Task-Checkboxen.
2. `superpowers:subagent-driven-development` (gateway für die Python-Agent-Seite).
3. `superpowers:test-driven-development` — gegen `native_sim` (Broker, Subscription, Dead-Man, Generizität).
4. `superpowers:verification-before-completion`, dann PR + copilot-loop.

Repo: `station-manager` (`station_agent`). Kleine begleitende FW-Erweiterung (`min_interval`) in `FW-RemoteStation`. Ein Deliverable-Branch je Repo, ein PR je Repo.

---

*Diese Spec definiert den Agent-Broker **und** den langlebigen Agent↔Server-Control-Vertrag (descriptor-getrieben, generisch adressiert, Hybrid-Push, Subscription-Telemetrie, TX-Lock + PTT-Dead-Man, versioniert). D4 (Server) und D5 (UI) implementieren die Gegenseite dieses Vertrags. Folgt dem Muster spec → plan → code.*
