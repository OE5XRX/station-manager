# Sub-Spec 1a Foundation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Backend-Foundation für den User-Domain-Redesign: 10 neue Profil-Felder am User-Modell, 8 neue Audit-EventTypes, Visibility-Helper-Modul, Geocoding-Service, Avatar-Upload-Pipeline. Keine UI-Änderungen.

**Architecture:** Reine Backend-Erweiterung in `apps/accounts/`. Drei neue Helper-Module (`visibility.py`, `geocoding.py`, `avatars.py`), zwei Migrationen (User-Felder, EventType-Choices), Erweiterung des bestehenden `apps/stations/signals.py` (Station-Assignment-Doppel-Emit), Erweiterung der `UserAdmin`-Fieldsets. Alle neuen Module sind pure Python ohne UI-Abhängigkeit.

**Tech Stack:** Python 3.14, Django 6.0, Pillow ≥ 11.0 (avatar resize), requests ≥ 2.32 (geocoding HTTP), pytest ≥ 8.0 + pytest-django ≥ 4.9 (testing).

**Spec:** `docs/superpowers/specs/2026-06-12-user-domain-1a-foundation-design.md`
**Overview:** `docs/superpowers/specs/2026-06-09-user-domain-redesign-overview.md`

---

## File Structure

### Files to CREATE

| Pfad | Zweck |
|---|---|
| `apps/accounts/visibility.py` | Audience-Enum + `audience_for` + Field-Sets + `directory_visible_fields` + `user_can_view_directory`. Reine Pure-Python-Logik. |
| `apps/accounts/geocoding.py` | `geocode_address` (Nominatim) + `lat_lon_to_locator` (Maidenhead). Externe-Service-Wrapper + Pure-Math. |
| `apps/accounts/avatars.py` | `validate_avatar_upload` (Size + Format-Check) + `process_avatar_file` (Pillow Resize). Filesystem-Helper. |
| `apps/accounts/migrations/0008_user_profile_fields.py` | 10 neue Felder am User-Modell. |
| `apps/accounts/migrations/0009_audit_user_crud_event_types.py` | AlterField auf `AccountAuditLog.event_type.choices`. |
| `tests/test_user_profile_fields.py` | Tests für neue User-Felder + Validator. |
| `tests/test_user_audit_event_types.py` | Tests für neue EventType-Werte. |
| `tests/test_user_station_assignment_audit.py` | Tests für Station-Assignment-Doppel-Emit. |
| `tests/test_user_visibility.py` | Tests für visibility.py. |
| `tests/test_user_geocoding.py` | Tests für geocode_address. |
| `tests/test_user_locator.py` | Tests für lat_lon_to_locator. |
| `tests/test_user_avatar.py` | Tests für avatars.py. |

### Files to MODIFY

| Pfad | Änderung |
|---|---|
| `apps/accounts/models.py` | 10 neue Felder am `User`-Modell + 8 neue `AccountAuditLog.EventType`-Werte + Modul-Konstanten `LOCATOR_REGEX`, `locator_validator`, Funktion `_avatar_upload_path`. |
| `apps/accounts/admin.py` | `UserAdmin.fieldsets` um „Profile", „Address & Location", „Directory"-Fieldsets erweitern. |
| `apps/stations/signals.py` | `_on_station_assignment_save` und `_on_station_assignment_delete` zusätzlich `AccountAuditLog`-Eintrag schreiben. |

### Files unchanged

Alle UI-Templates, alle Views, alle Forms — siehe Sub-Spec 1b/1c.

---

## Tasks

### Task 1: Pre-flight + baseline sanity

**Files:**
- Read only

- [ ] **Step 1: Verify current branch and worktree**

Run: `git -C /home/pbuchegger/OE5XRX/station-manager/.worktrees/feat-user-domain-redesign branch --show-current`
Expected output: `feat/user-domain-redesign`

- [ ] **Step 2: Run baseline test suite (all tests should pass before changes)**

Run: `cd /home/pbuchegger/OE5XRX/station-manager/.worktrees/feat-user-domain-redesign && pytest tests/ -x --tb=short 2>&1 | tail -20`
Expected: All tests pass. If any fail, fix or escalate before continuing.

- [ ] **Step 3: Verify migration baseline**

Run: `cd /home/pbuchegger/OE5XRX/station-manager/.worktrees/feat-user-domain-redesign && python manage.py showmigrations accounts | tail -10`
Expected: Latest migration is `0007_drop_legacy_role_groups`.

---

### Task 2: Locator-Validator + Avatar-Path-Helper als Modul-Konstanten

**Files:**
- Modify: `apps/accounts/models.py` (top of file, before `class User`)
- Test: `tests/test_user_profile_fields.py`

- [ ] **Step 1: Write failing test for LOCATOR_REGEX**

Append to NEW file `tests/test_user_profile_fields.py`:

```python
"""Tests for User profile fields (Sub-Spec 1a Foundation)."""

import pytest


class TestLocatorRegex:
    """Maidenhead 6-char locator format: 2 letters + 2 digits + 2 letters."""

    def test_valid_locator(self):
        from apps.accounts.models import LOCATOR_REGEX
        assert LOCATOR_REGEX.match("JN78AB")
        assert LOCATOR_REGEX.match("AA00AA")
        assert LOCATOR_REGEX.match("RR99XX")

    def test_invalid_locator_too_short(self):
        from apps.accounts.models import LOCATOR_REGEX
        assert not LOCATOR_REGEX.match("JN78")
        assert not LOCATOR_REGEX.match("JN78A")

    def test_invalid_locator_wrong_case(self):
        from apps.accounts.models import LOCATOR_REGEX
        # Regex requires uppercase
        assert not LOCATOR_REGEX.match("jn78ab")

    def test_invalid_locator_digits_in_first_pair(self):
        from apps.accounts.models import LOCATOR_REGEX
        assert not LOCATOR_REGEX.match("12 78AB")
        assert not LOCATOR_REGEX.match("J278AB")

    def test_invalid_locator_letters_out_of_range(self):
        from apps.accounts.models import LOCATOR_REGEX
        # First pair: A-R only
        assert not LOCATOR_REGEX.match("SS78AB")
        # Last pair: A-X only
        assert not LOCATOR_REGEX.match("JN78YY")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_user_profile_fields.py::TestLocatorRegex -v 2>&1 | tail -20`
Expected: ImportError or AttributeError because `LOCATOR_REGEX` doesn't exist in `apps.accounts.models` yet.

- [ ] **Step 3: Implement LOCATOR_REGEX + locator_validator + _avatar_upload_path**

Edit `apps/accounts/models.py`. After the existing imports (around line 8), add:

```python
import re
import uuid
from pathlib import Path

from django.core.validators import RegexValidator

# Maidenhead 6-character grid locator: 2 letters (field, A-R) + 2 digits
# (square, 0-9) + 2 letters (subsquare, A-X). The Maidenhead system is
# defined for amateur radio location reporting.
LOCATOR_REGEX = re.compile(r"^[A-R]{2}[0-9]{2}[A-X]{2}$")

locator_validator = RegexValidator(
    regex=LOCATOR_REGEX,
    message=_("Maidenhead locator must be 2 letters + 2 digits + 2 letters (e.g. JN78AB)."),
)


def _avatar_upload_path(instance, filename):
    """Per-user randomised storage path: avatars/<user_id>/<random>.<ext>.

    Each upload produces a fresh path — old files become orphaned but
    are not auto-cleaned (Cleanup-Job out-of-scope; siehe Overview Sektion 7).
    Using a random suffix means re-uploading the same file twice doesn't
    overwrite (and doesn't break browser caching for the old URL).
    """
    ext = Path(filename).suffix.lower() or ".jpg"
    return f"avatars/{instance.pk or 'new'}/{uuid.uuid4().hex[:12]}{ext}"
```

Note: `_("...")` ist die `gettext_lazy`-Alias-Funktion, die bereits in der Datei importiert ist (`from django.utils.translation import gettext_lazy as _`).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_user_profile_fields.py::TestLocatorRegex -v 2>&1 | tail -20`
Expected: All 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/accounts/models.py tests/test_user_profile_fields.py
git commit -m "feat(accounts): add LOCATOR_REGEX, locator_validator, _avatar_upload_path

Module-level constants/helpers for the new User-profile fields (1a
Foundation). LOCATOR_REGEX validates 6-char Maidenhead format,
_avatar_upload_path produces per-user randomised storage paths."
```

---

### Task 3: 10 neue User-Modell-Felder

**Files:**
- Modify: `apps/accounts/models.py` (inside `class User`)
- Test: `tests/test_user_profile_fields.py`

- [ ] **Step 1: Write failing test for new field defaults**

Append to `tests/test_user_profile_fields.py`:

```python
@pytest.mark.django_db
class TestUserProfileFieldDefaults:
    """Newly added profile fields exist with the expected defaults."""

    def test_bio_default_empty(self):
        from apps.accounts.models import User
        user = User.objects.create_user(username="OE5TEST", password="x")
        assert user.bio == ""

    def test_avatar_default_none(self):
        from apps.accounts.models import User
        user = User.objects.create_user(username="OE5TEST", password="x")
        # ImageField when no file: falsy, often .name == ""
        assert not user.avatar

    def test_qth_name_default_empty(self):
        from apps.accounts.models import User
        user = User.objects.create_user(username="OE5TEST", password="x")
        assert user.qth_name == ""

    def test_qrz_url_default_empty(self):
        from apps.accounts.models import User
        user = User.objects.create_user(username="OE5TEST", password="x")
        assert user.qrz_url == ""

    def test_address_default_empty(self):
        from apps.accounts.models import User
        user = User.objects.create_user(username="OE5TEST", password="x")
        assert user.address == ""

    def test_phone_default_empty(self):
        from apps.accounts.models import User
        user = User.objects.create_user(username="OE5TEST", password="x")
        assert user.phone == ""

    def test_latitude_default_none(self):
        from apps.accounts.models import User
        user = User.objects.create_user(username="OE5TEST", password="x")
        assert user.latitude is None

    def test_longitude_default_none(self):
        from apps.accounts.models import User
        user = User.objects.create_user(username="OE5TEST", password="x")
        assert user.longitude is None

    def test_locator_default_empty(self):
        from apps.accounts.models import User
        user = User.objects.create_user(username="OE5TEST", password="x")
        assert user.locator == ""

    def test_is_directory_visible_default_true(self):
        from apps.accounts.models import User
        user = User.objects.create_user(username="OE5TEST", password="x")
        assert user.is_directory_visible is True


@pytest.mark.django_db
class TestUserLocatorValidator:
    """User.locator field uses locator_validator."""

    def test_valid_locator_saves(self):
        from apps.accounts.models import User
        user = User.objects.create_user(username="OE5TEST", password="x")
        user.locator = "JN78DH"
        user.full_clean()  # runs validators
        user.save()
        user.refresh_from_db()
        assert user.locator == "JN78DH"

    def test_invalid_locator_raises_validation_error(self):
        from django.core.exceptions import ValidationError
        from apps.accounts.models import User
        user = User.objects.create_user(username="OE5TEST", password="x")
        user.locator = "INVALID"
        with pytest.raises(ValidationError):
            user.full_clean()

    def test_empty_locator_allowed(self):
        from django.core.exceptions import ValidationError
        from apps.accounts.models import User
        user = User.objects.create_user(username="OE5TEST", password="x")
        user.locator = ""
        user.full_clean()  # should not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_user_profile_fields.py -v 2>&1 | tail -30`
Expected: New tests FAIL with AttributeError (`user has no attribute 'bio'` etc.). Earlier `TestLocatorRegex` tests still PASS.

- [ ] **Step 3: Add fields to `User` model**

Edit `apps/accounts/models.py`. Inside `class User(AbstractUser)`, add the new fields. Locate the existing `membership_level` field and add the new fields immediately after it (before `objects = UserManager()`):

```python
    # === Profile fields (added in Sub-Spec 1a Foundation) ===
    # Self-Description, max 500 chars
    bio = models.TextField(_("bio"), max_length=500, blank=True)

    # Profile picture; resized to max 512x512 JPEG by ProfileForm.save()
    avatar = models.ImageField(
        _("avatar"),
        upload_to=_avatar_upload_path,
        null=True,
        blank=True,
    )

    # Amateur-radio standortlabel ("QTH" = ham slang for location)
    qth_name = models.CharField(_("QTH name"), max_length=128, blank=True)

    # Public QRZ.com profile URL — convenience deep-link
    qrz_url = models.URLField(_("QRZ URL"), max_length=200, blank=True)

    # Postal address as free text (multi-line). Geocoding consumes this.
    address = models.TextField(_("address"), blank=True)

    # Phone, free format (international)
    phone = models.CharField(_("phone"), max_length=32, blank=True)

    # Geographic coordinates from geocoding `address`. Not user-edited.
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

    # Maidenhead 6-char locator, computed from lat/lon OR user-set override
    locator = models.CharField(
        _("Maidenhead locator"),
        max_length=6,
        blank=True,
        validators=[locator_validator],
    )

    # Master directory-visibility switch. When False, other members see
    # only callsign + membership pill + avatar.
    is_directory_visible = models.BooleanField(
        _("visible in member directory"),
        default=True,
    )
```

- [ ] **Step 4: Generate the migration**

Run: `cd /home/pbuchegger/OE5XRX/station-manager/.worktrees/feat-user-domain-redesign && python manage.py makemigrations accounts --name user_profile_fields`
Expected: Creates `apps/accounts/migrations/0008_user_profile_fields.py`.

- [ ] **Step 5: Apply the migration**

Run: `cd /home/pbuchegger/OE5XRX/station-manager/.worktrees/feat-user-domain-redesign && python manage.py migrate accounts`
Expected: `Applying accounts.0008_user_profile_fields... OK`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_user_profile_fields.py -v 2>&1 | tail -30`
Expected: All tests in TestUserProfileFieldDefaults and TestUserLocatorValidator PASS.

- [ ] **Step 7: Verify no other tests broke**

Run: `pytest tests/ -x --tb=short 2>&1 | tail -20`
Expected: All tests PASS.

- [ ] **Step 8: Commit**

```bash
git add apps/accounts/models.py apps/accounts/migrations/0008_user_profile_fields.py tests/test_user_profile_fields.py
git commit -m "feat(accounts): add 10 profile fields to User model

bio, avatar, qth_name, qrz_url, address, phone, latitude, longitude,
locator, is_directory_visible. All optional with sensible defaults
(empty string / null / True for is_directory_visible).

Foundation for Sub-Spec 1a — UI consumes these in 1b + 1c.
Bestehende User starten mit leeren Profilen und directory-visible=True."
```

---

### Task 4: Django-Admin-Registrierung der neuen Felder

**Files:**
- Modify: `apps/accounts/admin.py`
- Test: `tests/test_user_profile_fields.py`

- [ ] **Step 1: Read current admin.py**

Run: `cat apps/accounts/admin.py`
Note: Identify the existing `UserAdmin` class (or its base).

- [ ] **Step 2: Write failing test for admin fieldsets**

Append to `tests/test_user_profile_fields.py`:

```python
class TestUserAdminFieldsets:
    """UserAdmin exposes the new profile fields in dedicated fieldsets."""

    def test_admin_has_profile_fieldset(self):
        from apps.accounts.admin import UserAdmin
        from django.contrib import admin
        from apps.accounts.models import User

        admin_instance = admin.site._registry.get(User)
        # admin_instance is the registered UserAdmin instance
        assert admin_instance is not None

        fieldset_labels = [fs[0] for fs in admin_instance.fieldsets]
        assert "Profile" in fieldset_labels

    def test_admin_has_address_fieldset(self):
        from django.contrib import admin
        from apps.accounts.models import User

        admin_instance = admin.site._registry.get(User)
        fieldset_labels = [fs[0] for fs in admin_instance.fieldsets]
        assert "Address & Location" in fieldset_labels

    def test_admin_has_directory_fieldset(self):
        from django.contrib import admin
        from apps.accounts.models import User

        admin_instance = admin.site._registry.get(User)
        fieldset_labels = [fs[0] for fs in admin_instance.fieldsets]
        assert "Directory" in fieldset_labels

    def test_profile_fieldset_contains_expected_fields(self):
        from django.contrib import admin
        from apps.accounts.models import User

        admin_instance = admin.site._registry.get(User)
        profile_fieldset = next(
            fs for fs in admin_instance.fieldsets if fs[0] == "Profile"
        )
        fields = profile_fieldset[1]["fields"]
        assert "avatar" in fields
        assert "bio" in fields
        assert "qth_name" in fields
        assert "qrz_url" in fields
        assert "phone" in fields

    def test_address_fieldset_contains_expected_fields(self):
        from django.contrib import admin
        from apps.accounts.models import User

        admin_instance = admin.site._registry.get(User)
        addr_fieldset = next(
            fs for fs in admin_instance.fieldsets if fs[0] == "Address & Location"
        )
        fields = addr_fieldset[1]["fields"]
        assert "address" in fields
        assert "latitude" in fields
        assert "longitude" in fields
        assert "locator" in fields

    def test_directory_fieldset_contains_is_directory_visible(self):
        from django.contrib import admin
        from apps.accounts.models import User

        admin_instance = admin.site._registry.get(User)
        dir_fieldset = next(
            fs for fs in admin_instance.fieldsets if fs[0] == "Directory"
        )
        assert "is_directory_visible" in dir_fieldset[1]["fields"]
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_user_profile_fields.py::TestUserAdminFieldsets -v 2>&1 | tail -20`
Expected: AssertionError because the new fieldsets don't exist yet.

- [ ] **Step 4: Extend UserAdmin in admin.py**

Edit `apps/accounts/admin.py`. The current state ends with:

```python
fieldsets = BaseUserAdmin.fieldsets + ((_("Station Manager"), {"fields": ("language",)}),)

