# User-Domain-Redesign — Liste, Detail, Edit, Create, Delete, Audit — Design

**Status:** Draft, brainstormed 2026-06-09.
**Ziel:** Die User-Verwaltung im station-manager strukturell überarbeiten. Heute besteht sie aus einer Liste und einem überladenen Edit-Form, das de facto die zentrale User-Management-Surface ist — ohne dass das aus dem URL- oder Template-Namen ersichtlich wäre. Dieser Spec führt eine echte Detail-Seite ein, reduziert das Edit-Form auf Identity, baut die List/Create/Delete-Templates mobil-tauglich um und schließt die Audit-Lücken bei den Identity-CRUD-Operationen.

Der Redesign folgt dem etablierten Pattern aus `station_detail.html` (Tabs + Cards + Summary-Bar) und passt das User-Domain konzeptionell an die Stations-Domain an: List → Detail → Edit-Form sind drei klar getrennte Surfaces.

---

## 1. Kontext und Begriffe

Die User-Verwaltung lebt in `apps/accounts/`. Ein User hat:

- Identity-Felder (Username, Email, First/Last name, Language, is_active).
- Eine **Membership-Level**-Rolle (`applicant` / `member` / `staff` / `admin`) — gesetzt per HTMX-Card auf dem Edit-Form, geloggt in `AccountAuditLog`.
- **Topology-Assignments**:
  - `RegionAssignment` mit Rolle `manager` — pro Region maximal 1 pro User.
  - `StationAssignment` mit Rolle `admin` (pro Station max. 1 global) oder `maintainer`.
- **SSO-Bezüge**: `AppGrant` (App-spezifischer Zugriff), `TokenSession` (laufende OIDC-Sessions), Django `auth.Group`-Mitgliedschaften (= Tags).

Die HTMX-Endpoints für Membership-Set, Region/Station-Assignment-Create/Revoke und SSO Grant-Toggle / Session-Revoke / Tag-Toggle sind etabliert und bleiben in diesem Redesign **unverändert**. Was sich ändert, ist die Render-Surface, auf der ihre Karten leben.

**Audit-Modelle**, die User-relevant sind:

- `AccountAuditLog` (apps/accounts/models.py) — System-weites Account-Log: Membership-Promote/-Demote, Region-Assignment-Create/-Revoke, Region-CRUD.
- `SsoAuditLog` (apps/sso/models.py) — alles SSO/OIDC: Login, Token, Grant-Toggle, Session-Revoke, App-Policy-Change, Group-Membership-Change.
- `StationAuditLog` (apps/stations/models.py) — pro Station: u.a. Station-Assignment-Create/-Revoke (mit Station als Subjekt, User in der Message).

Was heute **nicht** geloggt wird (Lücke, die dieser Spec schließt):

- Identity-CRUD: `UserCreateView`, `UserUpdateView`, `UserDeleteView` emittieren nichts.
- Station-Assignment-Bezug auf User-Seite: `StationAuditLog` hat den User nur in der Message; auf der User-Detail-Audit-Sicht ist das so nicht filterbar.

---

## 2. Architektur — Überblick

**Neue Route + View**:

- `GET users/<pk>/` → `UserDetailView` — DetailView mit Tabs, lädt alle Management-Daten (war bisher im `UserUpdateView`).

**Geänderte Routes**:

- `users/<pk>/edit/` bleibt, aber `UserUpdateView` verliert seinen fetten `get_context_data`-Block. Success-Redirect zeigt auf die Detail-Seite.
- `users/<pk>/delete/` zeigt Impact-Details vor dem Delete. Success-Redirect zur Liste.
- `users/create/` Success-Redirect zur Detail-Seite des neu erstellten Users.

**Neue EventTypes** in `AccountAuditLog.EventType`:

- `USER_CREATED`, `USER_UPDATED`, `USER_DELETED`, `USER_ACTIVATED`, `USER_DEACTIVATED`,
  `STATION_ASSIGNMENT_CREATED`, `STATION_ASSIGNMENT_REVOKED`.

