# Teilbereich A — Modul-Inventar & Identität — Design

**Programm:** Modul-Firmware-Update über den station-manager
**Overview:** `docs/superpowers/specs/2026-09-12-module-firmware-update-design.md`
**Teilbereich:** A (Fundament) — station-manager
**Datum:** 2026-09-13
**Status:** Design in Review (awaiting approval → implementation plan)

## Ziel

Module einer Remote-Station werden zu **getrackten physischen Objekten** — geführt
über eine eingebrannte, self-reported **UID**. Der komplette Lebenszyklus (wo steckt
das Teil, seit wann, in welchem Zustand) und später die Firmware-Historie folgen dem
**physischen Modul**, unabhängig davon, in welcher Station / welchem Slot es gerade
steckt. Teilbereich A legt dafür das **Identitäts- und Inventar-Fundament**, auf dem
B (Release-Management), C (Desired-State/Reconciler) und D (Agent-Flash) aufbauen.

A liefert: Modul-Entität (UID-geführt) + Typ-Registry + Zuordnungs-Historie, die
Verdrahtung der Modul-Inventory aus dem Heartbeat (bislang Platzhalter), Auto-Swap-
Erkennung, Audit-Integration (Modul als Subjekt) und die dazugehörigen Sichten.

## Scope

**Im Scope (alles im `station-manager`-Repo — Server, `station_agent/` und
`tests/fake_fw.py` liegen alle hier):**
- Neue Django-App `apps/module_firmware` mit der Identitäts-Ebene: `ModuleType`,
  `Module`, `ModuleAssignmentHistory`.
- `StationModule` (`apps/control`) um nullable FK → `Module` erweitern.
- `StationAuditLog` (`apps/stations`) so erweitern, dass ein Modul auditierbares
  Subjekt sein kann (nullable `module`-FK; `station` wird nullable).
- Heartbeat-Inventory-Ingestion: pro Slot `uid` + `uid_source` parsen, `Module`
  anlegen/aktualisieren, Slot verlinken, Swaps erkennen, Assignment-History + Audit
  schreiben.
- `station_agent` Slot-Discovery/Heartbeat: `uid` + `uid_source` aus dem Identity-
  Descriptor durchreichen.
- Sim-Testbarkeit: `tests/fake_fw.py` (und der native_sim-Modul-Kontrakt) melden eine
  **synthetische, persistierte UID** mit `uid_source=synthetic`, sodass A end-to-end
  ohne Hardware getestet wird.
