# User-Domain Redesign — Sub-Spec 1b: Member-Directory

**Status:** Draft, abgeleitet aus dem Master-Overview am 2026-06-12.
**Bogen:** Zweiter Sub-Spec. Folgt dem Overview `2026-06-09-user-domain-redesign-overview.md` und baut auf 1a auf.
**Branch:** `feat/user-domain-1b-directory` (von `main`, nach Merge von 1a).
**Ziel:** Die Browse-Surface des Mitgliederverzeichnisses bauen. UserDetailView mit audience-aware Tabs, UserListView audience-aware mit Filter-Bar, Card-Migration aus user_form.html in user_detail.html, Audit-Tab + Global-Filter, Mobile-Polish für die Browse-Surface.

Nach Merge dieses Specs können Mitglieder andere Mitglieder browsen und ihre eigene Detail-Seite ansehen. Admins haben den ganzen Management-Footprint im Detail-View (Cards). Der Edit-Form (UserUpdateView) ist auf Identity reduziert (Cards sind weg) — aber kommt **ohne** die neuen Profile-Felder im Form selbst (das macht 1c). Die ProfileView bleibt der bestehende 4-Felder-Stub bis 1c.

---

## 1. Kontext

Voraussetzung: **1a ist gemergt.** Damit verfügbar:

- `apps/accounts/visibility.py` mit `audience_for`, `directory_visible_fields`, `user_can_view_directory`.
- User-Modell hat die 10 neuen Felder (lat/lon/locator/avatar/bio/qth_name/qrz_url/address/phone/is_directory_visible).
- AccountAuditLog kennt die neuen EventTypes inkl. STATION_ASSIGNMENT_CREATED/REVOKED.
- StationAssignment-Signal emittiert AccountAuditLog-Eintrag pro Subject-User.

Dieser Sub-Spec ergänzt:

- Neue `UserDetailView` als audience-aware DetailView mit Tabs.
- UserListView audience-aware (Member-Sicht filtert Applicants).
- Card-Migration: bestehende HTMX-Cards von user_form.html wandern in user_detail.html.
- Audit-Tab im DetailView mit Per-User-Audit-Feed.
- `target_user`-Filter im globalen Audit-Log.
- Mobile-Polish: inline `max-width:640px/960px` raus, `grid grid-main`, `form-row`, Touch-Targets.

---

## 2. Permission-Modell für Detail- und List-View

### 2.1 UserDetailView

```python
class UserDetailView(LoginRequiredMixin, DetailView):
    model = User
    template_name = "accounts/user_detail.html"
    context_object_name = "object"

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        from .visibility import audience_for
        aud = audience_for(self.request.user, obj)
        if aud is None:
            raise Http404("User not found")
        self._audience = aud
        return obj

    def get_context_data(self, **kwargs):
        from .visibility import Audience, directory_visible_fields
        ctx = super().get_context_data(**kwargs)
        aud = self._audience
        ctx["audience"] = aud.value
        ctx["is_admin_view"] = aud == Audience.ADMIN
        ctx["is_self_view"] = aud in (Audience.SELF, Audience.APPLICANT)
        ctx["is_member_view"] = aud == Audience.MEMBER
        ctx["visible_fields"] = directory_visible_fields(self.request.user, self.object)

        # Card-Daten nur für Admin laden (Member/Self brauchen weniger).
        if aud == Audience.ADMIN:
            ctx.update(self._admin_context_data())
        elif aud in (Audience.SELF, Audience.APPLICANT):
            ctx.update(self._self_context_data())

        # Assignments für Pills (alle Audiences außer Member-bei-invisible-target).
        if "region_assignments" in ctx["visible_fields"]:
            ctx["region_assignment_pills"] = (
                self.object.region_assignments.select_related("region")
            )
        if "station_assignments" in ctx["visible_fields"]:
            ctx["station_assignment_pills"] = (
                self.object.station_assignments.select_related("station")
            )

        # Audit-Tab nur für Self + Admin.
        if aud in (Audience.ADMIN, Audience.SELF, Audience.APPLICANT):
            ctx["user_audit_entries"] = self._build_user_audit(self.object)

        return ctx
```

