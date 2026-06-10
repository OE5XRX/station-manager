# User-Domain-Redesign — Mitgliederverzeichnis, Detail, Edit, Create, Delete, Audit — Design

**Status:** Draft, brainstormed 2026-06-09 (initial) + 2026-06-09 (extended).
**Ziel:** Die User-Verwaltung im station-manager strukturell überarbeiten und gleichzeitig zu einem **Mitgliederverzeichnis** öffnen. Heute besteht sie aus einer Admin-only Liste und einem überladenen Edit-Form, das de facto die zentrale User-Management-Surface ist — ohne dass das aus dem URL- oder Template-Namen ersichtlich wäre. Mitglieder selbst haben keinen Einblick in andere Mitglieder.

Dieser Spec führt eine echte Detail-Seite ein, reduziert das Edit-Form auf Identity, baut List/Create/Delete-Templates mobil-tauglich um, schließt die Audit-Lücken bei den Identity-CRUD-Operationen — und macht die List- + Detail-Seite zu einem **Mitgliederverzeichnis**, das Vereins-Mitglieder (Membership-Level ≥ MEMBER) verwenden können, um sich gegenseitig zu kontaktieren. Bewerber (APPLICANT) sind weiterhin Außenstehende: sie sehen nur sich selbst.

Der Redesign folgt dem etablierten Pattern aus `station_detail.html` (Tabs + Cards + Summary-Bar) und passt das User-Domain konzeptionell an die Stations-Domain an: List → Detail → Edit-Form sind drei klar getrennte Surfaces. Neu kommt eine **vier-Audience-Berechtigungslogik** hinzu: Admin / Self / Member / Applicant — jede sieht eine andere Sichtbarkeitsstufe derselben Templates.

Parallel zum Verzeichnis-Charakter wird das User-Modell um Kontakt- und Standortdaten erweitert (Adresse, Telefon, Locator + lat/lon, Avatar, Bio, QTH-Name, QRZ-URL). Lat/lon wird aus der Adresse via OpenStreetMap-Nominatim geocoded; der Maidenhead-Locator wird aus lat/lon berechnet. Damit ist das Fundament für eine spätere User-und-Station-Map (out-of-scope hier) gelegt.

---

## 1. Kontext und Begriffe

Die User-Verwaltung lebt in `apps/accounts/`. Ein User hat heute:

- Identity-Felder (Username, Email, First/Last name, Language, is_active).
- Eine **Membership-Level**-Rolle (`applicant` / `member` / `staff` / `admin`) — gesetzt per HTMX-Card auf dem Edit-Form, geloggt in `AccountAuditLog`. Konvention: `username` trägt im Verein das **Rufzeichen** (Callsign).
- **Topology-Assignments**:
  - `RegionAssignment` mit Rolle `manager` — pro Region maximal 1 pro User.
  - `StationAssignment` mit Rolle `admin` (pro Station max. 1 global) oder `maintainer`.
- **SSO-Bezüge**: `AppGrant` (App-spezifischer Zugriff), `TokenSession` (laufende OIDC-Sessions), Django `auth.Group`-Mitgliedschaften (= Tags).

Die HTMX-Endpoints für Membership-Set, Region/Station-Assignment-Create/Revoke und SSO Grant-Toggle / Session-Revoke / Tag-Toggle sind etabliert und bleiben in diesem Redesign **unverändert**. Was sich ändert, ist die Render-Surface, auf der ihre Karten leben.

### 1.1 Neu: User-Modell-Felder (Schema-Erweiterung)

Dieser Spec ergänzt im `User`-Modell:

| Feld | Typ | Zweck |
|---|---|---|
| `bio` | `TextField(max_length=500, blank=True)` | Selbst-Description, Conversation-Starter im Verein |
| `avatar` | `ImageField(upload_to="avatars/", null=True, blank=True)` | Profilbild, Fallback Buchstaben-Avatar |
| `qth_name` | `CharField(max_length=128, blank=True)` | Klassischer HAM-Standortname (z.B. „Linz", „Klausriegler Berg") |
| `qrz_url` | `URLField(max_length=200, blank=True)` | Convenience-Link zum öffentlichen QRZ-Profil |
| `address` | `TextField(blank=True)` | Postadresse als Freitext (Straße, PLZ, Ort, Land) |
| `phone` | `CharField(max_length=32, blank=True)` | Telefon im internationalen Format, frei |
| `latitude` | `DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)` | Aus Geocoding der Adresse |
| `longitude` | `DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)` | Aus Geocoding der Adresse |
| `locator` | `CharField(max_length=6, blank=True)` | Maidenhead 6-char, aus lat/lon berechnet, überschreibbar |
| `is_directory_visible` | `BooleanField(default=True)` | Master-Switch: erlaubt anderen Mitgliedern, das Profil zu sehen |

Schema-Details (Validators, upload_to-Logik, Locator-Format) siehe Sektion 11.

### 1.2 Neu: Audience-Modell (vier Sichtbarkeitsstufen)

Der zentrale konzeptionelle Shift dieses Specs:

| Audience | Sieht List | Sieht Detail | Sichtbare Felder im Detail |
|---|---|---|---|
| **Admin** | ✓ inkl. Applicants | ✓ alle User | alle Felder |
| **Self** | ✓ ohne Applicants | ✓ eigene Detail-Seite | alle eigenen Felder (read-only) |
| **Member** (≥ MEMBER) | ✓ ohne Applicants | ✓ andere Mitglieder | nur „öffentliche" Felder (siehe Sektion 3.2) |
| **Applicant** | ✗ | ✓ nur eigene Detail-Seite | alle eigenen Felder (read-only) |

Ein Self-View, der `is_directory_visible=False` gesetzt hat, ist für andere Mitglieder reduziert auf Callsign + Membership-Pill — Admins sehen ihn weiterhin voll.

**Audit-Modelle**, die User-relevant sind:

- `AccountAuditLog` (apps/accounts/models.py) — System-weites Account-Log: Membership-Promote/-Demote, Region-Assignment-Create/-Revoke, Region-CRUD.
- `SsoAuditLog` (apps/sso/models.py) — alles SSO/OIDC: Login, Token, Grant-Toggle, Session-Revoke, App-Policy-Change, Group-Membership-Change.
- `StationAuditLog` (apps/stations/models.py) — pro Station: u.a. Station-Assignment-Create/-Revoke (mit Station als Subjekt, User in der Message).

Was heute **nicht** geloggt wird (Lücke, die dieser Spec schließt):

- Identity-CRUD: `UserCreateView`, `UserUpdateView`, `UserDeleteView` emittieren nichts.
- Station-Assignment-Bezug auf User-Seite: `StationAuditLog` hat den User nur in der Message; auf der User-Detail-Audit-Sicht ist das so nicht filterbar.
- Neue Profile-Felder (Avatar, Bio, Adresse, Telefon, QTH, QRZ-URL, Locator, address-derived lat/lon, is_directory_visible) — werden hier vom Start an in den `USER_UPDATED`-Tracked-Set aufgenommen.

---

## 2. Architektur — Überblick

**Schema-Erweiterung** (apps/accounts/models.py):

- 10 neue Felder am `User`-Modell (siehe Sektion 1.1 + 11).
- 7 neue EventTypes in `AccountAuditLog.EventType` (siehe Sektion 8).

**Neue Route + View**:

- `GET users/<pk>/` → `UserDetailView` — DetailView mit Tabs, lädt alle Management-Daten (war bisher im `UserUpdateView`). Implementiert die vier-Audience-Logik (siehe Sektion 3).

**Geänderte Routes**:

- `users/` → `UserListView` ist jetzt für Members offen (war Admin-only). Member-Sicht filtert Applicants raus, Admin sieht alle.
- `users/<pk>/edit/` bleibt Admin-only, aber `UserUpdateView` verliert seinen fetten `get_context_data`-Block. Form expandiert auf alle neuen Felder. Success-Redirect zeigt auf die Detail-Seite.
- `users/<pk>/delete/` zeigt Impact-Details vor dem Delete. Success-Redirect zur Liste.
- `users/create/` Success-Redirect zur Detail-Seite des neu erstellten Users.
- `accounts/profile/` → `ProfileView` wird der Self-Service-Edit-Ort für alle neuen Felder (inkl. Adresse, Telefon, Avatar, Bio, QTH, QRZ-URL, `is_directory_visible`-Toggle). Identity-Felder (Username, Email, First/Last, Language) bleiben — Membership-Level und is_active bleiben Admin-Domain. Das Template enthält mehrere getrennte Forms (Identity / Profil / Adresse / Passwort) mit eigenen Action-URLs.
- `accounts/profile/password/` → `ProfilePasswordChangeView` — neuer POST-only Endpoint für Self-Service Password-Change (Sektion 6.2.1). Re-Auth via `current_password`-Feld, `update_session_auth_hash` damit User eingeloggt bleibt.

**Neue Helpers** (apps/accounts/visibility.py):

- `user_can_view_directory(viewer)` — gate für UserListView und UserDetailView.
- `directory_visible_fields(viewer, target)` — gibt das Feld-Set zurück, das `viewer` von `target` sehen darf. Single source of truth für die ganze Sichtbarkeitslogik.

**Neuer Service** (apps/accounts/geocoding.py):

- `geocode_address(address: str) -> (lat, lon) | None` — Nominatim-Anfrage mit Rate-Limit-Pause und User-Agent-Header.
- `lat_lon_to_locator(lat: float, lon: float, precision: int = 6) -> str` — pure Python, Maidenhead-Berechnung.

**Emission**:

- `UserCreateView.form_valid` → `USER_CREATED`.
- `UserUpdateView.form_valid` → `USER_UPDATED` (Diff-Liste in Message, jetzt alle profile + identity-Felder) + ggf. `USER_ACTIVATED` / `USER_DEACTIVATED`.
- `ProfileView.form_valid` → `USER_UPDATED` mit `actor=request.user, target_user=request.user` (Self-Edit). Ein eigenes EventType nicht nötig — die Konstellation `actor == target_user` ist eindeutig identifizierbar.
- `UserDeleteView.form_valid` → `USER_DELETED` (vor `super().delete()`).
- `apps/stations/signals._on_station_assignment_save/_delete` → zusätzlich `AccountAuditLog.STATION_ASSIGNMENT_*` mit `target_user=instance.user`.

**Neue Templates**:

- `accounts/user_detail.html` — die zentrale Surface.
- `accounts/_identity_overview_card.html`, `accounts/_user_audit_card.html`, `accounts/_profile_card_public.html` — Partials für den Detail-View.

**Geänderte Templates**:

- `accounts/user_list.html` — Filter-Bar + View-Action + audience-aware Spalten.
- `accounts/user_form.html` — schlankes Identity-Form (Admin-Mode), erweitert um die neuen Felder, mobile-freundlich, ohne Sub-Cards.
- `accounts/user_confirm_delete.html` — Impact-Anzeige.
- `accounts/profile.html` — erweitert auf alle Self-edit-baren Felder (Avatar, Bio, Adresse, Telefon, QTH, QRZ-URL, `is_directory_visible`).

**Unveränderte Templates** (werden nur in die Detail-Tabs eingehängt):

- `accounts/_membership_card.html`, `accounts/_region_assignments_card.html`, `accounts/_station_assignments_card.html`.
- `sso/_app_grants_card.html`, `sso/_sessions_card.html`, `sso/_tags_card.html`.

Inline `max-width:640px` und `max-width:960px` in diesen Cards wird entfernt — die Breite kommt vom Tab-Container.

---

## 3. Audience-Modell, Permissions, Visibility

### 3.1 Vier Audiences

Statt eines binären Admin/Self-Splits hat dieser Spec vier Audiences mit gestaffelter Sicht. In `apps/accounts/visibility.py` lebt die zentrale Berechnungslogik:

```python
class Audience(enum.Enum):
    ADMIN = "admin"
    SELF = "self"
    MEMBER = "member"
    APPLICANT = "applicant"  # für sich selbst

def audience_for(viewer, target):
    """Berechnet die Audience-Stufe.

    Returns None für 'no access' (-> 404).
    """
    if viewer.is_admin:
        return Audience.ADMIN
    if viewer.pk == target.pk:
        # Self-Sicht — Applicant darf sich selbst sehen.
        return Audience.SELF
    # Cross-User-Sicht.
    if viewer.membership_level == User.MembershipLevel.APPLICANT:
        return None  # Applicants sehen niemand außer sich selbst
    if target.membership_level == User.MembershipLevel.APPLICANT:
        return None  # Member sehen Applicants nicht
    return Audience.MEMBER
```

### 3.2 Sichtbare Felder pro Audience

```python
# apps/accounts/visibility.py

# Felder, die jeder logged-in Member sehen darf (wenn target.is_directory_visible).
# Reihenfolge mirroring der Anzeige im Overview-Tab.
PUBLIC_PROFILE_FIELDS = (
    "username",            # Callsign
    "first_name", "last_name",
    "email",
    "membership_level",
    "avatar",
    "bio",
    "qth_name",
    "locator",
    "qrz_url",
    "date_joined_year",    # nur Jahr, nicht das Datum
    "region_assignments",  # als Pill-Liste
    "station_assignments", # als Pill-Liste
)

# Zusätzlich nur Self und Admin.
PRIVATE_PROFILE_FIELDS = (
    "address",
    "phone",
    "latitude", "longitude",   # numerisch, in Admin-Debug-Block
    "language",
    "last_login",
    "is_active",
    "is_directory_visible",
)

# Nur Admin.
ADMIN_ONLY_FIELDS = (
    "sso_grants",
    "sso_sessions",
    "tag_memberships",
    "global_audit_actions",  # Promote/Demote, Region-/Station-Assignment-Mgmt
)

def directory_visible_fields(viewer, target):
    aud = audience_for(viewer, target)
    if aud is None:
        return set()  # no access
    if aud in (Audience.ADMIN,):
        return set(PUBLIC_PROFILE_FIELDS + PRIVATE_PROFILE_FIELDS + ADMIN_ONLY_FIELDS)
    if aud in (Audience.SELF, Audience.APPLICANT):
        # Self/Applicant sehen ihre eigenen privaten Felder (read-only).
        # ADMIN_ONLY_FIELDS bleiben außen vor — z.B. eigene SSO-Sessions
        # sind in der Sessions-Card als "self-only Variante" sichtbar
        # (Revoke der eigenen Session); SSO-Grants sind reine Admin-Decision.
        return set(PUBLIC_PROFILE_FIELDS + PRIVATE_PROFILE_FIELDS) | {"sso_sessions_self"}
    # MEMBER:
    if not target.is_directory_visible:
        # Master-Switch off → nur das absolute Minimum.
        return {"username", "membership_level", "avatar"}
    return set(PUBLIC_PROFILE_FIELDS)
```

Das Template nutzt diesen Set für conditional Rendering:

```django
{% if "phone" in visible_fields and object.phone %}
  <dt>{% trans "Phone" %}</dt><dd class="t-mono">{{ object.phone }}</dd>
{% endif %}
```

### 3.3 UserDetailView — dispatch und Datenladung

```python
class UserDetailView(LoginRequiredMixin, DetailView):
    model = User
    template_name = "accounts/user_detail.html"
    context_object_name = "object"

    def dispatch(self, request, *args, **kwargs):
        # get_object darf erst nach login_required laufen
        response = super().dispatch(request, *args, **kwargs)
        return response

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        from .visibility import audience_for
        aud = audience_for(self.request.user, obj)
        if aud is None:
            raise Http404("User not found")
        self._audience = aud
        return obj

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        from .visibility import directory_visible_fields, Audience

        aud = self._audience
        ctx["audience"] = aud.value
        ctx["is_admin_view"] = aud == Audience.ADMIN
        ctx["is_self_view"] = aud in (Audience.SELF, Audience.APPLICANT)
        ctx["is_member_view"] = aud == Audience.MEMBER
        ctx["visible_fields"] = directory_visible_fields(
            self.request.user, self.object
        )

        # Card-Daten nur für Admin laden (Member/Self brauchen weniger).
        if aud == Audience.ADMIN:
            ctx.update(self._admin_context_data())
        elif aud in (Audience.SELF, Audience.APPLICANT):
            ctx.update(self._self_context_data())
        # Member-View braucht nur die public Felder + assignments für Pills.

        # Assignments für Pills (Member + Self + Admin):
        ctx["region_assignment_pills"] = self.object.region_assignments.select_related("region")
        ctx["station_assignment_pills"] = self.object.station_assignments.select_related("station")

        # Audit-Tab nur für Self (eigene) + Admin (alles).
        if aud in (Audience.ADMIN, Audience.SELF, Audience.APPLICANT):
            ctx["user_audit_entries"] = self._build_user_audit(self.object)

        return ctx
```

