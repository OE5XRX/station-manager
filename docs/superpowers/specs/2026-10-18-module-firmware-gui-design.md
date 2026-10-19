# Modul-Firmware-Update — GUI fertigstellen

**Datum:** 2026-10-18 · **Status:** approved scope (Brief `feature/module-firmware-gui/brief`), Detail-Calls vom Builder (markiert als *builder call*).
**Bezug:** Overview `2026-09-12-module-firmware-update-design.md`, B `2026-09-14-…-B-release-management-design.md`, C `2026-09-25-…-C-reconciler-design.md`.

## Problem

Der Modul-FW-Workflow ist nur halb in der GUI: `ModuleType` (`firmware_repo`, `release_asset_prefix`) ist nur im Django-Admin anlegbar → das Import-Panel auf `/modules/firmware/` bleibt kommentarlos leer. Die Firmware-Seite fehlt im Menü. Soll-Versionen (`ModuleFirmwareTarget`) und Quarantäne-Freigabe haben gar keine UI. Ziel: der gesamte Workflow (Typ anlegen → Release importieren → Soll-Version setzen → Konvergenz beobachten → Quarantäne freigeben) in der normalen GUI, übers Menü findbar, ohne Django-Admin.

Scope ausschließlich `apps/module_firmware` (+ Sidebar, + zwei `StationAuditLog.EventType`-Werte). Draußen: Agent/Flash (Teil D), REST-API, linux-image, andere Apps.

## Berechtigungen

