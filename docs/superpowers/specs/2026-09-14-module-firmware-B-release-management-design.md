# Teilbereich B — Firmware-Release-Management (Import + Pin + Serve) — Design

**Programm:** Modul-Firmware-Update über den station-manager
**Overview:** `docs/superpowers/specs/2026-09-12-module-firmware-update-design.md`
**Teilbereich:** B — station-manager (baut auf A)
**Datum:** 2026-09-14
**Status:** Design in Review (awaiting approval → implementation plan)

## Ziel

Modul-Firmware-Releases werden **importiert, auf Server-Storage gepinnt und
device-key-authentifiziert an Stationen ausgeliefert** — analog zum bestehenden
Linux-Image-OTA (`apps/images`), aber für die signierten DFU-Artefakte aus
`FW-RemoteStation`. B ist die **Release-Registry + Distribution** unter dem Programm:
A liefert die Modul-Identität, B den pinnbaren Firmware-Katalog, C (später) den
Soll-Zustand/Reconciler, D den Flash.

**HAMNET-Motivation (aus dem Overview):** Feld-Stationen erreichen GitHub nicht; der
station-manager pinnt eine unveränderliche Kopie und ist der einzige Download-Endpunkt.

## Scope

**Im Scope (`station-manager`, App `apps/module_firmware`):**
- `ModuleType` um **`firmware_repo`** + **`release_asset_prefix`** erweitern (FW-Quelle
  pro Typ, deklarativ).
- **`ModuleFirmwareRelease`** — importiertes & gepinntes Artefakt (Manager/Soft-Delete
  gespiegelt von `ImageRelease`).
- **`ModuleFirmwareImportJob`** — async Import, verarbeitet im bestehenden
  `run_background_jobs`-Command.
- **GitHub-Release-Browser** (reuse `apps/images/github_releases`) + Import-Flow inkl.
  SHA-256-Check und **cosign-Verify** (identisch zum Image-Muster, andere Identity).
- **Serve:** device-key-authentifizierter Download-Endpoint (gespiegelt von
  `DeploymentDownloadView`), autorisiert über A's Modul-Assignment.
- UI (Release-Liste + GH-Browser + Archive/Restore) + Django-Admin.

**Bewusst NICHT in B:**
- `is_latest`/Desired-State/Target, Reconciler, Quarantäne, Canary (→ C).
- DFU/Flash-Ausführung (→ D).
- **Keine Rootfs-Extraktion** — das signierte App-Binary wird **roh** gepinnt (im
  Gegensatz zu `apps/images`, das root_a entpackt).
- Kein `mcuboot_hex`-Handling (Bootloader wird physisch provisioniert, E; nicht OTA).

## Getroffene Entscheidungen (Brainstorm 2026-09-14)

1. **Serve in B, Authz per Assignment:** device-key-Download-Endpoint lebt in B; eine
   Station darf ein Release laden, wenn sie eine **offene Assignment** (A's
   `ModuleAssignmentHistory`) eines `Module` mit passendem `module_type` hat.
   Varianten-präzises Matching = C's Kompatibilitäts-Gate (A's `Module` erfasst noch
   kein Band).
2. **`variant`-Feld an `ModuleFirmwareRelease`:** ein Modultyp (`fm`) liefert pro Version
   zwei Binaries (`vhf`/`uhf`). Key = `(module_type, variant, version)`.
3. **Typ-Mapping via ModuleType:** `firmware_repo` + `release_asset_prefix` an
   `ModuleType`. Import parst `{prefix}[-{variant}].signed.bin` → `(module_type,
   variant)`.
4. **Import pro Tag:** ein Job pro Release-Tag zieht **alle** Varianten-Assets und legt
   je eine `ModuleFirmwareRelease` an.

## Datenmodell

### `ModuleType`-Erweiterung
| Feld | Typ | Notiz |
|---|---|---|
| `firmware_repo` | `CharField(200)`, blank | z.B. `OE5XRX/FW-RemoteStation`; leer = keine OTA-FW-Quelle |
| `release_asset_prefix` | `CharField(64)`, blank | z.B. `fm-sa818`; Basis fürs Asset-Parsing |

Bestehende Felder (`key`, `display_name`, `hw_repo`) unverändert.