`_admin_context_data` lädt alles wie heute (existing_region_assignments + available_regions, existing_station_assignments + all_stations, app_grants_list, user_sessions, tag_entries, membership_level_choices, alle Counts).

`_self_context_data` lädt nur die eigenen Region/Station-Assignments + die eigenen SSO-Sessions (für Self-Revoke).

`UserUpdateView.get_context_data` wird auf das absolute Minimum reduziert (`form_title` reicht).

### 3.4 Success-Redirects

| View | Erfolgs-Redirect |
|---|---|
| `UserCreateView` | `users:user_detail` mit pk des neuen Users |
| `UserUpdateView` | `users:user_detail` mit pk des bearbeiteten Users |
| `UserDeleteView` | `users:user_list` |
| `ProfileView` | `users:user_detail` mit pk des request.users (war bisher `accounts:profile`) |
| Alle HTMX-Endpoints | unverändert (200 JSON, Client reloaded die Seite) |

### 3.5 List-Permission

`UserListView` wird von `LoginRequiredMixin` + Custom-Check ersetzt:

```python
class UserListView(LoginRequiredMixin, ListView):
    def dispatch(self, request, *args, **kwargs):
        if request.user.membership_level == User.MembershipLevel.APPLICANT:
            raise Http404()
        return super().dispatch(request, *args, **kwargs)
```

Applicants bekommen 404 (nicht 403 — kein Existenz-Leak der Liste). Member sehen die Liste mit gefilterten Spalten/Aktionen. Admin sieht alles inkl. Applicants.

---

## 4. Detail-Page — Layout (audience-aware)

Template: `apps/accounts/templates/accounts/user_detail.html`. Strukturell parallel zu `stations/station_detail.html`. Tabs werden conditional je nach Audience gerendert.

### 4.1 Page-Head

```
page-head
├ page-eyebrow:
│   ├ Admin / Self / Applicant-View:  "User · #{{ object.pk|stringformat:'03d' }}"
│   └ Member-View:                    "Verein · Mitglied"
├ page-title:
│   ├ avatar (sb-avatar 48px, mit Image wenn object.avatar gesetzt sonst Buchstabe)
│   ├ callsign (= username) — primär
│   └ subtitle:
│       ├ full name (wenn "first_name" + "last_name" in visible_fields und gesetzt)
│       └ sonst muted "—"
├ pills-row (audience-aware):
│   ├ membership-level pill              (immer)
│   ├ is_active "INACTIVE" pill          (nur wenn nicht aktiv UND admin oder self)
│   ├ language pill                       (nur self + admin)
│   └ qth_name pill (wenn gesetzt + sichtbar)
└ page-head-actions:
    ├ Admin-view (nicht self):
    │   ├ [Edit identity]   → users:user_edit
    │   └ [Delete]          → users:user_delete
    ├ Self / Applicant-view:
    │   └ [Edit profile]    → accounts:profile
    └ Member-view:
        └ (keine Actions)
```

Avatar im Page-Head verlinkt für Self/Admin zur Datei selbst (Lightbox/größere Anzeige optional, out-of-scope).

### 4.2 Summary-Bar

Audience-gefiltert. Member sieht nur Felder, die in `visible_fields` liegen.

Mögliche Items:

| Item | Self/Admin | Member |
|---|---|---|
| Email | ✓ | ✓ (wenn in visible_fields) |
| Phone | ✓ | ✗ |
| Locator | ✓ | ✓ |
| QTH | ✓ | ✓ |
| Date joined / Mitglied seit (Jahr) | ✓ | ✓ |
| Last login | ✓ | ✗ |
| # Region-Assignments | ✓ | ✓ |
| # Station-Assignments | ✓ | ✓ |
| # Active SSO Sessions | ✓ (Admin), ✓ (Self) | ✗ |

Bei Member-Sicht auf `is_directory_visible=False`-Profile: Summary-Bar ist komplett leer (oder nur Mitglied-seit-Jahr).

### 4.3 Tabs

Wiederverwendet wird der `data-tabs`-Container und das bestehende JS aus `station_detail.html`. Tabs werden conditional je Audience angezeigt:

| Tab | Admin | Self | Member | Applicant (self) |
|---|---|---|---|---|
| Overview | ✓ | ✓ | ✓ | ✓ |
| Rollen & Topologie | ✓ (edit) | ✓ (readonly) | ✓ (readonly, nur Assignments-Pills) | ✓ (readonly) |
| Single Sign-On | ✓ | ✓ (eigene Sessions) | ✗ | ✓ (eigene Sessions) |
| Audit | ✓ | ✓ (eigene) | ✗ | ✓ (eigene) |

Member sieht 2 Tabs: Overview + Rollen & Topologie.

#### Overview-Tab

`grid grid-main`. Layout audience-abhängig:

**Admin / Self / Applicant:**
- Linke Spalte: Identity-Panel mit dlist über alle in `visible_fields`. Reihenfolge: Callsign (groß) → Name → Bio → Email → Phone → Adresse → QTH → Locator → QRZ-URL → Language → Date joined → Last login → is_active.
- Rechte Spalte (`aside.stack-gap-14`):
  - Avatar-Preview-Panel (vergrößertes Avatar oben).
  - "Status-Snapshot" (Counts).
  - "Zuletzt im Audit-Log" (Mini-Feed, 3-5 Einträge) — nur Admin/Self.
  - lat/lon-Numerisch + Geocoding-Status (Admin-Debug-only).

**Member:**
- Linke Spalte: Identity-Panel reduziert. Reihenfolge: Callsign → Name → Bio → Email → QTH → Locator → QRZ-URL → "Mitglied seit YYYY". Phone/Adresse/Language/Last-login fehlen komplett.
- Rechte Spalte: nur Avatar-Preview-Panel + Status-Snapshot (Counts der Topology-Rollen).

**Member auf `is_directory_visible=False`-Profil:**
- Empty-State-Panel: "Dieses Mitglied hat sein Profil im Verzeichnis verborgen." + Avatar + Membership-Pill.
- Keine Tabs außer Overview.

#### Rollen-&-Topologie-Tab

**Admin** (mit Edit-Buttons):
```
membership-card                    (volle Breite, oben)
├─ region-assignments-card           ─┬ grid grid-main
└─ station-assignments-card          ─┘  (stacked auf Mobile)
```

**Self / Applicant / Member** (read-only, gleiches Template via `readonly=True`-Flag):
```
membership-card           (Pill ohne Set-Form)
├─ region-assignments-card   (Pills ohne Add/Revoke)
└─ station-assignments-card  (Pills ohne Add/Revoke)
```

Die Read-only-Variante der Cards wird durch ein `readonly`-Flag im Template-Kontext gesteuert: bestehende Cards bekommen ein `{% if readonly %}…{% else %}…{% endif %}` um Add/Revoke-Forms. Alternative — eigene `_..._readonly.html`-Partials — wird verworfen, weil zu viel Duplizierung.

#### Single-Sign-On-Tab

**Admin:** Drei Cards untereinander (Grants, Sessions, Tags), wie heute. HTMX-Targets bleiben.

**Self / Applicant:** Nur die Sessions-Card, audience-flagged als „nur eigene Sessions". Revoke der eigenen Sessions ist erlaubt (selber Endpoint). Grants und Tags sind reine Admin-Decisions und im Self-View ausgeblendet.

**Member:** Tab ausgeblendet — die SSO-Sicht über fremde User ist Admin-only.

#### Audit-Tab

Siehe Sektion 8. Sichtbar für Admin (alle Einträge zum target_user), Self/Applicant (eigene Einträge — DSGVO-Selbsteinsicht). Member sehen den Tab nicht.

