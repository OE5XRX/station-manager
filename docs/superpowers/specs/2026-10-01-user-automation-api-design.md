# User/Automation API — Design

**Status:** Approved design, pre-plan
**Date:** 2026-10-01
**Repo:** station-manager

## Problem & Intent

station-manager braucht eine programmatische API, über die **externe
Skripte / CLI / Automation** auf die Ressourcen des Fleet-Management-Servers
zugreifen können. Primärer Konsument sind Vereinsmitglieder und Ops, die
eigene Skripte schreiben (Stationen abfragen, Rollouts triggern, Firmware
verwalten etc.).

Gewünschter Umfang: **vollständiges CRUD** auf den fachlich relevanten
Ressourcen — aber bewusst auf einer kuratierten Allowlist, nicht blind über
jedes Django-Model. Rechte werden **1:1 aus dem bestehenden Access-Control-
Modell** abgeleitet (Membership-Level + Topology-Assignments); es entsteht
kein zweites Rechtesystem.

Erfolg =
- Ein Skript kann mit einem persönlichen Token genau die Ressourcen lesen und
  mutieren, die sein Besitzer auch in der UI dürfte — nicht mehr, nicht
  weniger.
- Die API hat einen stabilen, versionierten, maschinenlesbaren Contract
  (OpenAPI), aus dem sich Clients generieren lassen.
- Sensible Interna (Raw-Device-Keys, OIDC-Keys, Token-Hashes) sind strukturell
  nicht exponierbar.

## Kontext: Ist-Zustand im Repo

- **`apps/api` ist heute reine Device/Agent-Fläche.** Ed25519-`DeviceKey`-
  Signatur-Auth (`authentication.DeviceKeyAuthentication`,
  `permissions.IsDevice`), Endpunkte: `v1/health/`, `v1/heartbeat/`,
  `v1/stations/<id>/inventory/`, `v1/deployments/…`. Diese Fläche bleibt
  unangetastet und getrennt (Maschine-zu-Maschine, eigenes Auth-Modell mit
  A/B-Key-Rotation).
- **DRF ist konfiguriert** (`REST_FRAMEWORK` in `config/settings/base.py`):
  `DeviceKeyAuthentication` + `SessionAuthentication`, Default-Permission
  `IsAuthenticated`, `ScopedRateThrottle` (Rates: `heartbeat`, `register`).
- **`AccountToken`** (in `apps/accounts/models.py`) ist ausschließlich für
  Welcome/Reset/Verify (single-use). **Kein** API-Token — Token-Infra für
  Automation ist genuin neu.
- **Access-Control-Material ist vorhanden, aber ohne Scoping-Helper:**
  - `accounts.User.MembershipLevel` — `applicant` / `member` / `staff` /
    `admin`, plus Helfer wie `is_admin`, staff-Check.
  - `stations.RegionAssignment` (Role: `manager`) und
    `stations.StationAssignment` (Rollen in `Role`), beide mit
    `_ApplicantForbiddenMixin` (Applicants dürfen keine Topology-Rolle haben).
  - Es gibt **noch keine** zentralen „welche Objekte darf User X"-Querysets;
    die UI leitet das heute ad hoc ab. Diese Helper bauen wir als Teil dieser
    Arbeit und nutzen sie sowohl in der API als auch (perspektivisch) in der
    UI.
- **`drf-spectacular` fehlt** und wird für den OpenAPI-Contract ergänzt.

## Scope — Ressourcen-Allowlist

Gegen die tatsächlichen Models auf `main` gemappt. Drei Klassen.

### CRUD — schreibbar, scope-gefiltert

| Ressource | App | Min-Rolle schreibend | Anmerkung |
|---|---|---|---|
| Station | stations | region-manager (eigene Region) / staff | delete nur staff/admin |
| Region | stations | staff/admin | member nur read |
| StationTag | stations | staff | Taxonomie |
| StationAssignment | stations | region-manager / staff | `_ApplicantForbiddenMixin` gilt |
| RegionAssignment | stations | staff/admin | Topology-Admin |
| StationLogEntry | stations | station-assigned | create im Scope |
| StationPhoto | stations | station-assigned | Attachment |
| RolloutSequence | rollouts | region-manager / staff | |
| RolloutSequenceEntry | rollouts | region-manager / staff | nested unter Sequence |
| Deployment | deployments | region-manager / staff | create = Deploy triggern, scope=Station; kein delete |
| AlertRule | monitoring | region-manager / staff | Monitoring-Config |
| ProvisioningJob | provisioning | staff | create = Provisioning triggern |
| User / Membership | accounts | **staff/admin only** | kein Self-Service; self-read für alle |

