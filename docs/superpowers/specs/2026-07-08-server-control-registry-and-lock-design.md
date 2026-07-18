# Design: Server-Seite — StationModule-Registry, Control-Relay & TX-Lock (D4)

**Datum:** 2026-07-08
**Status:** Design (genehmigt, vor Implementierung)
**Repo:** `station-manager` (Django/Channels).
**Bezug:** implementiert die **Server-Gegenseite** des in D3 definierten Agent↔Server-Vertrags (`2026-07-05-agent-broker-and-control-protocol-design.md`). Baut auf D1 (FW-Modul-Interface), D2 (Slot-Discovery), D3 (Agent-Broker + Control-WS). Konsumiert von D5 (UI). Verfeinert Parent-Spec §5.5.

---

## 1. Ziel & Kontext

Der Agent (D3) verbindet sich nach außen über eine persistente Control-WS und spricht den §7-Vertrag (`inventory`/`state`/`result`/`event` ↑, `command`/`subscribe`/`unsubscribe`/`ptt_keepalive` ↓). **D4 baut die Server-Seite:** nimmt diese Agent-Verbindung an, **relayt** zum Browser (und zurück), **cached** die Modul-Registry, **erzwingt** Access-Control + TX-Lock. Der Server **transformiert den Vertrag nicht** — ein Vokabular von der Firmware bis zum Browser; er *gated* und *cached*.

## 2. Leitprinzipien

- **Spiegelt das erprobte Terminal-Relay-Muster** (`tunnel.TerminalConsumer` + `AgentTerminalConsumer`, gebrückt über eine Channel-Layer-Group). Kein neues Transport-Paradigma.
- **Relay + Gate + Cache, keine Transformation.** Der §7-Vertrag fließt verbatim; D4 fügt Registry, Zugriff, Lock hinzu.
- **Rechte-Modell wiederverwenden**, nicht neu erfinden (siehe §6).

## 3. Zwei Consumer + Channel-Group-Relay

```
Browser  ─ws/control/<station_id>/─►  ControlConsumer      ┐
                                                            ├─ Channel-Group  control_<station_id>
Agent    ─ws/agent/control/<station_id>/─► AgentControlConsumer ┘   (D3s ControlClient dockt hier an)
```

- **`AgentControlConsumer`** (agent-facing, Ed25519-signiert wie `AgentTerminalConsumer`): empfängt `inventory`/`state`/`result`/`event` → aktualisiert Registry + `group_send` an die Viewer. Sendet `command`/`subscribe`/`unsubscribe`/`ptt_keepalive` (vom Server, gated) an den Agent. **Genau eine** Agent-Verbindung pro Station.
- **`ControlConsumer`** (browser-facing): Access-controlled (§6). Nimmt Operator-Aktionen entgegen (Kommando, Lock-anfragen/übergeben/freigeben, PTT-Keepalive, subscribe), **gated durch den Lock**, und relayt sie an den Agent. Pusht `state`/Telemetrie/`inventory` + Lock-Status an alle Viewer der Station.
- Gebrückt über die Channel-Group `control_<station_id>`. Eigener Kanal, getrennt vom bestehenden `StationStatusConsumer` (allgemeiner Stations-Status) und `TerminalConsumer` (Shell).

## 4. StationModule-Registry (neues Model, greenfield)

Legacy `ModuleType`/`installed_modules` ist durch PR #82 bereits entfernt → sauberer Tisch.

`StationModule`:
- `station` (FK→`stations.Station`), `slot`, `module_id` — **unique `(station, slot, module_id)`** (Slot disambiguiert identische Module).
- Identity: `type`, `model`, `version`.
- `capability_descriptor` (JSON) — der `describe`-Descriptor inkl. Constraints/benannter Ranges.
- `last_state` (JSON) — **letzter bekannter Settings-Zustand** (für Offline-Rendering).
- `online` (bool), `last_seen`.