add_fieldsets = BaseUserAdmin.add_fieldsets + (
    (_("Station Manager"), {"fields": ("language",)}),
)
```

Replace the `fieldsets` line with the version that appends three more fieldsets (Profile, Address & Location, Directory). Leave `add_fieldsets` unchanged — new users get created with identity-only fields, profile gets filled later:

```python
fieldsets = BaseUserAdmin.fieldsets + (
    (_("Station Manager"), {"fields": ("language",)}),
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

Note: The test uses string literals (`"Profile"`, `"Address & Location"`, `"Directory"`) — but `_("Profile")` returns a `__proxy__` object. The string-comparison works because Django's translation-proxies compare equal to their source string when not in a translation context. If the test fails with proxy-vs-str mismatch, change the test to compare `str(fs[0])` instead.

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_user_profile_fields.py::TestUserAdminFieldsets -v 2>&1 | tail -20`
Expected: All 6 tests PASS. If proxy-comparison fails: edit test to wrap `fs[0]` in `str()`.

- [ ] **Step 6: Smoke-test admin in browser (optional manual step)**

Run dev-server briefly and verify User-Change-Page renders without 500:
Run: `cd /home/pbuchegger/OE5XRX/station-manager/.worktrees/feat-user-domain-redesign && python manage.py check`
Expected: `System check identified no issues (0 silenced).`

- [ ] **Step 7: Commit**

```bash
git add apps/accounts/admin.py tests/test_user_profile_fields.py
git commit -m "feat(accounts): register new profile fields in UserAdmin

Three new fieldsets (Profile, Address & Location, Directory) make
the new fields editable through Django Admin. Übergangsweise füllen
Admins so Profile-Daten, bis 1c die ProfileView-UI bringt."
```

---

### Task 5: 8 neue AccountAuditLog EventTypes

**Files:**
- Modify: `apps/accounts/models.py` (inside `class AccountAuditLog.EventType`)
- Test: `tests/test_user_audit_event_types.py`

- [ ] **Step 1: Write failing test for new EventType values**

Create NEW file `tests/test_user_audit_event_types.py`:

```python
"""Tests for AccountAuditLog EventType additions (Sub-Spec 1a)."""

import pytest

from apps.accounts.models import AccountAuditLog


class TestNewEventTypes:
    """Eight new EventType members added for user-CRUD + station-assignment."""

    def test_user_created_present(self):
        assert AccountAuditLog.EventType.USER_CREATED == "user_created"

    def test_user_updated_present(self):
        assert AccountAuditLog.EventType.USER_UPDATED == "user_updated"

    def test_user_deleted_present(self):
        assert AccountAuditLog.EventType.USER_DELETED == "user_deleted"

    def test_user_activated_present(self):
        assert AccountAuditLog.EventType.USER_ACTIVATED == "user_activated"

    def test_user_deactivated_present(self):
        assert AccountAuditLog.EventType.USER_DEACTIVATED == "user_deactivated"

    def test_password_changed_present(self):
        assert AccountAuditLog.EventType.PASSWORD_CHANGED == "password_changed"

    def test_station_assignment_created_present(self):
        assert AccountAuditLog.EventType.STATION_ASSIGNMENT_CREATED == "station_assignment_created"

    def test_station_assignment_revoked_present(self):
        assert AccountAuditLog.EventType.STATION_ASSIGNMENT_REVOKED == "station_assignment_revoked"

    def test_all_existing_event_types_still_present(self):
        """Regression: existing EventTypes must not be removed."""
        assert AccountAuditLog.EventType.MEMBERSHIP_PROMOTED == "membership_promoted"
        assert AccountAuditLog.EventType.MEMBERSHIP_DEMOTED == "membership_demoted"
        assert AccountAuditLog.EventType.REGION_ASSIGNMENT_CREATED == "region_assignment_created"
        assert AccountAuditLog.EventType.REGION_ASSIGNMENT_REVOKED == "region_assignment_revoked"
        assert AccountAuditLog.EventType.REGION_CREATED == "region_created"
        assert AccountAuditLog.EventType.REGION_UPDATED == "region_updated"
        assert AccountAuditLog.EventType.REGION_DELETED == "region_deleted"


@pytest.mark.django_db
class TestEventTypeDBPersistence:
    """New EventTypes are saveable to AccountAuditLog."""

    def test_user_created_persists(self):
        entry = AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.USER_CREATED,
            message="OE5TEST <test@example.org>",
        )
        assert entry.pk is not None
        entry.refresh_from_db()
        assert entry.event_type == "user_created"

    def test_station_assignment_created_persists(self):
        entry = AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.STATION_ASSIGNMENT_CREATED,
            message="station=OE5XRX, role=admin",
        )
        assert entry.pk is not None
        entry.refresh_from_db()
        assert entry.event_type == "station_assignment_created"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_user_audit_event_types.py -v 2>&1 | tail -30`
Expected: AttributeError because `EventType.USER_CREATED` etc. don't exist yet.

- [ ] **Step 3: Add the 8 new EventType members**

Edit `apps/accounts/models.py`. In `class AccountAuditLog`, find `class EventType(models.TextChoices)`. Add the new members **after** the existing ones (keep existing order intact for migration stability):

```python
class EventType(models.TextChoices):
    # === Existing (do not reorder) ===
    MEMBERSHIP_PROMOTED = "membership_promoted", _("Membership Promoted")
    MEMBERSHIP_DEMOTED = "membership_demoted", _("Membership Demoted")
    REGION_ASSIGNMENT_CREATED = "region_assignment_created", _("Region Assignment Created")
    REGION_ASSIGNMENT_REVOKED = "region_assignment_revoked", _("Region Assignment Revoked")
    REGION_CREATED = "region_created", _("Region Created")
    REGION_UPDATED = "region_updated", _("Region Updated")
    REGION_DELETED = "region_deleted", _("Region Deleted")
    # === Added in Sub-Spec 1a Foundation ===
    USER_CREATED = "user_created", _("User Created")
    USER_UPDATED = "user_updated", _("User Updated")
    USER_DELETED = "user_deleted", _("User Deleted")
    USER_ACTIVATED = "user_activated", _("User Activated")
    USER_DEACTIVATED = "user_deactivated", _("User Deactivated")
    PASSWORD_CHANGED = "password_changed", _("Password Changed")
    STATION_ASSIGNMENT_CREATED = "station_assignment_created", _("Station Assignment Created")
    STATION_ASSIGNMENT_REVOKED = "station_assignment_revoked", _("Station Assignment Revoked")
```

- [ ] **Step 4: Generate the migration**

Run: `cd /home/pbuchegger/OE5XRX/station-manager/.worktrees/feat-user-domain-redesign && python manage.py makemigrations accounts --name audit_user_crud_event_types`
Expected: Creates `apps/accounts/migrations/0009_audit_user_crud_event_types.py`. Will be an `AlterField` on `event_type.choices`.

- [ ] **Step 5: Apply the migration**

Run: `cd /home/pbuchegger/OE5XRX/station-manager/.worktrees/feat-user-domain-redesign && python manage.py migrate accounts`
Expected: `Applying accounts.0009_audit_user_crud_event_types... OK`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_user_audit_event_types.py -v 2>&1 | tail -30`
Expected: All tests PASS.

- [ ] **Step 7: Verify pre-existing audit-log test still passes**

Run: `pytest tests/test_account_audit_log.py -v 2>&1 | tail -20`
Expected: All existing tests PASS (especially `test_event_type_choices`).

- [ ] **Step 8: Commit**

```bash
git add apps/accounts/models.py apps/accounts/migrations/0009_audit_user_crud_event_types.py tests/test_user_audit_event_types.py
git commit -m "feat(accounts): add USER_CRUD and STATION_ASSIGNMENT EventTypes

8 new event types in AccountAuditLog.EventType:
- USER_CREATED, UPDATED, DELETED, ACTIVATED, DEACTIVATED
- PASSWORD_CHANGED
- STATION_ASSIGNMENT_CREATED, STATION_ASSIGNMENT_REVOKED

State-only migration (TextChoices addition). Emissions in 1c view
form_valid + 1a stations/signals.py (next task)."
```

---

### Task 6: Station-Assignment-Doppel-Emit in signals.py

**Files:**
- Modify: `apps/stations/signals.py`
- Test: `tests/test_user_station_assignment_audit.py`

- [ ] **Step 1: Write failing test for AccountAuditLog emission on save**

Create NEW file `tests/test_user_station_assignment_audit.py`:

```python
"""Tests for AccountAuditLog doppel-emit on StationAssignment save/delete.

Sub-Spec 1a Foundation Sektion 3.2. Bestehender StationAuditLog-Emit
bleibt unverändert — wir prüfen den ZUSÄTZLICHEN AccountAuditLog-Eintrag.
"""

import pytest

from apps.accounts.models import AccountAuditLog, User
from apps.stations.models import Region, Station, StationAssignment, StationAuditLog


@pytest.fixture
def region(db):
    return Region.objects.create(name="Innviertel")


@pytest.fixture
def station(db, region):
    return Station.objects.create(name="OE5XRX-Test", callsign="OE5XRX", region=region)


@pytest.fixture
def assigner(db):
    # Wer das Assignment vergibt (z.B. ein Admin).
    return User.objects.create_user(
        username="OE5ADMIN",
        password="x",
        membership_level=User.MembershipLevel.ADMIN,
    )


@pytest.fixture
def member(db):
    return User.objects.create_user(
        username="OE5MEMBER",
        password="x",
        membership_level=User.MembershipLevel.MEMBER,
    )


@pytest.mark.django_db
class TestStationAssignmentDoppelEmit:
    """Pro StationAssignment.save schreibt das Signal sowohl
    StationAuditLog als auch AccountAuditLog."""

    def test_create_emits_account_audit_log(self, station, member, assigner):
        before = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.STATION_ASSIGNMENT_CREATED
        ).count()
        StationAssignment.objects.create(
            station=station,
            user=member,
            role=StationAssignment.Role.MAINTAINER,
            assigned_by=assigner,
        )
        after = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.STATION_ASSIGNMENT_CREATED
        ).count()
        assert after == before + 1

    def test_create_emits_with_target_user(self, station, member, assigner):
        StationAssignment.objects.create(
            station=station,
            user=member,
            role=StationAssignment.Role.MAINTAINER,
            assigned_by=assigner,
        )
        entry = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.STATION_ASSIGNMENT_CREATED,
            target_user=member,
        ).latest("created_at")
        assert entry.target_user == member
        assert entry.actor == assigner

    def test_create_message_contains_station_and_role(self, station, member, assigner):
        StationAssignment.objects.create(
            station=station,
            user=member,
            role=StationAssignment.Role.MAINTAINER,
            assigned_by=assigner,
        )
        entry = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.STATION_ASSIGNMENT_CREATED,
            target_user=member,
        ).latest("created_at")
        # message format: "station=<callsign or name>, role=<role display>"
        assert "OE5XRX" in entry.message
        assert "Station-Maintainer" in entry.message or "maintainer" in entry.message.lower()

    def test_create_also_emits_station_audit_log(self, station, member, assigner):
        """Regression: bestehender StationAuditLog-Emit bleibt unverändert."""
        before = StationAuditLog.objects.filter(
            station=station,
            event_type=StationAuditLog.EventType.STATION_ASSIGNMENT_CREATED,
        ).count()
        StationAssignment.objects.create(
            station=station,
            user=member,
            role=StationAssignment.Role.MAINTAINER,
            assigned_by=assigner,
        )
        after = StationAuditLog.objects.filter(
            station=station,
            event_type=StationAuditLog.EventType.STATION_ASSIGNMENT_CREATED,
        ).count()
        assert after == before + 1

    def test_delete_emits_account_audit_log_revoked(self, station, member, assigner):
        assignment = StationAssignment.objects.create(
            station=station,
            user=member,
            role=StationAssignment.Role.MAINTAINER,
            assigned_by=assigner,
        )
        before = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.STATION_ASSIGNMENT_REVOKED
        ).count()
        assignment.delete()
        after = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.STATION_ASSIGNMENT_REVOKED
        ).count()
        assert after == before + 1

    def test_delete_emits_with_target_user(self, station, member, assigner):
        assignment = StationAssignment.objects.create(
            station=station,
            user=member,
            role=StationAssignment.Role.MAINTAINER,
            assigned_by=assigner,
        )
        assignment.delete()
        entry = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.STATION_ASSIGNMENT_REVOKED,
            target_user=member,
        ).latest("created_at")
        assert entry.target_user == member
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_user_station_assignment_audit.py -v 2>&1 | tail -30`
Expected: Most tests FAIL because no AccountAuditLog emission yet. `test_create_also_emits_station_audit_log` should PASS (existing behavior).

- [ ] **Step 3: Extend the signals**

Edit `apps/stations/signals.py`. Find `_on_station_assignment_save` and `_on_station_assignment_delete`. Add the AccountAuditLog emission immediately after the existing StationAuditLog emission:

```python
@receiver(post_save, sender=StationAssignment)
def _on_station_assignment_save(sender, instance, created, **kwargs):
    if not created:
        return
    # Bestehender StationAuditLog-Eintrag (unverändert):
    StationAuditLog.log(
        station=instance.station,
        event_type=StationAuditLog.EventType.STATION_ASSIGNMENT_CREATED,
        user=instance.assigned_by,
        message=f"{instance.user} → {instance.get_role_display()}",
    )
    # NEU in 1a: zusätzlich AccountAuditLog mit target_user=<assignee>
    # so dass User-Detail-Audit-Tab das findet (Subjekt = User).
    AccountAuditLog.log(
        event_type=AccountAuditLog.EventType.STATION_ASSIGNMENT_CREATED,
        actor=instance.assigned_by,
        target_user=instance.user,
        message=(
            f"station={instance.station.callsign or instance.station.name}, "
            f"role={instance.get_role_display()}"
        ),
    )


@receiver(post_delete, sender=StationAssignment)
def _on_station_assignment_delete(sender, instance, **kwargs):
    # Bestehender StationAuditLog-Eintrag (unverändert):
    StationAuditLog.log(
        station=instance.station,
        event_type=StationAuditLog.EventType.STATION_ASSIGNMENT_REVOKED,
        user=None,
        message=(f"{instance.user} ({instance.get_role_display()}) entfernt"),
    )
    # NEU in 1a: zusätzlich AccountAuditLog.
    AccountAuditLog.log(
        event_type=AccountAuditLog.EventType.STATION_ASSIGNMENT_REVOKED,
        target_user=instance.user,
        message=(
            f"station={instance.station.callsign or instance.station.name}, "
            f"role={instance.get_role_display()}"
        ),
    )
```

The imports `AccountAuditLog` und `StationAuditLog` sind bereits in der Datei (siehe Zeile 22 von signals.py).

- [ ] **Step 4: Run new tests to verify they pass**

Run: `pytest tests/test_user_station_assignment_audit.py -v 2>&1 | tail -30`
Expected: All tests PASS.

- [ ] **Step 5: Verify no regression in stations tests**

Run: `pytest tests/ -k "station" --tb=short 2>&1 | tail -30`
Expected: All station-related tests PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/stations/signals.py tests/test_user_station_assignment_audit.py
git commit -m "feat(audit): doppel-emit AccountAuditLog on StationAssignment save/delete

Existing StationAuditLog emission (Subjekt=Station) bleibt unverändert.
Zusätzlicher AccountAuditLog-Eintrag (Subjekt=User) erlaubt das
Per-User-Audit-Tab in 1b, ohne über StationAuditLog.message zu filtern.

Pro Event entstehen zwei Audit-Zeilen — bewusst, eine Sicht pro Subjekt."
```

---

### Task 7: visibility.py — Audience-Enum + audience_for

**Files:**
- Create: `apps/accounts/visibility.py`
- Test: `tests/test_user_visibility.py`

- [ ] **Step 1: Write failing test for audience_for**

Create NEW file `tests/test_user_visibility.py`:

```python
"""Tests for apps/accounts/visibility.py (Sub-Spec 1a Foundation)."""

import pytest

from apps.accounts.models import User


@pytest.fixture
def admin(db):
    return User.objects.create_user(
        username="OE5ADMIN",
        password="x",
        membership_level=User.MembershipLevel.ADMIN,
    )


@pytest.fixture
def staff(db):
    return User.objects.create_user(
        username="OE5STAFF",
        password="x",
        membership_level=User.MembershipLevel.STAFF,
    )


@pytest.fixture
def member(db):
    return User.objects.create_user(
        username="OE5MEM1",
        password="x",
        membership_level=User.MembershipLevel.MEMBER,
    )


@pytest.fixture
def other_member(db):
    return User.objects.create_user(
        username="OE5MEM2",
        password="x",
        membership_level=User.MembershipLevel.MEMBER,
    )


@pytest.fixture
def applicant(db):
    return User.objects.create_user(
        username="OE5BEW1",
        password="x",
        membership_level=User.MembershipLevel.APPLICANT,
    )


@pytest.fixture
def other_applicant(db):
    return User.objects.create_user(
        username="OE5BEW2",
        password="x",
        membership_level=User.MembershipLevel.APPLICANT,
    )


@pytest.mark.django_db
class TestAudienceFor:
    """audience_for(viewer, target) returns the right Audience tier."""

    def test_admin_sees_other_member_as_admin(self, admin, member):
        from apps.accounts.visibility import Audience, audience_for
        assert audience_for(admin, member) == Audience.ADMIN

    def test_admin_sees_applicant_as_admin(self, admin, applicant):
        from apps.accounts.visibility import Audience, audience_for
        assert audience_for(admin, applicant) == Audience.ADMIN

    def test_admin_sees_self_as_self(self, admin):
        """Admin sieht sich selbst zwar im SELF-Sinn, weil viewer.pk==target.pk
        Vorrang vor is_admin haben sollte — oder umgekehrt? Spec sagt: Admin-Check
        zuerst (Admin sieht sich als Admin)."""
        from apps.accounts.visibility import Audience, audience_for
        # Per Spec: viewer.is_admin precedes viewer==target check.
        assert audience_for(admin, admin) == Audience.ADMIN

    def test_member_sees_self_as_self(self, member):
        from apps.accounts.visibility import Audience, audience_for
        assert audience_for(member, member) == Audience.SELF

    def test_member_sees_other_member_as_member(self, member, other_member):
        from apps.accounts.visibility import Audience, audience_for
        assert audience_for(member, other_member) == Audience.MEMBER

    def test_member_sees_applicant_returns_none(self, member, applicant):
        from apps.accounts.visibility import audience_for
        assert audience_for(member, applicant) is None

    def test_applicant_sees_self_as_applicant(self, applicant):
        from apps.accounts.visibility import Audience, audience_for
        assert audience_for(applicant, applicant) == Audience.APPLICANT

    def test_applicant_sees_other_applicant_returns_none(self, applicant, other_applicant):
        from apps.accounts.visibility import audience_for
        assert audience_for(applicant, other_applicant) is None

    def test_applicant_sees_member_returns_none(self, applicant, member):
        from apps.accounts.visibility import audience_for
        assert audience_for(applicant, member) is None

    def test_anonymous_returns_none(self, member):
        from django.contrib.auth.models import AnonymousUser
        from apps.accounts.visibility import audience_for
        assert audience_for(AnonymousUser(), member) is None

    def test_staff_sees_member_as_member(self, staff, member):
        """Staff ist nicht is_admin (per User.is_admin property → nur Vereins-Admin).
        Daher behandelt audience_for() Staff wie einen normalen Member."""
        from apps.accounts.visibility import Audience, audience_for
        assert audience_for(staff, member) == Audience.MEMBER
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_user_visibility.py::TestAudienceFor -v 2>&1 | tail -30`
Expected: ImportError because `apps.accounts.visibility` doesn't exist yet.

- [ ] **Step 3: Create apps/accounts/visibility.py with Audience + audience_for**

Create NEW file `apps/accounts/visibility.py`:

```python
"""Audience-aware visibility for the User-Domain Member-Directory.

Central single-source-of-truth for who-sees-what. Templates, list-views,
and detail-views consume `audience_for()` and `directory_visible_fields()`
to render audience-appropriate output.

Specification: docs/superpowers/specs/2026-06-12-user-domain-1a-foundation-design.md
"""

