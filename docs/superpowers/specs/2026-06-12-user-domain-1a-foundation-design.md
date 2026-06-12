# User-Domain Redesign — Sub-Spec 1a: Foundation

**Status:** Draft, abgeleitet aus dem Master-Overview am 2026-06-12.
**Bogen:** Erster von drei Sub-Specs. Folgt dem Overview `2026-06-09-user-domain-redesign-overview.md`.
**Branch:** `feat/user-domain-1a-foundation`.
**Ziel:** Reine Backend-Foundation für den ganzen User-Domain-Redesign. Schema-Erweiterung am User-Modell + Audit-EventTypes + Visibility-Helper-Modul + Geocoding-Service + Avatar-Upload-Pipeline. **Keine UI-Änderung** — alle neuen Felder sind via Django Admin füllbar, aber kein Frontend nutzt sie noch.

Nach Merge dieses Specs sieht der User keine sichtbare Änderung. Die Foundation steht für 1b (Member-Directory) und 1c (Self-Service), die darauf bauen.

---

## 1. Kontext

Liegt in `apps/accounts/`. Vor dem Redesign:

- User-Modell mit Identity-Feldern + `membership_level` + `language`.
- `AccountAuditLog` mit EventTypes für Membership + Region-Assignment + Region-CRUD.
- `StationAuditLog` (in `apps/stations/`) loggt Station-Assignment-CRUD aus Station-Subject-Sicht.

Dieser Sub-Spec ergänzt:

- Schema-Erweiterung mit 10 neuen Profil-Feldern am User-Modell.
- 8 neue Audit-EventTypes.
- Signal-Doppel-Emit für Station-Assignment (zusätzlicher AccountAuditLog-Eintrag pro Subject-User).
- Eigenständiges `apps/accounts/visibility.py`-Modul mit dem Audience-Modell.
- Eigenständiges `apps/accounts/geocoding.py`-Modul (Nominatim + Maidenhead).
- Avatar-Upload-Helper-Module in `apps/accounts/avatars.py` (Pillow Resize + Validation).

Alle Module sind pure Python, ohne UI-Abhängigkeit. Sie haben eigenständige Tests.

---

## 2. Schema-Erweiterung

### 2.1 Neue Felder am `User`-Modell

```python
# apps/accounts/models.py

import re
from django.core.validators import RegexValidator
from django.utils.translation import gettext_lazy as _

LOCATOR_REGEX = re.compile(r"^[A-R]{2}[0-9]{2}[A-X]{2}$")

locator_validator = RegexValidator(
    regex=LOCATOR_REGEX,
    message=_("Maidenhead locator must be 2 letters + 2 digits + 2 letters (e.g. JN78AB)."),
)

def _avatar_upload_path(instance, filename):
    """avatars/<user_id>/<random>.<ext>

    Hash-basiert: jeder Upload erzeugt einen frischen Pfad — alte Files bleiben
    orphaned (Cleanup-Job out-of-scope, siehe Overview Sektion 7).
    """
    import uuid
    from pathlib import Path
    ext = Path(filename).suffix.lower() or ".jpg"
    return f"avatars/{instance.pk or 'new'}/{uuid.uuid4().hex[:12]}{ext}"


class User(AbstractUser):
    ...
    # Bestehende Felder bleiben unverändert: language, membership_level, ...

    # NEU
    bio = models.TextField(_("bio"), max_length=500, blank=True)
    avatar = models.ImageField(
        _("avatar"),
        upload_to=_avatar_upload_path,
        null=True,
        blank=True,
    )
    qth_name = models.CharField(_("QTH name"), max_length=128, blank=True)
    qrz_url = models.URLField(_("QRZ URL"), max_length=200, blank=True)
    address = models.TextField(_("address"), blank=True)
    phone = models.CharField(_("phone"), max_length=32, blank=True)
    latitude = models.DecimalField(
        _("latitude"),
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
    )
    longitude = models.DecimalField(
        _("longitude"),
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
    )
    locator = models.CharField(
        _("Maidenhead locator"),
        max_length=6,
        blank=True,
        validators=[locator_validator],
    )
    is_directory_visible = models.BooleanField(
        _("visible in member directory"),
        default=True,
    )
```