---

## 5. List-Page — Redesign (audience-aware)

Template: `apps/accounts/templates/accounts/user_list.html`.

### 5.1 Page-Head + Filter

Audience-aware Page-Head:

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
```

**Filter-Bar** — Admin und Member sehen sie ähnlich:

```
filter-bar:
├ input[name=q]   — Callsign/Email/Full-name (icontains, GET-Param)
├ select[name=role]:
│   ├ Member-Sicht:  Alle | Mitglied | Staff | Admin
│   └ Admin-Sicht:   Alle | Bewerber | Mitglied | Staff | Admin
├ Admin-only:  select[name=status]  — Alle | Aktiv | Inaktiv
└ [Reset filters] (Link, wenn Params gesetzt)
```

Per User-Entscheidung: **Admin sieht Applicants immer**, kein Toggle. Member sehen Applicants nie. Die Rolle-Selectbox ist der einzige Weg für Admin, gezielt nur Bewerber rauszuziehen.

`UserListView.get_queryset`:

```python
def get_queryset(self):
    qs = User.objects.order_by("username")

    # Audience-Filter: Member sehen niemals Applicants.
    if not self.request.user.is_admin:
        qs = qs.exclude(membership_level=User.MembershipLevel.APPLICANT)
    # Admin sieht standardmäßig alle inkl. Applicants — kein Default-Filter.

    # Search
    q = self.request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(username__icontains=q)
            | Q(email__icontains=q)
            | Q(first_name__icontains=q)
            | Q(last_name__icontains=q)
        )

    # Role-Filter
    role = self.request.GET.get("role", "")
    valid_roles = {x.value for x in User.MembershipLevel}
    if not self.request.user.is_admin:
        valid_roles -= {User.MembershipLevel.APPLICANT.value}
    if role in valid_roles:
        qs = qs.filter(membership_level=role)

    # Status (Admin only)
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

Pagination bleibt bei 25/Seite.

### 5.2 Tabelle

Spalten audience-aware:

| Spalte | Admin | Member | Mobile-Verhalten |
|---|---|---|---|
| **User** | Avatar + Callsign + Name (data-primary) | Avatar + Callsign + Name (data-primary) | Bleibt prominent, oben in Card |
| Role | Membership-Pill | Membership-Pill | data-label |
| Email | mono | mono (wenn directory-visible) | data-label |
| QTH | nicht angezeigt | mono (wenn gesetzt) | data-label |
| Topology | "{n_region}·{n_station}" mini-pill | "{n_region}·{n_station}" mini-pill | data-label |
| Last login | relative time | nicht angezeigt | data-label |
| Active | is_active pill (wenn !active) | nicht angezeigt | data-label |
| Actions | [View] [Edit] [Delete] | [View] | actions-Klasse |

Bei Member-Sicht auf User mit `is_directory_visible=False`: Row zeigt nur Avatar + Callsign + Membership-Pill + [View]-Button. Andere Spalten leer/em-dash.

Wichtig: **[View]** ist der primäre Button (kein btn-ghost), Edit/Delete bleiben sekundär. Klick auf den Primary-Cell-Bereich navigiert zur Detail-Seite — implementiert per `<a href="{% url 'accounts:user_detail' u.pk %}" style="color:inherit;text-decoration:none;">…</a>`-Wrap um den Inhalt der ersten Zelle. Das ist genau das Pattern aus `stations/_station_table.html` Zeile 23.

Self-Row (request.user) hat keinen Delete-Button.

---

## 6. Edit-Form, Create-Form, Profile-Form — Mobile-friendly

Drei Formulare teilen sich denselben Feldsatz, mit unterschiedlichen Subsets:

| Feld | UserCreationForm (Admin) | UserChangeForm (Admin) | ProfileForm (Self) |
|---|:---:|:---:|:---:|
| username | ✓ | ✓ | ✗ (Admin-only-Änderung) |
| email | ✓ | ✓ | ✓ |
| first_name, last_name | ✓ | ✓ | ✓ |
| language | ✓ | ✓ | ✓ |
| is_active | ✗ (default True) | ✓ | ✗ |
| password1, password2 | ✓ | ✗ | ✗ (eigener PWChangeForm) |
| **bio** | ✗ | ✓ | ✓ |
| **avatar** | ✗ | ✓ | ✓ |
| **qth_name** | ✗ | ✓ | ✓ |
| **qrz_url** | ✗ | ✓ | ✓ |
| **address** | ✗ | ✓ | ✓ |
| **phone** | ✗ | ✓ | ✓ |
| **locator** | ✗ | ✓ (read-only oder override) | ✓ (override-only) |
| **is_directory_visible** | ✗ | ✓ | ✓ |

`UserCreationForm` bleibt schlank — Admin gibt einem neuen User nur Username, Email, Name, Language, Passwort. Alles andere füllt der User selbst über `ProfileView` aus, oder der Admin nachträglich über den Edit-Form.

### 6.1 Layout — Edit-Form (`user_form.html`, Admin)

```
page-head:
├ breadcrumb: Users > {username} > Edit  (oder: Users > Create)
├ page-title: form_title
└ (kein Page-Head-Actions im Edit-Mode)

grid grid-main:
├ Linke Spalte (Form, mehrere Panels):
│   │
│   ├ Panel "Identity":
│   │   ├ form-row: [username] [is_active checkbox]
│   │   ├ form-row: [first_name] [last_name]
│   │   ├ form-row: [email]
│   │   ├ form-row: [language] [is_directory_visible checkbox]
│   │   └ (Create-Mode) form-row: [password1] [password2]
│   │
│   ├ Panel "Profil" (nur Edit-Mode):
│   │   ├ form-row: [avatar (FileInput)]
│   │   ├ form-row: [bio (Textarea, 3 rows)]
│   │   ├ form-row: [qth_name] [qrz_url]
│   │   └ form-row: [phone]
│   │
│   └ Panel "Adresse & Standort" (nur Edit-Mode):
│       ├ form-row: [address (Textarea, 4 rows)]
│       ├ Hinweis-Text: "Locator + lat/lon werden bei Speichern aus der Adresse berechnet."
│       └ form-row: [locator (manuelle Override)]
│
└ Rechte Spalte (aside, panel):
    ├ Im Edit-Mode: dlist mit pk, date_joined, last_login, current membership-level,
    │                lat/lon (Debug-Anzeige falls geocodet), is_directory_visible-State
    └ Im Create-Mode: Info-Box "Profil-Daten ergänzt der User selbst über sein Profil,
                       Topology-Assignments setzt du nach dem Speichern auf der Detail-Seite."

panel-foot:
├ [Save user]
└ [Cancel] → Detail (Edit) bzw. Liste (Create)
```

Inline `style="max-width:640px"` wird komplett entfernt. Das `grid grid-main` collapsed bei `≤ 1024px` automatisch zu single-column (existierendes CSS). `form-row` collapsed bei `≤ 720px` zu single-column (existierendes CSS).

### 6.2 Layout — Profile-Form (`profile.html`, Self)

Template komplett neu strukturiert. Heute hat es ein einziges Form-Panel. Neu:

```
page-head:
├ page-eyebrow: "Your account"
├ page-title: "Profile"
└ page-sub: "Verwalte deine Identität, Profil, Kontaktdaten und Standort."

grid grid-main:
├ Linke Spalte (mehrere Panels — separate forms je Panel, eigene POST-URLs):
│   ├ Panel "Identity" → POST accounts:profile:
│   │   email, first_name, last_name, language
│   │
│   ├ Panel "Profil" → POST accounts:profile:
│   │   avatar, bio, qth_name, qrz_url, phone
│   │
│   ├ Panel "Adresse & Standort" → POST accounts:profile:
│   │   address, locator (override), Hinweis "Locator wird berechnet"
│   │
│   └ Panel "Passwort ändern" → POST accounts:password_change:
│       current_password, new_password1, new_password2
│
└ Rechte Spalte (aside, panel):
    ├ Identity-dlist: Callsign (username — readonly), Membership-Pill,
    │                 last_login, date_joined
    ├ Panel "Sichtbarkeit im Verzeichnis":
    │   Toggle is_directory_visible (eigene HTMX-POST), Erklär-Text
    │   "Wenn aus, sehen andere Mitglieder nur Callsign + Rolle."
    └ Panel "Eigene Sessions": Mini-Übersicht aktiver SSO-Sessions mit Revoke
        (entfaltet sich aus _sessions_card.html mit readonly-self-Flag)

panel-foot je Form-Panel: eigener [Save]-Button.
```