**Emission**:

- `UserCreateView.form_valid` → `USER_CREATED`.
- `UserUpdateView.form_valid` → `USER_UPDATED` (Diff-Liste in Message) + ggf. `USER_ACTIVATED` / `USER_DEACTIVATED`.
- `UserDeleteView.form_valid` → `USER_DELETED` (vor `super().delete()`).
- `apps/stations/signals._on_station_assignment_save/_delete` → zusätzlich `AccountAuditLog.STATION_ASSIGNMENT_*` mit `target_user=instance.user` (das bestehende `StationAuditLog`-Emit bleibt, doppelte Schreibung ist gewollt — pro Subjekt eine View).

**Neue Templates**:

- `accounts/user_detail.html` — die zentrale Surface.
- `accounts/_identity_overview_card.html`, `accounts/_user_audit_card.html` — als Partials für den Detail-View.

**Geänderte Templates**:

- `accounts/user_list.html` — Filter-Bar + View-Action.
- `accounts/user_form.html` — schlankes Identity-Form, mobile-freundlich, ohne Sub-Cards.
- `accounts/user_confirm_delete.html` — Impact-Anzeige.

**Unveränderte Templates** (werden nur in die Detail-Tabs eingehängt):

- `accounts/_membership_card.html`, `accounts/_region_assignments_card.html`, `accounts/_station_assignments_card.html`.
- `sso/_app_grants_card.html`, `sso/_sessions_card.html`, `sso/_tags_card.html`.

Inline `max-width:640px` und `max-width:960px` in diesen Cards wird entfernt — die Breite kommt vom Tab-Container.

---

## 3. UserDetailView — Permissions und Datenladung

### 3.1 Permission-Modell

`UserDetailView` ist mit `LoginRequiredMixin` geschützt und implementiert eine eigene `dispatch`-Logik:

- Admin (`request.user.is_admin`) → darf alle User sehen.
- Self (`object.pk == request.user.pk`) → darf eigene Detail-Seite sehen.
- Sonst → 404 (kein 403, um Existenz-Leak zu vermeiden).

In `get_context_data` wird `is_self_view` und `is_admin_view` ans Template gereicht. Die Cards rendern nur für Admin (`request.user.is_admin`) — der Self-View sieht Identity-Overview, Topology-Read-only (Pills ohne Edit/Revoke-Buttons), eigene SSO-Sessions (read-only) und den Audit-Tab.

Die Read-only-Variante der Cards wird durch ein `readonly`-Flag im Template-Kontext gesteuert: bestehende Cards bekommen ein `{% if readonly %}…{% else %}…{% endif %}` um Add/Revoke-Forms. Alternative — eigene `_..._readonly.html`-Partials — wird verworfen, weil zu viel Duplizierung.

### 3.2 Datenladung

`UserDetailView.get_context_data` lädt:

- `existing_region_assignments`, `available_regions`
- `existing_station_assignments`, `all_stations`
- `app_grants_list`, `user_sessions` (über bestehende SSO-Helper)
- `tag_entries`, `membership_level_choices`
- `user_audit_entries` — siehe Sektion 7.
- Counts für die Summary-Bar (`n_region_assignments`, `n_station_assignments`, `n_active_sessions`).

`UserUpdateView.get_context_data` wird auf das absolute Minimum reduziert (`form_title` reicht).

### 3.3 Success-Redirects

| View | Erfolgs-Redirect |
|---|---|
| `UserCreateView` | `users:user_detail` mit pk des neuen Users |
| `UserUpdateView` | `users:user_detail` mit pk des bearbeiteten Users |
| `UserDeleteView` | `users:user_list` |
| Alle HTMX-Endpoints | unverändert (200 JSON, Client reloaded die Seite) |

---

## 4. Detail-Page — Layout

Template: `apps/accounts/templates/accounts/user_detail.html`. Strukturell parallel zu `stations/station_detail.html`.

### 4.1 Page-Head