**Sonderfall ImageRelease** (images): **kein** generisches create/delete.
Delete ist soft (Archive/Restore), Quelle ist der GitHub-Release-Browser —
nie manuelle Tag-Eingabe (Tippfehler → stille Mismatches mit dem signierten
Asset). API-Fläche:

- `GET /api/v1/images/` — importierte Releases (read, scope-gefiltert).
- `POST /api/v1/images/{id}/archive/` · `POST …/restore/` — Soft-Delete-Lifecycle.
- **`GET /api/v1/images/available/`** — Liste der **importierbaren
  GitHub-Releases** (proxyt den GitHub-Release-Browser / die GH-API, wie das
  UI-Dropdown). Read-only Discovery-Endpoint, damit Skripte wissen, was es zu
  importieren gibt.
- **`POST /api/v1/images/import/`** — Import eines gewählten GitHub-Release
  anstoßen (Input: Release-Tag/-ID aus `available/`, kein Freitext-Tag).
  Legt `ImageImportJob` an, entpackt beim Import serverseitig (nicht erst beim
  OTA-Push). Rückgabe = `ImageImportJob` zum Status-Polling via
  `GET /api/v1/image-import-jobs/{id}/`.

Min-Rolle für `import` / `archive` / `restore`: staff. Hard-create/-delete von
ImageRelease bleibt draußen.

### Read-only

| Ressource | App | Grund |
|---|---|---|
| StationAuditLog | stations | Audit append-only |
| AccountAuditLog | accounts | dito |
| StationInventory | stations | vom Agent-Heartbeat gefüllt |
| DeploymentResult | deployments | Ergebnis, nicht editierbar |
| Alert | monitoring | gefeuerte Alerts (optional `acknowledge`-Action später) |
| ImageImportJob | images | Import-Status |
| StationModule | control | welche Module eine Station hat |

### Bewusst draußen — kein Endpoint

| Ressource | App | Grund |
|---|---|---|
| DeviceKey | api | Device-Auth-Geheimnis (Ed25519) — separate Agent-Fläche |
| AccountToken | accounts | Welcome/Reset/Verify-Hashes |
| PersonalAccessToken | api | nur self-scoped `/tokens/`-Mgmt, kein Fremd-CRUD |
| **gesamte `sso`-Domäne** | sso | AppGrant, ApplicationPolicy, SsoAuditLog, TokenSession, OIDC-Keys — eigene Identity-Domäne, nicht über die Automation-API |
| ControlLock | control | Live-Control-Idle-Lock — Echtzeit-Control-Pfad |
| AudioGate, AudioSubscription | audio | Echtzeit-Audio-Plane |
| TerminalSession | tunnel | WebSocket-Terminal, nicht REST-CRUD |

**Entscheidungen:**
- Die **Echtzeit-/Control-Primitives** (ControlLock, AudioGate,
  AudioSubscription, TerminalSession) bleiben **komplett draußen** — der
  Live-Control-Pfad gehört zum Web-Frontend; die REST-API bleibt auf
  Fleet-Management fokussiert. (`StationModule` bleibt als reine
  Hardware-Inventar-Sicht read-only.)
- Die **gesamte `sso`-Domäne** bleibt draußen — OIDC/Identity ist ein eigenes
  Subsystem mit eigenem Zugriffsmodell.
- **User/Membership-Mutation** ist **staff/admin only, kein Self-Service**.

Das ist die Leak-Schutz-Grenze, die „full CRUD" verantwortbar macht: ein
kompromittierter Token kann die fachlichen Daten im Scope seines Besitzers
mutieren, aber niemals Auth-Geheimnisse exfiltrieren oder fremde
Device-Identitäten übernehmen.

> Hinweis: Die in CLAUDE.md gelistete App `firmware` hat auf `main` aktuell
> kein Model (WIP auf anderem Branch) — daher nicht in der Allowlist. Beim
> Landen der Firmware-Modelle als eigene Allowlist-Erweiterung nachziehen.