**Begründung für Pro-Panel-Save:** Vier semantisch unterschiedliche Konzepte (Identity, Profil-Kosmetik, Adresse-mit-Geocoding-Side-Effect, Passwort-mit-Re-Auth). Password-Change-Form *muss* sowieso separat sein (eigener Re-Auth-Schritt mit `current_password`). Adresse-Form triggert Geocoding und sollte unabhängig speicherbar sein. Profile-Form mit alles-oder-nichts-Save würde bei Geocoding-Failure den ganzen Bulk-Save verlieren.

Implementierung: Das eine Template enthält drei `<form>`-Elemente mit unterschiedlichen `action`-URLs, und ein viertes für Passwort. Server-seitig sind das vier views (`ProfileIdentityView`, `ProfileProfileView`, `ProfileAddressView`, `ProfilePasswordView`) bzw. eine Dispatch-View mit `form_name`-Parameter — letzteres ist kompakter. Implementierung-Detail in Phase 8.

### 6.2.1 Password-Change-Panel

Eigenes Form basiert auf Django's `PasswordChangeForm`:

```python
# apps/accounts/forms.py
from django.contrib.auth.forms import PasswordChangeForm as DjangoPasswordChangeForm

class PasswordChangeForm(DjangoPasswordChangeForm):
    """Bootstrap-styled overlay über Django's PasswordChangeForm."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
```

```python
# apps/accounts/views.py
class ProfilePasswordChangeView(LoginRequiredMixin, View):
    """Self-only password change endpoint, posted from the Profile page."""

    def post(self, request):
        form = PasswordChangeForm(user=request.user, data=request.POST)
        if form.is_valid():
            user = form.save()
            # update_session_auth_hash → User bleibt eingeloggt nach PW-change
            update_session_auth_hash(request, user)
            AccountAuditLog.log(
                event_type=AccountAuditLog.EventType.PASSWORD_CHANGED,
                actor=request.user,
                target_user=request.user,
                message="self-edit changed: password",
                ip_address=_get_client_ip(request),
            )
            messages.success(request, _("Password updated successfully."))
        else:
            for error in form.errors.values():
                messages.error(request, "; ".join(error))
        return redirect("accounts:profile")
```

`update_session_auth_hash` ist wichtig: ohne den Call killt der Password-Change die laufende Session.

Audit: Neuer EventType `PASSWORD_CHANGED` (siehe Sektion 7.1 — wird dort ergänzt).

### 6.2.2 Onboarding-Empty-State

Wenn der eingeloggte User Profil-Felder leer hat, rendert das Profile-Template dezente Empty-State-Hinweise innerhalb der jeweiligen Panel-Bodies — kein Modal, kein blocking Banner:

```django
{# Beispiel im Profil-Panel #}
{% if not user.avatar %}
  <div class="onboarding-hint" role="note">
    <span class="onboarding-hint-icon">📷</span>
    <span class="onboarding-hint-text">
      {% trans "Lade ein Profilbild hoch, damit dich andere Mitglieder im Verzeichnis erkennen." %}
    </span>
  </div>
{% endif %}
```

Pro Sektion ein Hinweis nach folgendem Mapping:

| Panel | Trigger | Text |
|---|---|---|
| Identity | `not user.first_name and not user.last_name` | „Trag deinen Real-Namen ein — andere Mitglieder sehen ihn im Verzeichnis." |
| Profil | `not user.avatar` | „Lade ein Profilbild hoch." |
| Profil | `not user.bio` | „Stell dich kurz vor (max. 500 Zeichen)." |
| Profil | `not user.qth_name` | „QTH-Name? Das ist dein Funker-Standort-Label." |
| Adresse | `not user.address` | „Trag deine Adresse ein — Locator und lat/lon werden automatisch berechnet." |

Visual: dezent — kleine `border-left:3px solid var(--accent-soft)` Box mit Icon + 1 Satz Text. Wenn das Feld ausgefüllt ist, verschwindet der Hinweis.

CSS-Klasse `onboarding-hint` neu in `app.css` (kompakter Style, Mobile-tauglich). Nicht-stylistisch in der Detail-Page — diese Hilfe gehört nur auf die Edit-Surface.

### 6.3 Form-Klassen

`UserChangeForm`, `UserCreationForm`, `ProfileForm` bekommen die neuen Felder mit den entsprechenden Widget-Klassen:

```python
class ProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = (
            "email", "first_name", "last_name", "language",
            "avatar", "bio", "qth_name", "qrz_url", "phone",
            "address", "locator", "is_directory_visible",
        )
        widgets = {
            "email": forms.EmailInput(attrs={"class": "form-control"}),
            "first_name": forms.TextInput(attrs={"class": "form-control"}),
            "last_name": forms.TextInput(attrs={"class": "form-control"}),
            "language": forms.Select(attrs={"class": "form-select"}),
            "avatar": forms.ClearableFileInput(attrs={"class": "form-control", "accept": "image/*"}),
            "bio": forms.Textarea(attrs={"class": "form-control", "rows": 3, "maxlength": 500}),
            "qth_name": forms.TextInput(attrs={"class": "form-control"}),
            "qrz_url": forms.URLInput(attrs={"class": "form-control"}),
            "phone": forms.TextInput(attrs={"class": "form-control"}),
            "address": forms.Textarea(attrs={"class": "form-control", "rows": 4}),
            "locator": forms.TextInput(attrs={"class": "form-control", "placeholder": "JN78AB"}),
            "is_directory_visible": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def clean_locator(self):
        loc = self.cleaned_data.get("locator", "").strip().upper()
        if loc and not LOCATOR_REGEX.match(loc):
            raise ValidationError(_("Locator muss 2 Buchstaben + 2 Ziffern + 2 Buchstaben sein (z.B. JN78AB)."))
        return loc
```

`LOCATOR_REGEX = re.compile(r"^[A-R]{2}[0-9]{2}[A-X]{2}$")` — Maidenhead 6-char Validator.

`UserChangeForm` (Admin) erbt von dieser Liste + zusätzlich `username` und `is_active`.

`UserCreationForm` (Admin, Create) bleibt schlank wie bisher und bekommt nur Identity-Felder.

### 6.4 Audit-Emission im UpdateView (UserUpdateView, Admin)

```python
TRACKED_USER_FIELDS = {
    "username", "email", "first_name", "last_name", "language",
    "bio", "avatar", "qth_name", "qrz_url", "phone",
    "address", "locator", "is_directory_visible",
}

class UserUpdateView(AdminRequiredMixin, UpdateView):
    ...
    def form_valid(self, form):
        changed_fields = set(form.changed_data)
        response = super().form_valid(form)

        # ProfileForm.save() triggert ggf. Geocoding — kann latitude/longitude
        # implizit ändern. Diese werden NICHT als "changed" geloggt
        # (sie sind aus address abgeleitet).
        changed = changed_fields & TRACKED_USER_FIELDS
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

Wenn nur `is_active` flippt aber kein anderes Feld ändert, gibt es **nur** `USER_ACTIVATED/DEACTIVATED`, kein `USER_UPDATED`. Das hält den Feed pro Ereignis-Typ sauber.

### 6.5 Audit-Emission im ProfileView (Self)

Identisch zum UpdateView, nur dass `actor == target_user == request.user`:

```python
class ProfileView(LoginRequiredMixin, UpdateView):
    ...
    def form_valid(self, form):
        changed_fields = set(form.changed_data)
        response = super().form_valid(form)
        changed = changed_fields & TRACKED_USER_FIELDS
        if changed:
            AccountAuditLog.log(
                event_type=AccountAuditLog.EventType.USER_UPDATED,
                actor=self.request.user,
                target_user=self.object,
                message=f"self-edit changed: {', '.join(sorted(changed))}",
                ip_address=_get_client_ip(self.request),
            )
        return response