```
page-head
├ page-eyebrow: "User · #{{ object.pk|stringformat:'03d' }}"
├ page-title:
│   ├ avatar (sb-avatar, vergrößert auf 48px)
│   ├ username
│   └ subtitle: full name (oder muted "—" wenn leer)
├ pills-row: membership-level | is_active | language
└ page-head-actions:
    ├ [Edit identity]   → users:user_edit   (nur wenn Admin UND nicht self — Self editiert via Profile)
    └ [Delete]          → users:user_delete (nur wenn Admin UND nicht self)
```

### 4.2 Summary-Bar

Horizontal-scrollbar auf Mobile, gleiche `summary-bar` / `summary-item`-Klassen wie auf Station-Detail. Felder:

- Email
- Last login (relative + tooltip absolute)
- Date joined
- Language (Display)
- Region-Assignments (Count)
- Station-Assignments (Count)
- Active SSO Sessions (Count)

### 4.3 Tabs

Wiederverwendet wird der `data-tabs`-Container und das bestehende JS aus `station_detail.html`. Vier Tabs:

```
Overview            (Identity dlist + Snapshot)
Rollen & Topologie  (Membership + Regions + Stations)
Single Sign-On      (Grants + Sessions + Tags)
Audit               (Per-User Audit-Feed)
```

#### Overview-Tab

`grid grid-main`. Linke Spalte: Identity-Panel mit `dlist` (Username, Email, First/Last name, Language, is_active, Date joined, Last login). Rechte Spalte (`aside.stack-gap-14`): zwei kompakte Panels — "Zuletzt im Audit-Log" (3-5 jüngste Einträge mit "alle anzeigen →"-Link zum Audit-Tab) und "Status-Snapshot" (Counter-Visualisierung der Topology).

Für Self-View ist die Identity-dlist read-only — Self darf sich selbst nicht editieren (das macht die `profile`-Seite). Konsistenz: auch ein Admin sieht auf seiner *eigenen* Detail-Seite den Edit-Button **nicht** — der Profile-Flow bleibt der Self-Service-Weg, und Admins können sich selbst nicht den Membership-Level demoten (das wird im `MembershipSetView` ohnehin geblockt).

#### Rollen-&-Topologie-Tab

Drei Cards. Admin-Mode (mit Edit-Buttons):

```
membership-card                    (volle Breite, oben)
├─ region-assignments-card           ─┬ grid grid-main
└─ station-assignments-card          ─┘  (stacked auf Mobile)
```

Self-Mode (read-only):

```
membership-card-readonly            (zeigt Pill mit aktuellem Level)
├─ region-assignments-card-readonly  ─┬ grid grid-main
└─ station-assignments-card-readonly ─┘
```

#### Single-Sign-On-Tab

Drei Cards untereinander (Grants, Sessions, Tags), wie heute auf der Edit-Seite. SSO-Cards behalten ihre bestehenden HTMX-Targets — der `outerHTML`-Swap funktioniert genauso innerhalb des Tabs.

Self-Mode: Grants read-only (kein Toggle), Sessions zeigt nur eigene Sessions mit Revoke-Button (User darf eigene Sessions beenden — selbe Konvention wie der bestehende `profile`-Flow es bald hätte), Tags-Card komplett ausgeblendet (Tag-Membership ist eine Admin-Decision).

#### Audit-Tab

Siehe Sektion 7.

---

## 5. List-Page — Redesign

Template: `apps/accounts/templates/accounts/user_list.html`.

### 5.1 Page-Head + Filter

```
page-head
├ page-eyebrow: "Administration · People"
├ page-title: "Users"
├ page-sub: "Add, view, edit, and remove member, staff, and admin accounts."
└ page-head-actions: [+ New user]

filter-bar:
├ input[name=q]   — Username/Email/Full-name (icontains, GET-Param)
├ select[name=role]    — Alle | Bewerber | Mitglied | Staff | Admin
├ select[name=status]  — Alle | Aktiv | Inaktiv
└ [Reset filters] (Link, wenn Params gesetzt)
```