### `ModuleFirmwareRelease`
| Feld | Typ | Notiz |
|---|---|---|
| `module_type` | `FK → ModuleType` (`PROTECT`) | |
| `variant` | `CharField(32)`, blank | `vhf`/`uhf`; leer für band-lose Typen |
| `version` | `CharField(64)` | Git-Tag `YY.MM.DD-NN` |
| `storage_key` | `CharField(512)` | gepinntes `.signed.bin` (roh) |
| `sha256` | `CharField(64)` | aus `SHA256SUMS` des Releases |
| `size_bytes` | `BigIntegerField` | |
| `cosign_bundle_key` | `CharField(512)`, blank | gepinntes `.bundle` (Re-Verify) |
| `source_repo` | `CharField(200)` | z.B. `OE5XRX/FW-RemoteStation` |
| `source_tag` | `CharField(64)` | Release-Tag |
| `source_github_url` | `CharField(512)`, blank | Asset-URL (Provenance) |
| `imported_at` | `auto_now_add` | |
| `imported_by` | `FK User` (`SET_NULL`, null) | |
| `archived_at` | `DateTimeField`, null | Soft-Delete |

- Manager: `objects` (versteckt `archived_at`-Zeilen) + `all_objects`;
  `base_manager_name = "all_objects"` (FKs sehen archivierte Zeilen — wichtig sobald C
  darauf FK'd). `archive()`/`restore()` idempotent, konkurrenzsicher (gespiegelt von
  `ImageRelease`).
- Unique `(module_type, variant, version)`. **Kein `is_latest`.**
- Index auf `(module_type, variant)`.
- `__str__`: `f"{module_type.key}/{variant or '-'} {version}"`.

### `ModuleFirmwareImportJob`
| Feld | Typ | Notiz |
|---|---|---|
| `module_type` | `FK → ModuleType` | |
| `source_repo` | `CharField(200)` | aus ModuleType.firmware_repo (Snapshot) |
| `tag` | `CharField(64)` | zu importierender Release-Tag |
| `status` | `CharField` choices `pending/running/ready/failed` | |
| `error` | `TextField`, blank | Fehlermeldung bei `failed` |
| `requested_by` | `FK User` (`SET_NULL`, null) | |
| `created_at`/`updated_at` | auto | |

## Import-Flow

Neuer Tick **`process_pending_module_firmware_imports()`** in
`apps/provisioning/management/commands/run_background_jobs.py`, mit demselben
atomaren `_claim_one_pending`-Muster wie der Image-Import. Pro geclaimtem Job:

1. Release-Metadaten vom `source_repo` holen (reuse `apps/images/github_releases`).
2. `SHA256SUMS` des Releases laden; alle Assets `{prefix}[-{variant}].signed.bin`
   ermitteln (+ zugehörige `.bundle`). Für jede Variante:
3. `.signed.bin` + `.bundle` herunterladen; **SHA-256 gegen `SHA256SUMS` prüfen**
   (Mismatch → Job `failed`, nichts pinnen).
4. **cosign `verify_blob`** (reuse/parametrisiere `apps/images/cosign`): Identity-Regexp
   `^https://github\.com/OE5XRX/FW-RemoteStation/\.github/workflows/release\.yml@refs/heads/main$`
   (FW-Release ist `workflow_dispatch` **nur auf dem Default-Branch `main`** —
   bestätigt via `release.yml`-Guard; abweichend von `apps/images`, das
   `@refs/tags/{tag}` nutzt, weil dessen Release tag-getriggert ist). Der Repo-Teil ist
   aus `source_repo` abgeleitet, der Ref-Teil (`refs/heads/main`) aus dem Default-Branch
   des `firmware_repo`. Fehlschlag → Job `failed`.
5. Roh auf Storage pinnen: `module_firmware/{module_type.key}/{version}/{variant}.signed.bin`
   (+ `.bundle`), `ModuleFirmwareRelease` anlegen.
6. **Idempotent:** existiert (module_type, variant, version) bereits aktiv → überspringen;
   existiert archiviert → `restore()` + Storage neu pinnen. Alle Varianten des Tags
   erfolgreich → Job `ready`.

**Asset-Parse-Konvention:** Basename ohne `.signed.bin`, `release_asset_prefix`
abschneiden; verbleibendes `-{variant}` → `variant` (leer, falls kein Suffix). Beispiel:
`fm-sa818-vhf.signed.bin` mit Prefix `fm-sa818` → `variant="vhf"`.

**Verortung:** Import-Logik in `apps/module_firmware/importer.py` (ORM + I/O gekapselt),
GH-Zugriff über `apps/images/github_releases`, Storage über eine kleine
`apps/module_firmware/storage.py` (Key-Schema + `default_storage`-Wrapper, analog
`apps/images/storage.py`), cosign über `apps/images/cosign` (Identity parametrisiert).

## Serve — Device-Download

`GET /api/v1/module-firmware/<int:release_id>/download/`

- Auth: `DeviceKeyAuthentication`; Permission: `IsDevice`. Gespiegelt von
  `apps/deployments/api_views.py::DeploymentDownloadView`.
- **Authz:** Station aus `request.auth.station`. Erlaubt, wenn eine **offene**
  `ModuleAssignmentHistory` (`to_ts__isnull=True`) an dieser Station existiert, deren
  `module.module_type_id == release.module_type_id`. Sonst **403** (opak, kein Probing).