```

Self-Edit wird im Audit über die `actor==target_user`-Konstellation identifiziert; das Präfix `self-edit changed:` ist Convenience für die Audit-Anzeige.

### 6.6 Audit-Emission im CreateView

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

### 6.7 Geocoding-Trigger im form_valid

Sowohl `UserUpdateView.form_valid` (Admin) als auch `ProfileView.form_valid` (Self) lösen Geocoding aus, wenn `address` geändert wurde:

```python
if "address" in changed_fields and self.object.address:
    from .geocoding import geocode_address, lat_lon_to_locator
    coords = geocode_address(self.object.address)
    if coords:
        lat, lon = coords
        self.object.latitude = lat
        self.object.longitude = lon
        # Locator nur dann setzen wenn User keinen manuellen Override eingegeben hat.
        if not self.object.locator or "locator" not in changed_fields:
            self.object.locator = lat_lon_to_locator(lat, lon)
        self.object.save(update_fields=["latitude", "longitude", "locator"])
elif "address" in changed_fields and not self.object.address:
    # Adresse wurde geleert — lat/lon entfernen, Locator nur wenn nicht manuell gesetzt.
    self.object.latitude = None
    self.object.longitude = None
    if "locator" not in changed_fields:
        self.object.locator = ""
    self.object.save(update_fields=["latitude", "longitude", "locator"])
```

Geocoding-Detail siehe Sektion 9.

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
PASSWORD_CHANGED          = "password_changed",            _("Password Changed")
STATION_ASSIGNMENT_CREATED = "station_assignment_created",  _("Station Assignment Created")
STATION_ASSIGNMENT_REVOKED = "station_assignment_revoked",  _("Station Assignment Revoked")
```

`PASSWORD_CHANGED` emittiert sich selbstständig aus `ProfilePasswordChangeView` (Sektion 6.2.1). Die `message` ist konstant `"self-edit changed: password"` — der Passwort-Wert selbst wird nie geloggt. Audit-Anzeige im Per-User-Tab + im globalen Feed unter Account-Kategorie.

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

## 9. Geocoding-Service + Locator-Berechnung

### 9.1 Provider: Nominatim/OpenStreetMap

Free-Tier, kein API-Key, dokumentiert auf https://nominatim.org/release-docs/latest/api/Search/.

**Policy-Constraints** (verpflichtend für die OSM-Free-Instance):

- **User-Agent-Header MUSS gesetzt sein.** Beispiel: `"OE5XRX-StationManager/1.0 (admin@oe5xrx.org)"`.
- **Max 1 Request pro Sekunde** — wir blockieren das beim Form-Save serialisiert via Lock-File oder einfach `time.sleep(1)` davor (synchrones Save, kein Pool).
- **Caching ist verpflichtend.** Wir caches: nur Recompute wenn `address`-Feld geändert wurde, sonst lat/lon unverändert.

Alternative-Provider (out-of-scope, falls Free-Tier reibt): Mapbox (kostenpflichtig), Google (kostenpflichtig + lizensiert).

### 9.2 Implementation

`apps/accounts/geocoding.py`:

```python
import logging
import time
from decimal import Decimal
from typing import Optional

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_TIMEOUT = 10  # Sekunden
USER_AGENT = "OE5XRX-StationManager/1.0 (peter.buchegger7@gmail.com)"

def geocode_address(address: str) -> Optional[tuple[Decimal, Decimal]]:
    """Resolve eine Postadresse zu (latitude, longitude).

    Returns None bei Fehler (Network, kein Result, Timeout).
    Throttled per call: ein time.sleep(1) am Start serialisiert mit
    der Nominatim-Free-Tier-Rate-Limit-Policy.
    """
    if not address or not address.strip():
        return None
    time.sleep(1)  # Rate-Limit-Compliance
    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={
                "q": address.strip(),
                "format": "json",
                "limit": 1,
                "accept-language": "de,en",
            },
            headers={"User-Agent": USER_AGENT},
            timeout=NOMINATIM_TIMEOUT,
        )
        resp.raise_for_status()
        results = resp.json()
        if not results:
            return None
        first = results[0]
        return (Decimal(first["lat"]), Decimal(first["lon"]))
    except (requests.RequestException, ValueError, KeyError) as e:
        logger.warning("Nominatim geocode failed for address %r: %s", address, e)
        return None
```

**Fehler-Verhalten:** Schweigend zurückkehren mit `None`. Die View-Logik (siehe 6.7) lässt dann lat/lon/locator unverändert oder leert sie. User sieht das Save-OK, kann optional manuell Locator setzen.

### 9.3 Maidenhead-Locator-Berechnung

Pure-Python, kein Dep:

```python
def lat_lon_to_locator(lat: Decimal | float, lon: Decimal | float, precision: int = 6) -> str:
    """Maidenhead-Locator aus lat/lon.

    precision=6 ergibt ein 6-Zeichen-Locator (z.B. 'JN78AB').
    precision=4 ergibt ein 4-Zeichen-Locator (Grid-square 'JN78').

    Algorithmus:
    1. lon + 180, lat + 90  -> immer positiv
    2. Erste 2 Stellen: Felder à 20° lon / 10° lat (A-R)
    3. Nächste 2 Stellen: Quadrate à 2° lon / 1° lat (0-9)
    4. Letzte 2 Stellen: Subquadrate à 5' lon / 2.5' lat (A-X)
    """
    lat_f = float(lat) + 90
    lon_f = float(lon) + 180
    A = ord("A")
    lon_field, lon_rest = divmod(lon_f, 20)
    lat_field, lat_rest = divmod(lat_f, 10)
    out = chr(A + int(lon_field)) + chr(A + int(lat_field))
    lon_sq, lon_rest = divmod(lon_rest, 2)
    lat_sq, lat_rest = divmod(lat_rest, 1)
    out += str(int(lon_sq)) + str(int(lat_sq))
    if precision >= 6:
        lon_sub = int(lon_rest * 12)  # 2° / (5'/60°) = 24, half-step = 12
        lat_sub = int(lat_rest * 24)  # 1° / (2.5'/60°) = 24
        out += chr(A + lon_sub) + chr(A + lat_sub)
    return out
```

Edge-Case-Tests (Sektion 12):

- Wien (48.2°N, 16.4°E) → `JN88EF` (etwa, je nach Stelle Linz/Wien-Nachbarschaft)
- OE5XRX-Standort prüfen
- South-Pole, Equator-Crossing, Date-Line-Crossing

### 9.4 Trigger-Logik

Geocoding läuft **synchron im form_valid**:

- Trigger nur wenn `address` in `form.changed_data`.
- Trigger nur wenn `address` nicht leer (sonst lat/lon explizit leeren).
- Result: lat/lon werden auf die Instance gesetzt und mit `update_fields=["latitude", "longitude", "locator"]` extra-gespeichert (zweiter DB-Hit, aber save() ist ohnehin sehr selten — Self-Edit ist kein Heißpfad).
- Locator-Berechnung **überschreibt nicht** einen User-gesetzten Locator: wenn der User im selben Form das `locator`-Feld auch geändert hat (manueller Override), behalten wir den User-Wert.

Asynchrone Verarbeitung (Celery-Task) bewusst out-of-scope — würde Dep-Stack auflagern. Bei 100 Mitgliedern sind das wenige Geocoding-Calls pro Monat.

---

## 10. Avatar-Upload

### 10.1 Storage

`avatar = ImageField(upload_to=_avatar_upload_path, null=True, blank=True)`. Path-Helper:

```python
def _avatar_upload_path(instance, filename):
    """avatars/<user_id>/<sha-of-content>.<ext>

    Hash-basiert: derselbe Inhalt ergibt denselben Pfad — alte Files werden
    von Django nicht überschrieben. Bei jedem Upload entsteht ein neues File,
    das alte bleibt orphaned. Cleanup-Routine wäre eigener Job (out-of-scope).
    """
    ext = Path(filename).suffix.lower() or ".jpg"
    return f"avatars/{instance.pk or 'new'}/{uuid.uuid4().hex[:12]}{ext}"
```