import enum

from django.contrib.auth import get_user_model

User = get_user_model()


class Audience(enum.Enum):
    """Four-tier audience model for the user-directory.

    ADMIN     — sees everything for any user.
    SELF      — sees own data; Applicant variant below.
    MEMBER    — sees other members' public fields (subject to is_directory_visible).
    APPLICANT — sees own data only; no list, no other-user views.
    """

    ADMIN = "admin"
    SELF = "self"
    MEMBER = "member"
    APPLICANT = "applicant"


def audience_for(viewer, target):
    """Return the Audience tier `viewer` has on `target`.

    Returns None when the viewer has no access — caller raises Http404
    to avoid existence-leaking via 403.

    Note: viewer.is_admin precedes the viewer-equals-target check, so a
    Vereins-Admin sieht sich selbst auch als ADMIN (nicht als SELF). Das
    ist erwünscht — Admin braucht beim Self-View dieselben Werkzeuge
    wie bei anderen.
    """
    if not viewer.is_authenticated:
        return None
    if viewer.is_admin:
        return Audience.ADMIN
    if viewer.pk == target.pk:
        # Self-Sicht. Applicant-Variante für die wenigen Stellen, wo
        # man unterscheiden muss (z.B. List-Filter, der Applicants
        # ausnimmt).
        if viewer.membership_level == User.MembershipLevel.APPLICANT:
            return Audience.APPLICANT
        return Audience.SELF
    # Cross-User-Sicht.
    if viewer.membership_level == User.MembershipLevel.APPLICANT:
        # Applicants sehen niemand außer sich selbst.
        return None
    if target.membership_level == User.MembershipLevel.APPLICANT:
        # Member sehen Applicants nicht (Bewerber bleiben „außerhalb").
        return None
    return Audience.MEMBER
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_user_visibility.py::TestAudienceFor -v 2>&1 | tail -30`
Expected: All 11 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/accounts/visibility.py tests/test_user_visibility.py
git commit -m "feat(accounts): add visibility.py with Audience enum + audience_for

Central audience-tier computation for the Member-Directory.
audience_for(viewer, target) returns ADMIN / SELF / MEMBER / APPLICANT
or None (= no access, 404 in view). Konsumiert von 1b (UserDetailView,
UserListView) und 1c (ProfileView-Self-checks)."
```

---

### Task 8: visibility.py — Field-Sets + directory_visible_fields + user_can_view_directory

**Files:**
- Modify: `apps/accounts/visibility.py`
- Test: `tests/test_user_visibility.py`

- [ ] **Step 1: Write failing tests for field-sets and directory_visible_fields**

Append to `tests/test_user_visibility.py`:

```python
@pytest.mark.django_db
class TestDirectoryVisibleFields:
    """directory_visible_fields(viewer, target) returns the right set."""

    def test_admin_sees_public_private_and_admin_only(self, admin, member):
        from apps.accounts.visibility import (
            ADMIN_ONLY_FIELDS, PRIVATE_PROFILE_FIELDS, PUBLIC_PROFILE_FIELDS,
            directory_visible_fields,
        )
        fields = directory_visible_fields(admin, member)
        assert fields >= PUBLIC_PROFILE_FIELDS
        assert fields >= PRIVATE_PROFILE_FIELDS
        assert fields >= ADMIN_ONLY_FIELDS

    def test_self_sees_public_and_private(self, member):
        from apps.accounts.visibility import (
            PRIVATE_PROFILE_FIELDS, PUBLIC_PROFILE_FIELDS,
            directory_visible_fields,
        )
        fields = directory_visible_fields(member, member)
        assert fields >= PUBLIC_PROFILE_FIELDS
        assert fields >= PRIVATE_PROFILE_FIELDS

    def test_self_sees_own_sso_sessions(self, member):
        from apps.accounts.visibility import directory_visible_fields
        fields = directory_visible_fields(member, member)
        assert "sso_sessions_self" in fields

    def test_self_does_not_see_admin_only_fields(self, member):
        from apps.accounts.visibility import (
            ADMIN_ONLY_FIELDS, directory_visible_fields,
        )
        fields = directory_visible_fields(member, member)
        # No admin-only sub-overlap (apart from sso_sessions_self which is
        # explicitly not in ADMIN_ONLY_FIELDS — siehe Sektion 4.3 spec).
        for f in ADMIN_ONLY_FIELDS:
            assert f not in fields, f"unexpected admin-only field {f} in self set"

    def test_self_sees_is_active_and_last_login(self, member):
        """Self soll eigenen is_active und last_login sehen (Sub-Spec 1a v2)."""
        from apps.accounts.visibility import directory_visible_fields
        fields = directory_visible_fields(member, member)
        assert "is_active" in fields
        assert "last_login" in fields

    def test_member_sees_other_member_public_only(self, member, other_member):
        from apps.accounts.visibility import (
            PUBLIC_PROFILE_FIELDS, directory_visible_fields,
        )
        # default: target is_directory_visible=True
        fields = directory_visible_fields(member, other_member)
        assert fields == PUBLIC_PROFILE_FIELDS

    def test_member_sees_invisible_member_minimal(self, member, other_member):
        from apps.accounts.visibility import (
            MINIMAL_DIRECTORY_FIELDS, directory_visible_fields,
        )
        other_member.is_directory_visible = False
        other_member.save()
        fields = directory_visible_fields(member, other_member)
        assert fields == MINIMAL_DIRECTORY_FIELDS

    def test_no_access_returns_empty(self, applicant, member):
        from apps.accounts.visibility import directory_visible_fields
        # Applicant sieht Member nicht
        fields = directory_visible_fields(applicant, member)
        assert fields == frozenset()


@pytest.mark.django_db
class TestUserCanViewDirectory:
    """user_can_view_directory(viewer) gates the UserListView."""

    def test_admin_can(self, admin):
        from apps.accounts.visibility import user_can_view_directory
        assert user_can_view_directory(admin) is True

    def test_member_can(self, member):
        from apps.accounts.visibility import user_can_view_directory
        assert user_can_view_directory(member) is True

    def test_staff_can(self, staff):
        from apps.accounts.visibility import user_can_view_directory
        assert user_can_view_directory(staff) is True

    def test_applicant_cannot(self, applicant):
        from apps.accounts.visibility import user_can_view_directory
        assert user_can_view_directory(applicant) is False

    def test_anonymous_cannot(self):
        from django.contrib.auth.models import AnonymousUser
        from apps.accounts.visibility import user_can_view_directory
        assert user_can_view_directory(AnonymousUser()) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_user_visibility.py -v 2>&1 | tail -30`
Expected: ImportError on the new constants. TestAudienceFor PASSES.

- [ ] **Step 3: Add field-sets + directory_visible_fields + user_can_view_directory to visibility.py**

Append to `apps/accounts/visibility.py`:

```python
# === Field-Visibility-Sets ===========================================
#
# Strings here are *concept keys* the templates check, e.g.
#   {% if "phone" in visible_fields and object.phone %}…
# They mostly mirror User-Modell-Feldnamen, plus zusammengesetzte Keys
# wie "region_assignments", "date_joined_year", "sso_sessions_self".

# Sichtbar für jeden eingeloggten Member (wenn target.is_directory_visible).
# Reihenfolge mirroring der Overview-Tab-Anzeige.
PUBLIC_PROFILE_FIELDS = frozenset({
    "username",            # = Rufzeichen / Callsign
    "first_name", "last_name",
    "email",
    "membership_level",
    "avatar",
    "bio",
    "qth_name",
    "locator",
    "qrz_url",
    "date_joined_year",    # nur Jahr, nicht das Datum
    "region_assignments",  # Pill-Liste
    "station_assignments", # Pill-Liste
})

# Self + Admin sehen die. Member nicht.
PRIVATE_PROFILE_FIELDS = frozenset({
    "address",
    "phone",
    "latitude", "longitude",  # numerisch, Admin-Debug-Block + Self
    "language",
    "last_login",             # Self sieht eigenen; Admin sieht alle
    "is_active",              # Self sieht eigenen Aktivitätsstatus; Admin sieht alle
    "is_directory_visible",
})

# Nur Admin sieht die.
ADMIN_ONLY_FIELDS = frozenset({
    "sso_grants",
    "sso_sessions",
    "tag_memberships",
    "global_audit_actions",  # Promote/Demote, Region-/Station-Assignment-Mgmt
})

# Reduzierter Set, wenn target.is_directory_visible=False und viewer Member.
MINIMAL_DIRECTORY_FIELDS = frozenset({
    "username",
    "membership_level",
    "avatar",
})


def directory_visible_fields(viewer, target):
    """Return the frozenset of concept-keys `viewer` may see on `target`.

    Templates / serializers consume this:
        if "phone" in visible_fields and target.phone:
            render(target.phone)
    """
    aud = audience_for(viewer, target)
    if aud is None:
        return frozenset()
    if aud == Audience.ADMIN:
        return PUBLIC_PROFILE_FIELDS | PRIVATE_PROFILE_FIELDS | ADMIN_ONLY_FIELDS
    if aud in (Audience.SELF, Audience.APPLICANT):
        # Self/Applicant: eigene private + public Felder (read-only).
        # ADMIN_ONLY_FIELDS bleiben außen vor; sso_sessions_self ist die
        # Read-Only-Self-Variante des SSO-Sessions-Cards.
        return PUBLIC_PROFILE_FIELDS | PRIVATE_PROFILE_FIELDS | frozenset({"sso_sessions_self"})
    # Audience.MEMBER:
    if not target.is_directory_visible:
        return MINIMAL_DIRECTORY_FIELDS
    return PUBLIC_PROFILE_FIELDS


def user_can_view_directory(viewer):
    """Gate for the UserListView. Applicants and Anonymous get 404."""
    if not viewer.is_authenticated:
        return False
    if viewer.is_admin:
        return True
    return viewer.membership_level != User.MembershipLevel.APPLICANT
```

- [ ] **Step 4: Run all visibility tests to verify they pass**

Run: `pytest tests/test_user_visibility.py -v 2>&1 | tail -40`
Expected: All tests PASS (TestAudienceFor + TestDirectoryVisibleFields + TestUserCanViewDirectory).

- [ ] **Step 5: Verify whole test suite still passes**

Run: `pytest tests/ -x --tb=short 2>&1 | tail -10`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/accounts/visibility.py tests/test_user_visibility.py
git commit -m "feat(accounts): add field-sets + directory_visible_fields + user_can_view_directory

PUBLIC/PRIVATE/ADMIN_ONLY/MINIMAL frozensets + directory_visible_fields()
+ user_can_view_directory(). Komplettiert das Visibility-Modul. Templates
in 1b nutzen 'if FIELD in visible_fields' für conditional Rendering."
```

---

### Task 9: geocoding.py — lat_lon_to_locator (pure math)

**Files:**
- Create: `apps/accounts/geocoding.py`
- Test: `tests/test_user_locator.py`

- [ ] **Step 1: Write failing test for Maidenhead computation**

Create NEW file `tests/test_user_locator.py`:

```python
"""Tests for apps/accounts/geocoding.lat_lon_to_locator (Sub-Spec 1a)."""


class TestMaidenheadLocator:
    """Pure-Python Maidenhead grid locator computation."""

    def test_linz_returns_jn78dh(self):
        from apps.accounts.geocoding import lat_lon_to_locator
        # Linz Hauptplatz: 48.30694° N, 14.28583° E
        assert lat_lon_to_locator(48.30694, 14.28583) == "JN78DH"

    def test_vienna_returns_jn88ee(self):
        from apps.accounts.geocoding import lat_lon_to_locator
        # Wien Stephansdom: 48.2° N, 16.37° E
        assert lat_lon_to_locator(48.2, 16.37) == "JN88EE"

    def test_precision_4_returns_4chars(self):
        from apps.accounts.geocoding import lat_lon_to_locator
        result = lat_lon_to_locator(48.30694, 14.28583, precision=4)
        assert len(result) == 4
        assert result == "JN78"

    def test_equator_zero_meridian(self):
        from apps.accounts.geocoding import lat_lon_to_locator
        # (lat=0, lon=0) is the boundary of JJ00AA in some conventions
        result = lat_lon_to_locator(0, 0)
        # Field calculation: lon+180=180, lat+90=90 → (J9, J9) → "JJ"
        # 180/20=9 → 'J', 90/10=9 → 'J'
        assert result.startswith("JJ")

    def test_negative_latitude_works(self):
        from apps.accounts.geocoding import lat_lon_to_locator
        # Sydney, AU: lat=-33.87, lon=151.21 → QF56OD area
        result = lat_lon_to_locator(-33.87, 151.21)
        assert result.startswith("QF")
        assert len(result) == 6

    def test_negative_longitude_works(self):
        from apps.accounts.geocoding import lat_lon_to_locator
        # San Francisco: lat=37.77, lon=-122.42 → CM87 area
        result = lat_lon_to_locator(37.77, -122.42)
        assert result.startswith("CM")
        assert len(result) == 6

    def test_accepts_decimal_input(self):
        from decimal import Decimal
        from apps.accounts.geocoding import lat_lon_to_locator
        # Same as Linz but via Decimal
        result = lat_lon_to_locator(Decimal("48.30694"), Decimal("14.28583"))
        assert result == "JN78DH"

    def test_default_precision_is_6(self):
        from apps.accounts.geocoding import lat_lon_to_locator
        result = lat_lon_to_locator(48.30694, 14.28583)
        assert len(result) == 6
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_user_locator.py -v 2>&1 | tail -20`
Expected: ImportError because `apps.accounts.geocoding` doesn't exist yet.

- [ ] **Step 3: Create apps/accounts/geocoding.py with lat_lon_to_locator**

Create NEW file `apps/accounts/geocoding.py`:

```python
"""Geocoding + Maidenhead locator helpers (Sub-Spec 1a Foundation).

`geocode_address` resolves a postal address to (lat, lon) via Nominatim/OSM.
`lat_lon_to_locator` computes the Maidenhead 6-char grid locator from
(lat, lon).

Spec: docs/superpowers/specs/2026-06-12-user-domain-1a-foundation-design.md
"""

from typing import Union


def lat_lon_to_locator(lat: Union[float, "Decimal"], lon: Union[float, "Decimal"], precision: int = 6) -> str:  # noqa: F821
    """Maidenhead-Locator aus (lat, lon).

    precision=6 → 6-Zeichen-Locator (z.B. 'JN78DH').
    precision=4 → 4-Zeichen Grid-Square (z.B. 'JN78').

    Algorithmus:
      1. Verschiebung: lon += 180, lat += 90 (alles wird positiv).
      2. Fields (1. Letter-Pair): 18×18 Grid à 20° lon / 10° lat (A-R).
      3. Squares (2. Digit-Pair): 10×10 Grid à 2° lon / 1° lat (0-9).
      4. Subsquares (3. Letter-Pair): 24×24 Grid à 5' lon / 2.5' lat (A-X).

    Akzeptiert float, int, Decimal — wird intern zu float konvertiert.
    """
    lat_f = float(lat) + 90.0
    lon_f = float(lon) + 180.0
    A = ord("A")
    lon_field, lon_rest = divmod(lon_f, 20.0)
    lat_field, lat_rest = divmod(lat_f, 10.0)
    out = chr(A + int(lon_field)) + chr(A + int(lat_field))
    lon_sq, lon_rest = divmod(lon_rest, 2.0)
    lat_sq, lat_rest = divmod(lat_rest, 1.0)
    out += str(int(lon_sq)) + str(int(lat_sq))
    if precision >= 6:
        # Per 2° lon → 24 subsquares (5'/60° per subsquare), so multiply by 12
        # to map [0, 2) to [0, 24).
        lon_sub = int(lon_rest * 12)
        # Per 1° lat → 24 subsquares (2.5'/60° per subsquare), so multiply by 24
        # to map [0, 1) to [0, 24).
        lat_sub = int(lat_rest * 24)
        out += chr(A + lon_sub) + chr(A + lat_sub)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_user_locator.py -v 2>&1 | tail -20`
Expected: All 8 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/accounts/geocoding.py tests/test_user_locator.py
git commit -m "feat(accounts): add lat_lon_to_locator (Maidenhead 6-char)

Pure-Python computation of the Maidenhead grid locator from (lat, lon).
Konsumiert in 1c's form_valid: bei address-change geocoden, dann
lat/lon → locator umrechnen (oder User-Override behalten).

Tests cover Linz, Vienna, equator, negative coords, Decimal input."
```

---

### Task 10: geocoding.py — geocode_address (Nominatim HTTP)

**Files:**
- Modify: `apps/accounts/geocoding.py`
- Test: `tests/test_user_geocoding.py`

- [ ] **Step 1: Write failing test for geocode_address**

Create NEW file `tests/test_user_geocoding.py`:

```python
"""Tests for apps/accounts/geocoding.geocode_address (Sub-Spec 1a Foundation).

Geocoding via Nominatim/OSM — wir mocken requests.get, kein echter
HTTP-Call im Test.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import requests


class TestGeocodeAddress:
    """geocode_address(address) returns (Decimal, Decimal) or None."""

    @patch("apps.accounts.geocoding.time.sleep")  # rate-limit umgehen im Test
    @patch("apps.accounts.geocoding.requests.get")
    def test_valid_response_returns_decimal_tuple(self, mock_get, _mock_sleep):
        mock_response = MagicMock()
        mock_response.json.return_value = [
            {"lat": "48.30694", "lon": "14.28583", "display_name": "Linz, Austria"}
        ]
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        from apps.accounts.geocoding import geocode_address
        result = geocode_address("Hauptstraße 1, 4020 Linz")
        assert result is not None
        lat, lon = result
        assert lat == Decimal("48.30694")
        assert lon == Decimal("14.28583")

    @patch("apps.accounts.geocoding.time.sleep")
    @patch("apps.accounts.geocoding.requests.get")
    def test_user_agent_header_is_set(self, mock_get, _mock_sleep):
        mock_response = MagicMock()
        mock_response.json.return_value = [{"lat": "0", "lon": "0"}]
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        from apps.accounts.geocoding import geocode_address
        geocode_address("Any address")

        call_kwargs = mock_get.call_args.kwargs
        headers = call_kwargs["headers"]
        assert "User-Agent" in headers
        assert "OE5XRX" in headers["User-Agent"]

    @patch("apps.accounts.geocoding.time.sleep")
    @patch("apps.accounts.geocoding.requests.get")
    def test_empty_address_returns_none_without_http_call(self, mock_get, _mock_sleep):
        from apps.accounts.geocoding import geocode_address
        assert geocode_address("") is None
        assert geocode_address("   ") is None
        assert geocode_address(None) is None
        mock_get.assert_not_called()

    @patch("apps.accounts.geocoding.time.sleep")
    @patch("apps.accounts.geocoding.requests.get")
    def test_no_result_returns_none(self, mock_get, _mock_sleep):
        mock_response = MagicMock()
        mock_response.json.return_value = []
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        from apps.accounts.geocoding import geocode_address
        assert geocode_address("Nonsense location") is None

    @patch("apps.accounts.geocoding.time.sleep")
    @patch("apps.accounts.geocoding.requests.get")
    def test_http_error_returns_none(self, mock_get, _mock_sleep):
        mock_get.side_effect = requests.HTTPError("500 Server Error")

        from apps.accounts.geocoding import geocode_address
        assert geocode_address("Hauptstraße 1, 4020 Linz") is None

    @patch("apps.accounts.geocoding.time.sleep")
    @patch("apps.accounts.geocoding.requests.get")
    def test_timeout_returns_none(self, mock_get, _mock_sleep):
        mock_get.side_effect = requests.Timeout("Connection timed out")

        from apps.accounts.geocoding import geocode_address
        assert geocode_address("Hauptstraße 1, 4020 Linz") is None

    @patch("apps.accounts.geocoding.time.sleep")
    @patch("apps.accounts.geocoding.requests.get")
    def test_malformed_response_returns_none(self, mock_get, _mock_sleep):
        # Antwort fehlen lat/lon-Keys
        mock_response = MagicMock()
        mock_response.json.return_value = [{"some_other_field": "value"}]
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        from apps.accounts.geocoding import geocode_address
        assert geocode_address("Hauptstraße 1, 4020 Linz") is None

    @patch("apps.accounts.geocoding.time.sleep")
    @patch("apps.accounts.geocoding.requests.get")
    def test_rate_limit_sleep_invoked(self, mock_get, mock_sleep):
        mock_response = MagicMock()
        mock_response.json.return_value = [{"lat": "0", "lon": "0"}]
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        from apps.accounts.geocoding import geocode_address
        geocode_address("Some place")
        mock_sleep.assert_called_once_with(1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_user_geocoding.py -v 2>&1 | tail -30`
Expected: ImportError because `geocode_address` doesn't exist in `apps.accounts.geocoding` yet.

- [ ] **Step 3: Implement geocode_address**

Edit `apps/accounts/geocoding.py`. At the top, after the module docstring, add imports:

```python
"""... (existing docstring)"""

import logging
import time
from decimal import Decimal
from typing import Optional, Union

import requests

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_TIMEOUT = 10  # seconds
# Generic project default — deployments override via NOMINATIM_USER_AGENT
# setting if Nominatim requires a contact handle. Personal contact info
# does NOT live in source control.
DEFAULT_USER_AGENT = "OE5XRX-StationManager/1.0"


def _user_agent():
    return getattr(settings, "NOMINATIM_USER_AGENT", DEFAULT_USER_AGENT)


def geocode_address(address: Optional[str]) -> Optional[tuple[Decimal, Decimal]]:
    """Resolve a postal address to (latitude, longitude) via Nominatim.

    Returns None on any error (network, no result, malformed response,
    timeout, parse failure). The function rate-limits itself with a
    1-second sleep per call to comply with the Nominatim Free-Tier policy.

    NOT thread-safe across multiple concurrent calls in the same process —
    the rate-limit pause is local. For a small Verein (≤ a few save-events
    per minute) that's fine.

    Privacy: the address must NOT appear in the warning log — it is PII.
    Only the exception class + message is logged.
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
            headers={"User-Agent": _user_agent()},
            timeout=NOMINATIM_TIMEOUT,
        )
        resp.raise_for_status()
        results = resp.json()
        if not results:
            return None
        first = results[0]
        return (Decimal(first["lat"]), Decimal(first["lon"]))
    except (
        requests.RequestException,
        ValueError,
        KeyError,
        TypeError,
        InvalidOperation,
    ) as exc:
        # Privacy: do NOT include `address` in the log record.
        logger.warning("Nominatim geocode failed: %s: %s", type(exc).__name__, exc)
        return None
```

Note: Move the existing `lat_lon_to_locator` function below the new imports, OR keep imports merged at top. The final structure should be: imports → constants → `geocode_address` → `lat_lon_to_locator`. Adapt to keep one clean module.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_user_geocoding.py -v 2>&1 | tail -30`
Expected: All 8 tests PASS.

- [ ] **Step 5: Re-run locator tests (the file got restructured)**

Run: `pytest tests/test_user_locator.py -v 2>&1 | tail -20`
Expected: All 8 tests still PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/accounts/geocoding.py tests/test_user_geocoding.py
git commit -m "feat(accounts): add geocode_address (Nominatim/OSM)

External HTTP service wrapper. Returns (Decimal lat, Decimal lon) or
None on any failure (no exceptions propagate to caller).

Rate-limit-compliant (1s sleep per call), required User-Agent header
set. Konsumiert in 1c's form_valid bei address-change."
```

---

### Task 11: avatars.py — validate_avatar_upload

**Files:**
- Create: `apps/accounts/avatars.py`
- Test: `tests/test_user_avatar.py`

- [ ] **Step 1: Write failing test for validate_avatar_upload**

Create NEW file `tests/test_user_avatar.py`:

```python
"""Tests for apps/accounts/avatars.py (Sub-Spec 1a Foundation)."""

import io

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile


def _make_jpeg(width=100, height=100, mode="RGB"):
    """Helper: build a small in-memory JPEG."""
    from PIL import Image
    img = Image.new(mode, (width, height), color=(255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    buf.seek(0)
    return buf


def _make_png_with_alpha(width=100, height=100):
    """Helper: build a small in-memory PNG with alpha channel."""
    from PIL import Image
    img = Image.new("RGBA", (width, height), color=(0, 255, 0, 128))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


class TestValidateAvatarUpload:
    """validate_avatar_upload raises ValidationError on bad files."""

    def test_none_returns_silently(self):
        from apps.accounts.avatars import validate_avatar_upload
        validate_avatar_upload(None)  # should not raise

    def test_oversized_file_raises(self):
        from apps.accounts.avatars import (
            MAX_AVATAR_BYTES, validate_avatar_upload,
        )
        # Build a 3 MB blob
        payload = b"\xff" * (MAX_AVATAR_BYTES + 100)
        f = SimpleUploadedFile("big.jpg", payload, content_type="image/jpeg")
        with pytest.raises(ValidationError) as exc:
            validate_avatar_upload(f)
        assert "2 MB" in str(exc.value) or "MB" in str(exc.value)

    def test_non_image_raises(self):
        from apps.accounts.avatars import validate_avatar_upload
        f = SimpleUploadedFile("notimg.jpg", b"plain text content", content_type="image/jpeg")
        with pytest.raises(ValidationError):
            validate_avatar_upload(f)

    def test_valid_jpeg_passes(self):
        from apps.accounts.avatars import validate_avatar_upload
        buf = _make_jpeg(256, 256)
        f = SimpleUploadedFile("ok.jpg", buf.read(), content_type="image/jpeg")
        validate_avatar_upload(f)  # should not raise

    def test_valid_png_with_alpha_passes(self):
        from apps.accounts.avatars import validate_avatar_upload
        buf = _make_png_with_alpha(256, 256)
        f = SimpleUploadedFile("ok.png", buf.read(), content_type="image/png")
        validate_avatar_upload(f)  # should not raise

    def test_validate_does_not_advance_cursor(self):
        """validate_avatar_upload must not leave file.tell() != 0,
        sonst kann der nachgelagerte upload-flow das File nicht mehr lesen."""
        from apps.accounts.avatars import validate_avatar_upload
        buf = _make_jpeg(256, 256)
        f = SimpleUploadedFile("ok.jpg", buf.read(), content_type="image/jpeg")
        validate_avatar_upload(f)
        # After validate, the file should be re-seekable to start
        assert f.tell() == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_user_avatar.py::TestValidateAvatarUpload -v 2>&1 | tail -30`
