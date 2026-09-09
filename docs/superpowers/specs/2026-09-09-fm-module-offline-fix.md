# FM-Modul „offline" Fix + Audio/Control-Flag-Removal — Spec

**Status:** LOCKED (mit User abgestimmt — nicht neu brainstormen)
**Scope:** NUR `station-manager` (Server + `station_agent`). FW-Repo (periodische
Status-Zeile → pull, `audio`-Capability in `describe`) läuft in einer PARALLELEN
Session. Kein `linux-image`-SRCREV-Bump in diesem PR (nur im PR-Text erwähnen).

## Problem / Root Cause (bereits bewiesen)

Beim Agent-Start verliert die Slot-Discovery (`control_client → discover_slots →
probe_slot`) ein Timing-Race mit dem gesprächigen Control-Console des FM-Moduls.
Die Platine sendet auf derselben Control-UART (`/dev/oe5xrx/slotN/control`, cdc_acm)
async Zephyr-Logs **plus** eine PERIODISCHE Status-Zeile
(`"NN Status - Power: ON, PTT: OFF, SQL: CLOSED"`).

Effekt: Eine async Log-Zeile kann mitten in die `MODULE-LIST {...}`-Antwortzeile
gedruckt werden → die JSON-Payload dieser Zeile ist zerrissen → `probe_slot` findet
„no MODULE-LIST" → `discover_slots` liefert leeres Inventory → `control_client` sendet
**genau einmal** `{"type":"inventory","slots":[]}` und **nie wieder** → Modul dauerhaft
offline.

Beweise, dass HW ok ist:
- `python -m station_agent selftest serial --slot 3` = OK (SA818-V, volle Caps).
- Manuell / im „settled"-Zustand klappt `discover_slots`.

Zusätzlich: Audio war aus, weil `Station.audio_enabled` (DB-Feld, default `False`) nur
bei der qemu-Station umgelegt war.

## Locked Design

### 1. Agent-Robustheit
- `station_agent/slot_discovery.py`:
  - `module list` in `probe_slot` mit **RETRY** (2–3 Versuche + kurzer Settle
    zwischen den Versuchen). Ein durch async Log-/Status-Zeilen zerrissener erster
    Versuch darf nicht permanent fehlschlagen.
  - Robustes Framing: nur `MODULE-*`-Präfix-Zeilen als Protokoll werten, async
    Log-/Status-Zeilen ignorieren (Verhalten bereits vorhanden, wird abgesichert).
- `station_agent/control_client.py`:
  - Inventory **nicht nur einmal** beim Connect senden. **RE-DISCOVERY** bei leerem
    Inventory + periodisch (bzw. bei Hotplug); geändertes Inventory neu senden.
  - Ein einmaliges Race darf ein Modul nie dauerhaft offline lassen.

### 2. Control auto-on
`control_enabled` ist **kein** DB-Feld — nur hardgecodetes `control_enabled: true` in
`apps/provisioning/config_render.py` (+ `seed_dev_station.py`). Raus damit + das
Agent-Gate entfernen → Control-Channel läuft **immer**, wenn `server_url` gesetzt ist
(`server_url` ist bereits Pflichtfeld in `AgentConfig.validate`).

### 3. Audio auto (INTERIM, entkoppelt von der FW)
Der Agent aktiviert Audio, wenn ein **entdeckter Slot einen Audio-Pfad** hat
(udev-getaggte ALSA-Karte / PipeWire-Node vorhanden), unabhängig von jedem Flag.
Mechanismus im Repo: `PipeWireRouterBackend.list_audio_slots()` (enumeriert ALSA-Karten
mit `OE5XRX_SLOT`-udev-Tag). Ist die Liste nicht leer → Audio an.

Klarer TODO/Hook hinterlassen: später auf die `audio`-Capability aus `module describe`
umstellen (kommt aus der PARALLELEN FW-Session).

### 4. Flags weg
`Station.audio_enabled` komplett entfernen:
- DB-Feld (`apps/stations/models.py`) + Django-Admin (`apps/stations/admin.py`) + Migration.
- `render_config`-Arg (`apps/provisioning/config_render.py`) + Caller
  (`apps/provisioning/management/commands/run_background_jobs.py`).
- config.yml-Keys `audio_enabled` **und** `control_enabled`.
- Agent-Config (`station_agent/config.py`) entsprechend bereinigen (beide Felder raus).

## Validierung (Serial-Boundary-Ehrlichkeitsregel)
Auf echter CM4 (`ssh root@192.168.88.211`) muss nach einem frischen Agent-Start das
FM-Modul ONLINE gehen **und** `python -m station_agent selftest serial --slot 3` grün
sein. Sim-grün allein reicht nicht.

## Grenzen
- Nur `station-manager`.
- Kein `linux-image`-SRCREV-Bump in diesem PR.
- FW-Repo nicht anfassen.