`MEDIA_ROOT` und `MEDIA_URL` sind im Project bereits konfiguriert (für Station-Photos). Kein zusätzlicher Storage-Setup.

### 10.2 Validierung und Resize

Bei Form-Save:

```python
def clean_avatar(self):
    f = self.cleaned_data.get("avatar")
    if not f:
        return f
    if f.size > 2 * 1024 * 1024:
        raise ValidationError(_("Avatar darf max. 2 MB sein."))
    # Pillow ist bereits im Stack (Stations-Photos)
    from PIL import Image, UnidentifiedImageError
    try:
        img = Image.open(f)
        img.verify()
    except (UnidentifiedImageError, OSError):
        raise ValidationError(_("Datei ist kein gültiges Bild."))
    return f
```

**Resize-Logik** läuft im `ProfileForm.save()` (resp. `UserChangeForm.save()`) NACH dem `super().save()`:

```python
def save(self, commit=True):
    user = super().save(commit=commit)
    if "avatar" in self.changed_data and user.avatar:
        from PIL import Image
        img = Image.open(user.avatar.path)
        img.thumbnail((512, 512))
        # Re-encode als JPEG mit quality=85 (egal welches Input-Format)
        img.convert("RGB").save(user.avatar.path, "JPEG", quality=85, optimize=True)
    return user
```

512x512 als max — Avatar wird im Header sb-avatar mit 48px gerendert, im Detail-Aside mit 128px. 512 reicht für Retina.

### 10.3 Fallback (kein Avatar)

Wenn `user.avatar` leer/None: Template fällt auf den bestehenden `sb-avatar`-Buchstaben-Block zurück (existiert schon, mit `username[0].upper()`).

### 10.4 Privacy

Avatar ist **immer Member-sichtbar** (auch bei `is_directory_visible=False` — der Master-Switch erlaubt nur Callsign + Pill + Avatar). Begründung: ein Avatar ist explizit von User upgeloaded → consent ist implizit.

Wer komplett anonym bleiben will: kein Avatar hochladen → Buchstaben-Avatar.

---

## 11. Schema und Migrations

### 11.1 Migration 1: User-Profile-Felder

`apps/accounts/migrations/0XXX_user_profile_fields.py` — fügt 10 neue Felder zum User-Modell hinzu:

```python
operations = [
    migrations.AddField(model_name="user", name="bio",
                        field=models.TextField(blank=True, max_length=500, verbose_name="bio")),
    migrations.AddField(model_name="user", name="avatar",
                        field=models.ImageField(blank=True, null=True,
                                                 upload_to=_avatar_upload_path,
                                                 verbose_name="avatar")),
    migrations.AddField(model_name="user", name="qth_name",
                        field=models.CharField(blank=True, max_length=128, verbose_name="QTH name")),
    migrations.AddField(model_name="user", name="qrz_url",
                        field=models.URLField(blank=True, max_length=200, verbose_name="QRZ URL")),
    migrations.AddField(model_name="user", name="address",
                        field=models.TextField(blank=True, verbose_name="address")),
    migrations.AddField(model_name="user", name="phone",
                        field=models.CharField(blank=True, max_length=32, verbose_name="phone")),
    migrations.AddField(model_name="user", name="latitude",
                        field=models.DecimalField(blank=True, decimal_places=6,
                                                  max_digits=9, null=True, verbose_name="latitude")),
    migrations.AddField(model_name="user", name="longitude",
                        field=models.DecimalField(blank=True, decimal_places=6,
                                                  max_digits=9, null=True, verbose_name="longitude")),
    migrations.AddField(model_name="user", name="locator",
                        field=models.CharField(blank=True, max_length=6, verbose_name="Maidenhead locator",
                                                validators=[RegexValidator(LOCATOR_REGEX)])),
    migrations.AddField(model_name="user", name="is_directory_visible",
                        field=models.BooleanField(default=True,
                                                   verbose_name="visible in member directory")),
]
```

Alle Felder optional / mit defaults. Keine Datenmigration nötig — bestehende User starten mit leeren Profilen und `is_directory_visible=True`.

### 11.2 Migration 2: AccountAuditLog EventTypes

`apps/accounts/migrations/0XXX_audit_user_crud_event_types.py` — `AlterField` auf `event_type.choices`. State-only-Update (siehe SSO-Spec).