### Explizit kein Ziel (YAGNI)
- Kein generisches Auto-Model-Exposing (Ansatz A verworfen).
- Kein GraphQL (Ansatz C verworfen — zweiter Stack, Object-Level-Authz
  fummelig, schlechter für curl/CLI).
- Keine Radio-Control-/Live-Steuerung über diese API (das ist die
  Web-Frontend-/`control`-Fläche mit eigenem Echtzeit-Pfad).
- Kein OAuth-Client-Credentials-Flow für Service-zu-Service in Phase 1
  (SSO-Consumer nutzen weiter OIDC; kann später ergänzt werden).

## Architektur

### 1. Authentifizierung — `PersonalAccessToken`

Neues Model (`apps/api/models.py`, neben `DeviceKey`):

- Felder: `user` (FK → accounts.User), `name` (menschlicher Label),
  `token_hash` (SHA-256, **nur Hash** persistiert), `prefix` (kurzer
  nicht-geheimer Präfix für Anzeige/Lookup), `created_at`, `last_used_at`
  (nullable), `expires_at` (nullable), `revoked_at` (nullable).
- Raw-Token via `secrets.token_urlsafe(32)`, **einmalig** bei Erstellung
  zurückgegeben, nie wieder abrufbar — Muster exakt wie `AccountToken` /
  `DeviceKey`-Private-Key.
- Methode `issue(user, name, expires_at=None) -> (instance, raw_token)` und
  `is_active()` (nicht revoked, nicht expired).

Neue DRF-Auth-Klasse `PersonalAccessTokenAuthentication`
(`apps/api/authentication.py`):

- Header `Authorization: Bearer <raw_token>`.
- Hash vergleichen (constant-time), aktiven Token auflösen → `request.user`,
  `request.auth = token`.
- `last_used_at` best-effort aktualisieren (ein UPDATE, kein Blocking).
- In `REST_FRAMEWORK.DEFAULT_AUTHENTICATION_CLASSES` **vor**
  `SessionAuthentication` einreihen; `DeviceKeyAuthentication` bleibt für die
  Device-Endpunkte.

UI: Token-Management im Account-Bereich (`apps/accounts`) — Liste eigener
Tokens (Name, Präfix, created/last_used/expiry), Erstellen (Raw einmalig
angezeigt), Widerrufen. Keine Raw-Anzeige nach dem ersten Mal.

### 2. Autorisierung — bestehendes Modell spiegeln

Zentrale Scoping-Helper (neues Modul, z.B. `apps/stations/scoping.py`),
die auf Membership + Topology aufsetzen:

- `accessible_stations(user) -> QuerySet[Station]`
- `accessible_regions(user) -> QuerySet[Region]`
- analoge Helper bzw. abgeleitete Filter für abhängige Ressourcen
  (Deployments/Rollouts/LogEntries/Inventory hängen an Stationen).

Regel-Matrix:
- **admin / staff:** alles.
- **member mit RegionAssignment(manager):** CRUD auf Stationen der
  zugewiesenen Region(en) und deren abhängige Objekte.
- **member mit StationAssignment:** Scope auf die zugewiesene(n) Station(en).
- **applicant:** kein API-Zugriff (spiegelt `_ApplicantForbiddenMixin`).

`TopologyScopedPermission` (`apps/api/permissions.py`):
- `has_permission`: Token aktiv + User nicht applicant + Membership erfüllt
  Mindestlevel der View.
- `has_object_permission`: Objekt liegt im Scope des Users (delegiert an die
  Scoping-Helper).
- Jedes ViewSet setzt zusätzlich `get_queryset()` auf den gescopten
  QuerySet — Objekte außerhalb des Scopes sind schon im Listing unsichtbar
  (nicht nur 403 bei Detailzugriff).