**Lifecycle:**
- **Upsert aus `inventory`** (vom Agent). Nicht mehr gemeldete Module → `online=false` (soft, nicht löschen — Audit/History + FK-Sicherheit, konsistent mit dem Soft-Delete-Muster von ImageRelease).
- **Online/Offline** über die **Control-WS-Liveness** (Agent verbunden + Modul im letzten `inventory`) — nicht über den 120-s-HTTP-Heartbeat.
- **Persistenz-Regel:** Descriptoren **+ `last_state` (Settings)** persistent → die UI rendert das Panel **auch bei Offline-Station**. **Telemetrie (RSSI) ist ephemer** — nie persistiert, nur live per Subscription.

## 5. TX-Lock (per-Station, erweiterbar)

- **Zustand:** `FREE` oder `HELD von <user>` mit `last_activity`. Der Halter steht im Push an alle Viewer → jeder sieht live, wer steuert.
- **Granularität:** keyed `(station, scope)` mit `scope=station` heute — später trivial auf `scope=module` oder Rollen (Lehrer/Schüler) erweiterbar. **Server-Logik, kein Protokoll-Vertrag** → jederzeit änderbar.
- **Der Lock gehört dem *User*** (nicht der rohen WS): mehrere Tabs desselben Users teilen den Hold.

**Hand-off:**
1. **Kooperativ:** Halter „Freigeben" → `FREE`. Oder Viewer „Control anfragen" → Halter bekommt `control_requested` → „übergeben" = **gezielter atomarer Transfer** an den Anfrager (Halter → read-only).
2. **Auto-Free:** Halter-Disconnect (nach kurzer **Reconnect-Grace ~10–15 s**, damit ein Netz-Blip die Kontrolle nicht verliert) · **Inaktivitäts-Timeout `T_idle`** (Minuten, kein Kommando/Keepalive → frei).
3. **Eskalation:** **Station-ADMIN / globaler Admin** kann **erzwungen übernehmen** (Preemption); Halter → benachrichtigt + read-only. (Haken für Lehrer/Supervisor + Notfall.)

**Kommando-/PTT-Gating:** Nur Kommandos **des Lock-Halters** werden an den Agent relayt; nur der Halter keyt PTT. Koppelt an den agent-lokalen **PTT-Dead-Man** (D3): Keepalive weg → Agent unkeyt **sofort** + Server released den Lock (nach der Grace) → nächster kann übernehmen.

**Zwei Timer — nicht verwechseln:**
| Timer | Wo | Skala | Zweck |
|---|---|---|---|
| `T_ptt` (Dead-Man, D3) | agent-lokal | Sekunden | kein Dauerträger, nur während aktivem TX; feuert **sofort** bei WS-Drop |
| `T_idle` (Lock, D4) | Server | Minuten | idle Lock freigeben |
| Reconnect-Grace | Server | ~10–15 s | Lock übersteht einen kurzen Blip (PTT-Unkey erfolgt trotzdem sofort) |

## 6. Access-Control (bestehendes Modell wiederverwenden)

- Global: `accounts.User.MembershipLevel` (`APPLICANT`→`MEMBER`→`STAFF`→`ADMIN`). Applicants ausgeschlossen.
- Pro Station/Region: `StationAssignment` (ADMIN/Maintainer), `RegionAssignment` (Manager). Helper: `can_use_station`, `can_maintain_station`, `can_administer_station`, `is_station_admin`, `is_region_manager`, `is_admin`.
- **Mapping:**
  - **Sehen + Steuern (Lock holen, Kommandos, PTT)** → **`can_use_station(station)`**. Viewer vs. Operator ist **der Lock**, keine eigene Rechte-Stufe.
  - **Erzwungene Übernahme / Config** → `is_station_admin` / `can_administer_station` / globaler Admin.
- **Künftiger Haken (nicht jetzt):** ein „darf senden / lizenziert"-Bit für Spectator-nur-schauen vs. lizenziert-operieren — erweiterbar obendrauf.

## 7. Edge-Cases & Robustheit