- Release archiviert/fehlt → **404**. Storage-Fehler → **502**.
- **Range/Streaming:** HTTP-Range (206 Partial Content, seekable-Check), 1 MiB-Chunks,
  `StreamingHttpResponse`, `Content-Disposition: attachment; filename="{type}-{variant}-{version}.signed.bin"`,
  `Accept-Ranges`, `Content-Length`/`Content-Range`. Identisch zum Image-Muster.
- `content_type = "application/octet-stream"` (rohes Binary).

> Der Endpoint ist bewusst „vor seinem Konsumenten": C's Reconcile-Check liefert dem
> Agent später die Release-Id/URL; die Assignment-Authz hier ist ein sinnvolles,
> eigenständiges Gate („du hast ein Modul dieses Typs → du darfst dessen FW laden").

## UI / Admin

**Zugriffsregel (A's Muster):** Lesen = jeder eingeloggte User; Mutationen
(Import/Archive/Restore, ModuleType-FW-Felder) = admin/staff.

- **Release-Liste** (`/module-firmware/` o.ä.): Tabelle (Typ, Variante, Version,
  Größe, imported_at, Storage-Backend-Label, archiviert). Filter: Typ, Variante,
  `show_archived`.
- **GitHub-Release-Browser** (HTMX, pro ModuleType mit `firmware_repo`): listet Releases
  des Repos, markiert bereits importierte/queued/ready; Import-Button (queued einen
  `ModuleFirmwareImportJob` für den Tag).
- **Archive/Restore**-Aktionen.
- **Django-Admin:** `ModuleFirmwareRelease` (Storage-/sha/cosign-Felder readonly,
  `all_objects`-Queryset), `ModuleFirmwareImportJob` (Status/Log), ModuleType-Admin um
  die zwei neuen Felder erweitert.
- **Bau-Hinweis:** Pixel-Agent invoked `Skill("frontend-design")` für die Web-Ansichten.

## Audit

Import-/Archive-Ereignisse über das bestehende Audit-System, wo eine Station-/Modul-
Zuordnung besteht; Release-Registry-Events (Import/Archive) sind primär admin-Aktionen
und werden im ImportJob-Status + optional als generisches Audit festgehalten. Kein neues
Audit-System (Overview-Regel).

## Testing

pytest/pytest-django (`tests/`), GH-API + cosign + Storage gemockt:
- **Import:** Asset-Parse pro Variante (inkl. band-loser Prefix), SHA-256-Match/Mismatch,
  cosign-OK/Fail, Pin-Keys korrekt, `ModuleFirmwareRelease`-Anlage je Variante,
  Job-`ready`/`failed`.
- **Idempotenz:** Re-Import überspringt aktive, stellt archivierte wieder her.
- **Serve:** Assignment-Authz (erlaubt/403), archiviert→404, Range/206, sha256-Konsistenz,
  Streaming-Chunking.
- **Soft-Delete:** `objects` versteckt archivierte, `all_objects` zeigt sie.
- **UI:** Zugriffsregel (Lesen alle / Mutation staff), GH-Browser-Markierung, Filter.

## Repo-übergreifende Abhängigkeit (deferred, kein Blocker)

Echter End-to-End-Import braucht einen realen `FW-RemoteStation`-Release mit den
`fm-sa818-vhf/uhf.signed.bin` + `.bundle` + `SHA256SUMS`-Assets (per `workflow_dispatch`
schneidbar; die Pipeline existiert seit PR #49/#60/#64, Assets seit PR #66 auf vhf/uhf
benannt). B wird gegen Fixtures + gemocktes GH/cosign gebaut/getestet; erste echte
Verifikation beim ersten geschnittenen Release. Default-Branch = `main` (bestätigt).

## Offene Implementierungspunkte (kein Architektur-Risiko)

1. `verify_blob` um einen Identity-Regexp-Parameter erweitern vs. eigener
   module_firmware-Wrapper (Default-Branch `main` bereits bestätigt).
2. Ob der GH-Browser mehrere `firmware_repo`s (mehrere Modultypen) parallel listet oder
   pro Typ — UI-Detail.
3. Exakte URL-Namespaces (`module_firmware:release_list`, api-URL) analog images.

## Übergabe an die Umsetzung

Nach Freigabe: `writing-plans` → Plan auf diesem Branch
(`feature/module-firmware-release-mgmt`). Umsetzung als Kontor-Kind (Manager-Muster):
eigener git-worktree, TDD, PR → CI grün → Copilot-Loop bis 0, dann „done"; diese
Eltern-Session reviewt + merged über die GitHub-API. Ein PR → `main` (Squash).