`UserListView.get_queryset` parst diese Params, mit `select_related` auf nichts (User selbst reicht) und `prefetch_related("region_assignments", "station_assignments")` für die Anzeige der Topology-Count-Hinweise — siehe 5.2.

Pagination bleibt bei 25/Seite.

### 5.2 Tabelle

Spalten:

| Spalte | Inhalt | Mobile-Verhalten |
|---|---|---|
| **User** | Avatar + Username + Full-name (data-primary) | Bleibt prominent, oben in Card |
| Role | Membership-Pill | data-label |
| Email | mono | data-label |
| Topology | "{n_region}·{n_station}" mini-pill (Tooltip mit Details) | data-label |
| Last login | relative time | data-label |
| Joined | YYYY-MM-DD | data-label |
| Actions | [View] [Edit] [Delete] | actions-Klasse, Buttons schrumpfen nicht |

Wichtig: **[View]** ist der primäre Button (kein btn-ghost), Edit/Delete bleiben sekundär. Klick auf den Primary-Cell-Bereich navigiert zur Detail-Seite — implementiert per `<a href="{% url 'accounts:user_detail' u.pk %}" style="color:inherit;text-decoration:none;">…</a>`-Wrap um den Inhalt der ersten Zelle. Das ist genau das Pattern aus `stations/_station_table.html` Zeile 23.

Self-Row (request.user) hat keinen Delete-Button.

---

## 6. Edit-Form und Create-Form — Mobile-friendly Identity

Template: `apps/accounts/templates/accounts/user_form.html`. Wird im Edit-Mode **drastisch verkleinert** — alle 6 Sub-Card-Includes raus. Im Create-Mode war es schon schlank, bleibt strukturell gleich, bekommt nur das Mobile-Layout.

### 6.1 Layout

```
page-head:
├ breadcrumb: Users > {username} > Edit  (oder: Users > Create)
├ page-title: form_title
└ (kein Page-Head-Actions im Edit-Mode — der Edit-Button ist die Detail-Page)

grid grid-main:
├ Linke Spalte (form panel, panel-body):
│   ├ form-row: [username] [is_active checkbox]   ← is_active nur im Edit-Mode
│   ├ form-row: [first_name] [last_name]
│   ├ form-row: [email]
│   ├ form-row: [language]
│   └ (Create-Mode) form-row: [password1] [password2]
│
└ Rechte Spalte (aside, panel):
    ├ Im Edit-Mode: dlist mit pk, date_joined, last_login, current membership-level
    └ Im Create-Mode: Info-Box "Topology-Assignments setzt du nach dem Speichern auf der Detail-Seite."

panel-foot:
├ [Save user]
└ [Cancel] → Detail (Edit) bzw. Liste (Create)
```

Inline `style="max-width:640px"` wird komplett entfernt. Das `grid grid-main` collapsed bei `≤ 1024px` automatisch zu single-column (existierendes CSS). `form-row` collapsed bei `≤ 720px` zu single-column (existierendes CSS).

### 6.2 Form-Klassen

`UserChangeForm` und `UserCreationForm` bleiben strukturell gleich. Klassen auf Widgets bleiben `form-control` / `form-select` / `form-check-input` — diese sind im CSS bereits mit `width:100%`, `min-height:44px` (Touch-Target) und Focus-Styling versehen.

### 6.3 Audit-Emission im UpdateView

```python
class UserUpdateView(AdminRequiredMixin, UpdateView):
    ...
    def form_valid(self, form):
        # changed_data muss vor super() ausgelesen werden, sonst leer
        # (Form-Reset nach Save in manchen Django-Versionen).
        changed_fields = set(form.changed_data)
        response = super().form_valid(form)

        tracked = {"username", "email", "first_name", "last_name", "language"}
        changed = changed_fields & tracked
        if changed:
            AccountAuditLog.log(
                event_type=AccountAuditLog.EventType.USER_UPDATED,
                actor=self.request.user,
                target_user=self.object,
                message=f"changed: {', '.join(sorted(changed))}",
                ip_address=_get_client_ip(self.request),
            )
        if "is_active" in changed_fields:
            event = (
                AccountAuditLog.EventType.USER_ACTIVATED if self.object.is_active
                else AccountAuditLog.EventType.USER_DEACTIVATED
            )
            AccountAuditLog.log(
                event_type=event,
                actor=self.request.user,
                target_user=self.object,
                message="",
                ip_address=_get_client_ip(self.request),
            )
        return response
```

