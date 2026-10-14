# Unified Permissions — Design

**Status:** Approved design, pre-plan
**Date:** 2026-10-14
**Repo:** station-manager

## Problem & Intent

Rechte werden heute an vielen Stellen mit unterschiedlichen Mitteln geprüft:
`is_admin`, `is_internal`, Djangos `is_staff`, vier verschiedene View-Mixins
(teils dupliziert), `write_scoping.py` in der API, Topologie-Scoping
(`accessible_stations`) nur in der API, ein eigenes Rang-Vokabular in
`control/capability_policy.py`. Ein Audit am 2026-10-14 (main @71604f7) hat
ergeben:

- Die REST-API erlaubt Staff und Region-Managern vieles, was die UI nur Admins
  erlaubt (Zuweisungen, Regionen, Images, Provisioning, User-Edit, globale
  Rollout-Sequenz / AlertRules); Staff sieht per API alle User inkl.
  Adressen/Telefon.
- Umgekehrt sieht ein Member ohne Zuweisung per API 0 Stationen, in der UI alle.
- `module_firmware` prüft Djangos `is_staff`, das nie mit `membership_level`
  synchronisiert wird → per UI beförderte Staff/Admins haben dort keine Rechte.
- Terminal: Status-Endpoint sagt `is_internal`, Consumer verlangt `is_admin`.
- Diverse UI-Gating-Brüche (Alerts quittieren ohne Liste, Push-Links → 403,
  Sidebar versteckt Erlaubtes).

Das ist nicht der erste Rechte-Fix. Ziel ist, das **ein für alle Mal** zu lösen:

- **Die UI ist die Referenz.** API und WebSockets erlauben exakt dasselbe.
- **Sehen darf (fast) jeder, ändern nur bestimmte.** Bereiche werden read-only
  dargestellt statt versteckt.
- **Rechte stehen an genau einem Ort** und jede Prüfung (Views, Templates, API,
  WebSockets) liest von dort. Kein Alias-/Zwischen-Layer, kein Rest der alten
  Helper.
- Abweichungen fallen in Tests auf, Verweigerungen sind für User verständlich
  und für Admins sichtbar.

Erfolg =
- Eine Registry-Datei definiert alle Rechte; `docs/permissions.md` wird daraus
  generiert und von CI auf Aktualität geprüft.
- Jede URL (UI + API) deklariert einen Bereich oder ist begründet ausgenommen —
  per Test erzwungen.
- Die alten Helper/Mixins existieren nicht mehr — per Test erzwungen.
- Ein Matrix-Test prüft jede Stufe gegen jedes Recht in UI und API.

## Entscheidungen (User, 2026-10-14)

1. UI gibt vor; API wird angeglichen.
2. Topologie-Rollen (Region-Manager, Station-Admin, Maintainer) bekommen
   **keine** Extra-Rechte über die Stufe hinaus. Einzige Ausnahme: Control-
   Override (siehe §3.4). Alert-Routing über Topologie bleibt unverändert (ist
   Empfänger-Logik, keine Berechtigung).
3. Lese-Bereiche sind ab **Applicant** sichtbar (auch das Control-Panel,
   read-only inkl. RX-Audio). Ersetzt die frühere Entscheidung „Control-Button
   nur ab Member".
