# Sub-Spec 1c Self-Service — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Write-Surface des User-Domain. UserChangeForm bekommt die neuen Profil-Felder; UserUpdateView + UserCreateView emittieren USER_UPDATED/USER_CREATED-Audits; ProfileView wird komplett umgebaut (4 Forms: Identity / Profil / Adresse / Passwort); ProfilePasswordChangeView mit Re-Auth + PASSWORD_CHANGED-Audit; UserDeleteView bekommt Impact-Anzeige + USER_DELETED-Audit; Onboarding-Empty-State-Hinweise auf der Profile-Page.

**Architecture:** Read-side ist seit 1b komplett. 1c verdrahtet die Form-Layer mit den 1a-Helpern (`apps/accounts/avatars.py`, `geocoding.py`, `models.LOCATOR_REGEX`) und erzeugt die audit-events, die der Per-User-Audit-Tab aus 1b bereits konsumieren kann. Profile-Page wird zur multi-Form-Surface mit Form-Dispatch via Hidden-Field. Mobile-Polish (form-row + grid-main, inline max-width raus) zieht durch alle Form-Templates.

**Tech Stack:** Python 3.14, Django 6.0, pytest + pytest-django, ruff. Pillow + requests sind weiterhin aus 1a aktiv.

**Spec:** `docs/superpowers/specs/2026-06-12-user-domain-1c-self-service-design.md`
**Overview:** `docs/superpowers/specs/2026-06-09-user-domain-redesign-overview.md`

---

## File Structure

### Files to CREATE

| Pfad | Zweck |
|---|---|
| `tests/test_user_change_form.py` | UserChangeForm-Erweiterung + Validator + avatar-process Tests. |
| `tests/test_user_update_create_audit.py` | USER_UPDATED / USER_CREATED / USER_ACTIVATED / USER_DEACTIVATED Audit-Emission + Geocoding-Trigger im UserUpdateView. |
| `tests/test_profile_view.py` | ProfileView form-dispatch, 4 Forms, USER_UPDATED-Self-Edit-Audit. |
| `tests/test_profile_geocoding.py` | ProfileView Adresse-save triggert Geocoding (mit Mock). |
| `tests/test_profile_onboarding.py` | Onboarding-Hint-Kontext + Render-Bedingungen. |
| `tests/test_password_change.py` | ProfilePasswordChangeView Re-Auth, Session-Hash-Update, PASSWORD_CHANGED. |
| `tests/test_user_delete_view.py` | UserDeleteView Counts-Context + USER_DELETED-Audit + Self-Delete-Block. |

### Files to MODIFY

| Pfad | Änderung |
|---|---|
| `apps/accounts/forms.py` | UserChangeForm um 8 neue Felder + `clean_avatar`/`clean_locator`/`save()`. Neue Forms: `ProfileIdentityForm`, `ProfileProfileForm`, `ProfileAddressForm`, `PasswordChangeForm`. |
| `apps/accounts/views.py` | TRACKED_USER_FIELDS-Konstante; UserUpdateView + UserCreateView + UserDeleteView form_valid emit Audit + Geocoding; ProfileView wird `TemplateView` mit 4-Form-dispatch + `_save_*` Methoden + `_maybe_geocode` + `_emit_user_updated` + `_onboarding_hints`. Neuer ProfilePasswordChangeView. |
| `apps/accounts/urls.py` | Neue URL `profile/password/` → `ProfilePasswordChangeView`. |
| `apps/accounts/templates/accounts/user_form.html` | Mobile-Refactor: 3 Panels (Identity / Profil / Adresse) + Aside + form-row + grid-main; inline max-width raus. |
| `apps/accounts/templates/accounts/profile.html` | Komplett-Rewrite: 4 Panels (Identity / Profil / Adresse / Passwort) + Sidebar (User-dlist + Self-Sessions). |
| `apps/accounts/templates/accounts/user_confirm_delete.html` | Impact-Panel mit Counts + Station-Admin-Warnung; inline max-width raus. |
| `static/css/app.css` | Neue Klasse `.onboarding-hint` (dezenter Border-Left, Mobile-Padding). |

### Files unchanged

- Alle `views_*assignments.py` und `views_membership.py` (HTMX-Endpoints).
- `apps/accounts/visibility.py`, `geocoding.py`, `avatars.py`, `models.py` (1a-Foundation bleibt).
- `apps/accounts/templates/accounts/user_detail.html`, `user_list.html` (1b).

---

## Tasks

### Task 1: Pre-flight + baseline sanity

**Files:**
- Read only

- [ ] **Step 1: Verify branch + worktree**

Run: `git -C /home/pbuchegger/OE5XRX/station-manager/.worktrees/feat-user-domain-1c-self-service branch --show-current`
Expected: `feat/user-domain-1c-self-service`

- [ ] **Step 2: Run baseline test suite**

Run: `cd /home/pbuchegger/OE5XRX/station-manager/.worktrees/feat-user-domain-1c-self-service && uv run pytest tests/ -x --tb=short 2>&1 | tail -5`
Expected: `792 passed` (alle Tests aus 1a + 1b + bestehende).

- [ ] **Step 3: Verify migrations clean**

Run: `uv run python manage.py makemigrations --check --dry-run 2>&1 | tail -5`
Expected: keine pending Migrations für `accounts`.

---

### Task 2: Module-level helpers — `TRACKED_USER_FIELDS` + `_get_client_ip` re-use

**Files:**
- Modify: `apps/accounts/views.py` (top of file)

This task lays groundwork: a constant the form_valid methods will use to diff the user's identity fields, plus we reuse the existing `_get_client_ip` helper that lives in `apps/accounts/views_membership.py:30`.

- [ ] **Step 1: Add `TRACKED_USER_FIELDS` near the top of `apps/accounts/views.py`** (after `User = get_user_model()`):

```python
# Set of User fields whose changes are tracked in USER_UPDATED audit
# entries (form_valid diffs form.changed_data against this set). Geocoding-
# derived fields (latitude/longitude) are intentionally NOT tracked — they
# are recomputed from `address`, not user-edited.
TRACKED_USER_FIELDS = frozenset(
    {
        "username",
        "email",
        "first_name",
        "last_name",
        "language",
        "bio",
        "avatar",
        "qth_name",
        "qrz_url",
        "phone",
        "address",
        "locator",
        "is_directory_visible",
    }
)
```

- [ ] **Step 2: ruff format + check the file**

Run: `cd /home/pbuchegger/OE5XRX/station-manager/.worktrees/feat-user-domain-1c-self-service && uv run ruff format apps/accounts/views.py 2>&1 | tail -2`
Run: `uv run ruff check apps/accounts/views.py 2>&1 | tail -3`
Expected: Clean.

- [ ] **Step 3: Commit**

```bash
git add apps/accounts/views.py
git commit -m "chore(accounts): add TRACKED_USER_FIELDS frozenset

Module-level constant defining which User fields are diffed by
form_valid for USER_UPDATED audit emission. latitude/longitude are
NOT tracked — they're geocoding-derived, not user-edited."
```

---

### Task 3: UserChangeForm — neue Felder + Validators + Avatar-Resize-Save

**Files:**
- Modify: `apps/accounts/forms.py`
- Create: `tests/test_user_change_form.py`

- [ ] **Step 1: Write failing tests**

Create NEW file `tests/test_user_change_form.py`:

```python
"""UserChangeForm — Admin-side edit of an existing user.

Sub-Spec 1c Sektion 3.1. The form gains 8 new profile fields plus a
clean_avatar / clean_locator gate and an avatar-resize side effect on
save().
"""

import io

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.accounts.forms import UserChangeForm
from apps.accounts.models import User


def _make_jpeg(width=200, height=200):
    from PIL import Image

    img = Image.new("RGB", (width, height), color=(255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    buf.seek(0)
    return buf


@pytest.fixture
def member(db):
    return User.objects.create_user(
        username="OE5MEM1",
        password="x",
        membership_level=User.MembershipLevel.MEMBER,
    )


@pytest.mark.django_db
class TestUserChangeFormFields:
    def test_all_new_fields_present(self, member):
        form = UserChangeForm(instance=member)
        for field in [
            "username", "email", "first_name", "last_name", "language",
            "is_active",
            "bio", "avatar", "qth_name", "qrz_url", "phone",
            "address", "locator",
            "is_directory_visible",
        ]:
            assert field in form.fields, f"missing field: {field}"


@pytest.mark.django_db
class TestUserChangeFormLocatorValidation:
    def test_valid_locator_passes(self, member):
        form = UserChangeForm(
            data={
                "username": member.username,
                "email": "x@example.org",
                "first_name": "",
                "last_name": "",
                "language": "en",
                "is_active": "on",
                "bio": "",
                "qth_name": "",
                "qrz_url": "",
                "phone": "",
                "address": "",
                "locator": "JN78AB",
                "is_directory_visible": "on",
            },
            instance=member,
        )
        assert form.is_valid(), form.errors
        assert form.cleaned_data["locator"] == "JN78AB"

    def test_lowercase_locator_is_normalised_to_uppercase(self, member):
        form = UserChangeForm(
            data={
                "username": member.username,
                "email": "x@example.org",
                "first_name": "",
                "last_name": "",
                "language": "en",
                "is_active": "on",
                "bio": "",
                "qth_name": "",
                "qrz_url": "",
                "phone": "",
                "address": "",
                "locator": "jn78ab",
                "is_directory_visible": "on",
            },
            instance=member,
        )
        assert form.is_valid(), form.errors
        assert form.cleaned_data["locator"] == "JN78AB"

    def test_invalid_locator_rejected(self, member):
        form = UserChangeForm(
            data={
                "username": member.username,
                "email": "x@example.org",
                "first_name": "",
                "last_name": "",
                "language": "en",
                "is_active": "on",
                "bio": "",
                "qth_name": "",
                "qrz_url": "",
                "phone": "",
                "address": "",
                "locator": "XX",
                "is_directory_visible": "on",
            },
            instance=member,
        )
        assert not form.is_valid()
        assert "locator" in form.errors

    def test_empty_locator_accepted(self, member):
        form = UserChangeForm(
            data={
                "username": member.username,
                "email": "x@example.org",
                "first_name": "",
                "last_name": "",
                "language": "en",
                "is_active": "on",
                "bio": "",
                "qth_name": "",
                "qrz_url": "",
                "phone": "",
                "address": "",
                "locator": "",
                "is_directory_visible": "on",
            },
            instance=member,
        )
        assert form.is_valid(), form.errors
        assert form.cleaned_data["locator"] == ""


@pytest.mark.django_db
class TestUserChangeFormAvatarValidation:
    def test_oversized_avatar_rejected(self, member):
        from apps.accounts.avatars import MAX_AVATAR_BYTES

        payload = b"\xff" * (MAX_AVATAR_BYTES + 100)
        f = SimpleUploadedFile("big.jpg", payload, content_type="image/jpeg")
        form = UserChangeForm(
            data={
                "username": member.username,
                "email": "x@example.org",
                "first_name": "",
                "last_name": "",
                "language": "en",
                "is_active": "on",
                "bio": "",
                "qth_name": "",
                "qrz_url": "",
                "phone": "",
                "address": "",
                "locator": "",
                "is_directory_visible": "on",
            },
            files={"avatar": f},
            instance=member,
        )
        assert not form.is_valid()
        assert "avatar" in form.errors

    def test_non_image_avatar_rejected(self, member):
        f = SimpleUploadedFile("notimg.jpg", b"plain text", content_type="image/jpeg")
        form = UserChangeForm(
            data={
                "username": member.username,
                "email": "x@example.org",
                "first_name": "",
                "last_name": "",
                "language": "en",
                "is_active": "on",
                "bio": "",
                "qth_name": "",
                "qrz_url": "",
                "phone": "",
                "address": "",
                "locator": "",
                "is_directory_visible": "on",
            },
            files={"avatar": f},
            instance=member,
        )
        assert not form.is_valid()
        assert "avatar" in form.errors

    def test_valid_avatar_save_triggers_resize(self, member, tmp_path, settings, monkeypatch):
        """Form.save() must call process_avatar_file on the uploaded file."""
        from apps.accounts.avatars import process_avatar_file as real_resize

        settings.MEDIA_ROOT = str(tmp_path)
        calls = []

        def fake_process(path):
            calls.append(path)
            real_resize(path)

        monkeypatch.setattr("apps.accounts.forms.process_avatar_file", fake_process)

        buf = _make_jpeg(1024, 768)
        f = SimpleUploadedFile("ok.jpg", buf.read(), content_type="image/jpeg")
        form = UserChangeForm(
            data={
                "username": member.username,
                "email": "x@example.org",
                "first_name": "",
                "last_name": "",
                "language": "en",
                "is_active": "on",
                "bio": "",
                "qth_name": "",
                "qrz_url": "",
                "phone": "",
                "address": "",
                "locator": "",
                "is_directory_visible": "on",
            },
            files={"avatar": f},
            instance=member,
        )
        assert form.is_valid(), form.errors
        form.save()
        assert len(calls) == 1, calls
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_user_change_form.py -v 2>&1 | tail -20`
Expected: Tests fail because UserChangeForm has only 6 fields (Identity-only after 1b).