Wichtig: Nicht-trackable Felder (z.B. `groups` falls je in das Form käme) zählen nicht. Wenn nur `is_active` flippt aber kein anderes Feld ändert, gibt es **nur** `USER_ACTIVATED/DEACTIVATED`, kein `USER_UPDATED`. Das hält den Feed pro Ereignis-Typ sauber.

### 6.4 Audit-Emission im CreateView

```python
def form_valid(self, form):
    response = super().form_valid(form)
    AccountAuditLog.log(
        event_type=AccountAuditLog.EventType.USER_CREATED,
        actor=self.request.user,
        target_user=self.object,
        message=f"{self.object.username} <{self.object.email}>",
        ip_address=_get_client_ip(self.request),
    )
    return response
```

---

## 7. Audit — neue EventTypes, Per-User-Feed, Self-Sichtbarkeit

### 7.1 EventType-Erweiterung

In `apps/accounts/models.py:AccountAuditLog.EventType`:

```python
USER_CREATED              = "user_created",                _("User Created")
USER_UPDATED              = "user_updated",                _("User Updated")
USER_DELETED              = "user_deleted",                _("User Deleted")
USER_ACTIVATED            = "user_activated",              _("User Activated")
USER_DEACTIVATED          = "user_deactivated",            _("User Deactivated")
STATION_ASSIGNMENT_CREATED = "station_assignment_created",  _("Station Assignment Created")
STATION_ASSIGNMENT_REVOKED = "station_assignment_revoked",  _("Station Assignment Revoked")
```

Migration: schema-leere `AlterField` auf `event_type.choices` — TextChoices-Erweiterung erfordert in Django keine DB-Änderung, aber `makemigrations` möchte den State-Change festschreiben. Standard für dieses Projekt (siehe SSO-Spec).

### 7.2 Station-Assignment-Doppel-Emit

In `apps/stations/signals.py`:

```python
@receiver(post_save, sender=StationAssignment)
def _on_station_assignment_save(sender, instance, created, **kwargs):
    if not created:
        return
    StationAuditLog.log(...)  # bestehend, unverändert
    AccountAuditLog.log(
        event_type=AccountAuditLog.EventType.STATION_ASSIGNMENT_CREATED,
        actor=instance.assigned_by,
        target_user=instance.user,
        message=f"station={instance.station.callsign or instance.station.name}, "
                f"role={instance.get_role_display()}",
    )

@receiver(post_delete, sender=StationAssignment)
def _on_station_assignment_delete(sender, instance, **kwargs):
    StationAuditLog.log(...)  # bestehend, unverändert
    AccountAuditLog.log(
        event_type=AccountAuditLog.EventType.STATION_ASSIGNMENT_REVOKED,
        target_user=instance.user,
        message=f"station={instance.station.callsign or instance.station.name}, "
                f"role={instance.get_role_display()}",
    )
```

Doppel-Emit ist gewollt: pro Subjekt eine Sicht. Das **Station-Detail** zeigt weiter `StationAuditLog` (Subjekt=Station), das **User-Detail** zeigt `AccountAuditLog` (Subjekt=User). Im gemergten globalen Feed taucht das Event als zwei Zeilen auf — bewusst und konsistent mit der Audit-Modell-Trennung.

### 7.3 Per-User-Audit-Queryset

In `UserDetailView.get_context_data`:

```python
account_qs = AccountAuditLog.objects.filter(
    target_user=self.object
).select_related("actor", "region").order_by("-created_at")[:MAX_PER_SOURCE]

sso_qs = SsoAuditLog.objects.filter(
    Q(target_user=self.object) | Q(actor=self.object)
).select_related("actor", "target_user", "application").order_by("-created_at")[:MAX_PER_SOURCE]

merged = (
    [("account", e) for e in account_qs]
    + [("sso", e) for e in sso_qs]
)
merged.sort(key=lambda pair: pair[1].created_at, reverse=True)
context["user_audit_entries"] = merged[:50]  # Top-50 für initiale Anzeige
```

`MAX_PER_SOURCE = 500` — analog zum `MERGE_FEED_CAP` im Global-Feed, aber per-User skaliert kleiner.

### 7.4 Audit-Tab-Template

Wiederverwendet `audit/_audit_table.html`'s Render-Logik (per-row-Kategorie). Da das globale Audit-Template drei Kategorien kennt (`station` / `account` / `sso`) und der Per-User-Feed nur `account` und `sso` enthält, sind die ungenutzten Template-Branches harmlos — kein Schaden.

Das Template bekommt **eine** kleine Erweiterung: ein optionaler `hide_subject`-Flag (Default `False`), der die Subject-Spalte ausblendet. Auf der User-Detail-Seite ist das Subjekt redundant (immer der User selbst).

```html
<div data-tab-panel="audit" hidden>
  <section class="panel">
    <div class="panel-head">
      <div class="panel-title"><span class="dot"></span>{% trans "Audit log" %}</div>
      <span class="t-label">{{ user_audit_entries|length }} {% trans "entries shown" %}</span>
    </div>
    <div class="panel-body flush" data-mobile-cards>
      {% if user_audit_entries %}
        {% include "audit/_audit_table.html" with audit_logs=user_audit_entries hide_subject=True %}
      {% else %}
        <div class="empty">…</div>
      {% endif %}
    </div>
    <div class="panel-foot row-split">
      <span class="t-mono">{% trans "Top 50 events — older entries via global audit log" %}</span>
      {% if request.user.is_admin %}
      <a class="btn btn-sm btn-ghost"
         href="{% url 'audit:list' %}?target_user={{ object.pk }}">
        {% trans "Open in global audit log" %} →
      </a>
      {% endif %}
    </div>
  </section>
</div>
```

`hide_subject=True` schaltet die Subject-Spalte im Template-Partial aus (siehe 7.4 oben). Default-Verhalten für den globalen Feed bleibt unverändert.

### 7.5 Self-Sichtbarkeit

Der Audit-Tab ist sowohl für Admin als auch für Self sichtbar. Damit erfüllt die Detail-Seite gleichzeitig zwei Aufgaben:

- **Admin-Forensik** pro User.
- **DSGVO-Selbsteinsicht** für den User — was wurde über mich erfasst.

Der globale Audit-Log bleibt Admin-only (kein Change an `AuditLogListView`). Der "Open in global audit log →"-Link rendert nur für Admins.

### 7.6 Global-Feed-Filter `target_user`

Der bestehende `apps/audit/views.py:AuditLogFilterMixin.apply_filters` filtert nur `StationAuditLog` (er hat `user`-FK). Für AccountAuditLog erweitern:

```python
def apply_shared_date_filters(self, queryset, params):
    # bestehende date-from/to-Logik
    ...
    target_user = params.get("target_user")
    if target_user:
        # AccountAuditLog hat target_user, SsoAuditLog hat target_user.
        queryset = queryset.filter(target_user_id=target_user)
    return queryset
```

Damit funktioniert der "Open in global audit log →"-Link auch dann, wenn der Admin zwischen User-Detail-Audit-Tab und globalem Feed hin- und herspringt.

---

## 8. Delete-Confirm — Impact-Anzeige

Template: `apps/accounts/templates/accounts/user_confirm_delete.html`.