Alle Felder optional + Defaults — bestehende Nutzer starten mit leeren Profilen und `is_directory_visible=True`.

### 2.2 Migration

`apps/accounts/migrations/0XXX_user_profile_fields.py`:

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
                        field=models.CharField(blank=True, max_length=6,
                                                verbose_name="Maidenhead locator",
                                                validators=[locator_validator])),
    migrations.AddField(model_name="user", name="is_directory_visible",
                        field=models.BooleanField(default=True,
                                                   verbose_name="visible in member directory")),
]
```

### 2.3 Django-Admin-Erweiterung

`apps/accounts/admin.py` registriert die neuen Felder im `UserAdmin`. Damit kann der Admin sie übergangsweise im Django-Admin füllen, bis 1b und 1c die UI bringen.

```python
class UserAdmin(BaseUserAdmin):
    fieldsets = BaseUserAdmin.fieldsets + (
        (_("Profile"), {
            "fields": ("avatar", "bio", "qth_name", "qrz_url", "phone"),
        }),
        (_("Address & Location"), {
            "fields": ("address", "latitude", "longitude", "locator"),
        }),
        (_("Directory"), {
            "fields": ("is_directory_visible",),
        }),
    )
```

---

## 3. Audit — EventTypes + Doppel-Emit

### 3.1 EventType-Erweiterung

`apps/accounts/models.py:AccountAuditLog.EventType`:

```python
USER_CREATED              = "user_created",                _("User Created")
USER_UPDATED              = "user_updated",                _("User Updated")
USER_DELETED              = "user_deleted",                _("User Deleted")
USER_ACTIVATED            = "user_activated",              _("User Activated")
USER_DEACTIVATED          = "user_deactivated",            _("User Deactivated")
PASSWORD_CHANGED          = "password_changed",            _("Password Changed")
STATION_ASSIGNMENT_CREATED = "station_assignment_created", _("Station Assignment Created")
STATION_ASSIGNMENT_REVOKED = "station_assignment_revoked", _("Station Assignment Revoked")
```

Migration `apps/accounts/migrations/0XXX_audit_user_crud_event_types.py` ist ein `AlterField` auf `event_type.choices` — TextChoices-Erweiterung erfordert in Django keine DB-Änderung, aber `makemigrations` möchte den State-Change festschreiben.

In diesem Sub-Spec werden **keine** Emissionen für USER_*-Events eingebaut — die kommen in 1c (UserCreateView, UserUpdateView, UserDeleteView, ProfilePasswordChangeView). Hier wird nur der EventType-Katalog gepflegt + der Station-Assignment-Doppel-Emit.

### 3.2 Station-Assignment-Doppel-Emit

`apps/stations/signals.py` bekommt zusätzlich einen `AccountAuditLog`-Eintrag pro `StationAssignment`-Save/Delete:

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

Doppel-Emit ist gewollt: pro Subjekt eine Sicht. Im gemergten globalen Feed taucht das Event als zwei Zeilen auf — bewusst und konsistent mit der Audit-Modell-Trennung.

---

## 4. Visibility-Helper (`apps/accounts/visibility.py`)

Neues Modul, zentrale Berechnung für die Audience-Sichtbarkeit. Wird in 1b und 1c importiert.

### 4.1 Audience-Enum

```python
# apps/accounts/visibility.py

import enum
from django.contrib.auth import get_user_model

User = get_user_model()


class Audience(enum.Enum):
    ADMIN = "admin"
    SELF = "self"
    MEMBER = "member"
    APPLICANT = "applicant"  # Self-Variante für Applicant