Beide Migrations sind unabhängig und können nacheinander oder in einer kombiniert werden — kombiniert ist sauberer (ein „User-Domain-Redesign"-Migration-Stitch).

### 11.3 Locator-Validator

In `apps/accounts/models.py`:

```python
LOCATOR_REGEX = re.compile(r"^[A-R]{2}[0-9]{2}[A-X]{2}$")

locator_validator = RegexValidator(
    regex=LOCATOR_REGEX,
    message=_("Maidenhead locator must be 2 letters + 2 digits + 2 letters (e.g. JN78AB)."),
)
```

Validator + Regex sind Modul-Konstanten, von Form und Migration referenziert.

---

## 12. Mobile-Spezifika

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

## 13. Testing

### 13.1 Audit-Emissions

`apps/accounts/tests/test_audit_user_crud.py` (neu):

- USER_CREATED bei `POST /users/create/` mit korrekten Feldern.
- USER_UPDATED bei Identity-Change, nicht bei No-Op.
- USER_UPDATED bei Profile-Field-Change (avatar/bio/phone/address/qth_name/qrz_url/locator/is_directory_visible).
- USER_ACTIVATED / USER_DEACTIVATED bei is_active-Flip, separat von USER_UPDATED.
- USER_DELETED vor Cascade emittiert, `target_user` nach Cascade `NULL` aber Username in message.
- STATION_ASSIGNMENT_CREATED/_REVOKED via Signal (doppelter Emit zu StationAuditLog).
- Self-Edit via ProfileView emittiert USER_UPDATED mit `actor==target_user==request.user`.

### 13.2 Visibility-Logik

`apps/accounts/tests/test_visibility.py` (neu):

- `audience_for(admin, member)` → ADMIN.
- `audience_for(self, self)` → SELF.
- `audience_for(member, other_member)` → MEMBER.
- `audience_for(member, applicant)` → None (404).
- `audience_for(applicant, other_applicant)` → None (404).
- `audience_for(applicant, member)` → None (404).
- `directory_visible_fields(member, target_with_dir_visible=False)` → `{username, membership_level, avatar}`.
- `directory_visible_fields(member, target_with_dir_visible=True)` → `PUBLIC_PROFILE_FIELDS`.
- `directory_visible_fields(admin, anyone)` → full set.

### 13.3 Detail-View

`apps/accounts/tests/test_user_detail.py` (neu):

- Admin sieht beliebigen User → 200.
- Self sieht eigene Detail-Seite → 200, ohne Admin-Cards.
- Member sieht anderen Member (directory-visible) → 200, Identity-Felder visible, Address/Phone NICHT im Content.
- Member sieht anderen Member (directory-invisible) → 200, nur Username + Membership-Pill.
- Member sieht Applicant → 404.
- Applicant sieht eigene Detail-Seite → 200, ohne Admin-Cards.
- Applicant sieht anderen User → 404.
- Context `visible_fields` enthält erwartete Field-Sets pro Audience.
- Audit-Entries enthalten Account + SSO, sortiert nach created_at desc.

### 13.4 List-View

`apps/accounts/tests/test_user_list.py` (neu):

- Applicant → 404.
- Admin sieht alle inkl. Applicants by default.
- Member sieht alle ohne Applicants.
- Member sieht `is_directory_visible=False`-User reduziert dargestellt.
- Search-Filter `?q=` matched username/email/name.
- Role-Filter `?role=member`.
- Admin-only Status-Filter `?status=inactive` für Member 400/ignored (graceful).

### 13.5 Geocoding + Locator

`apps/accounts/tests/test_geocoding.py` (neu, mit responses-Mock):

- `geocode_address("Hauptstraße 1, 4020 Linz")` → mockt Nominatim, prüft (lat, lon)-Tupel.
- Bei HTTP-500 / Timeout → None ohne Exception.
- Bei leerem Address → None.
- `lat_lon_to_locator(48.31, 14.29)` → `JN78AB` (etwa, exakter Wert siehe Test).
- `lat_lon_to_locator` Edge-Cases: Equator (lat=0), Date-Line (lon=180), Negative-Coords.

### 13.6 Avatar-Upload

`apps/accounts/tests/test_avatar.py` (neu):

- Upload-Form mit > 2MB → ValidationError.
- Upload Non-Image (z.B. .txt) → ValidationError.
- Valid Upload → File-Save + Resize auf max 512x512.
- Path: `avatars/<user_id>/<random>.jpg`.

### 13.7 Form-Trigger und Geocoding-Integration

`apps/accounts/tests/test_profile_form.py` (neu):

- `address` neu gesetzt → Geocoding-Mock liefert (lat, lon) → `user.locator` automatisch gesetzt.
- `address` und `locator` gleichzeitig geändert → User-Locator gewinnt (kein Overwrite).
- `address` geleert → lat/lon/locator auf null/leer (außer locator wurde manuell gesetzt).
- `is_directory_visible` toggle → korrekt persistiert + USER_UPDATED audit.

### 13.8 Password-Change-Self-Service

`apps/accounts/tests/test_password_change.py` (neu):

- `POST /accounts/profile/password/` mit gültigem current_password + matching new1/new2 → 302 redirect + Session bleibt → `PASSWORD_CHANGED`-Audit-Eintrag mit actor=target_user=self.
- Falsches current_password → Form-Error in `messages`, kein Audit.
- new1 != new2 → Form-Error, kein Audit.
- Password zu schwach (gegen `AUTH_PASSWORD_VALIDATORS`) → Form-Error.
- Audit-Message ist konstant `"self-edit changed: password"` — kein Passwort-Wert geleakt.
- Nach erfolgreichem Change: alter Hash funktioniert nicht mehr (Login mit altem PW schlägt fehl).

### 13.9 Onboarding-Empty-State-Render

`apps/accounts/tests/test_profile_onboarding.py` (neu):

- User mit allen Profilfeldern leer → alle 5 Onboarding-Hints im HTML.
- User mit Avatar gesetzt → Avatar-Hint fehlt, andere noch da.
- User mit allen Feldern gefüllt → keine Hints im HTML.
- Hints erscheinen nur auf `accounts:profile`, nicht auf `accounts:user_detail` (auch bei Self-View).

### 13.10 Permissions an HTMX-Endpoints

Bestehende Tests bleiben grün — kein Change an den HTMX-Views.

### 13.11 Mobile-Snapshot (optional)

Falls Playwright im Projekt vorhanden ist: Detail-, List-, Edit-, Create-, Delete-, Profile-Page bei Viewports 375 / 768 / 1024 px. Smoke-Tests: kein horizontal-scroll, alle Buttons sichtbar, Tabs umbrechen bzw. scrollen sauber.

### 13.12 Global-Audit-Filter

`apps/audit/tests/test_audit_filter.py`: `?target_user=<pk>` filtert AccountAuditLog und SsoAuditLog korrekt.

---

## 14. Implementation-Reihenfolge

Empfohlene Build-Phasen (für den Implementation-Plan):

1. **Schema-Foundation**: User-Felder-Migration + Locator-Validator + Avatar-Path-Helper. Models + Migration + basic Tests.
2. **Audit-Foundation**: EventType-Erweiterung + Signal-Doppel-Emit für Station-Assignment + Tests. Kann parallel zu Phase 1 laufen.
3. **Visibility-Layer**: `apps/accounts/visibility.py` mit `audience_for` + `directory_visible_fields` + Test-Suite. Reine Pure-Python-Logik, keine Templates.
4. **Geocoding + Locator-Service**: `apps/accounts/geocoding.py` + Tests mit responses-Mock. Standalone, kein UI-Koppel.
5. **Avatar-Upload-Pipeline**: Form-clean + Resize + Tests. Standalone.
6. **DetailView Audience-aware**: Skeleton mit allen Tabs (conditional je Audience), Routing umstellen; Permission-Tests aus 13.3.
7. **List-View Audience-aware**: Member-View filtert Applicants, Admin sieht alle; Filter-Bar + Audience-conditional Spalten/Aktionen.
8. **Identity- + Profile-Form-Erweiterung**: Edit-Form + Profile-Form (mit getrennten Forms je Panel) bekommen neue Felder; Profile-Page integriert Password-Change-Panel (`ProfilePasswordChangeView`, `update_session_auth_hash`, `PASSWORD_CHANGED`-Audit); form_valid triggert Geocoding; USER_UPDATED-Audit für TRACKED_USER_FIELDS; Onboarding-Empty-State-Hinweise je Panel.
9. **Card-Migration**: Bestehende HTMX-Cards von Edit-Form in Detail-Tabs umziehen; inline `max-width` raus; Self-readonly-Variante.
10. **Delete-Confirm**: Impact-Anzeige + USER_DELETED-Emit.
11. **Audit-Tab + Global-Filter**: Per-User-Feed-Query + `target_user`-Filter im Globalen.
12. **Mobile-Polish + Playwright-Snapshots** (falls verfügbar).

Abhängigkeiten:
- Phase 1-5 sind die Foundation, alle bauen auf Schema + Visibility + Services.
- Phase 6 (DetailView) erfordert Phase 1-3.
- Phase 7 (List) erfordert Phase 3.
- Phase 8 (Forms) erfordert Phase 1, 4, 5.
- Phase 9 (Cards) erfordert Phase 6.
- Phase 10 (Delete) ist unabhängig nach Phase 2.
- Phase 11 (Audit-Tab) erfordert Phase 6.
- Phase 12 (Polish) läuft parallel ab Phase 8.

Subagent-driven-development pro Phase. Phasen 1+2+3+4+5 können in Round-1 parallelisiert werden (independent Foundation-Stücke). Phasen 6-11 sind sequenzielle Rounds. Round-2 Watcher (audit, guard, vault, probe E2E) nach jeder Build-Round.

---

## 15. Out-of-Scope

Bewusst nicht in diesem Spec:

- **Password-Reset-Flow** (forgot-password mit Email-Token). Self-Service Password-*Change* (mit current_password Re-Auth) ist in diesem Spec drin — reset via Email-Link ist eigener Spec mit Token-Tabelle + Email-Templates.
- **2FA / TOTP** auf der Profile-Page. Sicherheits-Spec für sich, mit Recovery-Codes, Backup-Devices etc.
- **User-Bulk-Operationen** (mehrere User auf einmal löschen/promoten). Eigener Spec.
- **Mehrfach-Membership-Levels** oder feinere Permissions. Membership ist heute ein single-value Feld, das bleibt so.
- **User-und-Station-Map**. Lat/lon-Foundation wird hier gelegt, die Map ist eigener Spec.
- **Audit-Filter-Bar auf dem Per-User-Audit-Tab**. Top-50-Anzeige reicht — wer mehr will, geht in den globalen Feed mit `?target_user=<pk>`.
- **Membership-Level-Selector im Create-Form**. Bleibt 2-Schritt-Flow (APPLICANT auf Create, Promote auf Detail).
- **Per-Feld-Privacy-Switch durch User** (Modell B). Nur Master-Switch `is_directory_visible` plus System-Defaults pro Feld.
- **Avatar-Lightbox** / größere Anzeige durch Klick. Avatar wird im Header + Aside fest gerendert.
- **Geocoding-Background-Job** (Celery). Synchron im form_valid mit 1-Sekunde-Pause für Nominatim-Rate-Limit reicht für unsere Größenordnung.
- **Orphaned-Avatar-Cleanup-Job**. Bei jedem Re-Upload bleibt das alte File liegen; periodisches Cleanup wäre eigener Job.
- **Operating-Modes + Bänder-Multi-Select-Felder**. Funker-Profil bleibt minimal (QTH + QRZ-URL). Kann später ergänzt werden ohne Brechen der Visibility-Logik.
- **Lizenzklasse-Feld** (CEPT-1 / Newcomer / 4). User-Entscheidung gegen Aufnahme — kann später ergänzt werden.
- **Activity-Heatmap** / Login-Frequenz-Visualisierung. Eigener Spec.

---

## 16. Offene Punkte

Keine. Alle Entscheidungen sind in den Sektionen 1-15 festgehalten.
