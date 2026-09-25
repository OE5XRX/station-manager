# Teilbereich C — Desired-State & Reconciler (Modul-Firmware-Update)

**Datum:** 2026-09-25
**App:** `apps/module_firmware` (station-manager)
**Baut auf:** A (Modul-Inventar & Identität, #137) + B (Firmware-Release-Management, #138), beide auf `main`.
**Overview-Spec:** `docs/superpowers/specs/2026-09-12-module-firmware-update-design.md` (Abschnitte 2, 3, `ModuleFirmwareTarget`, `ModuleFirmwareConvergenceState`, Reconciler-Flow, Fehler-Eindämmung).

## Ziel

Das server-seitige „Gehirn" des Modul-Firmware-Updates: deklarativer Ist/Soll-Abgleich
+ Reconciler + Reconcile-API für den Agent. C endet, sobald der Agent die Anweisung
bekommt und Status meldet — **kein** DFU/Flash, **kein** idle-Gate/Drain-Enforcement,
**kein** `module_ota.py` (alles Teilbereich D).

## Kern-Entscheidungen (im Brainstorm geklärt)

1. **`variant` ist eine fixe Hardware-Eigenschaft** des physischen Moduls (Band-Einschränkung
   vhf/uhf/…, ändert sich nie). Deshalb lebt sie **auf `Module`**, wird beim Discovery aus dem
   Identity-Descriptor übernommen und ist danach immutable (spätere abweichende Reports →
   Audit-Anomalie + ignoriert, exakt wie das bestehende `module_type`-Mismatch-Handling in
   `ingest.py`).
2. **Target-Granularität = `module_type` + Version-Intent.** Der Reconciler löst die
   variant-genaue `ModuleFirmwareRelease` über `Module.variant` auf. Ein Target rollt beide
   Bänder derselben logischen Version — kein Doppelpflege-Aufwand. Die Release-Tabelle
   `(module_type, variant, version)` disambiguiert von selbst.
3. **Safety-Gate-Grenze: C liefert nur Intent/Flag.** C berechnet Drift + Quarantäne und
   exponiert `Module.firmware_convergence`. Das idle-Gate (kein Flash bei PTT/aktivem
   Control-Lock) **und** das Drain/Lock-out (keine neuen Control-Sessions) sind komplett D.
   C verdrahtet nichts in den Control-Lock-Pfad.
4. **Reconcile-API = eins nach dem anderen**, 1:1 am bestehenden Deployment-check/status/commit-
   Muster (`apps/deployments`).

## Datenmodell (`apps/module_firmware/models.py`)

### `Module` — zwei neue Felder
- `variant` — `CharField(max_length=32, blank=True)`. Fixe HW-Eigenschaft. Beim Discovery aus
  `identity.variant` gesetzt; danach immutable. Ein späterer Report mit abweichender, nicht-leerer
  Variante → Audit-Anomalie (einmalig, nicht pro Heartbeat) + ignoriert; **kein** Re-Typing.
  Analog zum bestehenden `module_type`-Mismatch-Pfad in `ingest.py`.
- `firmware_convergence` — `CharField` choices `ok | updating | quarantined | unknown`,
  default `unknown`. **Denormalisierter Rollup** aus der aktiven `ModuleFirmwareConvergenceState`.
  Das Feld, das D's Drain-Logik und das Dashboard billig lesen. Ausschließlich vom Reconciler
  gepflegt.

### `ModuleFirmwareTarget` (neu) — der Soll-Zustand
- `module_type` — FK `ModuleType` (`PROTECT`).
- `version` — `CharField`. Auswahl aus vorhandenen Release-Versionen für den `module_type`
  (kein Freitext im UI — analog B's Release-Browser-Muster).
- `scope` — `TextChoices`: `fleet | tag | station`.
- `tag` — FK (nullable), nur bei `scope=tag`. (Station-Tag-Modell; im Plan gegen den Bestand
  verifizieren — Deployment nutzt `target_tag`.)
- `station` — FK `stations.Station` (nullable), nur bei `scope=station`.
- `canary_tag` — FK (nullable). „Canary zuerst": solange gesetzt, gilt ein **Fleet**-Target nur
  für Stationen im `canary_tag`; Promotion = `canary_tag` leeren → fleet-weit.
- `created_by`, `created_at`, `updated_at`.
- **Constraints:** je genau ein Target pro `(module_type, scope, scope-ref)`:
  - unique `(module_type)` bei `scope=fleet` (partial: `scope='fleet'`),
  - unique `(module_type, tag)` bei `scope=tag`,
  - unique `(module_type, station)` bei `scope=station`.
- **Auflösungspräzedenz** pro `(Station, module_type)`: **Station-Override → Tag-Override →
  Fleet-Default**. Mehrere passende Tag-Targets → deterministisch höchstes `updated_at` gewinnt
  (dokumentiert).

### `ModuleFirmwareConvergenceState` (neu) — Reconciler-Buchhaltung
- `module` — FK `Module` (`CASCADE`).
- `target_release` — FK `ModuleFirmwareRelease` (`PROTECT`) — die **konkrete aufgelöste** Release.
- `state` — `TextChoices`: `ok | updating | quarantined`.
- `attempts` — `PositiveIntegerField`, default 0.
- `last_error_mode` — `CharField` choices `rejected | rolled_back | transient | ""` (blank).
- `last_error_message` — `TextField`, blank.
- `created_at`, `updated_at`, `last_attempt_at` (nullable).
- **Constraint:** unique `(module, target_release)`.
- **Quarantäne-Logik** (Default N=3, als Konstante/Setting):
  - `rejected` (Version ändert sich nie → fremde Signatur/Downgrade) → **sofort** `quarantined`,
    kein Retry.
  - `rolled_back` (geflasht, Health-Gate failed → revertiert) → zählt auf `attempts`;
    `attempts >= N` → `quarantined`.
  - `transient` (Download-Abbruch, offline, busy) → normaler Retry, zählt **nicht**.
  - Neue Ziel-Version = neuer `target_release` = neue Zeile → automatisch neu probiert
    (die alte quarantänierte Zeile bleibt als Historie).

## Reconciler (`apps/module_firmware/reconciler.py`, reine Funktionen)

- `effective_target(station, module_type) -> ModuleFirmwareTarget | None`
  Scope-Hierarchie + Canary-Gate. Fleet-Target mit gesetztem `canary_tag` gilt nur für Stationen
  im canary_tag; für andere Stationen kein effektives Target (→ keine Drift, `firmware_convergence`
  bleibt `ok`/`unknown`, kein Zwang auf alte Version).
- `desired_release_for_module(module) -> ModuleFirmwareRelease | None`
  Station der offenen `ModuleAssignmentHistory` + `module.module_type` + `module.variant` →
  effektive Release (nur `objects`, nicht-archiviert). **Typ- UND Varianten-Gate**: keine passende
  Release für die Variante → `None`, kein Flash, Drift bleibt sichtbar.
- `reconcile_module(module) -> ModuleFirmwareConvergenceState | None`
  Vergleicht `module.last_reported_version` gegen desired.version. Legt/aktualisiert die
  ConvergenceState an, pflegt den `Module.firmware_convergence`-Rollup. Idempotent. Aufgerufen aus:
  (a) Heartbeat-Ingestion nach Versions-Report (Hook in `ingest.py`/`registry.apply_inventory`),
  (b) Target-Änderung (Operator setzt/ändert Target → betroffene Module),
  (c) Agent-status/commit.
- **Zustands-Ableitung immer aus der real gemeldeten Version**, nie aus lokalen Flags
  (crash-sicher/idempotent, konsistent zum Overview-Agent-Prinzip):
  - gemeldet == desired → `ok`.
  - gemeldet != desired, nicht quarantiniert → `updating`.
  - Quarantäne erreicht → `quarantined`.
  - kein desired → kein Drift.

## Reconcile-API — eins nach dem anderen (1:1 Deployment-Muster)

`DeviceKeyAuthentication` + `IsDevice` (wie B). Neue Routen unter
`/api/v1/module-firmware/reconcile/` (in `apps/module_firmware/api_urls.py` ergänzen).

### `POST reconcile/check/`
- **Request** (optional, für Crash-Recovery/Ist-Abgleich): `{}` oder schlanker Ist-Report.
- **Auswahl:** genau **eine** Instruktion — höchste Priorität unter den der Station zugewiesenen,
  drift-behafteten, **nicht** quarantänierten Modulen. Ein mid-flight `updating`-Datensatz wird
  bevorzugt zurückgegeben (Resume nach Agent-Crash). Deterministische Reihenfolge (z. B. Slot).
- **Response 200:**
  ```json
  {
    "convergence_id": 12,
    "module_uid": "…",
    "slot": "slot0",
    "module_type": "fm",
    "variant": "vhf",
    "target_version": "26.09.15-01",
    "download_url": "/api/v1/module-firmware/<release_pk>/download/",
    "checksum_sha256": "…",
    "size_bytes": 123456
  }
  ```
  `download_url` zeigt auf **B's bestehenden** `ModuleFirmwareDownloadView` (Authz über offene
  Assignment ist dort bereits implementiert — nicht duplizieren).
- **Response 204:** nichts zu tun (alle `ok` oder `quarantined`), oder keine offene Assignment.

### `POST reconcile/<convergence_id>/status/`
- **Request:** `{"status": "...", "error_message": ""}`, status ∈
  `downloading | flashing | verifying | failed | rolled_back | rejected`.
- Treibt `attempts`/`last_error_mode`/`state`-Buchhaltung + Audit-Events. Fehlermodus-Mapping:
  `rejected` → sofort Quarantäne; `rolled_back` → attempts++; `failed`/`transient` → transient.
  `SELECT FOR UPDATE` gegen TOCTOU (wie Deployment-status).
- **Response 200:** `{"status": "ok"}`. 404 unbekannte convergence_id / keine Station-Bindung;
  409 bereits terminal/quarantiniert.

### `POST reconcile/commit/`
- **Request:** `{"convergence_id": 12, "version": "26.09.15-01"}`.
- Server matcht gemeldete vs. Ziel-Version → `success` (state `ok`, `Module.last_reported_version`
  wird ohnehin per Heartbeat aktualisiert) oder `rolled_back` (Version-Mismatch, deterministisch,
  kein Retry — wie Deployment-commit, 409).
- **Response 200:** `{"status": "ok"}`; 409 bei Mismatch mit `{"detail": "…"}`.

## Audit + Dashboard

- Neue `StationAuditLog.EventType` (dual-subject station+module — System steht seit A):
  `FIRMWARE_TARGET_SET`, `MODULE_FLASH_STARTED`, `MODULE_FLASH_SUCCESS`,
  `MODULE_FLASH_ROLLED_BACK`, `MODULE_FLASH_REJECTED`, `MODULE_FLASH_FAILED`, `MODULE_QUARANTINED`.
  Audit-Schreibvorgänge best-effort (nicht die primäre Zustandsänderung sprengen), Muster aus
  `ingest.py`/Deployment übernehmen.
- **Dashboard: minimal** — queryable „quarantänierte Module"-Liste + Warn-Indikator (Trigger für
  Modultausch, Betriebs-Kreis). Keine UI-Politur über das Nötige hinaus (YAGNI). Falls UI-Arbeit
  anfällt: `Skill("frontend-design")` (Projektregel).