```

### 4.2 `audience_for(viewer, target) -> Audience | None`

```python
def audience_for(viewer, target):
    """Berechnet die Audience-Stufe.

    Returns None für 'no access' (-> 404 im View).

    Wichtig: viewer und target sind beide User-Instanzen. viewer ist der
    eingeloggte User (request.user), target ist der User, dessen Daten
    gesehen werden sollen.
    """
    if not viewer.is_authenticated:
        return None
    if viewer.is_admin:
        return Audience.ADMIN
    if viewer.pk == target.pk:
        # Self-Sicht — auch Applicant darf sich selbst sehen.
        if viewer.membership_level == User.MembershipLevel.APPLICANT:
            return Audience.APPLICANT
        return Audience.SELF
    # Cross-User-Sicht.
    if viewer.membership_level == User.MembershipLevel.APPLICANT:
        return None  # Applicants sehen niemand außer sich selbst.
    if target.membership_level == User.MembershipLevel.APPLICANT:
        return None  # Member sehen Applicants nicht.
    return Audience.MEMBER
```

### 4.3 Field-Sichtbarkeits-Sets

```python
# Felder, die jeder logged-in Member sehen darf (wenn target.is_directory_visible).
# Reihenfolge mirroring der Anzeige im Overview-Tab.
PUBLIC_PROFILE_FIELDS = frozenset({
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
})

# Zusätzlich nur Self und Admin.
PRIVATE_PROFILE_FIELDS = frozenset({
    "address",
    "phone",
    "latitude", "longitude",   # numerisch, in Admin-Debug-Block
    "language",
    "last_login",              # Self sieht eigenen, Admin sieht alle
    "is_active",               # Self sieht eigenen Aktivitätsstatus
    "is_directory_visible",
})

# Nur Admin.
ADMIN_ONLY_FIELDS = frozenset({
    "sso_grants",
    "sso_sessions",
    "tag_memberships",
    "global_audit_actions",  # Promote/Demote, Region-/Station-Assignment-Mgmt
})

# Für Master-Switch-False Sicht.
MINIMAL_DIRECTORY_FIELDS = frozenset({
    "username",
    "membership_level",
    "avatar",
})
```

### 4.4 `directory_visible_fields(viewer, target) -> frozenset[str]`

```python
def directory_visible_fields(viewer, target):
    """Set der String-Field-Keys, die `viewer` von `target` sehen darf.

    Konsumenten (Templates, Serializer) prüfen für jedes Feld:
        if "phone" in visible_fields and target.phone: ...
    """
    aud = audience_for(viewer, target)
    if aud is None:
        return frozenset()
    if aud == Audience.ADMIN:
        return PUBLIC_PROFILE_FIELDS | PRIVATE_PROFILE_FIELDS | ADMIN_ONLY_FIELDS
    if aud in (Audience.SELF, Audience.APPLICANT):
        # Self/Applicant: ihre eigenen privaten Felder (read-only).
        # ADMIN_ONLY_FIELDS bleiben außen vor — z.B. eigene SSO-Sessions sind
        # in 1b über eine separate "sso_sessions_self"-Variante sichtbar.
        return PUBLIC_PROFILE_FIELDS | PRIVATE_PROFILE_FIELDS | frozenset({"sso_sessions_self"})
    # Audience.MEMBER:
    if not target.is_directory_visible:
        return MINIMAL_DIRECTORY_FIELDS
    return PUBLIC_PROFILE_FIELDS
```

### 4.5 Hilfsfunktion: `user_can_view_directory(viewer) -> bool`

```python
def user_can_view_directory(viewer):
    """Gate für die Liste (UserListView). Applicants haben keine Liste."""
    if not viewer.is_authenticated:
        return False
    return (
        viewer.is_admin
        or viewer.membership_level != User.MembershipLevel.APPLICANT
    )