Alles hängt am bestehenden Area `modules` (unified permissions #164): Lesen = `modules.view` (applicant+), jede Mutation = `modules.manage` (staff+). Views nutzen `AreaPermissionMixin` (POST → manage). Templates gaten Buttons mit `can.modules.manage`. *Builder call:* `verb_labels["manage"]` wird erweitert auf „Module bestätigen, Firmware importieren/archivieren, Modultypen, Soll-Versionen, Quarantäne freigeben".

## 1. Menü

Neue Sidebar-Sektion **„Module Firmware"** (gated `can.modules.view`), der bisherige „Modules"-Eintrag zieht aus „Operations" dorthin:

| Eintrag | URL-Name | aktiv wenn url_name … |
|---|---|---|
| Modules | `module_list` | `module_list`, `module_detail` |
| Firmware Releases | `release_list` | `release_list` |
| Targets (Soll-Versionen) | `target_list` | beginnt mit `target_` |
| Convergence | `convergence` | `convergence` |
| Module Types | `type_list` | beginnt mit `type_` |

## 2. Modultypen-CRUD

URLs (vor dem `<str:uid>/`-Catch-all registriert): `types/`, `types/new/`, `types/<pk>/edit/`, `types/<pk>/delete/`.

- **Liste** (view): key, display_name, hw_repo, firmware_repo, prefix, Zähler Module/Releases/Targets, Status-Chip „import-ready" vs. „repo/prefix missing".
- **Form** (`ModuleTypeForm`): `key` (Slug), `display_name`, `hw_repo`, `firmware_repo`, `release_asset_prefix`.
  - `hw_repo`/`firmware_repo`: leer erlaubt, sonst `^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$` (GitHub owner/repo).
  - `release_asset_prefix`: leer erlaubt, sonst `^[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*$` — kein trailing `-` (würde `parse_variant_assets` brechen), keine Pfadzeichen. *Builder call.*
  - **`key` ist nach Anlage read-only** (*builder call*): Ingestion mappt den vom Agent gemeldeten Typ-Key auf die Zeile; ein Rename würde gemeldete Module verwaisen.
- **Delete**: nur wenn unbenutzt — keine `Module`, keine `ModuleFirmwareRelease` (inkl. archivierter, `all_objects`), keine `ModuleFirmwareTarget`. Sonst Button deaktiviert mit Begründung; der POST prüft serverseitig nochmals und fängt `ProtectedError` → Fehlermeldung statt 500. Import-Jobs hängen per CASCADE dran (reine Historie, nach Löschung bedeutungslos).
- **Audit**: `AccountAuditLog` `CONFIG_CHANGED` (Präzedenz `apps/api/audit.audit_config_write`), Message „Module type <key> created/updated/deleted", `details` = `{"object": "module_type", "action": …, "key": …, "changes": {field: {old,new}}}`.

## 3. Import-UX

Auf `/modules/firmware/`:

- **Setup-Hinweise statt still leer** (*builder call*: Panel „Setup" oberhalb des Import-Panels):
  - keine Modultypen → „No module type configured" + (manage) Link „Create module type".
  - Typen ohne `firmware_repo`/`release_asset_prefix` → Liste der unvollständigen Typen mit fehlendem Feld + (manage) „Edit"-Link.
- **GitHub-Release-Vorschau** (`_github_releases.html`): pro Tag die per `parse_variant_assets` erkannten Varianten als Chips. Neuer Zustand **`no_assets`** wenn kein Asset `"{prefix}[-variant].signed.bin"` (+ `.bundle`) passt → kein Import-Button, Hinweis mit erwartetem Muster. **Mismatch-Hinweis**: Varianten, die Module dieses Typs melden (`Module.variant`, nicht leer), aber der Tag nicht liefert → Warnung „missing variant(s): …" (Import bleibt erlaubt).
- **Import-Jobs**: Panel wird HTMX-Partial (`firmware/jobs/`, view-Recht), zeigt die letzten 20 Jobs mit Status-Pill, Requester, Zeit, Fehlermeldung, bei `ready` die importierten Versionen. Solange ein Job `pending`/`running` ist, pollt das Partial alle 5 s (`hx-trigger="every 5s"` nur dann gerendert).
- **Feedback**: `FirmwareImportView`, Archive, Restore setzen `messages` (queued / already in flight / type not import-ready / archived / restored).
- **Audit**: Import-Queue, Archive, Restore → `AccountAuditLog` `CONFIG_CHANGED` mit `details.object = "module_firmware_release"` bzw. `"module_firmware_import"`.

## 4. Soll-Versionen (`ModuleFirmwareTarget`)

URLs: `targets/`, `targets/new/`, `targets/<pk>/edit/`, `targets/<pk>/delete/`.

- **Liste** gruppiert nach Modultyp: Fleet-Default (mit Canary-Tag-Badge), Tag-Overrides, Station-Overrides; Version, Varianten-Abdeckung (welche Varianten haben ein nicht-archiviertes Release dieser Version), wer/wann. Erklärzeile zur Präzedenz station > tag > fleet und zum Canary-Gate.
- **Form** (`ModuleFirmwareTargetForm`, ModelForm): Felder `release` (Pseudo-Feld), `scope`, `tag`, `station`, `canary_tag`.
  - **`release`** = Auswahl eines Paares *(module_type, version)* aus nicht-archivierten Releases, `<optgroup>` pro Modultyp, Label „1.2.0 — vhf, uhf". *Builder call:* ein kombiniertes Feld statt Typ + Version getrennt — so ist ein Typ/Version-Mismatch strukturell unmöglich und es braucht kein JS. Kein Freitext (Spec C). Beim Edit eines Targets, dessen Version inzwischen komplett archiviert ist, wird die aktuelle Kombination trotzdem angeboten (mit „(archived)"), damit das Speichern anderer Felder nicht scheitert.
  - Scope-Kohärenz + Canary-nur-Fleet + Unique-Constraints → `Model.clean()` / `validate_constraints` via ModelForm → feldbezogene 400-Form-Fehler statt IntegrityError.
  - Progressive Enhancement: kleines Script in `static/js/app.js` blendet `tag`/`station`/`canary_tag` je nach `scope` ein/aus (`data-scope-field="tag"` …); ohne JS sind alle Felder sichtbar und der Server validiert.
- **Delete**: POST mit `data-confirm`.
- **Nach jeder Mutation**: `services.reconcile_module_type(module_type)` — best-effort `reconcile_module` für alle Module dieses Typs mit offener Assignment, damit die Konvergenz-Übersicht sofort stimmt (sonst erst beim nächsten Heartbeat). Fehler pro Modul werden geloggt, nicht propagiert.
- **Audit**: jede Mutation → `AccountAuditLog` `CONFIG_CHANGED` (`details.object = "module_firmware_target"`, action, module_type, scope, version, tag/station/canary_tag, changes). Station-Scope zusätzlich `StationAuditLog` mit der Station: `FIRMWARE_TARGET_SET` (create/update) bzw. neu **`FIRMWARE_TARGET_REMOVED`** (delete), jeweils mit `user`.
- **Effektive Soll-Version** (`reconciler.effective_target`) sichtbar in der Konvergenz-Übersicht (pro Modul, mit Scope-Badge) und auf der Modul-Detailseite.

## 5. Konvergenz-Überblick + Quarantäne-Freigabe

URL `convergence/` (view):

- KPI-Zeile: Anzahl ok / updating / quarantined / unknown (`Module.firmware_convergence`-Rollup).
- Tabelle (paginiert 50): UID (Link Detail), Typ/Variante, Station/Slot (offene Assignment), Ist (`last_reported_version`), Soll (effektive Target-Version + Scope-Badge; „— no target" wenn keine; Hinweis „no release for variant" wenn Target existiert, aber keine passende Variante importiert ist), Zustand-Pill. Bei `quarantined`: Attempts + letzter Fehlermodus/-text aus der quarantänierten `ModuleFirmwareConvergenceState`.
- Filter: type, state, station, q (UID).
- Aktion **„Release quarantine"** (manage, POST `<uid>/release-quarantine/`, `data-confirm`), auch auf der Modul-Detailseite.
- `services.release_quarantine(module, *, user)`: in einer Transaktion, Lock-Reihenfolge module → convergence (wie Reconciler R2-1): alle `QUARANTINED`-ConvergenceStates des Moduls → `state=updating`, `attempts=0`, `last_error_mode=""`, `last_error_message=""`; danach `reconcile_module(module)` (setzt Rollup korrekt ok/updating). Keine quarantänierte Zeile → No-op, Rückgabe 0. Audit: `StationAuditLog` neu **`MODULE_QUARANTINE_RELEASED`**, Subjekte module + aktuelle Station (falls offen), `user`, `changes` mit freigegebenen Target-Versionen.

## Modul-Detailseite

Neues Panel „Firmware": Ist-Version, Variante, effektives Target (Version + Scope + Link auf Target-Liste), Rollup-Pill, ggf. Quarantäne-Info + Freigabe-Button.

## Tests (TDD, `tests/module_firmware/`)

Pro Feature: Permission-Matrix (anon → Login-Redirect, applicant/member → 403 bei Writes und 200 bei Reads, staff → OK), Validierung (Repo-/Prefix-Format, Scope-Kohärenz, Unique-Konflikte, Delete-referenziert-Block), Audit-Zeilen pro Write, Reconcile nach Target-Mutation, Quarantäne-Freigabe (State-Reset, Rollup, Audit, No-op), GH-Vorschau (Varianten-Chips, `no_assets`, Mismatch), Sidebar-Gating, Template-Guard bleibt grün.