## Nicht in C (= D)

DFU/Flash-Ausführung, idle-Gate, Drain/Lock-out-Enforcement im Control-Lock-Pfad, `module_ota.py`
im Agent, DFU-Tooling im Yocto-Image.

## Test-Scope (TDD)

- **Reconciler-Auflösung:** Scope-Präzedenz (station > tag > fleet), Canary-Gate, Typ+Varianten-Gate
  (fm-vhf-FW nie auf uhf), fehlende Release für Variante → kein Flash.
- **Konvergenz-Buchhaltung:** ok/updating/quarantined-Übergänge; Fehlermodi rejected (sofort),
  rolled_back (N=3), transient (zählt nicht); neue Ziel-Version → neuer Datensatz → Retry.
- **`Module.variant`-Immutabilität:** Discovery setzt, abweichender Report → Anomalie + ignoriert.
- **`firmware_convergence`-Rollup** korrekt gepflegt.
- **Reconcile-API:** check liefert genau eine Instruktion / 204; mid-flight-Resume; status/commit
  inkl. Version-Match/-Mismatch; Auth (DeviceKey) + Authz (offene Assignment); TOCTOU-Guard.
- **E2E (probe, Pflicht):** Agent-Heartbeat meldet Ist → Reconciler rechnet Soll → check liefert
  Instruktion → status-Verlauf → commit → Konvergenz `ok`; plus Quarantäne-Pfad bis Dashboard-Warnung.
