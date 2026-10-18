# Automation-API: module_firmware-Erweiterung — Design

**Status:** approved by user brief (2026-10-18), stacked on PR #166 (Modul-FW-GUI).
**Parent spec:** `2026-10-01-user-automation-api-design.md` (deferred module_firmware
as an own allowlist extension) + `2026-10-14-unified-permissions-design.md`.

## Ziel
Alles, was die Modul-FW-GUI (#166) kann, ist auch per Personal-Access-Token unter
`/api/v1/` erreichbar — gleiche Rechte, gleiche Validierung, gleiche Audit-Spur
(plus Suffix „via API token <prefix>").

## Rechte
Ein Bereich: `modules` aus dem unified permission registry (#164) —
`modules.view` (Applicant+) für alle Reads, `modules.manage` (Staff+) für jede
Mutation/Aktion inkl. `GET …/available/` (GitHub-Proxy, wie GUI-Partial und
`images/available/`). Kein Topology-Scoping: seit #164 gilt „API = UI", die GUI
filtert Module nicht nach Region, also auch die API nicht (`apps/api/scoping.py`
existiert nicht mehr; der Brief-Hinweis darauf ist durch #164 überholt).
Fehlende Objekte → 404; Rechte-Fehler → 403 mit `required: "modules.manage"`.

## Ressourcen (`apps/api/router.py`)
| Route | Ops |
|---|---|
| `module-types` | list/retrieve; create/update(PUT/PATCH); delete nur unreferenziert (sonst 409) |
| `modules` | list/retrieve (inkl. Ist-Version, Variante, Zustand, offene Station/Slot, effektives Ziel, live Quarantäne); PATCH `notes`/`lifecycle_status`; POST `{id}/confirm-registration/`, `{id}/release-quarantine/`. Kein create/delete (Ingest-owned) |
| `module-firmware-releases` | list/retrieve (`?include_archived=true`); POST `{id}/archive/`, `{id}/restore/` (idempotent, reconcile); GET `available/?module_type=<id>`; POST `import/` → 202 ImportJob. Kein create/update/delete (405) |
| `module-firmware-import-jobs` | list/retrieve |
| `module-firmware-targets` | full CRUD; Release per ID (`release` = ID einer beliebigen `ModuleFirmwareRelease`-Zeile des gewünschten (Modultyp, Version)-Paars; das Paar muss vom Formular angeboten werden, d.h. eine nicht-archivierte Variante haben oder das aktuelle Paar des Targets sein), kein Freitext |

## Wiederverwendung statt Duplikat
- ModuleType-Validierung = `ModuleTypeForm` (Repo-Form, Trusted Owner, `key` nach Anlage unveränderlich).
- Target-Validierung = `ModuleFirmwareTargetForm` (Scope/Ref-Kohärenz, Unique-Constraints, Release-Paar).
- Import-Queueing, GitHub-Release-Zeilen (State `ready/imported/queued/invalid_tag/no_assets`,
  Varianten, fehlende Flotten-Varianten), Archive/Restore und Target-Seiteneffekte
  (Audit + Reconcile) werden aus den GUI-Views in `apps/module_firmware/ops.py`
  extrahiert; GUI und API rufen dieselben Funktionen.
- Services `confirm_registration`, `set_lifecycle`, `release_quarantine` bekommen
  einen `audit_suffix`-kwarg (Default `""`, GUI unverändert).

## Import-Semantik (API)
`POST import/ {module_type, tag}`: Typ muss import-ready sein (`import_blockers` leer, sonst 400
auf `module_type`); Tag muss in den GitHub-Releases des Typs liegen (400 auf `tag`), und deren
State: `ready` → Job (202); `queued` / `imported` → 409; `invalid_tag` / `no_assets` → 400.
GitHub-Fehler → 502.

## Audit
Jede Mutation schreibt dieselben Einträge wie die GUI, Message mit Suffix aus
`apps.api.audit._token_suffix(request)`. Neu (GUI hat es nicht): Notes-Änderung
eines Moduls wird auditiert —
`StationAuditLog` mit Event `UPDATED`, `module=…`, `changes={"notes": …}`.

## OpenAPI
Alle Routen/Actions erscheinen sauber in `/api/v1/schema/` (drf-spectacular, `extend_schema`
für Actions; Schema-Generierung ohne Warnings für die neuen Views).