4. Station-Audit-Tab (zeigt IPs) = Staff. Deploy auslösen = Staff (UI-Upgrade-
   Button und „New Deployment" einheitlich). Images verwalten = Admin.
5. Modell: **Bereich × drei feste Verben**. Komplettumbau in einem Schwung.
6. Verweigerungen: eigene 403-Seite / JSON / WS-Nachricht + Audit-Eintrag.

## 1. Das Modell

### 1.1 Stufen

`Level` (IntEnum) mit derselben Ordnung wie `User.MembershipLevel`:
`APPLICANT=1 < MEMBER=2 < STAFF=3 < ADMIN=4`. Anonyme und inaktive User haben
keine Stufe (`None`) und bestehen keine Prüfung.

### 1.2 Verben

Genau drei, für alle Bereiche gleich:

| Verb | Bedeutung |
|---|---|
| `view` | sehen, read-only |
| `use` | bedienen ohne zu konfigurieren (nur wo es das gibt) |
| `manage` | anlegen, ändern, löschen, auslösen, konfigurieren |

Regeln:
- **Monotonie:** `level(view) ≤ level(use) ≤ level(manage)`; wird beim Import der
  Registry geprüft (`ImproperlyConfigured`).
- **Nicht definiertes niedrigeres Verb** erbt die Stufe des nächsthöheren
  definierten Verbs (z. B. `terminal.view` = `terminal.use` = Admin).
- **Nicht definiertes höheres Verb** ist für niemanden erlaubt (z. B.
  `audit.manage`).
- **Kein Verb pro Bereich erfinden.** Braucht etwas eine vierte Stufe, wird es
  ein neuer Bereich.

### 1.3 Bereiche (Soll-Matrix)

A = Applicant, M = Member, S = Staff, Ad = Admin, – = nicht definiert.

| Bereich | view | use | manage | Inhalt |
|---|---|---|---|---|
| `dashboard` | A | – | – | Startseite, Zähler |
| `profile` | – | A | – | eigenes Profil, Passwort, Notification-Settings, Web-Push-Abo |
| `stations` | A | – | S | Liste/Detail; manage: anlegen/bearbeiten/löschen, Device-Key, Tags, Logbuch, Fotos, Station-Audit-Tab |
| `control` | A | M | S | view: Panel read-only + RX-Audio; use: Lock, PTT, Bedien-Capabilities, Mic-Uplink; manage: Kalibrier-Capabilities (Filter, Volume), Audio-Diagnose |
| `terminal` | – | Ad | – | Root-Shell (Status-Endpoint + Consumer) |
| `topology` | A | – | Ad | Regionen, Station-/Region-Zuweisungen, Station→Region |
| `deployments` | A | – | S | Liste/Detail/Feed; manage: neu, abbrechen, Upgrade-Button |
| `rollouts` | A | – | Ad | Sequenz ansehen; manage: Sequenz bearbeiten, Gruppen-Upgrade |
| `images` | A | – | Ad | Liste; manage: GitHub-Import, Archiv, Restore, Latest |
| `provisioning` | – | – | Ad | Jobs anlegen, Status, Download (Secrets → kein Lesen) |
| `alerts` | A | – | S | Liste, Zähler; manage: quittieren, auflösen |
| `alert_rules` | A | – | Ad | Regeln, Settings, Test-Push |
| `modules` | A | – | S | Modul-Inventar + Firmware; manage: Confirm, Lifecycle, Notes, Import, Archiv |
| `users` | M | – | Ad | Verzeichnis; manage: anlegen, bearbeiten, Membership, Soft-Delete/Restore/Purge, Welcome-Mail |
| `api_tokens` | – | M | – | eigene PATs erstellen/widerrufen |
| `audit` | Ad | – | – | globales Audit-Log + Export |
| `sso` | – | – | Ad | SSO-Admin (Apps, Grants, Policies, Sessions, Tags) |

## 2. Registry & Kern-API

### 2.1 `apps/accounts/permissions.py` — einzige Definitionsstelle

```python
class Level(IntEnum): APPLICANT = 1; MEMBER = 2; STAFF = 3; ADMIN = 4

@dataclass(frozen=True)
class Area:
    label: str                       # übersetzbar, z. B. _("Images")
    view: Level | None = None
    use: Level | None = None
    manage: Level | None = None
    verb_labels: dict[str, str] = field(default_factory=dict)  # optional, für 403-Texte/Doku

AREAS: dict[str, Area] = {
    "images": Area(_("Images"), view=Level.APPLICANT, manage=Level.ADMIN,
                   verb_labels={"manage": _("Images importieren/archivieren/als Latest markieren")}),
    ...
}
```

Öffentliche Funktionen (nur diese werden außerhalb importiert):

| Funktion | Zweck |
|---|---|
| `level_of(user) -> Level \| None` | Stufe aus `membership_level`; `None` für anonym/inaktiv |
| `required_level(area, verb) -> Level \| None` | aufgelöste Stufe nach §1.2 (`None` = niemand) |
| `can(user, area, verb) -> bool` | **die** Prüfung. Unbekannter Bereich/Verb → `KeyError` (Tippfehler fallen im Test auf) |
| `require(user, area, verb, *, request=None, channel="web")` | wirft `AreaDenied` und loggt (§5) |
| `users_with(area, verb) -> QuerySet` | alle aktiven User mit dem Recht (z. B. für Empfängerlisten) |
| `can_override_station(user, station) -> bool` | die eine Zusatzregel (§3.4) |
| `matrix() -> list[...]` | für Doku-Generator und Tests |

`User` bekommt die Methode `user.can(area, verb)` (delegiert an `can`).
`AnonymousUser` wird in `can` behandelt (kein Monkeypatch).

### 2.2 Django `is_staff` / `is_superuser`

Werden **nur noch abgeleitet**, ausschließlich für Djangos `/admin/`:
`User.save()` setzt `is_staff = is_superuser = (membership_level == ADMIN)`.
`createsuperuser` setzt `membership_level=ADMIN`. Eine Datenmigration setzt beide
Felder für alle bestehenden User neu. Kein eigener Code liest sie (Test §6).

### 2.3 Entfernt (kein Alias, kein Übergang)

- `User.is_admin`, `User.is_internal`, `User.can_use_station`,
  `User.can_maintain_station`, `User._invalidate_role_cache`
- `AdminRequiredMixin` (accounts + monitoring-Kopie),
  `AdminOrOperatorRequiredMixin` (stations + monitoring-Kopie),
  `AdminOnlyMixin` (sso), `_StaffModuleMixin`, `_StaffFirmwareMixin`
- `apps/api/write_scoping.py`, `apps/api/write_permissions.py`,
  `apps/api/scoping.py`, `apps/stations/scoping.py`,
  `TopologyScopedPermission`
- `ROLE_RANK`, `ROLE_LABELS`, `viewer_role`, `role_allows` in
  `control/capability_policy.py`

`is_station_admin`, `is_region_manager`, `can_administer_station` werden
**nicht** mehr direkt aufgerufen; ihre Logik lebt in `can_override_station`.
Die Assignment-Modelle selbst bleiben (Alert-Routing, Override).

## 3. Anbindungen

### 3.1 HTTP-Views

Mixin `AreaPermissionMixin` in `apps/accounts/permissions.py`:

```python
class ImageListView(AreaPermissionMixin, ListView):
    area = "images"                     # GET/HEAD/OPTIONS → view, sonst → manage
class TerminalStatusView(AreaPermissionMixin, View):
    area = "terminal"; verb = "use"     # explizites Verb überschreibt die Methode
```

- Verb-Ableitung: sichere Methoden → `view`, alle anderen → `manage`; `verb = …`
  überschreibt für alle Methoden, `verbs = {"POST": "use"}` pro Methode.
- Fehlendes Recht → `require()` → `AreaDenied` (403, §5). Anonyme laufen wie
  bisher über `LoginRequiredMiddleware` auf den Login.
- Ausnahmen deklarieren `permission_exempt = "<Begründung>"`, z. B. Login/
  Logout/Passwort-Reset (`"auth-flow"`), OIDC-Endpoints und `/sso/authorize/`
  (`"oidc: per-app policy"`), Device-API (`"device-key"`), Health
  (`"health"`), `sw.js`/Manifest (`"static"`). Gilt auch für Funktions-Views
  (Decorator `@area_permission("images", "manage")` / `@permission_exempt("…")`).
- Objektbezogene Business-Regeln (z. B. „nur eigene Tokens widerrufen",
  „Membership nicht selbst ändern", Demote-Guard bei Assignments) bleiben in den
  Views/Serializern — sie sind Validierung, keine Rechte.

### 3.2 Templates

Context-Processor `apps.accounts.context_processors.permissions` liefert `can`:
`{% if can.images.manage %}…{% endif %}` (Lazy-Objekt, wertet `can()` aus).

- Seiten rendern für alle mit `view`; Buttons, Formulare, Bearbeiten-Links nur
  mit `use`/`manage`.
- `templates/includes/readonly_badge.html`: dezenter „nur lesend"-Hinweis auf
  Seiten, die der User nur sehen kann.
- Sidebar: jeder Eintrag erscheint bei `can.<bereich>.view` (Users-Verzeichnis
  und Tags bekommen damit ihre fehlenden Links).
- Alle `user.is_admin` / `user.is_internal` / `request.user.is_staff` in
  Templates werden ersetzt.

### 3.3 REST-API

Eine DRF-Permission `AreaPermission` ersetzt alle bisherigen User-Permissions:

- Jeder User-ViewSet deklariert `area`. Sichere Methoden → `view`, sonst
  `manage`. `@action`s deklarieren ihr Verb (`archive`, `restore`, `import`,
  `available` → `manage`).
- Nicht-User-Principals (DeviceKey) → 403. Device-Endpoints behalten `IsDevice`
  und sind `permission_exempt = "device-key"`.
- **Topologie-Scoping beim Lesen entfällt:** `get_queryset` liefert alles, was
  der Bereich zeigt (wie die UI).
- **Feld-Regel Station→Region:** Ändern bzw. Setzen von `region` am Station-
  Endpoint verlangt zusätzlich `topology.manage` (UI: eigener Set-Region-View).
- Mapping alt → neu (Auszug): Station-/Region-Assignments → `topology.manage`;
  Regions → `topology`; Station-Tags, Log-Entries, Photos → `stations.manage`;
  Rollout-Sequenz/-Entries → `rollouts.manage`; Deployment-Trigger →
  `deployments.manage`; AlertRules → `alert_rules.manage`; Provisioning →
  `provisioning.manage`; Images-Actions → `images.manage`; Users-Writes →
  `users.manage`; Audio-Diagnose → `control.manage`.
- **Users-Endpoint:** Lesen = `users.view`; Felder pro Ziel-User über
  `accounts/visibility.py` (`audience_for` / `directory_visible_fields`),
  identisch zum Verzeichnis. Admin-Audience = `can(user, "users", "manage")`.
  Damit sieht Staff per API nicht mehr Adressen/Telefon aller User.
- Bestehende Business-Validierung (400er, Lifecycle-Guards, Locks, Audit
  „via API token") bleibt unverändert.

### 3.4 WebSockets

| Consumer | Connect | Aktionen |
|---|---|---|
| `StationStatusConsumer` | `stations.view` | – |
| `DeploymentStatusConsumer` | `deployments.view` | – |
| `ControlConsumer` | `control.view` | Lock acquire, PTT, Commands: Verb aus Capability-Policy (`use`/`manage`); Persist: `control.manage`; Preempt/Override: `can_override_station` |
| `AudioConsumer` | `control.view` (RX hören) | Mic-Uplink: `control.use` |
| `TerminalConsumer` | `terminal.use` | – |

- **Jede Aktion prüft neu mit frisch geladenem User** (eine DB-Abfrage), damit
  Herabstufungen sofort wirken statt erst beim Reconnect.
- **`can_override_station(user, station)`** = `can(user, "control", "manage")`
  oder Station-Admin dieser Station oder Region-Manager ihrer Region. Einzige
  zuweisungsbasierte Regel im System; in `permissions.py` dokumentiert und in
  `docs/permissions.md` als Fußnote ausgegeben.
- **Capability-Policy** (`control/capability_policy.py`): `CapabilityPolicy(verb
  ="use" | "manage", persist=bool)`. Default `use`; Filter/Volume `manage` +
  persist (inhaltlich wie heute „staff"). Read-only-Badge-Text kommt aus
  `required_level("control", verb)`.
- Agent-Consumer (`/ws/agent/*`) bleiben Ed25519-authentifiziert und sind nicht
  Teil des Modells.

## 4. Datenformung bleibt getrennt

`accounts/visibility.py` entscheidet, **welche Felder** einer Person jemand
sieht (Datenschutz). Das ist keine Berechtigung und bleibt dort; nur ihre
Eingänge nutzen die Registry (`users.view` für Zugang, `users.manage` für
Admin-Audience). Ebenso bleiben SSO-App-Policies (`OPEN_TO_MEMBERS`, …) als
App-Zugangsregel bestehen, vergleichen aber über `level_of()`.

Alert-Empfänger (`monitoring/recipients.py`): Admins über
`users_with("alert_rules", "manage")` plus Topologie-Rollen wie bisher.

## 5. Verweigerungen: User-Feedback + Audit

### 5.1 Ein Ausnahmetyp

`AreaDenied(PermissionDenied)` trägt `area`, `verb`, `required_level`,
`actual_level`. Alle Kanäle verweigern über `require()`.

### 5.2 Für den User

- **Web:** `handler403` → `templates/403.html` im normalen Layout:
  „**Keine Berechtigung** — *<Verb-Label>* erfordert **<Stufe>**, du bist
  **<Stufe>**. Wenn du glaubst, dass das falsch ist, wende dich an einen Admin."
  + „Zurück" / „Zur Übersicht". Andere `PermissionDenied` (Business-Regeln)
  zeigen dieselbe Seite mit generischem Text bzw. der Exception-Message.
- **API:** `{"detail": "...", "required": "images.manage",
  "required_level": "admin", "your_level": "member"}` mit Status 403.
- **WebSocket:** Connect-Verweigerung: Fehler-Frame
  `{"type": "error", "code": "forbidden", "message": "..."}`, dann Close 4403.
  Aktions-Verweigerung: nur Fehler-Frame, Verbindung bleibt. Control-Panel-JS
  zeigt Fehler-Frames als Toast.

### 5.3 Für Admins

- Neuer `AccountAuditLog.EventType.PERMISSION_DENIED`; neues Feld
  `AccountAuditLog.details = JSONField(default=dict, blank=True)` mit
  `{area, verb, path, method, channel: web|api|ws, required_level,
  actual_level, count}`; `actor`, `ip_address` wie üblich.
- **Dedupe:** gleiche (User, Bereich, Verb, Pfad) innerhalb von 10 Minuten →
  kein neuer Eintrag, sondern `details.count += 1` am bestehenden (Row-PK im
  Django-Cache). Anonyme Zugriffe werden nicht geloggt.
- Audit-Feed: Event-Typ ist über den bestehenden `event_type`-Filter wählbar;
  zusätzlich ein Schnellfilter „Verweigerungen".
- Dashboard-Kachel (nur `audit.view`): „Zugriffsverweigerungen (24 h): N" mit
  Link auf den gefilterten Feed.
- Kein Push/Mail pro Verweigerung (später ggf. als Alert-Regel).

## 6. Tests & Wächter

1. **URL-Walk:** Jede aufgelöste URL (UI + DRF-Router inkl. Actions + Channels-
   Routing) hat `area` oder `permission_exempt` mit nicht-leerer Begründung.
2. **Matrix-Test:** Für jede Stufe (anonym, A, M, S, Ad) × jeden Bereich × jedes
   Verb: mindestens ein repräsentativer UI-Endpoint **und** (wo vorhanden) ein
   API-Endpoint; Erwartung aus `required_level()` abgeleitet, nicht hart codiert.
   Plus WS-Matrix für Control/Audio/Terminal/Status-Feeds.
3. **Verbotene Namen:** Grep-Test über `apps/`, `config/`, `templates/`,
   `station_manager*`: `is_admin`, `is_internal`, `is_staff`, `is_superuser`,
   `can_use_station`, `AdminRequiredMixin`, `AdminOrOperatorRequiredMixin`,
   `AdminOnlyMixin`, `write_scoping`, `accessible_stations`,
   `TopologyScopedPermission`, `ROLE_RANK`. Allowlist nur: `permissions.py`,
   `User.save()`/Manager (is_staff-Ableitung), `accounts/admin.py`, Migrationen.
   Direkte `MembershipLevel`-Vergleiche nur in `permissions.py`,
   `accounts/models.py`, `accounts/views_membership.py`, Membership-Serializer,
   Migrationen.
4. **Registry-Konsistenz:** Monotonie, jedes Verb-Label gehört zu einem
   definierten Verb, jeder Bereich hat mindestens ein Verb.
5. **Doku aktuell:** `manage.py permissions_table` == `docs/permissions.md`.
6. **403-Pfad:** HTML-Seite, JSON-Body, WS-Frame; Audit-Eintrag; Dedupe-Zähler;
   anonym → kein Eintrag.
7. **Bestehende Tests** werden auf Stufen umgestellt (u. a. module_firmware-Tests
   mit `is_staff=True`, `operator_user`-Fixture = Staff wird umbenannt).

## 7. Migration & Auslieferung

- Schema: `AccountAuditLog.details` (JSONField) + neue EventType-Choice.
- Daten: `is_staff`/`is_superuser` aller User aus `membership_level` neu setzen.
- Ein Feature-Branch `feature/unified-permissions`, ein PR (Spec + Plan + Code),
  subagent-driven-development, Review-Loop bis 0, Squash-Merge nach User-OK.
- `docs/permissions.md` (generiert) wird Teil des Repos.

### Sichtbare Verhaltensänderungen

| Wer | Änderung |
|---|---|
| Applicant | sieht Deployments, Rollouts, Images, Alerts, Regionen, Module, Control-Panel (read-only, RX) |
| Member | wie Applicant + bedient Control; Sidebar-Link zum User-Verzeichnis |
| Staff | darf Alerts in der UI quittieren (Liste sichtbar); verliert per API: Zuweisungen, Regionen, Images, Provisioning, User-Edit, Rollouts, AlertRules; sieht per API keine privaten User-Felder mehr; bekommt Modul-Firmware-Rechte (bisher nur mit Django-`is_staff`); sieht Terminal nicht mehr als verbindbar |
| Region-Manager / Station-Admin / Maintainer | verlieren alle API-Extrarechte; Override im Control-Panel bleibt |
| Ehemalige Superuser mit niedrigerer Stufe | verlieren `/admin/` und Modul-Rechte |
| API-Skripte | 403 statt Erfolg wo oben eingeschränkt; Fehlerbody nennt das fehlende Recht |

## Out of Scope

- Rechte pro Objekt/Station über die eine Override-Regel hinaus.
- Änderungen an der Alert-Empfänger-Logik (Topologie-Routing bleibt).
- Agent-/Device-Authentifizierung.
- Push/Mail bei Verweigerungen.