- 404 statt 403 vermeidet User-Existenz-Leak.
- `_admin_context_data` lädt alles wie heute im UserUpdateView (existing_region_assignments + available_regions, existing_station_assignments + all_stations, app_grants_list, user_sessions, tag_entries, membership_level_choices). Wandert 1:1 aus `apps/accounts/views.py:UserUpdateView.get_context_data`.
- `_self_context_data` lädt nur eigene SSO-Sessions (für Self-Revoke).
- `_build_user_audit` siehe Sektion 5.

### 2.2 UserListView

```python
class UserListView(LoginRequiredMixin, ListView):
    model = User
    template_name = "accounts/user_list.html"
    context_object_name = "users"
    paginate_by = 25

    def dispatch(self, request, *args, **kwargs):
        from .visibility import user_can_view_directory
        if not user_can_view_directory(request.user):
            raise Http404()
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        qs = User.objects.order_by("username")
        if not self.request.user.is_admin:
            qs = qs.exclude(membership_level=User.MembershipLevel.APPLICANT)
        # Admin sieht standardmäßig alle inkl. Applicants — kein Default-Filter.

        q = self.request.GET.get("q", "").strip()
        if q:
            qs = qs.filter(
                Q(username__icontains=q)
                | Q(email__icontains=q)
                | Q(first_name__icontains=q)
                | Q(last_name__icontains=q)
            )

        role = self.request.GET.get("role", "")
        valid_roles = {x.value for x in User.MembershipLevel}
        if not self.request.user.is_admin:
            valid_roles -= {User.MembershipLevel.APPLICANT.value}
        if role in valid_roles:
            qs = qs.filter(membership_level=role)

        if self.request.user.is_admin:
            status = self.request.GET.get("status", "")
            if status == "active":
                qs = qs.filter(is_active=True)
            elif status == "inactive":
                qs = qs.filter(is_active=False)

        return qs.prefetch_related(
            "region_assignments__region",
            "station_assignments__station",
        )
```

### 2.3 Success-Redirects

| View | Erfolgs-Redirect |
|---|---|
| `UserCreateView` | `users:user_detail` (war bisher `users:user_list`) |
| `UserUpdateView` | `users:user_detail` (war bisher `users:user_list`) |
| `UserDeleteView` | `users:user_list` (unverändert) |
| Alle HTMX-Endpoints | unverändert (200 JSON, Client reloaded die Seite) |

### 2.4 UserUpdateView — Card-Bereinigung

Bisher hat `UserUpdateView.get_context_data` 6 Card-Datensätze geladen (Membership, Region, Station, SSO-Grants, SSO-Sessions, SSO-Tags). Die werden in 1b komplett herausgenommen:

```python
class UserUpdateView(AdminRequiredMixin, UpdateView):
    model = User
    template_name = "accounts/user_form.html"
    form_class = UserChangeForm   # unverändert bis 1c
    success_url = ...             # reverse_lazy mit pk → user_detail

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["form_title"] = _("Edit User")
        return ctx
```

Das user_form.html-Template verliert in 1b alle Card-Includes. UserUpdateView wird ein reines Identity-Form ohne Cards. Identity-Form-Inhalt selbst (also welche Felder darin sind) ist **unverändert** — die Erweiterung um die neuen Profil-Felder kommt in 1c.

---

## 3. Detail-Page — Layout

Template: `apps/accounts/templates/accounts/user_detail.html` (neu).

Strukturell parallel zu `stations/station_detail.html`.

### 3.1 Page-Head