- UI + Admin: fleet-weite Modul-Inventarliste, Modul-Detail („Lebensgeschichte"),
  Stations-Detail-Integration, Onboarding-Inbox, Dashboard-Card, Django-Admin.

**Bewusst NICHT in A:**
- `ModuleFirmwareRelease` (→ B), `ModuleFirmwareTarget` /
  `ModuleFirmwareConvergenceState` / Reconciler (→ C), jegliches DFU/Flashen (→ D).
- Firmware-Konvergenz-Feld (`ok`/`updating`/`quarantined`) am `Module` — gehört zum
  Reconciler in C, wird dort ergänzt.
- `hw_version` / PCB-Revisions-Gate (programmweit zurückgestellt).
- provision.json-Import (Bench-Pre-Registrierung) — bewusst zurückgestellt; A nutzt
  ausschließlich Heartbeat-Auto-Discovery (siehe Entscheidung 2).

## Getroffene Entscheidungen (Brainstorm 2026-09-13)

1. **Onboarding bei unbekannter UID = auto-anlegen + flaggen (ii).** Der Server legt
   ein neu gemeldetes Modul automatisch an, Inventar/Assignment-History/Audit laufen
   sofort, aber `registration_status=unregistered` bis ein Admin bestätigt. Das Flag
   gatet später (C) Firmware-Updates; in A ist es Zustand + UI-Aktion.
2. **provision.json-Import zurückgestellt.** A verlässt sich allein auf Heartbeat-
   Auto-Discovery. Der E→A-Input (`provision-<uid>.json`) wird später als Pre-
   Registrierungs-Pfad nachgerüstet, ohne Umbau.
3. **Audit: `StationAuditLog` erweitern statt neues System.** Nullable `module`-FK
   hinzu, `station`-FK wird nullable — so existieren stationslose Modul-Events (Bench,
   Lager, `in_lab`) und beide Sichten (station- und modul-zentrisch) kommen aus einer
   Tabelle. Ehrt „kein neues Audit-System" aus dem Overview.
4. **Neue App `apps/module_firmware`** (kein bestehendes `apps/firmware` vorhanden —
   der Overview-Hinweis war veraltet). Hält jetzt A's Identitäts-Modelle und später
   B/C-Modelle.
5. **Graceful degradation ohne UID.** Heartbeat ohne `uid` für einen Slot ⇒ dieser
   bleibt Display-only `StationModule` wie heute, **kein** `Module` angelegt. Deckt
   Legacy-Firmware vor dem FW-UID-Descriptor ab.
6. **Lifecycle-Auto-Ableitung.** Aktive Assignment ⇒ `deployed`; endet die Assignment
   ⇒ zurück auf `ready`. Die sticky/manuellen Zustände `defect` / `in_lab` /
   `retired` sind operator-gesetzt und werden **nicht** auto-überschrieben.
7. **Sichtbarkeit: Lesen = jeder eingeloggte User (wie überall), Editieren =
   admin/staff-only.** Inventarliste + Detail read-only für alle Authentifizierten;
   Mutationen (Registrierung bestätigen, Lifecycle setzen, Notiz, Typ-Registry) nur
   für admin/staff.
8. **Voller UI-Umfang** für A (Liste + Detail + Stations-Integration + Onboarding-
   Inbox + Dashboard-Card + Admin).

## Datenmodell

### `ModuleType` — Registry flashbarer Modultypen
Kleine, admin-gepflegte Registry. Nur firmware-tragende Typen (`fm`, `power`,
`device-tester`, …) — keine bare PCBs (BusBoard, CM4-Carrier).

| Feld | Typ | Notiz |
|---|---|---|
| `key` | `SlugField`, unique | z. B. `fm`; Ingestion-Match gegen self-reported Typ |
| `display_name` | `CharField` | Anzeigename |
| `hw_repo` | `CharField`, blank | optionaler Anker aufs HW-Repo (spätere Metadaten) |
| `created_at` / `updated_at` | auto | |

`Module` (und später `ModuleFirmwareRelease`) referenzieren per FK. **Ein Typ muss
registriert sein, bevor ein Modul dieses Typs akzeptiert wird** (siehe Ingestion
Schritt 2). Referenzielle Integrität fürs Typ-Kompatibilitäts-Gate (relevant ab C).

### `Module` — das physische, UID-getrackte Objekt

| Feld | Typ | Notiz |
|---|---|---|
| `uid` | `CharField`, unique, indexed | opaker String (96-bit STM32-UID oder synthetisch). **Alleinige Identität**, self-reported. |
| `uid_source` | `CharField` choices `stm32_uid`/`synthetic` | unterscheidet echte HW von Sim fürs Audit/UI |
| `module_type` | `FK → ModuleType` (`PROTECT`) | self-reported, gegen Registry aufgelöst |
| `lifecycle_status` | `CharField` choices | `ready` · `deployed` · `defect` · `in_lab` · `retired` |
| `registration_status` | `CharField` choices | `unregistered` · `registered` (Onboarding ii) |
| `last_reported_version` | `CharField`, blank | denormalisierte aktuelle FW-Version fürs Modul-Detail |
| `first_seen` | `DateTimeField` | erster Heartbeat mit dieser UID |
| `last_seen` | `DateTimeField`, null | letzter Heartbeat mit dieser UID |
| `notes` | `TextField`, blank | operator-Notiz |
| `created_at` / `updated_at` | auto | |

- `lifecycle_status` und `registration_status` sind **orthogonal** (ein Modul kann
  `deployed` **und** `unregistered` sein).
- **Kein** Konvergenz-Feld in A (kommt in C).
- Index auf `uid` (Lookup pro Heartbeat), `registration_status` (Onboarding-Inbox),
  `lifecycle_status`.

### `ModuleAssignmentHistory` — temporaler Modul↔(Station, Slot)-Log

| Feld | Typ | Notiz |
|---|---|---|
| `module` | `FK → Module` (`CASCADE`) | |
| `station` | `FK → stations.Station` (`SET_NULL`, null) | Station kann später gelöscht werden, Historie bleibt |
| `slot` | `CharField` | Slot-Identifier |
| `from_ts` | `DateTimeField` | Beginn der Zuordnung |
| `to_ts` | `DateTimeField`, null | `null` = aktuell/offen |
| `reason` | `CharField`, blank | z. B. `auto-swap`, `manual` |
| `created_by` | `FK → User` (`SET_NULL`, null) | `null` = agent/auto |

- Invariante: **höchstens eine offene Zeile pro `module`** und **höchstens eine offene
  Zeile pro (`station`, `slot`)**. Durchgesetzt über `UniqueConstraint` mit
  `condition=Q(to_ts__isnull=True)` (partielle Unique-Constraints, zwei Stück).
- „Wo war UID wann" = direkte Abfrage; aktuelle Zuordnung = offene Zeile.

### Erweiterung `StationModule` (`apps/control`)
- Neu: `tracked_module = FK → module_firmware.Module` (`SET_NULL`, null, blank) =
  welches physische Modul aktuell in diesem Slot steckt (denormalisierter
  Aktuell-Pointer). **Feldname `tracked_module`, nicht `module`:** `StationModule`
  hat bereits ein `module_id`-CharField (Firmware-Modul-ID); ein FK namens `module`
  würde auf derselben `module_id`-Spalte/`attname` kollidieren. `related_name` =
  `station_modules`.
- Bestehende Felder (`type`/`model`/`version`/`capability_descriptor`/…) **bleiben**
  (Backward-Compat + Legacy-No-UID-Display).

### Erweiterung `StationAuditLog` (`apps/stations`)
- Neu: `module = FK → module_firmware.Module` (`SET_NULL`, null, blank).
- `station`-FK wird **nullable** (stationslose Modul-Events).
- Neue `EventType`-Choices: `MODULE_DISCOVERED`, `MODULE_REGISTERED`,
  `MODULE_SWAPPED`, `MODULE_LIFECYCLE_CHANGED`, `MODULE_ASSIGNMENT_CHANGED`.
- `StationAuditLog.log(...)` um optionale `module`/`module_id`-Parameter erweitern
  (analog zum bestehenden `station`/`station_id`-Muster).

> **Migrations-Hinweis:** `station` nullable machen ist eine additive Migration; alle
> bestehenden Zeilen behalten ihre Station. Kein Datenverlust.

## Ingestion & Lifecycle-Flow

Der Heartbeat trägt die Modul-Inventory als Liste pro Slot. Der Server konsumiert
die **Broker-Wire-Shape** pro Slot `{slot, control, modules: [{module, identity,
capabilities, state}]}` (das `id`-Feld existiert nur in der Discovery-Zwischenform
und wird vom Broker auf `module` gemappt). Pro gemeldetem Modul-Eintrag
(`{module, identity:{type, model, version, uid?, uid_source?}, capabilities, state}`):

1. **Keine `uid`** → Legacy-Pfad: `StationModule` wie heute anlegen/aktualisieren,
   **kein** `Module`, kein History-/Audit-Eintrag für die Identität. Fertig.
2. **`uid` vorhanden, aber `module_type` unbekannt in der Registry** → Eintrag
   **verwerfen** (kein `Module`), Warnung auditieren (`event_type` generisch, message
   „unbekannter Modultyp <x>, Registry-Eintrag fehlt"). Verhindert Wildwuchs.
3. **`uid` neu** → `Module` anlegen (`registration_status=unregistered`,
   `uid_source` aus dem Report, `first_seen`/`last_seen` = jetzt), Lifecycle über
   Assignment-Ableitung (Schritt 5/6). Audit `MODULE_DISCOVERED` (modul-zentrisch,
   station-verknüpft).
4. **`uid` bekannt** → `last_seen`, `last_reported_version` aktualisieren.
5. **Slot-Inhalt-Änderung** — d. h. der Slot (`station`, `slot`) trug zuvor eine
   andere offene UID, **oder** die gemeldete UID hat eine offene Assignment in einem
   anderen (`station`, `slot`):
   - alte offene Assignment(s) schließen (`to_ts = jetzt`),
   - neue offene Assignment öffnen (`from_ts = jetzt`, `reason=auto-swap`,
     `created_by=null`),
   - `StationModule.tracked_module` auf das neue `Module` umbiegen,
   - Audit `MODULE_SWAPPED` + `MODULE_ASSIGNMENT_CHANGED`.
6. **Lifecycle-Auto-Ableitung:** nach Assignment-Update — Modul mit aktiver
   (offener) Assignment ⇒ `deployed`; Modul ohne offene Assignment ⇒ `ready`.
   Ausnahme: `defect` / `in_lab` / `retired` sind sticky (operator-gesetzt) und
   werden **nicht** überschrieben. Statusänderung ⇒ Audit `MODULE_LIFECYCLE_CHANGED`.

**Idempotenz:** wiederholte Heartbeats mit unverändertem Slot-Inhalt erzeugen **keine**
neuen History-/Audit-Zeilen — nur `last_seen`/`last_reported_version` wandern. Swap-
Erkennung vergleicht gegen den aktuellen offenen Zustand, nicht gegen jeden Heartbeat.

**Verortung:** Ingestion-Logik als eigenständige Funktion/Service in
`apps/module_firmware` (z. B. `ingest.py::reconcile_module_inventory(station, modules,
*, now)`), aufgerufen aus dem bestehenden `HeartbeatView` nach dem `StationInventory`-
Update. Klar testbar, ohne den View-Code aufzublähen.

## Heartbeat / Serializer / Agent

- **Serializer** (`apps/api/serializers.py`): die Modul-Inventory-Shape im
  `inventory`-Blob um `uid` (optional) + `uid_source` (optional, default `stm32_uid`)
  im `identity`-Dict erweitern. `module_versions` (alter Platzhalter) bleibt
  unangetastet/deprecated — die Wahrheit kommt aus `inventory.modules`.
- **Agent** (`station_agent/slot_discovery.py` + `inventory.py`): `uid` + `uid_source`
  aus dem Identity-Descriptor der Firmware durchreichen (Pass-through; kein Parsing-
  Neubau). Fehlt `uid` im Descriptor (Legacy-FW) → Feld weglassen (Server-Schritt 1).
- **Kein** Bruch bestehender Heartbeats: alle neuen Felder optional.

## Sim-Testbarkeit

- `tests/fake_fw.py` (und der native_sim-Modul-Kontrakt hinter
  `/dev/oe5xrx/slotN/control`) melden im `MODULE-DESCRIBE` eine **synthetische,
  persistierte UID** + `uid_source=synthetic`. Persistenz = stabil über Neustarts im
  Test (fixe/aus Seed abgeleitete UID pro Sim-Modul).
- Damit sind A (Inventar/Identität), später B (Release) und C (Reconciler) am Sim voll
  testbar. D (physisches Flashen) bleibt real-HW; am Sim via Flash-Executor-Backend
  `sim` (D-Scope).
- **Trust-Ehrlichkeit:** Sim hat keine Hardware-Trust-Wurzel; `uid_source=synthetic`
  macht das im Audit/UI explizit sichtbar.

## UI / Admin (voller Umfang)

**Zugriffsregel durchgängig:** Lesen = jeder eingeloggte User; Mutationen =
admin/staff-only.

### Modul-Inventarliste (`/modules/`)
Fleet-weite Tabelle. Spalten: UID (mono, gekürzt), Typ, Lifecycle-Badge,
Registration-Badge, aktueller Standort (Station/Slot oder „— / Lager"), `last_seen`,
`last_reported_version`, `uid_source`-Badge. Filter: Typ, Lifecycle,
Registration-Status, Region/Station, `uid_source`. Suche nach UID. Sortierung.
Sim-Module (`uid_source=synthetic`) tragen ein deutliches **„SIM"-Badge**, per Filter
ausblendbar. Live-Status (`online`/`last_seen`) aus bestehenden Mustern (HTMX/
Bootstrap wie Stations-Liste).

### Modul-Detail (`/modules/<uid>/`) — die „Lebensgeschichte"
- **Header:** volle UID (kopierbar), Typ, `uid_source`, Lifecycle-Badge,
  Registration-Badge, aktueller Standort.
- **Aktionen (admin/staff):** Registrierung bestätigen (wenn `unregistered`);
  Lifecycle setzen (`defect`/`in_lab`/`retired`/`ready`); Notiz bearbeiten.
- **Assignment-History-Timeline:** Station/Slot, von–bis, aktuelle Zuordnung
  hervorgehoben.
- **Firmware-Sektion (in A bewusst dünn & ehrlich):** aktuelle
  `last_reported_version` + Versions-Änderungen aus dem Audit-Trail. Reiche Firmware-
  Historie (Releases/Konvergenz/Rollbacks) kommt mit B/C — hier nur der Platz dafür.
- **Modul-zentrischer Audit-Trail:** `StationAuditLog`-Zeilen mit `module=this`.

### Stations-Detail-Integration
Das bestehende `StationModule`-Panel bekommt pro belegtem Slot einen **Link aufs
Modul-Detail** + inline UID und Registration/Lifecycle-Badge. Ein erkannter Swap wird
hier sofort sichtbar. Stations-zentrische Sicht auf dieselbe Wahrheit.

### Onboarding-Inbox (unregistered)
**„Unregistered"-Tab/Filter mit Count-Badge** auf der Inventarliste + Bestätigen-
Aktion (einzeln im Detail, bulk im Admin). Kein separater Approval-Workflow-Motor.

### Dashboard-Card (`apps/dashboard`)
Karte „Module: N gesamt · X unregistered · Y defect/in_lab", verlinkt in die
gefilterte Inventarliste.

### Django-Admin
- **`ModuleType`:** hier wird die Registry gepflegt (admin-only).
- **`Module`:** `uid`/`module_type`/`uid_source` readonly (self-reported); editierbar
  Lifecycle/Registration/Notes. Bulk-Aktion „Registrierung bestätigen".
- **`ModuleAssignmentHistory`:** readonly, reine Inspektion.

**Bau-Hinweis:** Der Pixel-Agent **muss** `Skill("frontend-design")` für alle diese
Ansichten invoken (CLAUDE.md-Regel).

## Audit-Integration

Alle Modul-Ereignisse über das erweiterte `StationAuditLog`:
- `MODULE_DISCOVERED` (neue UID im Feld), `MODULE_REGISTERED` (Admin bestätigt),
  `MODULE_SWAPPED` (Slot-Tausch), `MODULE_LIFECYCLE_CHANGED`,
  `MODULE_ASSIGNMENT_CHANGED`.
- Jedes Event trägt `module` (immer) und `station` (wenn zutreffend), sodass sowohl
  die station-zentrische als auch die modul-zentrische Sicht dasselbe Ereignis zeigen.
- Emission konsistent mit dem bestehenden Signal-/`log()`-Muster (kein neuer
  Mechanismus). Wo sinnvoll (Assignment-/Lifecycle-Wechsel) via `post_save`-Signale,
  Ingestion-Events direkt aus der Ingestion-Funktion.

## Testing

pytest / pytest-django (Repo `tests/`), Fixtures für `ModuleType` / `Module`.
`tests/fake_fw.py` um synthetische persistierte UID + `uid_source=synthetic`
erweitert. Abdeckung:
- Ingestion: neue UID (create + `MODULE_DISCOVERED`), bekannte UID (nur
  `last_seen`/`version`), unbekannter Typ (verworfen + Warn-Audit), kein-uid
  (Legacy-Pfad, kein `Module`).
- Swap-Erkennung: Slot bekommt neue UID; bekannte UID wandert in anderen Slot;
  History-Zeilen korrekt geschlossen/geöffnet; partielle Unique-Constraints greifen.
- Onboarding: neue UID landet als `unregistered`; Bestätigen → `registered` +
  `MODULE_REGISTERED`.
- Lifecycle-Auto-Ableitung: aktive Assignment ⇒ `deployed`; Ende ⇒ `ready`;
  sticky-States nicht überschrieben.
- Audit-Dual-Subjekt: ein Event erscheint in station- und modul-zentrischer Abfrage.
- Idempotenz: wiederholter identischer Heartbeat erzeugt keine History-/Audit-Duplikate.
- UI: Zugriffsregel (Lesen alle / Editieren staff), Inventar-Filter, Detail-Rendering,
  Stations-Integration-Link.

## Repo-übergreifende Abhängigkeit (zurückgestellt, wie E's HIL)

Echtes Hardware-UID-Reporting braucht `FW-RemoteStation`, das `uid` (+ Modultyp +
Version) im Identity-Descriptor exponiert (im Overview als FW-Abhängigkeit gelistet).
A wird auf **Sim** gebaut und CI-verifiziert; echte-HW-UID landet, sobald der FW-
Descriptor sie liefert. Bis dahin greift die Graceful-Degradation (Entscheidung 5).

## Offene Implementierungspunkte (kein Architektur-Risiko)

1. Finale Enum-Werte/Labels für `lifecycle_status` und `registration_status`.
2. Exakte Query-Umsetzung der partiellen Unique-Constraints (DB: PostgreSQL →
   `UniqueConstraint(condition=…)` wird unterstützt).
3. Genaue Platzierung der Ingestion-Hook im `HeartbeatView` relativ zum bestehenden
   `StationInventory`-Update (Reihenfolge/Transaktion).
4. Feinheiten der Dashboard-Card-Platzierung im bestehenden Dashboard-Layout.

## Übergabe an die Umsetzung

Nach Freigabe dieser Spec: `writing-plans` → Implementierungs-Plan auf **diesem**
Branch (`feature/module-inventory-identity`). Umsetzung dann als Kontor-Kind-Session
(Manager-Muster): Kind arbeitet in eigenem git-worktree, Tasks → PR → CI grün →
Copilot-Loop bis 0, meldet erst dann „done"; diese Eltern-Session reviewt + merged
ausschließlich über die GitHub-API. Ein PR → `main` (Spec + Plan + Code zusammen; der
Overview-Doc reist mit).