- **Agent-Disconnect (Station offline):** Lock **freigeben**, Module `online=false`; UI rendert offline aus persistierten Descriptoren; Reconnect → frisches `inventory`. (Unterschieden von Browser-Halter-Disconnect.)
- **Command-Timeout:** kein `result` vom Agent in `T` → strukturierter Timeout-Fehler an den Browser (kein hängendes UI).
- **Audit-Logging** (passt zur Audit-Kultur): Lock acquire/übergeben/preempt/release, PTT on/off, gesetzte Kommandos → Audit-Log (Muster wie bestehende Station-/Account-Audit-Logs, Signal-basiert).

## 8. Datenfluss — „Operator setzt 145.500 MHz"

1. Browser (Lock-Halter) → `ControlConsumer`: `{command, capability:frequency, op:set, value:145.5, request_id}`.
2. `ControlConsumer` prüft Access (`can_use_station`) + Lock-Besitz → `group_send` an den Agent-Kanal.
3. `AgentControlConsumer` → Agent (Control-WS) → Broker validiert/übersetzt/führt aus (D3).
4. Agent → `result` + `state` → `AgentControlConsumer` → Registry-Update (`last_state`) + `group_send` an **alle Viewer**.
5. Nicht-Halter sehen den neuen Zustand read-only.

## 9. Scope

- **DRIN (D4):** beide Consumer + Channel-Relay, `StationModule`-Registry + Lifecycle, TX-Lock + Hand-off, Access-Control-Anbindung, Edge-Cases (§7). Nutzt den **D3-§7-Vertrag verbatim**.
- **DRAUSSEN:** Browser-UI/Renderer = **D5**. Audio = D6–D9. Per-Modul-Lock / Lehrer-Schüler-Rollen / Lizenz-Bit = spätere Erweiterungen (erweiterbar angelegt).

## 10. Konfiguration

`T_idle` (Lock-Inaktivität), `T_ptt` (Dead-Man; primär agentseitig), Reconnect-Grace, Max-Viewer pro Station (analog `MAX_SESSIONS_PER_STATION`). Sinnvolle Defaults, per Settings/Config überschreibbar.

## 11. Testing

- **Relay:** Command vom Browser → Agent-Kanal → `result`/`state` zurück an alle Viewer (Channels-Test mit Mock-Agent-Consumer).
- **Registry:** `inventory` → Upsert; Modul weg → `online=false`; Offline-Render aus `last_state`.
- **Lock:** acquire/übergeben (gezielt)/release · Nicht-Halter-Kommando abgelehnt · Disconnect+Grace · `T_idle`-Auto-Free · Admin-Preemption.
- **Access:** `can_use_station` gate für Sehen+Steuern; Applicant/kein-Zugriff abgelehnt.
- **Edge:** Agent-Disconnect → Lock frei + Module offline; Command-Timeout → Fehler an Browser; Audit-Einträge entstehen.

## 12. Implementierung (Prozess)

superpowers-/CLAUDE.md-Fluss (Brainstorming = dieses Dokument):
1. `superpowers:writing-plans` — Plan (Model + Migration, zwei Consumer + Routing, Channel-Relay, Lock-Logik + Hand-off, Access-Anbindung, Audit, Config) mit Task-Checkboxen.
2. `superpowers:subagent-driven-development` (gateway/vault für Django/Channels/Model).
3. `superpowers:test-driven-development` — Channels-Consumer-Tests (Relay, Lock, Registry, Access, Edge).
4. `superpowers:verification-before-completion`, dann PR + copilot-loop (station-manager PRs brauchen erfahrungsgemäß mehrere Runden).

Repo: `station-manager`. Code-Platzierung: Registry im `stations`-Umfeld, agent-facing Consumer nach `tunnel`-Muster (oder eigene `control`-App) — Detail im Plan. Ein Deliverable-Branch, ein PR.

---

*Diese Spec definiert die Server-Gegenseite des D3-Vertrags: Relay (zwei Consumer + Channel-Group), StationModule-Registry (descriptor + last-state persistent, Telemetrie ephemer), TX-Lock per-Station mit vollem Hand-off (kooperativ/auto/eskalation, User-Ownership + Reconnect-Grace, zwei Timer), Access-Control über das bestehende `can_use_station`-Modell, plus Agent-Disconnect/Command-Timeout/Audit. D5 (UI) rendert generisch aus der Registry. Folgt dem Muster spec → plan → code.*