```
page-head
├ page-eyebrow:
│   ├ Admin / Self / Applicant-View:  "User · #{{ object.pk|stringformat:'03d' }}"
│   └ Member-View:                    "Verein · Mitglied"
├ page-title:
│   ├ avatar (sb-avatar 48px, mit Image wenn object.avatar gesetzt, sonst Buchstabe)
│   ├ callsign (= username) — primär
│   └ subtitle:
│       ├ full name (wenn "first_name" + "last_name" in visible_fields und gesetzt)
│       └ sonst muted "—"
├ pills-row (audience-aware):
│   ├ membership-level pill              (immer)
│   ├ is_active "INACTIVE" pill          (nur Admin oder Self, und nur wenn nicht aktiv)
│   ├ language pill                      (nur Self + Admin)
│   └ qth_name pill (wenn gesetzt + sichtbar)
└ page-head-actions:
    ├ Admin-view (nicht self):
    │   ├ [Edit identity]   → users:user_edit
    │   └ [Delete]          → users:user_delete
    ├ Self / Applicant-view (auch wenn Self == Admin):
    │   └ [Edit profile]    → accounts:profile
    └ Member-view:
        └ (keine Actions)
```

Avatar im Page-Head ist klickbar nur für Admin (öffnet das Original-File). Für Member/Self ist es ein reines Display.

### 3.2 Summary-Bar

Audience-gefiltert. Member sieht nur Felder, die in `visible_fields` liegen.

Mögliche Items:

| Item | Self / Admin | Member |
|---|---|---|
| Email | ✓ | ✓ (wenn in visible_fields) |
| Phone | ✓ | ✗ |
| Locator | ✓ | ✓ |
| QTH | ✓ | ✓ |
| Date joined / Mitglied seit (Jahr) | ✓ | ✓ |
| Last login | ✓ (Admin only) | ✗ |
| # Region-Assignments | ✓ | ✓ |
| # Station-Assignments | ✓ | ✓ |
| # Active SSO Sessions | ✓ (Admin), ✓ (Self) | ✗ |

Bei Member-Sicht auf `is_directory_visible=False`-Profile: Summary-Bar ist leer oder zeigt nur „Mitglied seit YYYY".

### 3.3 Tabs

Wiederverwendet wird der `data-tabs`-Container und das bestehende JS aus `station_detail.html`. Tabs werden conditional je Audience angezeigt:

| Tab | Admin | Self | Member | Applicant (self) |
|---|:---:|:---:|:---:|:---:|
| Overview | ✓ | ✓ | ✓ | ✓ |
| Rollen & Topologie | ✓ (edit) | ✓ (readonly) | ✓ (readonly, nur Pills) | ✓ (readonly) |
| Single Sign-On | ✓ | ✓ (eigene Sessions) | ✗ | ✓ (eigene Sessions) |
| Audit | ✓ | ✓ (eigene) | ✗ | ✓ (eigene) |

Member sieht 2 Tabs (Overview + Rollen & Topologie). Self/Applicant sehen 4 Tabs.

#### Overview-Tab

`grid grid-main`. Audience-abhängig:

**Admin / Self / Applicant:**
- Linke Spalte: Identity-Panel mit dlist über alle in `visible_fields`. Reihenfolge: Callsign (groß) → Name → Bio → Email → Phone → Adresse → QTH → Locator → QRZ-URL → Language → Date joined → Last login → is_active.
- Rechte Spalte (`aside.stack-gap-14`):
  - Avatar-Preview-Panel (vergrößertes Avatar oben, 128px).
  - Status-Snapshot (Counts der Topology-Rollen).
  - Zuletzt im Audit-Log (Mini-Feed, 3-5 Einträge) — nur Admin/Self.
  - lat/lon-Numerisch + Geocoding-Status (Admin-Debug-only).

**Member:**
- Linke Spalte: Identity-Panel reduziert. Reihenfolge: Callsign → Name → Bio → Email → QTH → Locator → QRZ-URL → „Mitglied seit YYYY". Phone/Adresse/Language/Last-login fehlen komplett.
- Rechte Spalte: nur Avatar-Preview-Panel + Status-Snapshot.

**Member auf `is_directory_visible=False`-Profil:**
- Empty-State-Panel: „Dieses Mitglied hat sein Profil im Verzeichnis verborgen." + Avatar + Membership-Pill.
- Keine weiteren Tabs.