```
page-head:
├ t-danger "Delete user"
└ page-sub: "Delete user {username}? This cannot be undone."

panel (danger border):
├ panel-body:
│   ├ Identity-dlist: Username, Email, Role, Joined
│   ├ Trennlinie
│   ├ "Mit dem User werden gelöscht:" + dlist:
│   │   ├ Station-Assignments: {n}    "werden revoked, Audit emitted"
│   │   ├ Region-Assignments:  {n}    "werden revoked, Audit emitted"
│   │   ├ SSO Grants:          {n}    "werden revoked, Tokens invalidated"
│   │   ├ Active SSO Sessions: {n}    "werden terminated"
│   │   └ Group Memberships:   {n}    "werden entfernt"
│   └ Wenn User auf irgendeiner Station Admin ist:
│       Warnung mit Liste der betroffenen Stations.
│
└ panel-foot:
    ├ [Delete user] (data-confirm)
    └ [Cancel] → users:user_detail
```

`UserDeleteView.get_context_data` lädt die Counts. Die Cascade-Wirkung (FK `on_delete=CASCADE` für `StationAssignment` / `RegionAssignment`, `SET_NULL` für `target_user` in `AccountAuditLog`) wird im Template als Klartext beschrieben — der Admin trifft eine informierte Entscheidung.

`UserDeleteView.form_valid` emittiert `USER_DELETED` **vor** dem `super().form_valid` (sonst ist der Instance-PK weg und `target_user` wird `NULL` ohne Username-Snapshot):

```python
def form_valid(self, form):
    if self.get_object() == self.request.user:
        messages.error(self.request, _("You cannot delete your own account."))
        return redirect(self.success_url)
    AccountAuditLog.log(
        event_type=AccountAuditLog.EventType.USER_DELETED,
        actor=self.request.user,
        target_user=self.object,  # noch da
        message=f"{self.object.username} <{self.object.email}>",
        ip_address=_get_client_ip(self.request),
    )
    messages.success(self.request, _("User deleted successfully."))
    return super().form_valid(form)
```

Nach dem Cascade-Delete wird `target_user` in der Audit-Row durch `on_delete=SET_NULL` auf `NULL` gesetzt — der Username bleibt aber in `message` lesbar. Das ist konsistent mit dem bestehenden Pattern für gelöschte Regions.

---

## 9. Mobile-Spezifika

Die mobilen Fixes ziehen sich durch alle Templates:

- **Raus**: alle inline `style="max-width:640px"`, `max-width:960px`, `max-width:520px`, `max-width:280px` in den Cards. Layout-Constraints kommen aus dem Tab-Container (volle Breite).
- **Rein**: `grid grid-main` für 2-Spalten-Layouts. CSS collapsed bei `≤ 1024px` automatisch.
- **Forms**: `form-row` für nebeneinanderliegende Felder, collapsed bei `≤ 720px`.
- **Buttons in Cards**: bleiben `btn-sm`, aber Touch-Target via existierender Media-Query `@media (hover: none) and (pointer: coarse)` — falls die Buttons unter 40px fallen, im selben Refactor anheben.
- **Tabs**: bestehender `data-tabs`-JS aus station_detail.html wiederverwendet (kein neues JS).
- **Tags-Card-Buttons** (`+` / `✓`): Padding auf min. 36px Höhe, Gap zwischen Buttons auf 6px für Touch.
- **Tabellen in Cards** (SSO-Sessions, Audit): `data-mobile-cards` Attribut → existierender Card-Reflow.
- **Filter-Bar in List**: `flex-wrap` + max-width per Field; collapsed bei `≤ 640px` zu single-column stack.

Für das Detail-Page-Layout braucht es Mobile-Verifikation an drei Breakpoints: 375px (iPhone SE), 768px (Tablet portrait), 1024px (Tablet landscape). Idealerweise via Playwright-Snapshot — siehe Testing.

---

## 10. Testing

### 10.1 Audit-Emissions

`apps/accounts/tests/test_audit_user_crud.py` (neu):