- [ ] **Step 3: Extend `UserChangeForm` in `apps/accounts/forms.py`**

Replace the existing `UserChangeForm` class with the expanded version:

```python
class UserChangeForm(BaseUserChangeForm):
    """Form for admins to edit existing users.

    1c-Erweiterung: Identity-Felder plus die neuen Profile-Felder aus 1a.
    Avatar wird beim Save via process_avatar_file resized; Locator wird
    auf uppercase normalisiert und gegen LOCATOR_REGEX validiert.
    """

    password = None

    class Meta:
        model = User
        fields = (
            "username",
            "email",
            "first_name",
            "last_name",
            "language",
            "is_active",
            "bio",
            "avatar",
            "qth_name",
            "qrz_url",
            "phone",
            "address",
            "locator",
            "is_directory_visible",
        )
        widgets = {
            "username": forms.TextInput(attrs={"class": "form-control"}),
            "email": forms.EmailInput(attrs={"class": "form-control"}),
            "first_name": forms.TextInput(attrs={"class": "form-control"}),
            "last_name": forms.TextInput(attrs={"class": "form-control"}),
            "language": forms.Select(attrs={"class": "form-select"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "bio": forms.Textarea(
                attrs={"class": "form-control", "rows": 3, "maxlength": 500}
            ),
            "avatar": forms.ClearableFileInput(
                attrs={"class": "form-control", "accept": "image/*"}
            ),
            "qth_name": forms.TextInput(attrs={"class": "form-control"}),
            "qrz_url": forms.URLInput(attrs={"class": "form-control"}),
            "phone": forms.TextInput(attrs={"class": "form-control"}),
            "address": forms.Textarea(attrs={"class": "form-control", "rows": 4}),
            "locator": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "JN78AB"}
            ),
            "is_directory_visible": forms.CheckboxInput(
                attrs={"class": "form-check-input"}
            ),
        }

    def clean_avatar(self):
        from .avatars import validate_avatar_upload

        f = self.cleaned_data.get("avatar")
        validate_avatar_upload(f)
        return f

    def clean_locator(self):
        from .models import LOCATOR_REGEX

        loc = self.cleaned_data.get("locator", "").strip().upper()
        if loc and not LOCATOR_REGEX.match(loc):
            raise forms.ValidationError(
                _(
                    "Locator muss 2 Buchstaben + 2 Ziffern + 2 Buchstaben sein "
                    "(z.B. JN78AB)."
                )
            )
        return loc

    def save(self, commit=True):
        user = super().save(commit=commit)
        if commit and "avatar" in self.changed_data and user.avatar:
            process_avatar_file(user.avatar.path)
        return user
```

Add the import for `process_avatar_file` at the top of `forms.py` (after the existing imports):

```python
from .avatars import process_avatar_file
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_user_change_form.py -v 2>&1 | tail -20`
Expected: All tests PASS.

- [ ] **Step 5: Verify no regression**

Run: `uv run pytest tests/ -x --tb=short 2>&1 | tail -5`
Expected: All tests pass.

- [ ] **Step 6: ruff format + check**

Run: `uv run ruff format apps/accounts/forms.py tests/test_user_change_form.py 2>&1 | tail -2`
Run: `uv run ruff check apps/accounts/forms.py tests/test_user_change_form.py 2>&1 | tail -3`
Expected: Clean.

- [ ] **Step 7: Commit**

```bash
git add apps/accounts/forms.py tests/test_user_change_form.py
git commit -m "feat(accounts): extend UserChangeForm with profile fields

13 fields total (was 6 in 1b). New: bio, avatar, qth_name, qrz_url,
phone, address, locator, is_directory_visible. clean_avatar runs
validate_avatar_upload (size + Pillow verify + decompression bomb).
clean_locator uppercases and validates against LOCATOR_REGEX.
save() triggers process_avatar_file (Pillow resize + JPEG re-encode)
when avatar changed."
```

---

### Task 4: `UserUpdateView` + `UserCreateView` — audit + geocoding in `form_valid`

**Files:**
- Modify: `apps/accounts/views.py` (UserUpdateView, UserCreateView)
- Create: `tests/test_user_update_create_audit.py`

- [ ] **Step 1: Write failing tests**

Create NEW file `tests/test_user_update_create_audit.py`:

```python
"""USER_UPDATED / USER_CREATED / USER_ACTIVATED / USER_DEACTIVATED
audit emission from UserUpdateView + UserCreateView, plus
geocoding-trigger on address change.

Sub-Spec 1c Sektion 5 + 6.
"""

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.accounts.models import AccountAuditLog, User


@pytest.fixture
def admin(db):
    return User.objects.create_user(
        username="OE5ADMIN",
        password="x",
        membership_level=User.MembershipLevel.ADMIN,
    )


@pytest.fixture
def member(db):
    return User.objects.create_user(
        username="OE5MEM1",
        password="x",
        first_name="Hans",
        last_name="Müller",
        email="hans@example.org",
        language="en",
        membership_level=User.MembershipLevel.MEMBER,
    )


def _form_payload(member, **overrides):
    base = {
        "username": member.username,
        "email": member.email,
        "first_name": member.first_name,
        "last_name": member.last_name,
        "language": member.language,
        "is_active": "on" if member.is_active else "",
        "bio": member.bio,
        "qth_name": member.qth_name,
        "qrz_url": member.qrz_url,
        "phone": member.phone,
        "address": member.address,
        "locator": member.locator,
        "is_directory_visible": "on" if member.is_directory_visible else "",
    }
    base.update(overrides)
    return base


@pytest.mark.django_db
class TestUserUpdateViewAudit:
    def test_identity_change_emits_user_updated(self, client, admin, member):
        client.force_login(admin)
        before = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.USER_UPDATED
        ).count()
        client.post(
            reverse("accounts:user_edit", kwargs={"pk": member.pk}),
            _form_payload(member, email="new@example.org"),
        )
        after = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.USER_UPDATED,
            target_user=member,
        ).count()
        assert after == before + 1
        entry = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.USER_UPDATED, target_user=member
        ).latest("created_at")
        assert "email" in entry.message
        assert entry.actor == admin

    def test_no_change_no_audit(self, client, admin, member):
        client.force_login(admin)
        before = AccountAuditLog.objects.filter(target_user=member).count()
        client.post(
            reverse("accounts:user_edit", kwargs={"pk": member.pk}),
            _form_payload(member),
        )
        after = AccountAuditLog.objects.filter(target_user=member).count()
        # No changed_data → no audit
        assert after == before

    def test_is_active_flip_emits_deactivated(self, client, admin, member):
        client.force_login(admin)
        client.post(
            reverse("accounts:user_edit", kwargs={"pk": member.pk}),
            _form_payload(member, is_active=""),
        )
        member.refresh_from_db()
        assert not member.is_active
        entry = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.USER_DEACTIVATED,
            target_user=member,
        ).latest("created_at")
        assert entry.actor == admin

    def test_is_active_only_emits_only_deactivated_not_updated(
        self, client, admin, member
    ):
        client.force_login(admin)
        before_updated = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.USER_UPDATED, target_user=member
        ).count()
        client.post(
            reverse("accounts:user_edit", kwargs={"pk": member.pk}),
            _form_payload(member, is_active=""),
        )
        after_updated = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.USER_UPDATED, target_user=member
        ).count()
        # is_active alone → no USER_UPDATED
        assert after_updated == before_updated


@pytest.mark.django_db
class TestUserUpdateViewGeocodingTrigger:
    @patch("apps.accounts.views.geocode_address")
    def test_address_change_calls_geocode(self, mock_geocode, client, admin, member):
        mock_geocode.return_value = (Decimal("48.3"), Decimal("14.3"))
        client.force_login(admin)
        client.post(
            reverse("accounts:user_edit", kwargs={"pk": member.pk}),
            _form_payload(member, address="Hauptstraße 1, 4020 Linz"),
        )
        mock_geocode.assert_called_once()
        member.refresh_from_db()
        assert member.latitude == Decimal("48.3")
        assert member.longitude == Decimal("14.3")
        # Locator was computed from coords
        assert member.locator.startswith("JN")

    @patch("apps.accounts.views.geocode_address")
    def test_address_unchanged_no_geocode(self, mock_geocode, client, admin, member):
        client.force_login(admin)
        client.post(
            reverse("accounts:user_edit", kwargs={"pk": member.pk}),
            _form_payload(member, email="new@example.org"),
        )
        mock_geocode.assert_not_called()

    @patch("apps.accounts.views.geocode_address")
    def test_address_cleared_clears_coords(self, mock_geocode, client, admin, member):
        member.address = "Old address"
        member.latitude = Decimal("48.3")
        member.longitude = Decimal("14.3")
        member.locator = "JN78AB"
        member.save()
        client.force_login(admin)
        client.post(
            reverse("accounts:user_edit", kwargs={"pk": member.pk}),
            _form_payload(member, address=""),
        )
        member.refresh_from_db()
        assert member.address == ""
        assert member.latitude is None
        assert member.longitude is None
        # Locator follows the address when not explicitly overridden
        assert member.locator == ""
        mock_geocode.assert_not_called()


@pytest.mark.django_db
class TestUserCreateViewAudit:
    def test_create_emits_user_created(self, client, admin):
        client.force_login(admin)
        before = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.USER_CREATED
        ).count()
        client.post(
            reverse("accounts:user_create"),
            {
                "username": "OE5NEW1",
                "email": "new@example.org",
                "first_name": "",
                "last_name": "",
                "language": "en",
                "password1": "abcDEF123!xyz",
                "password2": "abcDEF123!xyz",
            },
        )
        after = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.USER_CREATED
        ).count()
        assert after == before + 1
        entry = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.USER_CREATED
        ).latest("created_at")
        assert "OE5NEW1" in entry.message
        assert entry.actor == admin
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_user_update_create_audit.py -v 2>&1 | tail -25`
Expected: Tests fail — no audit emission yet, no geocoding trigger yet.