#### Rollen-&-Topologie-Tab

**Admin** (mit Edit-Buttons): Membership-Card oben volle Breite, darunter Region-Assignments-Card und Station-Assignments-Card side-by-side in `grid grid-main`.

**Self / Applicant / Member** (read-only, gleiches Template via `readonly=True`-Flag): Pills ohne Add/Revoke-Forms.

Die Read-only-Variante der Cards wird durch ein `readonly`-Flag im Template-Kontext gesteuert: bestehende Cards bekommen ein `{% if readonly %}…{% else %}…{% endif %}` um die Add/Revoke-Forms.

#### Single-Sign-On-Tab

**Admin:** Drei Cards untereinander (Grants, Sessions, Tags), wie heute. HTMX-Targets bleiben.

**Self / Applicant:** Nur die Sessions-Card, audience-flagged als „nur eigene Sessions". Revoke der eigenen Sessions ist erlaubt (gleicher Endpoint).

**Member:** Tab ausgeblendet.

#### Audit-Tab

Siehe Sektion 5.

---

## 4. List-Page — Layout

Template: `apps/accounts/templates/accounts/user_list.html` (Refactor).

### 4.1 Page-Head + Filter

```
page-head
├ page-eyebrow:
│   ├ Admin:  "Administration · People"
│   └ Member: "Verein · Mitgliederverzeichnis"
├ page-title:
│   ├ Admin:  "Users"
│   └ Member: "Mitglieder"
├ page-sub:
│   ├ Admin:  "Add, view, edit, and remove member, staff, and admin accounts."
│   └ Member: "Vereinsmitglieder mit Kontaktdaten und Funktionen."
└ page-head-actions:
    └ Admin-only: [+ New user]

filter-bar:
├ input[name=q]   — Callsign/Email/Full-name (icontains, GET-Param)
├ select[name=role]:
│   ├ Member-Sicht:  Alle | Mitglied | Staff | Admin
│   └ Admin-Sicht:   Alle | Bewerber | Mitglied | Staff | Admin
├ Admin-only:  select[name=status]  — Alle | Aktiv | Inaktiv
└ [Reset filters] (Link, wenn Params gesetzt)
```

Pagination bleibt bei 25/Seite.

### 4.2 Tabelle

Spalten audience-aware:

| Spalte | Admin | Member | Mobile |
|---|:---:|:---:|---|
| **User** | Avatar + Callsign + Name (data-primary) | Avatar + Callsign + Name (data-primary) | Card-primary |
| Role | Membership-Pill | Membership-Pill | data-label |
| Email | mono | mono (wenn directory-visible) | data-label |
| QTH | — | mono (wenn gesetzt) | data-label |
| Topology | "{n_region}·{n_station}" mini-pill | "{n_region}·{n_station}" mini-pill | data-label |
| Last login | relative time | — | data-label |
| Active | is_active pill (wenn !active) | — | data-label |
| Actions | [View] [Edit] [Delete] | [View] | actions-Klasse |

Bei Member-Sicht auf User mit `is_directory_visible=False`: Row zeigt nur Avatar + Callsign + Membership-Pill + [View]. Andere Spalten leer/em-dash.

**[View]** ist der primäre Button. Klick auf den Primary-Cell-Bereich navigiert zur Detail-Seite — implementiert per `<a href="{% url 'accounts:user_detail' u.pk %}" style="color:inherit;text-decoration:none;">…</a>`-Wrap um den Inhalt der ersten Zelle. Pattern aus `stations/_station_table.html` Zeile 23.

Self-Row (request.user) hat keinen Delete-Button.

---

## 5. Audit-Tab + Global-Filter

### 5.1 Per-User-Audit-Queryset

`UserDetailView._build_user_audit`:

```python
MAX_PER_SOURCE = 500

def _build_user_audit(self, target_user):
    from apps.sso.models import SsoAuditLog
    account_qs = AccountAuditLog.objects.filter(
        target_user=target_user
    ).select_related("actor", "region").order_by("-created_at")[:MAX_PER_SOURCE]

    sso_qs = SsoAuditLog.objects.filter(
        Q(target_user=target_user) | Q(actor=target_user)
    ).select_related("actor", "target_user", "application").order_by("-created_at")[:MAX_PER_SOURCE]

    merged = (
        [("account", e) for e in account_qs]
        + [("sso", e) for e in sso_qs]
    )
    merged.sort(key=lambda pair: pair[1].created_at, reverse=True)
    return merged[:50]
```

`StationAuditLog` wird nicht direkt referenziert — die User-Subject-Sicht der Station-Assignments läuft komplett über `AccountAuditLog` (aus 1a's Doppel-Emit).

### 5.2 Audit-Tab-Template

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

### 5.3 `audit/_audit_table.html` Erweiterung

Optionaler `hide_subject=False`-Flag (Default `False`). Wenn `True`, wird die Subject-Spalte ausgeblendet. Default-Verhalten für den globalen Feed bleibt unverändert.

### 5.4 Global-Audit-Filter — `target_user`

`apps/audit/views.py:AuditLogFilterMixin.apply_shared_date_filters` wird erweitert:

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

Damit funktioniert der „Open in global audit log →"-Link mit vorgefüllten Filter-Params.

---

## 6. Card-Migration

Die 6 HTMX-Cards wandern in den DetailView. Templates bleiben fast unverändert — sie verlieren nur den inline `max-width:640px`/`max-width:960px`. Audience-aware Rendering kommt aus dem `readonly`-Flag im Template-Kontext.

### 6.1 Cards in user_detail.html

```django
{# Tab: Rollen & Topologie #}
{% if is_admin_view %}
  {% include "accounts/_membership_card.html" with readonly=False %}
  <div class="grid grid-main">
    {% include "accounts/_region_assignments_card.html" with readonly=False %}
    {% include "accounts/_station_assignments_card.html" with readonly=False %}
  </div>
{% elif is_self_view or is_member_view %}
  {% include "accounts/_membership_card.html" with readonly=True %}
  <div class="grid grid-main">
    {% include "accounts/_region_assignments_card.html" with readonly=True %}
    {% include "accounts/_station_assignments_card.html" with readonly=True %}
  </div>
{% endif %}

{# Tab: SSO #}
{% if is_admin_view %}
  {% include "sso/_app_grants_card.html" with target_user=object applications=app_grants_list %}
  {% include "sso/_sessions_card.html" with target_user=object sessions=user_sessions %}
  {% include "sso/_tags_card.html" with target_user=object tag_entries=tag_entries %}
{% elif is_self_view %}
  {% include "sso/_sessions_card.html" with target_user=object sessions=user_sessions readonly_self=True %}
{% endif %}
```

### 6.2 Card-Anpassungen

Jede Card bekommt ein `{% if readonly %}…{% else %}…{% endif %}` um die Add/Revoke-Forms:

```django
{# accounts/_region_assignments_card.html #}
{% if existing_region_assignments %}
  <ul class="stack-gap-2" ...>
    {% for ra in existing_region_assignments %}
      <li class="row-gap-8" ...>
        <span class="pill pill-muted">{{ ra.region.name }}</span>
        {% if not readonly %}
        <form hx-post="..." ...>...</form>
        {% endif %}
      </li>
    {% endfor %}
  </ul>
{% else %}
  <p class="t-muted">{% trans "No Region-Manager assignments yet." %}</p>
{% endif %}

{% if not readonly and available_regions %}
  <form hx-post="..." ...>
    {# Add-Form #}
  </form>
{% endif %}
```

Analog für `_station_assignments_card.html` und `_membership_card.html`.

`sso/_sessions_card.html` bekommt einen `readonly_self=True`-Flag — Sessions werden gerendert, aber nur eigene SSO-Sessions des request.user (User darf eigene revoken).

### 6.3 user_form.html-Bereinigung

In `apps/accounts/templates/accounts/user_form.html` werden die 6 `{% include %}`-Statements am Ende entfernt:

```diff
- {% if request.user.is_admin and object and object.pk != request.user.pk %}
-   {% include "accounts/_membership_card.html" %}
- {% endif %}
- ...
- {% include "sso/_tags_card.html" with target_user=object tag_entries=tag_entries %}
- {% endif %}
```

UserUpdateView's `get_context_data` braucht die Card-Daten nicht mehr.

---

## 7. Mobile-Spezifika

Während dieses Sub-Specs werden die Templates für DetailView und ListView mobile-tauglich gemacht:

- **Raus**: inline `style="max-width:640px"`, `max-width:960px` in den migrierten Cards (Sektion 6).
- **Rein**: `grid grid-main` für 2-Spalten-Layouts. CSS collapsed bei `≤ 1024px` automatisch.
- **Tabs**: bestehender `data-tabs`-JS aus station_detail.html wiederverwendet.
- **Tabellen in Cards** (SSO-Sessions, Audit): `data-mobile-cards`-Attribut → existierender Card-Reflow.
- **Filter-Bar in List**: `flex-wrap` + max-width per Field; collapsed bei `≤ 640px` zu single-column stack.
- **Tags-Card-Buttons** (`+` / `✓`): Padding auf min. 36px Höhe für Touch.

Mobile-Verifikation an drei Breakpoints: 375px (iPhone SE), 768px (Tablet portrait), 1024px (Tablet landscape). Idealerweise via Playwright-Snapshot — siehe Testing.

UI-Edits müssen durch den `pixel`-Subagent gehen, der `Skill("frontend-design")` invoken muss (CLAUDE.md-Konvention).

---

## 8. Testing

### 8.1 UserDetailView

`apps/accounts/tests/test_user_detail_view.py` (neu):

- Admin sieht beliebigen User → 200.
- Self sieht eigene Detail-Seite → 200, ohne Admin-Cards.
- Member sieht anderen Member (directory-visible) → 200, Public-Felder visible, Address/Phone NICHT im Content.
- Member sieht anderen Member (directory-invisible) → 200, nur Username + Membership-Pill + Avatar.
- Member sieht Applicant → 404.
- Applicant sieht eigene Detail-Seite → 200.
- Applicant sieht anderen User → 404.
- Context `visible_fields` enthält erwartete Field-Sets pro Audience.

### 8.2 UserListView

`apps/accounts/tests/test_user_list_view.py` (neu):

- Applicant → 404.
- Admin sieht alle inkl. Applicants by default.
- Member sieht alle ohne Applicants.
- Member sieht `is_directory_visible=False`-User reduziert dargestellt.
- Search-Filter `?q=` matched username/email/name.
- Role-Filter `?role=member`.
- Admin-only Status-Filter `?status=inactive` für Member 400/ignored (graceful).
- Pagination greift bei >25 Usern.

### 8.3 Card-Migration

`apps/accounts/tests/test_card_migration.py` (neu):

- user_detail.html rendert mit allen Cards für Admin.
- user_form.html rendert nur das Form (keine Cards) für Admin.
- Self-View auf user_detail.html zeigt Cards mit `readonly=True` (keine Add/Revoke-Forms im HTML).
- Member-View auf user_detail.html zeigt nur Pills (Region/Station-Assignments) ohne Add/Revoke-Forms.
- HTMX-Endpoints (membership_set, region_assignment_create/revoke, etc.) funktionieren weiterhin (Smoke-Test pro Endpoint).

### 8.4 Audit-Tab + Global-Filter

`apps/accounts/tests/test_audit_tab.py` (neu):

- `_build_user_audit` returnt gemergte Account + SSO-Einträge, sortiert nach `created_at` desc.
- Audit-Tab im DetailView rendert die Einträge für Self + Admin.
- Member-View hat keinen Audit-Tab im HTML.
- Audit-Tab leer → Empty-State.
- Subject-Spalte ist via `hide_subject=True` ausgeblendet (assert HTML).

`apps/audit/tests/test_audit_filter.py` (existiert wahrscheinlich, ergänzen):

- `?target_user=<pk>` filtert AccountAuditLog korrekt.
- `?target_user=<pk>` filtert SsoAuditLog korrekt (target_user oder actor).

### 8.5 Bestehende Tests

`apps/accounts/tests/...` und `apps/sso/tests/...`: alle bestehenden Tests bleiben grün. HTMX-Endpoints werden nicht refactored.

### 8.6 Mobile-Snapshot (optional)

Falls Playwright im Projekt vorhanden ist: User-Detail-, User-List-Page bei Viewports 375 / 768 / 1024 px. Smoke-Tests: kein horizontal-scroll, alle Buttons sichtbar, Tabs umbrechen bzw. scrollen sauber.

---

## 9. Implementation-Reihenfolge

**Round-1 — Build** (subagent-driven-development):

1. **Phase 1: UserDetailView + Permission + Datenladung** (Backend).
   Subagent: `gateway`.
   - View, URL, leeres Template-Skeleton.
   - `_admin_context_data` aus UserUpdateView übernommen.
   - Permission-Tests aus 8.1.
   - Dauer: ~2-3h.

2. **Phase 2: user_detail.html Layout + Tabs** (Template).
   Subagent: `pixel` mit `Skill("frontend-design")` invoked.
   - Page-Head, Summary-Bar, Tabs-Struktur.
   - Overview-Tab pro Audience.
   - Avatar-Display (mit Fallback Buchstabe).
   - Mobile-Polish.
   - Dauer: ~3-4h.

3. **Phase 3: Card-Migration** (Template).
   Subagent: `pixel` mit `Skill("frontend-design")` invoked.
   - Cards mit `readonly`-Flag erweitern.
   - Cards in user_detail.html einhängen.
   - Cards aus user_form.html entfernen.
   - UserUpdateView.get_context_data simplification.
   - inline `max-width:640px` raus.
   - Dauer: ~2-3h.

4. **Phase 4: UserListView audience-aware + Filter-Bar** (Backend + Template).
   Subagents: `gateway` (Backend) + `pixel` (Template).
   - Permission-Dispatch, Filter-Logik.
   - user_list.html Refactor mit Audience-aware Spalten.
   - Mobile-Card-Reflow.
   - Dauer: ~2-3h.

5. **Phase 5: Audit-Tab + Global-Filter** (Backend + Template).
   Subagents: `gateway` (Audit-Query, Global-Filter) + `pixel` (Tab-Template).
   - `_build_user_audit`-Methode.
   - Audit-Tab-Panel im user_detail.html.
   - `hide_subject`-Flag in `_audit_table.html`.
   - `target_user`-Filter in `apps/audit/views.py`.
   - Dauer: ~2-3h.

**Round-1.5: code-simplifier**.

**Round-2 — Watcher** (parallel):

- `audit` auf Backend-Files: View-Conventions, Visibility-Aufrufe konsistent.
- `guard` auf Permission-Dispatcher: Authorization-Edge-Cases, kein Existenz-Leak.
- `probe` auf E2E: bestehende Tests grün, neue Routes funktionieren, HTMX-Endpoints unverändert.

**Round-2.5: probe** Test-Writer für Gaps.

**Round-3 — `pr-review-toolkit:review-pr`**.

---

## 10. Out-of-Scope (für 1b)

- Neue Profile-Felder im Form (UserUpdateView bleibt mit den existierenden Identity-Feldern) → 1c.
- ProfileView-Refactor → 1c.
- Password-Change → 1c.
- Onboarding-Empty-State → 1c.
- USER_*-Audit-Emissionen in form_valid → 1c (in den Edit-Views).
- UserCreateView und UserDeleteView-Refactor → 1c.

---

## 11. Offene Punkte

Keine. Alle Entscheidungen sind im Master-Overview (Sektion 8) festgehalten.