```

### 4.6 Nicht-Ziele dieses Moduls

- Kein UI-Rendering.
- Kein DB-Query (nur Set-Berechnung aus zwei User-Instanzen).
- Keine Berücksichtigung von Field-Existence (`target.phone is not None`) — das macht der Konsument im Template.

---

## 5. Geocoding-Service (`apps/accounts/geocoding.py`)

Neues Modul, dient als reine Funktions-Sammlung. Wird in 1c im `form_valid` der Edit/Profile-Forms aufgerufen.

### 5.1 `geocode_address(address: str) -> tuple[Decimal, Decimal] | None`

```python
# apps/accounts/geocoding.py

import logging
import time
from decimal import Decimal
from typing import Optional

import requests

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_TIMEOUT = 10  # Sekunden
USER_AGENT = "OE5XRX-StationManager/1.0 (peter.buchegger7@gmail.com)"


def geocode_address(address: str) -> Optional[tuple[Decimal, Decimal]]:
    """Resolve eine Postadresse zu (latitude, longitude).

    Returns None bei Fehler (Network, kein Result, Timeout).
    Throttled per call: ein time.sleep(1) am Start serialisiert mit der
    Nominatim-Free-Tier-Rate-Limit-Policy.
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
    except (requests.RequestException, ValueError, KeyError) as exc:
        logger.warning("Nominatim geocode failed for address %r: %s", address, exc)
        return None
```

### 5.2 `lat_lon_to_locator(lat, lon, precision=6) -> str`

```python
def lat_lon_to_locator(lat, lon, precision: int = 6) -> str:
    """Maidenhead-Locator aus lat/lon.

    precision=6 ergibt ein 6-Zeichen-Locator (z.B. 'JN78AB').
    precision=4 ergibt ein 4-Zeichen-Locator (Grid-square 'JN78').
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
        lat_sub = int(lat_rest * 24)
        out += chr(A + lon_sub) + chr(A + lat_sub)
    return out
```

### 5.3 `USER_AGENT` als Config

Aktuell hardcoded — bei späteren Forks im Folge-Spec parametrierbar machen über `settings.NOMINATIM_USER_AGENT`. Für 1a reicht der Konstanten-Default.

### 5.4 Nicht-Ziele

- Kein Reverse-Geocoding.
- Kein User-Address-Validation (nur Lookup oder None).
- Kein Caching über mehrere Aufrufe hinaus — Caller speichert lat/lon im User-Objekt.

---

## 6. Avatar-Upload-Pipeline (`apps/accounts/avatars.py`)

Neues Hilfsmodul. Wird in 1c von ProfileForm + UserChangeForm aufgerufen.

### 6.1 `validate_avatar_upload(file)`

```python
# apps/accounts/avatars.py

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

MAX_AVATAR_BYTES = 2 * 1024 * 1024  # 2 MB


def validate_avatar_upload(file):
    """Raises ValidationError wenn die Datei nicht Avatar-tauglich ist.

    Aufrufer in 1c: ProfileForm.clean_avatar() ruft das hier.
    """
    if file is None:
        return
    if file.size > MAX_AVATAR_BYTES:
        raise ValidationError(_("Avatar darf max. 2 MB sein."))
    from PIL import Image, UnidentifiedImageError
    try:
        # Pillow Verify lädt den File-Header und prüft Format.
        img = Image.open(file)
        img.verify()
        file.seek(0)  # img.verify konsumiert das Datei-Cursor-Stand
    except (UnidentifiedImageError, OSError) as exc:
        raise ValidationError(_("Datei ist kein gültiges Bild.")) from exc
```

### 6.2 `process_avatar_file(file_field_path: str)`

```python
def process_avatar_file(file_field_path: str) -> None:
    """In-place: lädt das Avatar-File, resized auf max 512x512,
    schreibt als JPEG quality=85 zurück.

    Aufrufer in 1c: nach Form.save() ruft das hier mit user.avatar.path.
    Macht aus PNG/WebP/JPEG ein normalisiertes JPEG.
    """
    from PIL import Image
    img = Image.open(file_field_path)
    img.thumbnail((512, 512))
    img.convert("RGB").save(file_field_path, "JPEG", quality=85, optimize=True)