- USER_CREATED bei `POST /users/create/` mit korrekten Feldern.
- USER_UPDATED bei Identity-Change, nicht bei No-Op.
- USER_ACTIVATED / USER_DEACTIVATED bei is_active-Flip, separat von USER_UPDATED.
- USER_DELETED vor Cascade emittiert, `target_user` nach Cascade `NULL` aber Username in message.
- STATION_ASSIGNMENT_CREATED/_REVOKED via Signal (doppelter Emit zu StationAuditLog).

### 10.2 Detail-View

`apps/accounts/tests/test_user_detail.py` (neu):

- Admin sieht beliebigen User → 200.
- Self sieht eigene Detail-Seite → 200, ohne Admin-Cards.
- Non-Admin sieht andere User → 404.
- Context enthält alle Card-Daten + audit_entries.
- Audit-Entries enthalten Account + SSO, sortiert nach created_at desc.

### 10.3 Permissions an HTMX-Endpoints

Bestehende Tests bleiben grün — kein Change an den HTMX-Views.

### 10.4 Mobile-Snapshot (optional)

Falls Playwright im Projekt vorhanden ist: Detail-, List-, Edit-, Create-, Delete-Page bei Viewports 375 / 768 / 1024 px. Smoke-Tests: kein horizontal-scroll, alle Buttons sichtbar, Tabs umbrechen bzw. scrollen sauber.

### 10.5 Global-Audit-Filter

`apps/audit/tests/test_audit_filter.py`: `?target_user=<pk>` filtert AccountAuditLog und SsoAuditLog korrekt.

---

## 11. Migrations

- `apps/accounts/migrations/0XXX_audit_user_crud_event_types.py` — fügt die 7 neuen TextChoices-Werte zum EventType-Feld hinzu. Reines State-Update, kein Schema-Change.
- Keine weiteren Migrations notwendig.

---

## 12. Implementation-Reihenfolge

Empfohlene Build-Phasen (für den Implementation-Plan):

1. **Audit-Foundation**: EventType-Erweiterung + Migration + Signal-Doppel-Emit + Tests.
2. **DetailView + Template**: Skeleton mit allen 4 Tabs, leere Card-Inhalte; Routing umstellen; Self/Admin-Permission-Tests.
3. **Card-Migration**: Bestehende Cards von Edit-Form in Detail-Tabs umziehen; inline `max-width` raus; Self-Read-only-Variante.
4. **Identity-Form**: Edit + Create mobile, neue Audit-Emit in form_valid.
5. **List + Filter**: Filter-Bar + View-Button + Topology-Count-Spalte.
6. **Delete-Confirm**: Impact-Anzeige + USER_DELETED-Emit.
7. **Audit-Tab + Global-Filter**: Per-User-Feed-Query + `target_user`-Filter im Globalen.
8. **Mobile-Polish + Playwright-Snapshots** (falls verfügbar).

Phasen 1-7 sind sequenziell abhängig (Audit-Foundation muss vor allem CRUD-Emission da sein, DetailView vor Card-Migration). Phase 8 läuft parallel zu Phase 5-7.

---

## 13. Out-of-Scope

Bewusst nicht in diesem Spec:

- **Password-Reset** auf der Detail-Seite. Heute läuft das über Django Admin; eigene Surface ist eigener Spec.
- **User-Bulk-Operationen** (mehrere User auf einmal löschen/promoten). Eigener Spec.
- **Mehrfach-Membership-Levels** oder feinere Permissions. Membership ist heute ein single-value Feld, das bleibt so.
- **Activity-Heatmap** / Login-Frequenz-Visualisierung. Eigener Spec.
- **Audit-Filter-Bar auf dem Per-User-Audit-Tab**. Top-50-Anzeige reicht — wer mehr will, geht in den globalen Feed mit `?target_user=<pk>`.
- **Membership-Level-Selector im Create-Form**. Bleibt 2-Schritt-Flow (APPLICANT auf Create, Promote auf Detail).

---

## 14. Offene Punkte

Keine. Alle Entscheidungen sind in den Sektionen 1-13 festgehalten.