- [ ] **Step 3: Implement `form_valid` audit + geocoding for UserUpdateView**

Edit `apps/accounts/views.py`. Add imports near the existing ones:

```python
from django.db import models as _db_models  # noqa: F401  (for select_for_update if needed)
from .models import AccountAuditLog
from .geocoding import geocode_address, lat_lon_to_locator
from .views_membership import _get_client_ip
```

(`_get_client_ip` already lives in `views_membership.py` — re-export by importing.)

Replace `UserUpdateView` with the audit-emitting version:

```python
class UserUpdateView(AdminRequiredMixin, UpdateView):
    model = User
    template_name = "accounts/user_form.html"
    form_class = UserChangeForm

    def get_success_url(self):
        return reverse("accounts:user_detail", kwargs={"pk": self.object.pk})

    def form_valid(self, form):
        changed_fields = set(form.changed_data)
        response = super().form_valid(form)
        self._maybe_geocode(self.object, changed_fields)

        tracked = changed_fields & TRACKED_USER_FIELDS
        if tracked:
            AccountAuditLog.log(
                event_type=AccountAuditLog.EventType.USER_UPDATED,
                actor=self.request.user,
                target_user=self.object,
                message=f"changed: {', '.join(sorted(tracked))}",
                ip_address=_get_client_ip(self.request),
            )
        if "is_active" in changed_fields:
            event = (
                AccountAuditLog.EventType.USER_ACTIVATED
                if self.object.is_active
                else AccountAuditLog.EventType.USER_DEACTIVATED
            )
            AccountAuditLog.log(
                event_type=event,
                actor=self.request.user,
                target_user=self.object,
                message="",
                ip_address=_get_client_ip(self.request),
            )

        messages.success(self.request, _("User updated successfully."))
        return response

    def _maybe_geocode(self, user, changed_fields):
        if "address" not in changed_fields:
            return
        if not user.address:
            user.latitude = None
            user.longitude = None
            if "locator" not in changed_fields:
                user.locator = ""
            user.save(update_fields=["latitude", "longitude", "locator"])
            return
        coords = geocode_address(user.address)
        if coords:
            lat, lon = coords
            user.latitude = lat
            user.longitude = lon
            if "locator" not in changed_fields:
                user.locator = lat_lon_to_locator(float(lat), float(lon))
            user.save(update_fields=["latitude", "longitude", "locator"])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_title"] = _("Edit User")
        return context
```

Important: The old `form_valid` had a redundant `messages.success(...)` call before `return super().form_valid(form)`. The new version moves the success message to AFTER `super().form_valid(form)` so it fires only on successful save.

Remove the duplicate `messages.success` from the old position (you should see it next to `return super().form_valid(form)`). The new code already includes it.

- [ ] **Step 4: Implement `form_valid` audit for UserCreateView**

Replace `UserCreateView.form_valid` to also emit `USER_CREATED`:

```python
class UserCreateView(AdminRequiredMixin, CreateView):
    model = User
    template_name = "accounts/user_form.html"
    form_class = UserCreationForm

    def get_success_url(self):
        return reverse("accounts:user_detail", kwargs={"pk": self.object.pk})

    def form_valid(self, form):
        response = super().form_valid(form)
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.USER_CREATED,
            actor=self.request.user,
            target_user=self.object,
            message=f"{self.object.username} <{self.object.email}>",
            ip_address=_get_client_ip(self.request),
        )
        messages.success(self.request, _("User created successfully."))
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_title"] = _("Create User")
        return context
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_user_update_create_audit.py -v 2>&1 | tail -25`
Expected: All tests PASS.

- [ ] **Step 6: Full regression**

Run: `uv run pytest tests/ -x --tb=short 2>&1 | tail -5`
Expected: All tests pass.

- [ ] **Step 7: ruff format + check**

Run: `uv run ruff format apps/accounts/views.py tests/test_user_update_create_audit.py 2>&1 | tail -2`
Run: `uv run ruff check apps/accounts/views.py tests/test_user_update_create_audit.py 2>&1 | tail -3`
Expected: Clean.

- [ ] **Step 8: Commit**

```bash
git add apps/accounts/views.py tests/test_user_update_create_audit.py
git commit -m "feat(accounts): USER_UPDATED + USER_CREATED audit + geocoding trigger

UserUpdateView.form_valid now:
- emits USER_UPDATED with the diff (changed identity/profile fields)
- emits USER_ACTIVATED/USER_DEACTIVATED when is_active flips, separate
  from USER_UPDATED so the audit feed stays per-event-type clean
- triggers geocoding when address changes (clear coords if address
  was emptied; recompute lat/lon/locator from Nominatim otherwise),
  with respect to a manual locator override in the same submit

UserCreateView.form_valid emits USER_CREATED with the user's username
+ email in the message."
```

---

### Task 5: `user_form.html` mobile refactor + 3 panels

**Files:**
- Modify: `apps/accounts/templates/accounts/user_form.html`

> **Subagent:** `pixel`. MUST invoke `Skill("frontend-design")` before any HTML edit.

- [ ] **Step 1: Write failing test in tests/test_user_change_form.py (append)**

Append to `tests/test_user_change_form.py`:

```python
@pytest.mark.django_db
class TestUserFormTemplate:
    """user_form.html renders 3 panels in Edit-Mode (Identity / Profil /
    Adresse) and uses grid-main (no inline max-width)."""

    def test_edit_form_has_three_panels(self, client, admin_user, member):
        client.force_login(admin_user)
        resp = client.get(
            reverse("accounts:user_edit", kwargs={"pk": member.pk})
        )
        body = resp.content.decode()
        # Identity panel (always)
        assert ">Identity<" in body or "<h2>Identity</h2>" in body or "Identity" in body
        # Profil panel (Edit-Mode only)
        assert "Profil" in body
        # Address panel (Edit-Mode only)
        assert "Adresse" in body or "Address" in body
        # Mobile-friendly: no inline max-width on the form
        assert 'style="max-width:640px' not in body

    def test_create_form_omits_profile_address_panels(self, client, admin_user):
        client.force_login(admin_user)
        resp = client.get(reverse("accounts:user_create"))
        body = resp.content.decode()
        # Profil/Adresse only show up in Edit-Mode (1c spec Sektion 3.4)
        assert "Profil" not in body or "Adresse" not in body
```

Add `admin_user` fixture at the top of the file:

```python
@pytest.fixture
def admin_user(db):
    return User.objects.create_superuser(
        username="OE5ADMIN",
        password="x",
        email="admin@example.org",
    )
```

Also add `reverse` import: `from django.urls import reverse`.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_user_change_form.py::TestUserFormTemplate -v 2>&1 | tail -10`
Expected: At least the "no inline max-width" + panel-section tests fail (current template uses one panel + inline max-width:640px).

- [ ] **Step 3: Rebuild `user_form.html`**

Replace `apps/accounts/templates/accounts/user_form.html` with:

```django
{% extends "base.html" %}
{% load i18n %}

{% block title %}{{ form_title|default:_("User") }} · OE5XRX{% endblock %}

{% block breadcrumbs %}
  <a href="{% url 'accounts:user_list' %}">{% trans "Users" %}</a>
  <span class="sep">/</span>
  <span class="cur">{{ form_title }}</span>
{% endblock %}

{% block content %}
<div class="page-head">
  <div class="page-head-main">
    <div class="page-eyebrow">{% trans "Administration" %}</div>
    <h1 class="page-title">{{ form_title|default:_("Edit user") }}</h1>
  </div>
</div>