Der Token erbt **exakt** die Rechte seines Besitzers. Kein Scope-/Permission-
System obendrauf (Option „Token-Scopes" wurde verworfen zugunsten „bestehendes
Modell spiegeln").

### 3. Ressourcen-Layout

- DRF `DefaultRouter`, zentral unter `/api/v1/` registriert.
- Pro Ressource ein `ModelViewSet` + **expliziter** Serializer (keine
  `fields = "__all__"` — sensible Felder bleiben strukturell draußen).
- Nested Routes wo fachlich sinnvoll, z.B.
  `/api/v1/stations/{id}/log-entries/`, `/api/v1/stations/{id}/inventory/`.
- Filtering via `django-filter` (`DjangoFilterBackend`), SearchFilter,
  OrderingFilter.
- Pagination: `PageNumberPagination` als Default (konfigurierbare
  `page_size`).
- Konsistente Fehler-Envelopes (DRF-Default-Exception-Handler, ggf. dünner
  Custom-Handler für einheitliches Shape).

### 4. Discoverability & Contract

- `drf-spectacular` ergänzen → Schema unter `/api/v1/schema/`, Swagger-/Redoc-
  UI unter `/api/v1/docs/`.
- Versionierung über URL-Pfad (`v1`). Breaking Changes → `v2`, `v1` bleibt
  parallel.

### 5. Cross-cutting

- **Audit:** jede schreibende API-Aktion propagiert über die bestehenden
  Audit-Signale (StationAuditLog / AccountAuditLog), angereichert mit „via API
  token <prefix>". Keine stillen Mutationen.
- **Throttling:** eigene `ScopedRateThrottle`-Rate für Token-Auth (Startwert
  `api-token: 120/min`), getrennt von `heartbeat`.
- **Versionierung der Serializer:** v1-Serializer sind der eingefrorene
  Contract; interne Model-Änderungen dürfen v1 nicht brechen.

## Komponenten-Schnitt (Isolation)

| Unit | Zweck | Abhängigkeiten |
|------|-------|----------------|
| `PersonalAccessToken` (model) | Token-Lifecycle, Hash-Storage | accounts.User |
| `PersonalAccessTokenAuthentication` | Header → User auflösen | Token-Model |
| `scoping.py` Helper | „Was darf User X" als QuerySets | Membership, Assignments |
| `TopologyScopedPermission` | Permission-/Object-Checks | Scoping-Helper |
| ViewSets + Serializer pro Ressource | CRUD-Fläche | Scoping, Serializer |
| Token-Management-UI | Tokens erstellen/widerrufen | Token-Model |
| OpenAPI (spectacular) | Contract/Docs | ViewSets |

Jede Unit ist unabhängig testbar; die Scoping-Helper sind der
Single-Source-of-Truth, auf dem sowohl Permission als auch QuerySet-Filterung
aufsitzen.

## Error Handling

- Auth fehlt/ungültig/expired/revoked → 401.
- Authentifiziert, aber Scope/Level unzureichend → 403; Objekt außerhalb
  Scope erscheint gar nicht im Listing (kein Information-Leak über Existenz).
- Validierungsfehler → 400 mit feldbezogenem DRF-Error-Shape.
- Throttle überschritten → 429 mit `Retry-After`.

## Testing

Der kritische Teil ist **nicht** Happy-Path-CRUD, sondern die
**Permission-Matrix** pro Ressource:

- Achsen: Rolle (admin / staff / region-manager / station-assigned /
  applicant / anonym) × Operation (list / retrieve / create / update /
  delete) × Objekt im Scope vs. außerhalb.
- Token-Lifecycle: aktiv / expired / revoked / fremder User.
- Negativtests: sensible Felder tauchen nie im Serializer-Output auf;
  out-of-scope-Objekte nie im Listing.
- Audit: schreibende Calls erzeugen Audit-Einträge mit Token-Herkunft.

## Phasierung

Ein Feature-Branch (`feature/user-automation-api`), ein PR pro Phase
(Squash-Merge-Konvention).

1. **Fundament:** `PersonalAccessToken` + Auth-Klasse + Token-UI +
   Scoping-Helper + `drf-spectacular`. Kein Feature-Wert ohne diese Basis.
2. **Read-Fläche:** alle Allowlist-Ressourcen read-only (schnell nutzbar,
   niedriges Risiko), inkl. Permission-Matrix-Tests fürs Lesen.
3. **Write-Fläche:** CRUD auf der Allowlist, Ressource für Ressource, jeweils
   mit vollständiger Permission-Matrix und Audit-Verdrahtung.

## Offene Punkte / bewusste Abweichungen

- Abweichung von „volles CRUD auf *alles*": kuratierte Allowlist (§Scope) +
  read-vor-write-Phasierung — vom User bestätigt. Ziel: beherrschbarer
  Blast-Radius, kein Umfangsverlust.
- Service-zu-Service (OIDC-Client-Credentials) und Radio-Control sind
  explizit außerhalb dieser Spec und kämen als eigene Features.