Expected: ImportError because `apps.accounts.avatars` doesn't exist yet.

- [ ] **Step 3: Create apps/accounts/avatars.py with validate_avatar_upload**

Create NEW file `apps/accounts/avatars.py`:

```python
"""Avatar upload validation + post-save processing (Sub-Spec 1a Foundation).

Two helpers:

- `validate_avatar_upload(file)` — called from Form.clean_avatar().
- `process_avatar_file(path)` — called from Form.save() after the file
  is on disk; resizes to max 512×512 and re-encodes as JPEG.

Spec: docs/superpowers/specs/2026-06-12-user-domain-1a-foundation-design.md
"""

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

MAX_AVATAR_BYTES = 2 * 1024 * 1024  # 2 MB


def validate_avatar_upload(file):
    """Raises ValidationError if `file` is not a valid avatar upload.

    Checks: not None → exists; size ≤ 2 MB; Pillow recognises as an image.
    Resets the file cursor to 0 after Pillow consumed bytes.
    """
    if file is None:
        return
    if file.size > MAX_AVATAR_BYTES:
        raise ValidationError(_("Avatar darf max. 2 MB sein."))

    # Pillow's verify() reads the file-header and confirms format.
    from PIL import Image, UnidentifiedImageError
    try:
        img = Image.open(file)
        img.verify()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValidationError(_("Datei ist kein gültiges Bild.")) from exc
    finally:
        # img.verify() consumes the file cursor; reset so the
        # subsequent save-pipeline can read from the start.
        try:
            file.seek(0)
        except (AttributeError, OSError):
            pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_user_avatar.py::TestValidateAvatarUpload -v 2>&1 | tail -30`
Expected: All 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/accounts/avatars.py tests/test_user_avatar.py
git commit -m "feat(accounts): add validate_avatar_upload (Pillow-based size+format check)

ValidationError on file > 2 MB or non-image content. Resets file
cursor after verify so downstream Form.save() can re-read.

Konsumiert in 1c's UserChangeForm.clean_avatar() und ProfileProfileForm
.clean_avatar()."
```

---

### Task 12: avatars.py — process_avatar_file

**Files:**
- Modify: `apps/accounts/avatars.py`
- Test: `tests/test_user_avatar.py`

- [ ] **Step 1: Write failing test for process_avatar_file**

Append to `tests/test_user_avatar.py`:

```python
class TestProcessAvatarFile:
    """process_avatar_file resizes + re-encodes the file in-place."""

    def test_large_jpeg_resized_to_512(self, tmp_path):
        from PIL import Image
        from apps.accounts.avatars import process_avatar_file

        # 1024x768 source, will be downscaled
        src_path = tmp_path / "big.jpg"
        Image.new("RGB", (1024, 768), color=(255, 0, 0)).save(
            src_path, "JPEG", quality=85,
        )

        process_avatar_file(str(src_path))

        result = Image.open(src_path)
        assert max(result.size) == 512
        assert result.format == "JPEG"

    def test_png_converted_to_jpeg(self, tmp_path):
        from PIL import Image
        from apps.accounts.avatars import process_avatar_file

        src_path = tmp_path / "in.png"
        Image.new("RGB", (256, 256), color=(0, 255, 0)).save(src_path, "PNG")

        process_avatar_file(str(src_path))

        result = Image.open(src_path)
        assert result.format == "JPEG"

    def test_transparency_flattened_to_rgb(self, tmp_path):
        from PIL import Image
        from apps.accounts.avatars import process_avatar_file

        src_path = tmp_path / "alpha.png"
        Image.new("RGBA", (256, 256), color=(0, 0, 255, 128)).save(src_path, "PNG")

        process_avatar_file(str(src_path))

        result = Image.open(src_path)
        assert result.mode == "RGB"

    def test_small_image_not_upscaled(self, tmp_path):
        from PIL import Image
        from apps.accounts.avatars import process_avatar_file

        src_path = tmp_path / "small.jpg"
        Image.new("RGB", (200, 150), color=(255, 0, 0)).save(
            src_path, "JPEG", quality=85,
        )

        process_avatar_file(str(src_path))

        result = Image.open(src_path)
        # thumbnail() does not upscale — bleibt bei 200x150
        assert result.size == (200, 150)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_user_avatar.py::TestProcessAvatarFile -v 2>&1 | tail -30`
Expected: ImportError on `process_avatar_file`.

- [ ] **Step 3: Append process_avatar_file to avatars.py**

Append to `apps/accounts/avatars.py`:

```python
def process_avatar_file(file_field_path: str) -> None:
    """Resize and re-encode the avatar file at the given filesystem path.

    In-place mutation: opens, resizes to max 512×512 (proportional),
    converts to RGB (drops alpha), writes back as JPEG quality=85.

    Called from Form.save() after super().save() has written the file
    to MEDIA_ROOT — at that point file_field_path is the actual disk path.
    """
    from PIL import Image
    with Image.open(file_field_path) as img:
        img.thumbnail((512, 512))
        # Convert to RGB to drop alpha channel — JPEG doesn't support alpha.
        # Convert before save() so the conversion is part of the file.
        rgb = img.convert("RGB")
        rgb.save(file_field_path, "JPEG", quality=85, optimize=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_user_avatar.py -v 2>&1 | tail -30`
Expected: All TestValidateAvatarUpload + TestProcessAvatarFile tests PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/accounts/avatars.py tests/test_user_avatar.py
git commit -m "feat(accounts): add process_avatar_file (Pillow resize + JPEG re-encode)

In-place resize to max 512×512 (no upscaling), RGB-conversion (drops
alpha), JPEG quality=85 with optimize=True. Konsumiert in 1c's
Form.save() nach super().save() — File ist dann auf Disk."
```

---

### Task 13: Final integration verify

**Files:**
- Read only

- [ ] **Step 1: Run the entire test suite**

Run: `pytest tests/ --tb=short 2>&1 | tail -30`
Expected: All tests PASS (existing + new). No regressions.

- [ ] **Step 2: Run system check**

Run: `python manage.py check 2>&1 | tail -10`
Expected: `System check identified no issues (0 silenced).`

- [ ] **Step 3: Verify migrations are clean**

Run: `python manage.py makemigrations --check --dry-run 2>&1 | tail -10`
Expected: `No changes detected` (no pending model-change without migration).

- [ ] **Step 4: Verify both new migrations applied cleanly to a fresh DB**

Run: `python manage.py migrate --plan 2>&1 | tail -20`
Expected: Both `0008_user_profile_fields` and `0009_audit_user_crud_event_types` shown as applied.

- [ ] **Step 5: Smoke-test the Django Admin renders**

Note: This requires a running dev-server + browser. Skip if running headless. Manual verification:

```bash
python manage.py runserver 0.0.0.0:8000
# Then in browser:
# http://localhost:8000/admin/accounts/user/
# Verify the User-Change-Page renders without 500 and shows the new fieldsets.
```

For automated smoke-test instead:

```python
# Optional inline test:
from django.test import Client
from apps.accounts.models import User
client = Client()
admin = User.objects.create_superuser(username="adm", password="pw")
client.force_login(admin)
response = client.get(f"/admin/accounts/user/{admin.pk}/change/")
assert response.status_code == 200
```

- [ ] **Step 6: Verify line count + final structure**

Run: `wc -l apps/accounts/visibility.py apps/accounts/geocoding.py apps/accounts/avatars.py 2>&1`
Expected: visibility.py ~100 lines, geocoding.py ~80 lines, avatars.py ~50 lines (approximate; size sanity check).

- [ ] **Step 7: Branch summary**

Run: `git log --oneline origin/main..HEAD 2>&1`
Expected: ~10 commits since main (5 spec/plan commits already there + ~12 implementation commits from this plan).

- [ ] **Step 8: Final commit if anything pending**

Run: `git status 2>&1`
Expected: `nichts zum Commit vorgemerkt, Arbeitsverzeichnis unverändert` — alle Tasks haben pro-Schritt committed.

If anything is dangling, make a final wrap-up commit:

```bash
git commit -am "chore(accounts): final pass on Sub-Spec 1a Foundation"
```

---

## Summary

After this plan executes, the branch `feat/user-domain-redesign` has:

- 10 new User-model fields (bio, avatar, qth_name, qrz_url, address, phone, latitude, longitude, locator, is_directory_visible).
- 8 new AccountAuditLog EventTypes (USER_CREATED/UPDATED/DELETED/ACTIVATED/DEACTIVATED, PASSWORD_CHANGED, STATION_ASSIGNMENT_CREATED/REVOKED).
- 3 new migrations (0008 user profile fields, 0009 EventType choices, 0010 lat/lon range validators).
- 3 new modules in `apps/accounts/`: visibility.py, geocoding.py, avatars.py.
- Django Admin extended with 3 new fieldsets.
- StationAssignment-Signal emittiert zusätzlichen AccountAuditLog-Eintrag (Doppel-Emit).
- 7 new test files in `tests/`: profile-fields, audit-event-types, station-assignment-audit, visibility, locator, geocoding, avatar.

The PR for this branch contains spec + plan + foundation code together — ready to merge to `main`. After merge, Sub-Spec 1b (Member-Directory) and 1c (Self-Service) follow as separate PRs on their own branches.