<form method="post" enctype="multipart/form-data" class="grid grid-main">
  {% csrf_token %}

  <div class="stack-gap-14">
    {# Identity panel — always #}
    <section class="panel">
      <div class="panel-head">
        <div class="panel-title"><span class="dot"></span>{% trans "Identity" %}</div>
      </div>
      <div class="panel-body">
        {% with field=form.username %}
        <div class="form-group">
          <label class="form-label" for="{{ field.id_for_label }}">{{ field.label }}</label>
          {{ field }}
          {% if field.errors %}<div class="form-error">{{ field.errors|join:", " }}</div>{% endif %}
        </div>
        {% endwith %}
        {% with field=form.email %}
        <div class="form-group">
          <label class="form-label" for="{{ field.id_for_label }}">{{ field.label }}</label>
          {{ field }}
          {% if field.errors %}<div class="form-error">{{ field.errors|join:", " }}</div>{% endif %}
        </div>
        {% endwith %}
        <div class="form-row">
          {% with field=form.first_name %}
          <div class="form-group">
            <label class="form-label" for="{{ field.id_for_label }}">{{ field.label }}</label>
            {{ field }}
            {% if field.errors %}<div class="form-error">{{ field.errors|join:", " }}</div>{% endif %}
          </div>
          {% endwith %}
          {% with field=form.last_name %}
          <div class="form-group">
            <label class="form-label" for="{{ field.id_for_label }}">{{ field.label }}</label>
            {{ field }}
            {% if field.errors %}<div class="form-error">{{ field.errors|join:", " }}</div>{% endif %}
          </div>
          {% endwith %}
        </div>
        <div class="form-row">
          {% with field=form.language %}
          <div class="form-group">
            <label class="form-label" for="{{ field.id_for_label }}">{{ field.label }}</label>
            {{ field }}
            {% if field.errors %}<div class="form-error">{{ field.errors|join:", " }}</div>{% endif %}
          </div>
          {% endwith %}
          {% if form.is_active %}
          {% with field=form.is_active %}
          <div class="form-group">
            <label class="form-label" for="{{ field.id_for_label }}">{{ field.label }}</label>
            {{ field }}
            {% if field.errors %}<div class="form-error">{{ field.errors|join:", " }}</div>{% endif %}
          </div>
          {% endwith %}
          {% endif %}
        </div>
        {% if form.is_directory_visible %}
        {% with field=form.is_directory_visible %}
        <div class="form-group">
          <label class="form-label" for="{{ field.id_for_label }}">{{ field.label }}</label>
          {{ field }}
        </div>
        {% endwith %}
        {% endif %}
        {% if form.password1 %}
        <div class="form-row">
          {% with field=form.password1 %}
          <div class="form-group">
            <label class="form-label" for="{{ field.id_for_label }}">{{ field.label }}</label>
            {{ field }}
            {% if field.errors %}<div class="form-error">{{ field.errors|join:", " }}</div>{% endif %}
          </div>
          {% endwith %}
          {% with field=form.password2 %}
          <div class="form-group">
            <label class="form-label" for="{{ field.id_for_label }}">{{ field.label }}</label>
            {{ field }}
            {% if field.errors %}<div class="form-error">{{ field.errors|join:", " }}</div>{% endif %}
          </div>
          {% endwith %}
        </div>
        {% endif %}
      </div>
    </section>

    {% if form.avatar %}
    {# Profil panel — Edit-Mode only (UserCreationForm doesn't carry these) #}
    <section class="panel">
      <div class="panel-head">
        <div class="panel-title"><span class="dot"></span>{% trans "Profil" %}</div>
      </div>
      <div class="panel-body">
        {% with field=form.avatar %}
        <div class="form-group">
          <label class="form-label" for="{{ field.id_for_label }}">{{ field.label }}</label>
          {{ field }}
          {% if field.errors %}<div class="form-error">{{ field.errors|join:", " }}</div>{% endif %}
        </div>
        {% endwith %}
        {% with field=form.bio %}
        <div class="form-group">
          <label class="form-label" for="{{ field.id_for_label }}">{{ field.label }}</label>
          {{ field }}
          {% if field.errors %}<div class="form-error">{{ field.errors|join:", " }}</div>{% endif %}
        </div>
        {% endwith %}
        <div class="form-row">
          {% with field=form.qth_name %}
          <div class="form-group">
            <label class="form-label" for="{{ field.id_for_label }}">{{ field.label }}</label>
            {{ field }}
            {% if field.errors %}<div class="form-error">{{ field.errors|join:", " }}</div>{% endif %}
          </div>
          {% endwith %}
          {% with field=form.qrz_url %}
          <div class="form-group">
            <label class="form-label" for="{{ field.id_for_label }}">{{ field.label }}</label>
            {{ field }}
            {% if field.errors %}<div class="form-error">{{ field.errors|join:", " }}</div>{% endif %}
          </div>
          {% endwith %}
        </div>
        {% with field=form.phone %}
        <div class="form-group">
          <label class="form-label" for="{{ field.id_for_label }}">{{ field.label }}</label>
          {{ field }}
          {% if field.errors %}<div class="form-error">{{ field.errors|join:", " }}</div>{% endif %}
        </div>
        {% endwith %}
      </div>
    </section>
    {% endif %}

    {% if form.address %}
    {# Adresse & Standort panel — Edit-Mode only #}
    <section class="panel">
      <div class="panel-head">
        <div class="panel-title"><span class="dot"></span>{% trans "Adresse & Standort" %}</div>
      </div>
      <div class="panel-body">
        {% with field=form.address %}
        <div class="form-group">
          <label class="form-label" for="{{ field.id_for_label }}">{{ field.label }}</label>
          {{ field }}
          {% if field.errors %}<div class="form-error">{{ field.errors|join:", " }}</div>{% endif %}
        </div>
        {% endwith %}
        <p class="t-mono t-muted" style="margin:8px 0;">
          {% trans "Locator + lat/lon werden bei Speichern aus der Adresse berechnet." %}
        </p>
        {% with field=form.locator %}
        <div class="form-group">
          <label class="form-label" for="{{ field.id_for_label }}">{{ field.label }} ({% trans "manueller Override" %})</label>
          {{ field }}
          {% if field.errors %}<div class="form-error">{{ field.errors|join:", " }}</div>{% endif %}
        </div>
        {% endwith %}
      </div>
    </section>
    {% endif %}

    <div class="panel-foot row-gap-8">
      <button type="submit" class="btn btn-primary">{% trans "Save user" %}</button>
      {% if object %}
        <a href="{% url 'accounts:user_detail' object.pk %}" class="btn btn-ghost">{% trans "Cancel" %}</a>
      {% else %}
        <a href="{% url 'accounts:user_list' %}" class="btn btn-ghost">{% trans "Cancel" %}</a>
      {% endif %}
    </div>
  </div>

  <aside class="stack-gap-14">
    <section class="panel">
      <div class="panel-head">
        <div class="panel-title"><span class="dot"></span>
          {% if object %}{% trans "User-Info" %}{% else %}{% trans "Hinweis" %}{% endif %}
        </div>
      </div>
      <div class="panel-body">
        {% if object %}
          <dl class="dlist">
            <dt>{% trans "ID" %}</dt><dd class="t-mono">#{{ object.pk }}</dd>
            <dt>{% trans "Joined" %}</dt><dd class="t-mono-sm">{{ object.date_joined|date:"Y-m-d H:i" }}</dd>
            {% if object.last_login %}
            <dt>{% trans "Last login" %}</dt><dd class="t-mono-sm">{{ object.last_login|date:"Y-m-d H:i" }}</dd>
            {% endif %}
            <dt>{% trans "Role" %}</dt><dd>{{ object.get_membership_level_display }}</dd>
            {% if object.latitude is not None or object.longitude is not None %}
            <dt>{% trans "Lat/Lon" %}</dt><dd class="t-mono-sm t-muted">{{ object.latitude }}, {{ object.longitude }}</dd>
            {% endif %}
          </dl>
        {% else %}
          <p class="t-muted">
            {% trans "Profil-Daten ergänzt der User selbst über sein Profil." %}
          </p>
        {% endif %}
      </div>
    </section>
  </aside>
</form>
{% endblock %}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_user_change_form.py::TestUserFormTemplate -v 2>&1 | tail -10`
Expected: PASS.

- [ ] **Step 5: Full regression**

Run: `uv run pytest tests/ -x --tb=short 2>&1 | tail -5`
Expected: All pass.

- [ ] **Step 6: Commit**

```bash
git add apps/accounts/templates/accounts/user_form.html tests/test_user_change_form.py
git commit -m "feat(accounts): user_form.html 3-panel mobile-friendly refactor

Identity / Profil / Adresse Panels in a grid grid-main with an aside
showing User-Info (Edit) or a hint (Create). form-row + form-group
patterns throughout; inline max-width:640px removed. The Profil and
Adresse panels are gated on `form.avatar` / `form.address`, so the
UserCreationForm (Create-mode, 5 identity fields) still renders only
the Identity panel."
```

---

### Task 6: Profile forms (Identity / Profil / Adresse / Password)

**Files:**
- Modify: `apps/accounts/forms.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_user_change_form.py`:

```python
@pytest.mark.django_db
class TestProfileForms:
    def test_identity_form_fields(self, member):
        from apps.accounts.forms import ProfileIdentityForm

        form = ProfileIdentityForm(instance=member)
        assert set(form.fields.keys()) == {"email", "first_name", "last_name", "language"}

    def test_profile_form_fields(self, member):
        from apps.accounts.forms import ProfileProfileForm

        form = ProfileProfileForm(instance=member)
        assert set(form.fields.keys()) == {
            "avatar", "bio", "qth_name", "qrz_url", "phone", "is_directory_visible",
        }

    def test_address_form_fields(self, member):
        from apps.accounts.forms import ProfileAddressForm

        form = ProfileAddressForm(instance=member)
        assert set(form.fields.keys()) == {"address", "locator"}

    def test_address_form_locator_validation(self, member):
        from apps.accounts.forms import ProfileAddressForm

        form = ProfileAddressForm(
            data={"address": "anywhere", "locator": "XX"}, instance=member
        )
        assert not form.is_valid()
        assert "locator" in form.errors

    def test_profile_form_avatar_resize_called(
        self, member, tmp_path, settings, monkeypatch
    ):
        from apps.accounts.avatars import process_avatar_file as real_resize
        from apps.accounts.forms import ProfileProfileForm

        settings.MEDIA_ROOT = str(tmp_path)
        calls = []

        def fake_process(path):
            calls.append(path)
            real_resize(path)

        monkeypatch.setattr("apps.accounts.forms.process_avatar_file", fake_process)

        buf = _make_jpeg(1024, 768)
        f = SimpleUploadedFile("ok.jpg", buf.read(), content_type="image/jpeg")
        form = ProfileProfileForm(
            data={
                "bio": "",
                "qth_name": "",
                "qrz_url": "",
                "phone": "",
                "is_directory_visible": "on",
            },
            files={"avatar": f},
            instance=member,
        )
        assert form.is_valid(), form.errors
        form.save()
        assert len(calls) == 1


@pytest.mark.django_db
class TestPasswordChangeForm:
    def test_widgets_get_form_control_class(self, member):
        from apps.accounts.forms import PasswordChangeForm

        member.set_password("oldsecret123!")
        member.save()
        form = PasswordChangeForm(user=member)
        for field in form.fields.values():
            assert field.widget.attrs.get("class") == "form-control"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_user_change_form.py::TestProfileForms tests/test_user_change_form.py::TestPasswordChangeForm -v 2>&1 | tail -15`
Expected: `ImportError` because the new forms don't exist yet.

- [ ] **Step 3: Add the 4 Profile forms to `apps/accounts/forms.py`**

Append to `apps/accounts/forms.py`:

```python
from django.contrib.auth.forms import PasswordChangeForm as DjangoPasswordChangeForm


class ProfileIdentityForm(forms.ModelForm):
    """Self-edit of identity fields (Profile page → Identity panel)."""

    class Meta:
        model = User
        fields = ("email", "first_name", "last_name", "language")
        widgets = {
            "email": forms.EmailInput(attrs={"class": "form-control"}),
            "first_name": forms.TextInput(attrs={"class": "form-control"}),
            "last_name": forms.TextInput(attrs={"class": "form-control"}),
            "language": forms.Select(attrs={"class": "form-select"}),
        }


class ProfileProfileForm(forms.ModelForm):
    """Self-edit of profile-cosmetic fields (Profile page → Profil panel)."""

    class Meta:
        model = User
        fields = (
            "avatar", "bio", "qth_name", "qrz_url", "phone", "is_directory_visible",
        )
        widgets = {
            "avatar": forms.ClearableFileInput(
                attrs={"class": "form-control", "accept": "image/*"}
            ),
            "bio": forms.Textarea(
                attrs={"class": "form-control", "rows": 3, "maxlength": 500}
            ),
            "qth_name": forms.TextInput(attrs={"class": "form-control"}),
            "qrz_url": forms.URLInput(attrs={"class": "form-control"}),
            "phone": forms.TextInput(attrs={"class": "form-control"}),
            "is_directory_visible": forms.CheckboxInput(
                attrs={"class": "form-check-input"}
            ),
        }

    def clean_avatar(self):
        from .avatars import validate_avatar_upload

        f = self.cleaned_data.get("avatar")
        validate_avatar_upload(f)
        return f

    def save(self, commit=True):
        user = super().save(commit=commit)
        if commit and "avatar" in self.changed_data and user.avatar:
            process_avatar_file(user.avatar.path)
        return user


class ProfileAddressForm(forms.ModelForm):
    """Self-edit of address + locator override (Profile page → Adresse panel).

    Geocoding-Trigger lives in ProfileView._maybe_geocode, not here.
    """

    class Meta:
        model = User
        fields = ("address", "locator")
        widgets = {
            "address": forms.Textarea(attrs={"class": "form-control", "rows": 4}),
            "locator": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "JN78AB"}
            ),
        }

    def clean_locator(self):
        from .models import LOCATOR_REGEX

        loc = self.cleaned_data.get("locator", "").strip().upper()
        if loc and not LOCATOR_REGEX.match(loc):
            raise forms.ValidationError(
                _(
                    "Locator muss 2 Buchstaben + 2 Ziffern + 2 Buchstaben sein "
                    "(z.B. JN78AB)."
                )
            )
        return loc


class PasswordChangeForm(DjangoPasswordChangeForm):
    """Bootstrap-styled overlay over Django's PasswordChangeForm.

    Re-Auth via the inherited ``old_password`` field; ProfilePasswordChangeView
    calls ``update_session_auth_hash`` after save() so the user stays logged in.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
```

(`process_avatar_file` was imported at the top in Task 3 — re-use it.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_user_change_form.py::TestProfileForms tests/test_user_change_form.py::TestPasswordChangeForm -v 2>&1 | tail -15`
Expected: All PASS.

- [ ] **Step 5: ruff format + check**

Run: `uv run ruff format apps/accounts/forms.py tests/test_user_change_form.py 2>&1 | tail -2`
Run: `uv run ruff check apps/accounts/forms.py tests/test_user_change_form.py 2>&1 | tail -3`
Expected: Clean.

- [ ] **Step 6: Full regression**

Run: `uv run pytest tests/ -x --tb=short 2>&1 | tail -5`
Expected: All pass.

- [ ] **Step 7: Commit**

```bash
git add apps/accounts/forms.py tests/test_user_change_form.py
git commit -m "feat(accounts): add Profile* forms + PasswordChangeForm

Four new ModelForms for the Profile-Page multi-form layout:
- ProfileIdentityForm: email/first_name/last_name/language
- ProfileProfileForm: avatar/bio/qth_name/qrz_url/phone/
  is_directory_visible; avatar runs validate_avatar_upload and
  process_avatar_file on save
- ProfileAddressForm: address + locator (geocoding trigger in view)
- PasswordChangeForm: bootstrap-styled Django PasswordChangeForm"
```

---

### Task 7: ProfileView rewrite (4-form dispatch + audit + geocoding + onboarding)

**Files:**
- Modify: `apps/accounts/views.py`
- Create: `tests/test_profile_view.py`
- Create: `tests/test_profile_geocoding.py`
- Create: `tests/test_profile_onboarding.py`

- [ ] **Step 1: Write failing tests — `tests/test_profile_view.py`**

Create NEW file `tests/test_profile_view.py`:

```python
"""ProfileView rewrite: 4 forms with form_name dispatch.

Sub-Spec 1c Sektion 4.
"""

import pytest
from django.urls import reverse

from apps.accounts.models import AccountAuditLog, User


@pytest.fixture
def member(db):
    return User.objects.create_user(
        username="OE5MEM1",
        password="x",
        first_name="",
        last_name="",
        email="m@example.org",
        language="en",
        membership_level=User.MembershipLevel.MEMBER,
    )


@pytest.mark.django_db
class TestProfileViewGET:
    def test_get_renders_four_forms(self, client, member):
        client.force_login(member)
        resp = client.get(reverse("accounts:profile"))
        assert resp.status_code == 200
        for key in ("identity_form", "profile_form", "address_form", "password_form"):
            assert key in resp.context

    def test_get_has_onboarding_hints(self, client, member):
        client.force_login(member)
        resp = client.get(reverse("accounts:profile"))
        assert "onboarding_hints" in resp.context
        assert resp.context["onboarding_hints"]["name_missing"] is True

    def test_get_anonymous_redirected(self, client):
        resp = client.get(reverse("accounts:profile"))
        assert resp.status_code in (302, 401, 403)


@pytest.mark.django_db
class TestProfileViewPOSTIdentity:
    def test_identity_save(self, client, member):
        client.force_login(member)
        resp = client.post(
            reverse("accounts:profile"),
            {
                "form_name": "identity",
                "identity-email": "new@example.org",
                "identity-first_name": "Hans",
                "identity-last_name": "Müller",
                "identity-language": "en",
            },
        )
        assert resp.status_code == 302
        member.refresh_from_db()
        assert member.email == "new@example.org"
        assert member.first_name == "Hans"

    def test_identity_save_emits_audit(self, client, member):
        client.force_login(member)
        before = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.USER_UPDATED, target_user=member
        ).count()
        client.post(
            reverse("accounts:profile"),
            {
                "form_name": "identity",
                "identity-email": "new@example.org",
                "identity-first_name": "",
                "identity-last_name": "",
                "identity-language": "en",
            },
        )
        after = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.USER_UPDATED, target_user=member
        ).count()
        assert after == before + 1
        entry = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.USER_UPDATED, target_user=member
        ).latest("created_at")
        assert "self-edit" in entry.message
        # Self-edit: actor and target are the same
        assert entry.actor == member


@pytest.mark.django_db
class TestProfileViewPOSTProfile:
    def test_profile_save(self, client, member):
        client.force_login(member)
        resp = client.post(
            reverse("accounts:profile"),
            {
                "form_name": "profile",
                "profile-bio": "QRP enthusiast, 40m CW.",
                "profile-qth_name": "Linz",
                "profile-qrz_url": "",
                "profile-phone": "",
                "profile-is_directory_visible": "on",
            },
        )
        assert resp.status_code == 302
        member.refresh_from_db()
        assert member.bio == "QRP enthusiast, 40m CW."
        assert member.qth_name == "Linz"


@pytest.mark.django_db
class TestProfileViewPOSTAddress:
    def test_address_save_no_geocode_when_unchanged(self, client, member):
        member.address = "Unchanged"
        member.save()
        client.force_login(member)
        resp = client.post(
            reverse("accounts:profile"),
            {
                "form_name": "address",
                "address-address": "Unchanged",
                "address-locator": "",
            },
        )
        assert resp.status_code == 302


@pytest.mark.django_db
class TestProfileViewPOSTUnknownForm:
    def test_unknown_form_name_redirects_with_error(self, client, member):
        client.force_login(member)
        resp = client.post(
            reverse("accounts:profile"),
            {"form_name": "bogus"},
        )
        assert resp.status_code == 302
```

- [ ] **Step 2: Write failing tests — `tests/test_profile_geocoding.py`**

Create NEW file `tests/test_profile_geocoding.py`:

```python
"""ProfileView address-save → geocoding trigger.

Sub-Spec 1c Sektion 4 _maybe_geocode.
"""

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.accounts.models import User


@pytest.fixture
def member(db):
    return User.objects.create_user(
        username="OE5MEM1",
        password="x",
        membership_level=User.MembershipLevel.MEMBER,
    )


@pytest.mark.django_db
class TestProfileAddressGeocoding:
    @patch("apps.accounts.views.geocode_address")
    def test_address_change_triggers_geocode(self, mock_geocode, client, member):
        mock_geocode.return_value = (Decimal("48.3"), Decimal("14.3"))
        client.force_login(member)
        client.post(
            reverse("accounts:profile"),
            {
                "form_name": "address",
                "address-address": "Hauptstraße 1, 4020 Linz",
                "address-locator": "",
            },
        )
        mock_geocode.assert_called_once()
        member.refresh_from_db()
        assert member.latitude == Decimal("48.3")
        assert member.locator.startswith("JN")

    @patch("apps.accounts.views.geocode_address")
    def test_address_cleared_resets_coords(self, mock_geocode, client, member):
        member.address = "Old"
        member.latitude = Decimal("48.3")
        member.longitude = Decimal("14.3")
        member.locator = "JN78AB"
        member.save()
        client.force_login(member)
        client.post(
            reverse("accounts:profile"),
            {
                "form_name": "address",
                "address-address": "",
                "address-locator": "",
            },
        )
        member.refresh_from_db()
        assert member.latitude is None
        assert member.locator == ""
        mock_geocode.assert_not_called()

    @patch("apps.accounts.views.geocode_address")
    def test_geocode_failure_leaves_coords(self, mock_geocode, client, member):
        mock_geocode.return_value = None
        member.latitude = Decimal("48.3")
        member.longitude = Decimal("14.3")
        member.locator = "JN78AB"
        member.save()
        client.force_login(member)
        client.post(
            reverse("accounts:profile"),
            {
                "form_name": "address",
                "address-address": "Geocoding will fail for this",
                "address-locator": "",
            },
        )
        member.refresh_from_db()
        # Coords stay even though geocode returned None — the spec says
        # "fail closed: leave existing values, user can manual-override".
        assert member.latitude == Decimal("48.3")
        assert member.locator == "JN78AB"
```

- [ ] **Step 3: Write failing tests — `tests/test_profile_onboarding.py`**

Create NEW file `tests/test_profile_onboarding.py`:

```python
"""Onboarding-Hint-Kontext und Render-Bedingungen auf der Profile-Page.

Sub-Spec 1c Sektion 4.3.
"""

import pytest
from django.urls import reverse

from apps.accounts.models import User


@pytest.fixture
def empty_user(db):
    return User.objects.create_user(
        username="OE5EMPTY",
        password="x",
        first_name="",
        last_name="",
        email="empty@example.org",
        membership_level=User.MembershipLevel.MEMBER,
    )


@pytest.mark.django_db
class TestOnboardingHints:
    def test_empty_user_all_hints_active(self, client, empty_user):
        client.force_login(empty_user)
        resp = client.get(reverse("accounts:profile"))
        hints = resp.context["onboarding_hints"]
        assert hints["name_missing"]
        assert hints["avatar_missing"]
        assert hints["bio_missing"]
        assert hints["qth_missing"]
        assert hints["address_missing"]

    def test_bio_filled_no_bio_hint(self, client, empty_user):
        empty_user.bio = "I am a radio amateur."
        empty_user.save()
        client.force_login(empty_user)
        resp = client.get(reverse("accounts:profile"))
        hints = resp.context["onboarding_hints"]
        assert not hints["bio_missing"]
        # Others still missing
        assert hints["name_missing"]

    def test_fully_filled_user_no_hints(self, client, empty_user, tmp_path, settings):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from PIL import Image

        settings.MEDIA_ROOT = str(tmp_path)
        # Set name, bio, qth, address
        empty_user.first_name = "Hans"
        empty_user.bio = "QRP"
        empty_user.qth_name = "Linz"
        empty_user.address = "Hauptstraße 1"
        # Upload a fake avatar file
        img = Image.new("RGB", (50, 50), color=(255, 0, 0))
        import io

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        buf.seek(0)
        f = SimpleUploadedFile("a.jpg", buf.read(), content_type="image/jpeg")
        empty_user.avatar = f
        empty_user.save()

        client.force_login(empty_user)
        resp = client.get(reverse("accounts:profile"))
        hints = resp.context["onboarding_hints"]
        assert not hints["name_missing"]
        assert not hints["avatar_missing"]
        assert not hints["bio_missing"]
        assert not hints["qth_missing"]
        assert not hints["address_missing"]
```

- [ ] **Step 4: Run all three test files to verify they fail**

Run: `uv run pytest tests/test_profile_view.py tests/test_profile_geocoding.py tests/test_profile_onboarding.py -v 2>&1 | tail -25`
Expected: Tests fail — old `ProfileView` is an UpdateView with a single form, no form_name dispatch, no audit, no onboarding context.

- [ ] **Step 5: Rewrite `ProfileView` in `apps/accounts/views.py`**

Add the import for the new forms at the top of `views.py`:

```python
from .forms import (
    LoginForm,
    PasswordChangeForm,
    ProfileAddressForm,
    ProfileIdentityForm,
    ProfileProfileForm,
    UserChangeForm,
    UserCreationForm,
)
```

(Drop the unused `ProfileForm` import — it's gone now.)

Replace the existing `ProfileView` with:

```python
class ProfileView(LoginRequiredMixin, TemplateView):
    template_name = "accounts/profile.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        ctx["identity_form"] = ProfileIdentityForm(instance=user, prefix="identity")
        ctx["profile_form"] = ProfileProfileForm(instance=user, prefix="profile")
        ctx["address_form"] = ProfileAddressForm(instance=user, prefix="address")
        ctx["password_form"] = PasswordChangeForm(user=user)
        ctx["onboarding_hints"] = self._onboarding_hints(user)
        from apps.sso.views import _active_sessions_for

        ctx["self_sessions"] = _active_sessions_for(user)
        return ctx

    def post(self, request, *args, **kwargs):
        form_name = request.POST.get("form_name", "")
        user = request.user
        if form_name == "identity":
            return self._save_identity(request, user)
        if form_name == "profile":
            return self._save_profile(request, user)
        if form_name == "address":
            return self._save_address(request, user)
        messages.error(request, _("Unknown form."))
        return redirect("accounts:profile")

    def _save_identity(self, request, user):
        form = ProfileIdentityForm(
            request.POST, instance=user, prefix="identity"
        )
        if form.is_valid():
            changed = set(form.changed_data)
            form.save()
            self._emit_user_updated(request, user, changed)
            messages.success(request, _("Identity updated."))
        else:
            for errors in form.errors.values():
                messages.error(request, "; ".join(errors))
        return redirect("accounts:profile")

    def _save_profile(self, request, user):
        form = ProfileProfileForm(
            request.POST, request.FILES, instance=user, prefix="profile"
        )
        if form.is_valid():
            changed = set(form.changed_data)
            form.save()
            self._emit_user_updated(request, user, changed)
            messages.success(request, _("Profile updated."))
        else:
            for errors in form.errors.values():
                messages.error(request, "; ".join(errors))
        return redirect("accounts:profile")

    def _save_address(self, request, user):
        form = ProfileAddressForm(
            request.POST, instance=user, prefix="address"
        )
        if form.is_valid():
            changed = set(form.changed_data)
            form.save()
            self._maybe_geocode(user, changed)
            self._emit_user_updated(request, user, changed)
            messages.success(request, _("Address updated."))
        else:
            for errors in form.errors.values():
                messages.error(request, "; ".join(errors))
        return redirect("accounts:profile")

    def _maybe_geocode(self, user, changed_fields):
        if "address" not in changed_fields:
            return
        if not user.address:
            user.latitude = None
            user.longitude = None
            if "locator" not in changed_fields:
                user.locator = ""
            user.save(update_fields=["latitude", "longitude", "locator"])
            return
        coords = geocode_address(user.address)
        if coords:
            lat, lon = coords
            user.latitude = lat
            user.longitude = lon
            if "locator" not in changed_fields:
                user.locator = lat_lon_to_locator(float(lat), float(lon))
            user.save(update_fields=["latitude", "longitude", "locator"])

    def _emit_user_updated(self, request, user, changed_fields):
        tracked = changed_fields & TRACKED_USER_FIELDS
        if not tracked:
            return
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.USER_UPDATED,
            actor=user,
            target_user=user,
            message=f"self-edit changed: {', '.join(sorted(tracked))}",
            ip_address=_get_client_ip(request),
        )

    def _onboarding_hints(self, user):
        return {
            "name_missing": not (user.first_name or user.last_name),
            "avatar_missing": not user.avatar,
            "bio_missing": not user.bio,
            "qth_missing": not user.qth_name,
            "address_missing": not user.address,
        }
```

Important: At the top of `views.py`, the `from django.views.generic import ...` import needs `TemplateView` added:

```python
from django.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    ListView,
    TemplateView,
    UpdateView,
)
```

- [ ] **Step 6: Run the three test files**

Run: `uv run pytest tests/test_profile_view.py tests/test_profile_geocoding.py tests/test_profile_onboarding.py -v 2>&1 | tail -30`
Expected: All PASS.

- [ ] **Step 7: Full regression**

Run: `uv run pytest tests/ -x --tb=short 2>&1 | tail -5`
Expected: All pass (existing tests that use the old `ProfileForm` may need updates — `tests/test_accounts.py` has a `test_profile_view` etc. If anything breaks, the breakage is informative: the old test asserted on `form` context key whereas the new view uses `identity_form` etc.).

- [ ] **Step 8: ruff format + check**

Run: `uv run ruff format apps/accounts/views.py tests/test_profile_view.py tests/test_profile_geocoding.py tests/test_profile_onboarding.py 2>&1 | tail -2`
Run: `uv run ruff check apps/accounts/views.py tests/test_profile_view.py tests/test_profile_geocoding.py tests/test_profile_onboarding.py 2>&1 | tail -3`
Expected: Clean.

- [ ] **Step 9: Commit**

```bash
git add apps/accounts/views.py tests/test_profile_view.py \
        tests/test_profile_geocoding.py tests/test_profile_onboarding.py
git commit -m "feat(accounts): ProfileView rewrite — 4-form dispatch + audit + geocoding

Old single-form ProfileView is replaced with a TemplateView that
hosts four forms (Identity / Profil / Adresse / Passwort) and
dispatches the POST based on a hidden 'form_name' field. Each
sub-save emits a self-edit USER_UPDATED audit and the address
sub-save also runs geocoding (fail-closed).

Onboarding hints (name/avatar/bio/qth/address missing) land in the
context so the template can render per-panel CTAs."
```

---

### Task 8: ProfilePasswordChangeView + URL + tests

**Files:**
- Modify: `apps/accounts/views.py`
- Modify: `apps/accounts/urls.py`
- Create: `tests/test_password_change.py`

- [ ] **Step 1: Write failing tests**

Create NEW file `tests/test_password_change.py`:

```python
"""ProfilePasswordChangeView — self-service password change.

Sub-Spec 1c Sektion 4.4.
"""

import pytest
from django.contrib.auth import authenticate
from django.urls import reverse

from apps.accounts.models import AccountAuditLog, User


@pytest.fixture
def member(db):
    u = User.objects.create_user(
        username="OE5MEM1",
        membership_level=User.MembershipLevel.MEMBER,
    )
    u.set_password("oldsecret123!")
    u.save()
    return u


@pytest.mark.django_db
class TestProfilePasswordChange:
    def test_valid_change(self, client, member):
        client.force_login(member)
        resp = client.post(
            reverse("accounts:password_change"),
            {
                "old_password": "oldsecret123!",
                "new_password1": "newsecret456!",
                "new_password2": "newsecret456!",
            },
        )
        assert resp.status_code == 302
        member.refresh_from_db()
        # Old password no longer authenticates
        assert authenticate(username=member.username, password="oldsecret123!") is None
        # New one does
        assert authenticate(username=member.username, password="newsecret456!") is not None

    def test_session_stays_alive(self, client, member):
        """update_session_auth_hash must keep the session valid after change."""
        client.force_login(member)
        client.post(
            reverse("accounts:password_change"),
            {
                "old_password": "oldsecret123!",
                "new_password1": "newsecret456!",
                "new_password2": "newsecret456!",
            },
        )
        # Subsequent GET on profile still authenticated
        resp = client.get(reverse("accounts:profile"))
        assert resp.status_code == 200

    def test_emits_password_changed_audit(self, client, member):
        client.force_login(member)
        before = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.PASSWORD_CHANGED, target_user=member
        ).count()
        client.post(
            reverse("accounts:password_change"),
            {
                "old_password": "oldsecret123!",
                "new_password1": "newsecret456!",
                "new_password2": "newsecret456!",
            },
        )
        after = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.PASSWORD_CHANGED, target_user=member
        ).count()
        assert after == before + 1
        entry = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.PASSWORD_CHANGED, target_user=member
        ).latest("created_at")
        assert entry.message == "self-edit changed: password"
        assert entry.actor == member

    def test_wrong_old_password_no_change(self, client, member):
        client.force_login(member)
        resp = client.post(
            reverse("accounts:password_change"),
            {
                "old_password": "WRONG",
                "new_password1": "newsecret456!",
                "new_password2": "newsecret456!",
            },
        )
        assert resp.status_code == 302
        member.refresh_from_db()
        assert authenticate(username=member.username, password="oldsecret123!") is not None
        # No audit
        assert (
            AccountAuditLog.objects.filter(
                event_type=AccountAuditLog.EventType.PASSWORD_CHANGED, target_user=member
            ).count()
            == 0
        )

    def test_mismatched_new_passwords_no_change(self, client, member):
        client.force_login(member)
        client.post(
            reverse("accounts:password_change"),
            {
                "old_password": "oldsecret123!",
                "new_password1": "newsecret456!",
                "new_password2": "DIFFERENT",
            },
        )
        member.refresh_from_db()
        assert authenticate(username=member.username, password="oldsecret123!") is not None
        assert (
            AccountAuditLog.objects.filter(
                event_type=AccountAuditLog.EventType.PASSWORD_CHANGED, target_user=member
            ).count()
            == 0
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_password_change.py -v 2>&1 | tail -15`
Expected: `NoReverseMatch` for `accounts:password_change`.

- [ ] **Step 3: Add `ProfilePasswordChangeView` to `apps/accounts/views.py`**

Add `View` to the generic imports if not yet present:

```python
from django.views import View
```

Append after `ProfileView` (or where it fits logically):

```python
class ProfilePasswordChangeView(LoginRequiredMixin, View):
    """Self-only password change endpoint posted from the Profile page."""

    http_method_names = ["post"]

    def post(self, request):
        from django.contrib.auth import update_session_auth_hash

        form = PasswordChangeForm(user=request.user, data=request.POST)
        if form.is_valid():
            user = form.save()
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
            for errors in form.errors.values():
                messages.error(request, "; ".join(errors))
        return redirect("accounts:profile")
```

- [ ] **Step 4: Add URL in `apps/accounts/urls.py`**

Insert after `path("profile/", ...)`:

```python
path(
    "profile/password/",
    views.ProfilePasswordChangeView.as_view(),
    name="password_change",
),
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_password_change.py -v 2>&1 | tail -15`
Expected: All PASS.

- [ ] **Step 6: Full regression**

Run: `uv run pytest tests/ -x --tb=short 2>&1 | tail -5`
Expected: All pass.

- [ ] **Step 7: ruff format + check**

Run: `uv run ruff format apps/accounts/views.py apps/accounts/urls.py tests/test_password_change.py 2>&1 | tail -2`
Run: `uv run ruff check apps/accounts/views.py apps/accounts/urls.py tests/test_password_change.py 2>&1 | tail -3`
Expected: Clean.

- [ ] **Step 8: Commit**

```bash
git add apps/accounts/views.py apps/accounts/urls.py tests/test_password_change.py
git commit -m "feat(accounts): ProfilePasswordChangeView + PASSWORD_CHANGED audit

POST-only endpoint at accounts/profile/password/. Validates the
existing PasswordChangeForm (old_password re-auth, new password
strength via AUTH_PASSWORD_VALIDATORS), then runs
update_session_auth_hash so the user stays logged in. Emits
PASSWORD_CHANGED audit with a constant message — no password
value can leak."
```

---

### Task 9: profile.html rewrite + onboarding-hint CSS

**Files:**
- Modify: `apps/accounts/templates/accounts/profile.html`
- Modify: `static/css/app.css`

> **Subagent:** `pixel`. MUST invoke `Skill("frontend-design")` before any HTML edit.

- [ ] **Step 1: Add the `.onboarding-hint` CSS class to `static/css/app.css`**

Append to `static/css/app.css`:

```css
/* ---------------------------------------------------------------------------
   Onboarding hints (Profile page, 1c) — dezenter Border-Left, mobile-tauglich
   --------------------------------------------------------------------------- */
.onboarding-hint {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin: 12px 0;
  padding: 10px 12px;
  border-left: 3px solid var(--accent-soft, var(--accent));
  background: var(--bg-2);
  border-radius: 0 4px 4px 0;
  font-size: 13px;
  color: var(--ink-1);
}
.onboarding-hint-icon { flex: 0 0 auto; line-height: 1.4; }
.onboarding-hint-text { flex: 1; line-height: 1.4; }
```

- [ ] **Step 2: Rewrite `apps/accounts/templates/accounts/profile.html`**

Replace with:

```django
{% extends "base.html" %}
{% load i18n %}

{% block title %}{% trans "Profile" %} · OE5XRX{% endblock %}

{% block breadcrumbs %}
  <a href="{% url 'dashboard:index' %}">OE5XRX</a>
  <span class="sep">/</span>
  <span class="cur">{% trans "Profile" %}</span>
{% endblock %}

{% block content %}
<div class="page-head">
  <div class="page-head-main">
    <div class="page-eyebrow">{% trans "Your account" %}</div>
    <h1 class="page-title">{% trans "Profile" %}</h1>
    <p class="page-sub">{% trans "Verwalte deine Identität, Profil, Kontaktdaten und Standort." %}</p>
  </div>
</div>

<div class="grid grid-main">
  <div class="stack-gap-14">
    {# === Identity panel === #}
    <form method="post" action="{% url 'accounts:profile' %}">
      {% csrf_token %}
      <input type="hidden" name="form_name" value="identity">
      <section class="panel">
        <div class="panel-head">
          <div class="panel-title"><span class="dot"></span>{% trans "Identity" %}</div>
        </div>
        <div class="panel-body">
          {% if onboarding_hints.name_missing %}
            <div class="onboarding-hint" role="note">
              <span class="onboarding-hint-icon">👤</span>
              <span class="onboarding-hint-text">
                {% trans "Trag deinen Real-Namen ein — andere Mitglieder sehen ihn im Verzeichnis." %}
              </span>
            </div>
          {% endif %}
          <div class="form-group">
            <label class="form-label">{% trans "Email" %}</label>
            {{ identity_form.email }}
            {% if identity_form.email.errors %}<div class="form-error">{{ identity_form.email.errors|join:", " }}</div>{% endif %}
          </div>
          <div class="form-row">
            <div class="form-group">
              <label class="form-label">{% trans "First name" %}</label>
              {{ identity_form.first_name }}
            </div>
            <div class="form-group">
              <label class="form-label">{% trans "Last name" %}</label>
              {{ identity_form.last_name }}
            </div>
          </div>
          <div class="form-group">
            <label class="form-label">{% trans "Language" %}</label>
            {{ identity_form.language }}
          </div>
        </div>
        <div class="panel-foot row-gap-8">
          <button type="submit" class="btn btn-primary">{% trans "Save identity" %}</button>
        </div>
      </section>
    </form>

    {# === Profil panel === #}
    <form method="post" enctype="multipart/form-data" action="{% url 'accounts:profile' %}">
      {% csrf_token %}
      <input type="hidden" name="form_name" value="profile">
      <section class="panel">
        <div class="panel-head">
          <div class="panel-title"><span class="dot"></span>{% trans "Profil" %}</div>
        </div>
        <div class="panel-body">
          {% if onboarding_hints.avatar_missing %}
            <div class="onboarding-hint" role="note">
              <span class="onboarding-hint-icon">📷</span>
              <span class="onboarding-hint-text">{% trans "Lade ein Profilbild hoch." %}</span>
            </div>
          {% endif %}
          {% if onboarding_hints.bio_missing %}
            <div class="onboarding-hint" role="note">
              <span class="onboarding-hint-icon">✍️</span>
              <span class="onboarding-hint-text">{% trans "Stell dich kurz vor (max. 500 Zeichen)." %}</span>
            </div>
          {% endif %}
          {% if onboarding_hints.qth_missing %}
            <div class="onboarding-hint" role="note">
              <span class="onboarding-hint-icon">📍</span>
              <span class="onboarding-hint-text">{% trans "QTH-Name? Das ist dein Funker-Standort-Label." %}</span>
            </div>
          {% endif %}
          <div class="form-group">
            <label class="form-label">{% trans "Avatar" %}</label>
            {{ profile_form.avatar }}
            {% if profile_form.avatar.errors %}<div class="form-error">{{ profile_form.avatar.errors|join:", " }}</div>{% endif %}
          </div>
          <div class="form-group">
            <label class="form-label">{% trans "Bio" %}</label>
            {{ profile_form.bio }}
          </div>
          <div class="form-row">
            <div class="form-group">
              <label class="form-label">QTH</label>
              {{ profile_form.qth_name }}
            </div>
            <div class="form-group">
              <label class="form-label">QRZ-URL</label>
              {{ profile_form.qrz_url }}
            </div>
          </div>
          <div class="form-group">
            <label class="form-label">{% trans "Phone" %}</label>
            {{ profile_form.phone }}
          </div>
          <div class="form-group">
            <label class="form-label">
              {{ profile_form.is_directory_visible }}
              {% trans "Im Mitgliederverzeichnis sichtbar" %}
            </label>
            <div class="form-help">
              {% trans "Wenn aus, sehen andere Mitglieder nur Callsign + Rolle + Avatar." %}
            </div>
          </div>
        </div>
        <div class="panel-foot row-gap-8">
          <button type="submit" class="btn btn-primary">{% trans "Save profile" %}</button>
        </div>
      </section>
    </form>

    {# === Adresse panel === #}
    <form method="post" action="{% url 'accounts:profile' %}">
      {% csrf_token %}
      <input type="hidden" name="form_name" value="address">
      <section class="panel">
        <div class="panel-head">
          <div class="panel-title"><span class="dot"></span>{% trans "Adresse & Standort" %}</div>
        </div>
        <div class="panel-body">
          {% if onboarding_hints.address_missing %}
            <div class="onboarding-hint" role="note">
              <span class="onboarding-hint-icon">🏠</span>
              <span class="onboarding-hint-text">{% trans "Trag deine Adresse ein — Locator und lat/lon werden automatisch berechnet." %}</span>
            </div>
          {% endif %}
          <div class="form-group">
            <label class="form-label">{% trans "Address" %}</label>
            {{ address_form.address }}
            {% if address_form.address.errors %}<div class="form-error">{{ address_form.address.errors|join:", " }}</div>{% endif %}
          </div>
          <p class="t-mono t-muted" style="margin:8px 0;">
            {% trans "Locator + lat/lon werden bei Speichern aus der Adresse berechnet." %}
          </p>
          <div class="form-group">
            <label class="form-label">
              {% trans "Locator" %} ({% trans "manueller Override" %})
            </label>
            {{ address_form.locator }}
            {% if address_form.locator.errors %}<div class="form-error">{{ address_form.locator.errors|join:", " }}</div>{% endif %}
          </div>
        </div>
        <div class="panel-foot row-gap-8">
          <button type="submit" class="btn btn-primary">{% trans "Save address" %}</button>
        </div>
      </section>
    </form>

    {# === Password panel === #}
    <form method="post" action="{% url 'accounts:password_change' %}">
      {% csrf_token %}
      <section class="panel">
        <div class="panel-head">
          <div class="panel-title"><span class="dot"></span>{% trans "Passwort ändern" %}</div>
        </div>
        <div class="panel-body">
          {% for field in password_form %}
            <div class="form-group">
              <label class="form-label">{{ field.label }}</label>
              {{ field }}
              {% if field.errors %}<div class="form-error">{{ field.errors|join:", " }}</div>{% endif %}
              {% if field.help_text %}<div class="form-help">{{ field.help_text|safe }}</div>{% endif %}
            </div>
          {% endfor %}
        </div>
        <div class="panel-foot row-gap-8">
          <button type="submit" class="btn btn-primary">{% trans "Change password" %}</button>
        </div>
      </section>
    </form>
  </div>

  {# === Sidebar === #}
  <aside class="stack-gap-14">
    <section class="panel">
      <div class="panel-head">
        <div class="panel-title"><span class="dot"></span>{% trans "Identity" %}</div>
      </div>
      <div class="panel-body">
        <dl class="dlist">
          <dt>{% trans "Callsign" %}</dt><dd class="t-mono">{{ user.username }}</dd>
          <dt>{% trans "Role" %}</dt><dd>
            {% if user.membership_level == "admin" %}
              <span class="pill pill-accent">{{ user.get_membership_level_display }}</span>
            {% elif user.membership_level == "staff" %}
              <span class="pill pill-violet">{{ user.get_membership_level_display }}</span>
            {% elif user.membership_level == "member" %}
              <span class="pill">{{ user.get_membership_level_display }}</span>
            {% else %}
              <span class="pill pill-muted">{{ user.get_membership_level_display }}</span>
            {% endif %}
          </dd>
          <dt>{% trans "Last login" %}</dt><dd class="t-mono-sm">{{ user.last_login|date:"Y-m-d H:i"|default:"—" }}</dd>
          <dt>{% trans "Joined" %}</dt><dd class="t-mono-sm">{{ user.date_joined|date:"Y-m-d" }}</dd>
        </dl>
      </div>
    </section>

    {% include "sso/_sessions_card.html" with target_user=user sessions=self_sessions readonly_self=True %}
  </aside>
</div>
{% endblock %}
```

- [ ] **Step 3: Run the template-related tests**

Run: `uv run pytest tests/test_profile_view.py tests/test_profile_onboarding.py -v 2>&1 | tail -10`
Expected: PASS.

- [ ] **Step 4: Full regression**

Run: `uv run pytest tests/ -x --tb=short 2>&1 | tail -5`
Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add apps/accounts/templates/accounts/profile.html static/css/app.css
git commit -m "feat(accounts): profile.html 4-panel layout + onboarding-hint CSS

Profile-Page is now 4 panels (Identity / Profil / Adresse / Passwort)
in a grid-main with a sidebar (Identity-dlist + Self-Sessions card
in readonly_self mode). Each form has its own panel with its own
[Save] button — independent saves avoid the 'one geocode failure
kills the whole submit' trap.

Onboarding hints render conditionally per empty field with a
dezent left-border-accent box (.onboarding-hint CSS class, new in
app.css)."
```

---

### Task 10: UserDeleteView impact view + USER_DELETED audit

**Files:**
- Modify: `apps/accounts/views.py` (UserDeleteView)
- Modify: `apps/accounts/templates/accounts/user_confirm_delete.html`
- Create: `tests/test_user_delete_view.py`

- [ ] **Step 1: Write failing tests**

Create NEW file `tests/test_user_delete_view.py`:

```python
"""UserDeleteView Impact-Anzeige + USER_DELETED-Audit + Self-Block.

Sub-Spec 1c Sektion 7.
"""

import pytest
from django.urls import reverse

from apps.accounts.models import AccountAuditLog, User
from apps.stations.models import Region, RegionAssignment, Station, StationAssignment


@pytest.fixture
def admin(db):
    return User.objects.create_user(
        username="OE5ADMIN",
        password="x",
        membership_level=User.MembershipLevel.ADMIN,
    )


@pytest.fixture
def member(db):
    return User.objects.create_user(
        username="OE5MEM1",
        password="x",
        email="m@example.org",
        membership_level=User.MembershipLevel.MEMBER,
    )


@pytest.fixture
def region(db):
    return Region.objects.create(name="Innviertel")


@pytest.fixture
def station(db, region):
    return Station.objects.create(name="OE5XRX-Test", callsign="OE5XRX", region=region)


@pytest.mark.django_db
class TestDeleteImpactContext:
    def test_no_assignments_zero_counts(self, client, admin, member):
        client.force_login(admin)
        resp = client.get(reverse("accounts:user_delete", kwargs={"pk": member.pk}))
        ctx = resp.context
        assert ctx["n_station_assignments"] == 0
        assert ctx["n_region_assignments"] == 0
        assert ctx["station_admin_assignments"] == []

    def test_counts_reflect_assignments(
        self, client, admin, member, region, station
    ):
        RegionAssignment.objects.create(
            user=member, region=region, role=RegionAssignment.Role.MANAGER, assigned_by=admin
        )
        StationAssignment.objects.create(
            user=member,
            station=station,
            role=StationAssignment.Role.MAINTAINER,
            assigned_by=admin,
        )
        client.force_login(admin)
        resp = client.get(reverse("accounts:user_delete", kwargs={"pk": member.pk}))
        ctx = resp.context
        assert ctx["n_station_assignments"] == 1
        assert ctx["n_region_assignments"] == 1

    def test_station_admin_warning_list(self, client, admin, member, station):
        StationAssignment.objects.create(
            user=member, station=station, role=StationAssignment.Role.ADMIN, assigned_by=admin
        )
        client.force_login(admin)
        resp = client.get(reverse("accounts:user_delete", kwargs={"pk": member.pk}))
        ctx = resp.context
        admin_list = ctx["station_admin_assignments"]
        assert len(admin_list) == 1
        assert admin_list[0].station == station


@pytest.mark.django_db
class TestDeleteAuditAndCascade:
    def test_delete_emits_user_deleted_audit(self, client, admin, member):
        client.force_login(admin)
        before = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.USER_DELETED
        ).count()
        client.post(reverse("accounts:user_delete", kwargs={"pk": member.pk}))
        after = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.USER_DELETED
        ).count()
        assert after == before + 1
        # Username appears in message even though target_user gets SET_NULL
        # after cascade.
        entry = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.USER_DELETED
        ).latest("created_at")
        assert "OE5MEM1" in entry.message
        assert "m@example.org" in entry.message
        # actor stays admin (admin still exists)
        assert entry.actor == admin

    def test_self_delete_blocked(self, client, admin):
        client.force_login(admin)
        resp = client.post(reverse("accounts:user_delete", kwargs={"pk": admin.pk}))
        # Redirect, but user still exists
        assert resp.status_code == 302
        assert User.objects.filter(pk=admin.pk).exists()
        # No USER_DELETED audit for self
        assert (
            AccountAuditLog.objects.filter(
                event_type=AccountAuditLog.EventType.USER_DELETED, target_user=admin
            ).count()
            == 0
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_user_delete_view.py -v 2>&1 | tail -20`
Expected: Tests fail — current `UserDeleteView` doesn't load impact context and doesn't emit USER_DELETED.

- [ ] **Step 3: Extend `UserDeleteView` in `apps/accounts/views.py`**

Replace `UserDeleteView` with:

```python
class UserDeleteView(AdminRequiredMixin, DeleteView):
    model = User
    template_name = "accounts/user_confirm_delete.html"
    success_url = reverse_lazy("accounts:user_list")
    context_object_name = "target_user"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.object
        ctx["n_station_assignments"] = user.station_assignments.count()
        ctx["n_region_assignments"] = user.region_assignments.count()
        ctx["station_admin_assignments"] = list(
            user.station_assignments.filter(
                role=StationAssignment.Role.ADMIN
            ).select_related("station")
        )
        ctx["n_sso_grants"] = (
            user.app_grants.count() if hasattr(user, "app_grants") else 0
        )
        ctx["n_active_sessions"] = (
            user.token_sessions.filter(revoked_at__isnull=True).count()
            if hasattr(user, "token_sessions")
            else 0
        )
        ctx["n_group_memberships"] = user.groups.count()
        return ctx

    def form_valid(self, form):
        if self.get_object() == self.request.user:
            messages.error(
                self.request, _("You cannot delete your own account.")
            )
            return redirect(self.success_url)
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.USER_DELETED,
            actor=self.request.user,
            target_user=self.object,  # noch da, wird durch Cascade SET_NULL
            message=f"{self.object.username} <{self.object.email}>",
            ip_address=_get_client_ip(self.request),
        )
        messages.success(self.request, _("User deleted successfully."))
        return super().form_valid(form)
```

Add the `StationAssignment` import at the top:

```python
from apps.stations.models import StationAssignment
```

- [ ] **Step 4: Rewrite `apps/accounts/templates/accounts/user_confirm_delete.html`**

> **Subagent for this step:** `pixel`, MUST invoke `Skill("frontend-design")`. The rest of the task is backend-only and can be the same general-purpose subagent.

Replace `apps/accounts/templates/accounts/user_confirm_delete.html`:

```django
{% extends "base.html" %}
{% load i18n %}

{% block title %}{% trans "Delete user" %}{% endblock %}

{% block breadcrumbs %}
  <a href="{% url 'accounts:user_list' %}">{% trans "Users" %}</a>
  <span class="sep">/</span>
  <span class="cur">{% trans "Delete" %}</span>
{% endblock %}

{% block content %}
<div class="page-head"><div class="page-head-main">
  <div class="page-eyebrow t-danger">{% trans "Danger zone" %}</div>
  <h1 class="page-title">{% trans "Delete user" %}</h1>
  <p class="page-sub">{% blocktrans with username=target_user.username %}Delete user "{{ username }}"? This cannot be undone.{% endblocktrans %}</p>
</div></div>

<form method="post" class="panel" style="border-left:3px solid var(--danger);">
  {% csrf_token %}
  <div class="panel-body">
    <dl class="dlist">
      <dt>{% trans "Username" %}</dt><dd class="t-mono">{{ target_user.username }}</dd>
      <dt>{% trans "Email" %}</dt><dd class="t-mono">{{ target_user.email|default:"—" }}</dd>
      <dt>{% trans "Role" %}</dt><dd>
        {% if target_user.membership_level == "admin" %}
          <span class="pill pill-accent">{{ target_user.get_membership_level_display }}</span>
        {% elif target_user.membership_level == "staff" %}
          <span class="pill pill-violet">{{ target_user.get_membership_level_display }}</span>
        {% elif target_user.membership_level == "member" %}
          <span class="pill">{{ target_user.get_membership_level_display }}</span>
        {% else %}
          <span class="pill pill-muted">{{ target_user.get_membership_level_display }}</span>
        {% endif %}
      </dd>
      <dt>{% trans "Joined" %}</dt><dd class="t-mono-sm">{{ target_user.date_joined|date:"Y-m-d" }}</dd>
    </dl>

    <hr style="margin:14px 0;border:0;border-top:1px solid var(--line);">

    <p class="t-label" style="margin-bottom:8px;">{% trans "Mit dem User werden gelöscht:" %}</p>
    <dl class="dlist">
      <dt>{% trans "Station-Assignments" %}</dt>
      <dd class="t-mono">{{ n_station_assignments }} <span class="t-muted">— {% trans "werden revoked, Audit emitted" %}</span></dd>
      <dt>{% trans "Region-Assignments" %}</dt>
      <dd class="t-mono">{{ n_region_assignments }} <span class="t-muted">— {% trans "werden revoked, Audit emitted" %}</span></dd>
      <dt>{% trans "SSO Grants" %}</dt>
      <dd class="t-mono">{{ n_sso_grants }} <span class="t-muted">— {% trans "werden revoked, Tokens invalidated" %}</span></dd>
      <dt>{% trans "Active SSO Sessions" %}</dt>
      <dd class="t-mono">{{ n_active_sessions }} <span class="t-muted">— {% trans "werden terminated" %}</span></dd>
      <dt>{% trans "Group Memberships" %}</dt>
      <dd class="t-mono">{{ n_group_memberships }} <span class="t-muted">— {% trans "werden entfernt" %}</span></dd>
    </dl>

    {% if station_admin_assignments %}
      <div class="onboarding-hint" role="alert" style="border-left-color:var(--danger);margin-top:14px;">
        <span class="onboarding-hint-icon">⚠️</span>
        <span class="onboarding-hint-text">
          {% trans "Achtung: User ist Station-Admin auf folgenden Stationen — die Stationen verlieren ihren Admin:" %}
          <ul style="margin:4px 0 0 0;padding-left:20px;">
            {% for sa in station_admin_assignments %}
              <li>{{ sa.station.callsign|default:sa.station.name }}</li>
            {% endfor %}
          </ul>
        </span>
      </div>
    {% endif %}
  </div>
  <div class="panel-foot row-gap-8">
    <button type="submit" class="btn btn-danger" data-confirm="{% trans 'Delete this user?' %}">{% trans "Delete user" %}</button>
    <a href="{% url 'accounts:user_detail' target_user.pk %}" class="btn btn-ghost">{% trans "Cancel" %}</a>
  </div>
</form>
{% endblock %}
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_user_delete_view.py -v 2>&1 | tail -15`
Expected: All PASS.

- [ ] **Step 6: Full regression**

Run: `uv run pytest tests/ -x --tb=short 2>&1 | tail -5`
Expected: All pass.

- [ ] **Step 7: ruff format + check**

Run: `uv run ruff format apps/accounts/views.py tests/test_user_delete_view.py 2>&1 | tail -2`
Run: `uv run ruff check apps/accounts/views.py tests/test_user_delete_view.py 2>&1 | tail -3`
Expected: Clean.

- [ ] **Step 8: Commit**

```bash
git add apps/accounts/views.py \
        apps/accounts/templates/accounts/user_confirm_delete.html \
        tests/test_user_delete_view.py
git commit -m "feat(accounts): UserDeleteView impact view + USER_DELETED audit

Confirm-page now shows the cascade impact: counts for Station/Region
assignments, SSO grants/sessions, Group memberships, plus a warning
listing the Stations that would lose their Admin if the user is a
Station-Admin. USER_DELETED audit is emitted BEFORE super().form_valid
so the username/email survive the FK SET_NULL cascade in the message
field. Self-delete is blocked with a flash message and no audit."
```

---

### Task 11: Final integration verify

**Files:**
- Read only

- [ ] **Step 1: Run the entire test suite**

Run: `cd /home/pbuchegger/OE5XRX/station-manager/.worktrees/feat-user-domain-1c-self-service && uv run pytest tests/ --tb=short 2>&1 | tail -5`
Expected: All tests pass (~792 baseline + ~60 new tests).

- [ ] **Step 2: Django system check**

Run: `uv run python manage.py check 2>&1 | tail -5`
Expected: `System check identified no issues (0 silenced).`

- [ ] **Step 3: Migrations clean**

Run: `uv run python manage.py makemigrations --check --dry-run 2>&1 | tail -5`
Expected: no pending migrations for `accounts`.

- [ ] **Step 4: ruff check the whole tree**

Run: `uv run ruff check apps/ tests/ 2>&1 | tail -3`
Run: `uv run ruff format --check apps/ tests/ 2>&1 | tail -3`
Expected: Clean.

- [ ] **Step 5: Branch summary**

Run: `git log --oneline origin/main..HEAD 2>&1 | head -15`
Expected: ~10 commits since main, one per task.

---

## Summary

After this plan executes, the branch `feat/user-domain-1c-self-service` delivers:

- `UserChangeForm` carries all 14 fields (was 6) including validators + avatar resize on save.
- `UserUpdateView` + `UserCreateView` form_valid emit USER_UPDATED / USER_CREATED audits and trigger geocoding when address changes.
- New `ProfileIdentityForm`, `ProfileProfileForm`, `ProfileAddressForm`, `PasswordChangeForm`.
- `ProfileView` becomes a multi-form TemplateView with form_name-dispatch; each save emits a self-edit USER_UPDATED audit, address saves trigger geocoding.
- `ProfilePasswordChangeView` at `accounts/profile/password/` with re-auth + session-hash-update + PASSWORD_CHANGED audit.
- `UserDeleteView` shows impact (counts + Station-Admin warning) and emits USER_DELETED before cascade.
- `profile.html` is 4-panels + sidebar, with onboarding hints per empty field.
- `user_form.html` is 3 mobile-friendly panels (Identity / Profil / Adresse) + aside.
- `user_confirm_delete.html` shows the impact in a danger-bordered panel.
- New `.onboarding-hint` CSS class in `app.css`.

Test count grows by ~60 new tests across 7 new files. All HTMX endpoints from 1a/1b stay unchanged.

This completes the User-Domain-Redesign arc. The follow-up is Spec #2 "Account Lifecycle" (welcome email, password reset, soft-delete) — see Overview Sektion 6.