```

### 6.3 Nicht-Ziele

- Kein orphaned-File-Cleanup (alte Avatar-Files bleiben liegen — siehe Overview Sektion 7).
- Kein Crop-UI (User lädt Bild, System resized — kein Server-side Crop-Choice).
- Kein Animated-GIF-Support (Pillow konvertiert zu Static-JPEG).

---

## 7. Migrations

Zwei Migrations in diesem Sub-Spec:

- `0XXX_user_profile_fields.py` — 10 neue Felder am User-Modell (Sektion 2.2).
- `0XXX_audit_user_crud_event_types.py` — AlterField auf `AccountAuditLog.event_type.choices` (Sektion 3.1).

Beide unabhängig, können auch zusammengelegt werden — getrennt ist klarer für die Audit-Spur (welche Migration brachte welchen Schema-Change).

---

## 8. Testing

### 8.1 Schema + Migration

`apps/accounts/tests/test_user_model_fields.py` (neu):

- Neue Felder existieren am `User`-Modell.
- Defaults: `bio=""`, `is_directory_visible=True`, `avatar=None`, `latitude=None` etc.
- Locator-Validator akzeptiert `JN78AB`, lehnt `JN78` / `xx78AB` / `JNXXAB` ab.

### 8.2 Audit-EventTypes

`apps/accounts/tests/test_audit_event_types.py` (neu):

- Alle 8 neuen EventType-Werte sind im `AccountAuditLog.EventType` enthalten.
- `get_event_type_display()` returnt human-readable für jeden Wert.

### 8.3 Station-Assignment-Doppel-Emit

`apps/accounts/tests/test_station_assignment_doppel_emit.py` (neu):

- Create StationAssignment → exact ein neuer `StationAuditLog`-Eintrag + exact ein neuer `AccountAuditLog`-Eintrag mit `target_user=instance.user`.
- Delete StationAssignment → entsprechende Revoke-Einträge in beiden Logs.
- Bestehende `apps/stations/tests/test_signals.py` bleibt grün.

### 8.4 Visibility-Helper

`apps/accounts/tests/test_visibility.py` (neu):

- `audience_for`:
  - admin sieht anyone → `ADMIN`.
  - self sieht self → `SELF`.
  - member sieht other member → `MEMBER`.
  - member sieht applicant → `None`.
  - applicant sieht self → `APPLICANT`.
  - applicant sieht other applicant → `None`.
  - applicant sieht member → `None`.
  - anonymous sieht anyone → `None`.

- `directory_visible_fields`:
  - admin → `PUBLIC | PRIVATE | ADMIN_ONLY` (Set enthält u.a. `is_active`, `last_login`).
  - self → `PUBLIC | PRIVATE | {"sso_sessions_self"}` (Set enthält `is_active` und `last_login` der eigenen Person, aber kein SSO-Grant-Mgmt etc.).
  - member sieht other (directory-visible=True) → `PUBLIC` (kein `is_active`, kein `last_login`, kein `phone`).
  - member sieht other (directory-visible=False) → `MINIMAL_DIRECTORY_FIELDS`.
  - no-access (applicant→member) → leeres Set.

- `user_can_view_directory`:
  - admin → True.
  - member → True.
  - applicant → False.
  - anonymous → False.

### 8.5 Geocoding-Service

`apps/accounts/tests/test_geocoding.py` (neu, mit `responses`-Mock):

- `geocode_address("Hauptstraße 1, 4020 Linz")` → mockt Nominatim, prüft `(Decimal, Decimal)`-Tupel.
- Bei HTTP-500 → `None` ohne Exception.
- Bei HTTP-Timeout → `None`.
- Bei leerem Result → `None`.
- Bei leerem Address → `None`.
- User-Agent-Header ist gesetzt.
- Rate-Limit-`time.sleep(1)` wird gerufen (Mock-Time).

`apps/accounts/tests/test_locator.py` (neu):

- `lat_lon_to_locator(48.31, 14.29)` → `JN78AB` (oder exakter Wert per Re-Calc).
- Edge-Cases: Equator (lat=0), Date-Line (lon=180), Negative-Coords (lat<0, lon<0).
- Precision-4 ergibt 4-Zeichen-Locator.

### 8.6 Avatar-Upload

`apps/accounts/tests/test_avatar.py` (neu):

- `validate_avatar_upload`:
  - 3 MB JPEG → ValidationError.
  - Text-Datei mit `.png`-Endung → ValidationError.
  - Valider JPEG → kein Error.
- `process_avatar_file`:
  - 1024×768 JPEG-Eingabe → nach Aufruf 512×<scaled> JPEG.
  - PNG-Eingabe → nach Aufruf JPEG.
  - Transparenz (PNG mit Alpha) → konvertiert zu RGB.

### 8.7 Django-Admin-Erweiterung

`apps/accounts/tests/test_admin.py` (existiert wahrscheinlich; ergänzen):

- UserAdmin-Fieldsets enthalten "Profile", "Address & Location", "Directory".
- Admin-Seite rendert ohne Fehler mit den neuen Feldern.

---

## 9. Implementation-Reihenfolge

**Round-1 — Foundation** (in Subagent-driven-development parallelisierbar):

1. **Phase 1: Schema-Migration + locator-Validator + Admin-Erweiterung.**
   Subagent: `vault` (Schema + Migration).
   Dauer: ~1-2h.

2. **Phase 2: Audit-EventType-Erweiterung + Migration.**
   Subagent: `gateway` (Audit-Modell).
   Dauer: ~30min. Kann parallel zu Phase 1 laufen — andere File.

3. **Phase 3: Visibility-Helper `apps/accounts/visibility.py` + Test-Suite.**
   Subagent: `gateway`.
   Dauer: ~2h. Pure Python, unabhängig.

4. **Phase 4: Geocoding-Service `apps/accounts/geocoding.py` + Locator-Berechnung + Test-Suite (responses-mock).**
   Subagent: `gateway`.
   Dauer: ~2h. Pure Python, unabhängig.

5. **Phase 5: Avatar-Upload-Helper `apps/accounts/avatars.py` + Test-Suite (Pillow).**
   Subagent: `gateway`.
   Dauer: ~1-2h. Pure Python, unabhängig.

**Round-1.5: code-simplifier**.

**Round-2 — Watcher** (alle parallel):

- `audit` auf alle neuen Module: MISRA-äquivalent für Python, Coverage, Conventions.
- `guard` auf `geocoding.py`: External-Service-Best-Practices, Timeout, User-Agent, Rate-Limit-Compliance.
- `vault` auf Schema + Migrations: Constraints, Defaults, Reversibility.
- `probe` auf E2E: bestehende Tests bleiben grün, `python manage.py migrate` + `python manage.py test` passen sauber. Smoke-Test im Django-Admin.

**Round-2.5: probe** (Test-Writer): Coverage-Gaps füllen falls Watcher welche meldet.

**Round-3 — `pr-review-toolkit:review-pr`** → finaler Review-Lauf vor Commit + Push.

---

## 10. Out-of-Scope (für 1a)

- Alle UI-Änderungen → 1b (Detail/List) und 1c (Edit/Profile).
- Welcome-Email-Flow, Soft-Delete → Spec #2 (siehe Overview Sektion 6).
- USER_*-Audit-Emissionen im `form_valid` → 1c (dort, wo die Views existieren).
- ProfilePasswordChangeView → 1c.

---

## 11. Offene Punkte

Keine. Alle Entscheidungen sind im Master-Overview (Sektion 8) festgehalten und in den Sektionen 2-9 dieses Sub-Specs konkretisiert.
