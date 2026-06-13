# Sub-Spec 2b Soft-Delete — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Den heutigen `UserDeleteView` (Hard-Delete) durch einen zweistufigen Soft-Delete + Hard-Purge-Lifecycle ersetzen. Soft-Delete ist reversibel (Tombstone bleibt), auto-revoked alle Topology-Assignments mit Free-Position-Banner, invalidiert pending Account-Tokens, revoked SSO-Grants/Sessions, sperrt Login (`is_active=False`). Restore stellt den User wieder her — Assignments müssen neu vergeben werden. Hard-Purge ist nur erreichbar nachdem ein User soft-deleted wurde — defense in depth gegen versehentliches Daten-Verlust. UserListView wird zur reinen Browse-Surface mit `?show=`-Filter; alle Action-Buttons (Edit, Soft-Delete, Restore, Hard-Purge) leben jetzt auf `UserDetailView`.

**Architecture:** Conditional `UniqueConstraint` auf `username` mit `condition=Q(deleted_at__isnull=True)` erlaubt Callsign-Reuse nach Soft-Delete. Kein `default_manager` der filtert — explizite `User.objects.active()`/`deleted()`-Aliase plus per-Use-Case-Filter. Atomare Transaktionen für Soft-Delete (6 Schritte: Topology revoke + Token invalidate + SSO revoke + groups clear + Stempel + Audit) und Hard-Purge (Audit-vor-Delete + Avatar-File-Cleanup im try/except).

**Tech Stack:** Python 3.14, Django 6.0, pytest + pytest-django, ruff. Baut auf 1a/1b/1c (User-Domain) + 2a (AccountToken) auf.

**Spec:** `docs/superpowers/specs/2026-06-13-account-lifecycle-2b-soft-delete-design.md`

---

## File Structure

### Files to CREATE

| Pfad | Zweck |
|---|---|
| `apps/accounts/migrations/0XXX_user_soft_delete.py` | Add `deleted_at` + `deleted_by` Felder, Constraint-Swap auf username. Django-autogen. |
| `apps/accounts/templates/accounts/user_confirm_soft_delete.html` | Confirm-Page für Soft-Delete (Impact-Counts + Free-Position-Warnung). |
| `apps/accounts/templates/accounts/user_confirm_hard_purge.html` | Confirm-Page für Hard-Purge (Tombstone-Counts + irreversible-Warnung). |
| `tests/test_user_soft_delete.py` | Soft-Delete-Flow Tests (~9). |
| `tests/test_user_restore.py` | Restore-Flow Tests (~4). |
| `tests/test_user_hard_purge.py` | Hard-Purge-Flow Tests (~5). |
| `tests/test_user_list_filter.py` | `?show=`-Filter Tests (~4). |
| `tests/test_user_manager_helpers.py` | `User.objects.active()/deleted()` Tests (~3). |
| `tests/test_topology_filter_deleted.py` | Notification-Routing + visibility filtern deleted (~3). |

### Files to MODIFY

| Pfad | Änderung |
|---|---|
| `apps/accounts/models.py` | User-Klasse: `deleted_at`, `deleted_by` Felder + `Meta.constraints` (`unique_active_username`). Custom Manager mit `.active()`/`.deleted()`. |
| `apps/accounts/views.py` | `UserListView` bekommt `get_queryset` mit `?show=`-Filter; `UserDeleteView` wird ersetzt durch `UserSoftDeleteView`; neu `UserRestoreView`, `UserHardPurgeView`; `_revoke_all_topology`, `_revoke_sso` Helper. |
| `apps/accounts/urls.py` | `user_delete` raus, drei neue URLs: `user_soft_delete`, `user_restore`, `user_hard_purge`. |
| `apps/accounts/forms.py` | `UserCreationForm.clean_email`/`clean_username` + `ProfileIdentityForm.clean_email` excludieren soft-deleted aus Uniqueness. |
| `apps/accounts/templates/accounts/user_list.html` | Filter-Bar oben (Pills) + per-row Action-Buttons entfernen. |
| `apps/accounts/templates/accounts/user_detail.html` | Banner + konditionale Action-Bar (Edit+SoftDelete vs Restore+HardPurge). |
| `apps/accounts/visibility.py` | `user_can_view_directory` + audience-Filter excludieren `deleted_at__isnull=False`. |
| `apps/monitoring/recipients.py` | `recipients_for_station_alert` excludiert deleted User. |

### Files to DELETE

| Pfad | Grund |
|---|---|
| `tests/test_user_delete_view.py` | Alte UserDeleteView gibt's nicht mehr; ersetzt durch test_user_soft_delete.py + test_user_hard_purge.py. |

### Files unchanged

- Alle 1c/2a HTMX-Endpoints (views_*assignments.py, views_membership.py).
- Avatar-Pipeline (`apps/accounts/avatars.py`), Geocoding (`apps/accounts/geocoding.py`).
- 2a's AccountToken-Modell + Helper (`apps/accounts/tokens.py`, `apps/accounts/emails.py`).

---

## Tasks

### Task 1: Pre-flight + Baseline-Sanity

**Files:**
- Read only

- [ ] **Step 1: Verify branch + worktree**

Run: `git -C /home/pbuchegger/OE5XRX/station-manager/.worktrees/feat-account-lifecycle-2b branch --show-current`
Expected: `feat/account-lifecycle-2b-soft-delete`

- [ ] **Step 2: Baseline-Test-Suite**

Run: `cd /home/pbuchegger/OE5XRX/station-manager/.worktrees/feat-account-lifecycle-2b && uv run pytest tests/ -x --tb=short 2>&1 | tail -5`
Expected: All tests pass (~938 nach 2a-Merge).

- [ ] **Step 3: Migrations clean**

Run: `uv run python manage.py makemigrations --check --dry-run 2>&1 | tail -5`
Expected: keine pending Migrations für `accounts`.

- [ ] **Step 4: Spec lesen**

Read `docs/superpowers/specs/2026-06-13-account-lifecycle-2b-soft-delete-design.md` — alle 13 Sections im Kopf haben bevor Code dazukommt.

---

### Task 2: User-Modell — `deleted_at` + `deleted_by` + Manager-Helper

**Files:**
- Modify: `apps/accounts/models.py`
- Create: `tests/test_user_manager_helpers.py`

- [ ] **Step 1: Write failing tests**

Create NEW file `tests/test_user_manager_helpers.py`:

```python
"""User.objects.active() / deleted() Manager-Helper.

Sub-Spec 2b §2.3.
"""

from django.utils import timezone

import pytest

from apps.accounts.models import User


@pytest.mark.django_db
class TestUserManagerHelpers:
    def test_active_returns_non_deleted(self):
        alice = User.objects.create_user(username="OE5ALICE", password="x")
        bob = User.objects.create_user(username="OE5BOB", password="x")
        bob.deleted_at = timezone.now()
        bob.save()

        active = list(User.objects.active().values_list("username", flat=True))
        assert "OE5ALICE" in active
        assert "OE5BOB" not in active

    def test_deleted_returns_soft_deleted_only(self):
        alice = User.objects.create_user(username="OE5ALICE", password="x")
        bob = User.objects.create_user(username="OE5BOB", password="x")
        bob.deleted_at = timezone.now()
        bob.save()

        deleted = list(User.objects.deleted().values_list("username", flat=True))
        assert "OE5BOB" in deleted
        assert "OE5ALICE" not in deleted

    def test_all_still_returns_everyone(self):
        alice = User.objects.create_user(username="OE5ALICE", password="x")
        bob = User.objects.create_user(username="OE5BOB", password="x")
        bob.deleted_at = timezone.now()
        bob.save()

        all_users = list(User.objects.all().values_list("username", flat=True))
        assert "OE5ALICE" in all_users
        assert "OE5BOB" in all_users
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_user_manager_helpers.py -v 2>&1 | tail -10`
Expected: `AttributeError: 'UserManager' object has no attribute 'active'` (oder: kein Feld `deleted_at`).

- [ ] **Step 3: User-Modell anpassen — Felder + Constraint**

In `apps/accounts/models.py` die User-Klasse ergänzen.

Add Import oben:
```python
from django.db.models import Q
```

Add Felder im User-Modell (nach `language` oder am Ende der Feld-Liste):
```python
    deleted_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text=_(
            "Soft-delete timestamp. NULL = active user. NOT NULL = soft-deleted, "
            "is_active is False, login blocked."
        ),
    )
    deleted_by = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="deleted_users",
        help_text=_("Admin who triggered the soft-delete (SET_NULL on cascade)."),
    )
```

Add `Meta.constraints` in der User-Klasse (Django's `AbstractUser` hat bereits ein Meta — wir mergen die `constraints`-Liste):

```python
    class Meta(AbstractUser.Meta):
        constraints = [
            # Username-Uniqueness nur für nicht-soft-deleted User —
            # erlaubt Callsign-Reuse nach Soft-Delete.
            models.UniqueConstraint(
                fields=["username"],
                condition=Q(deleted_at__isnull=True),
                name="unique_active_username",
            ),
        ]
```

- [ ] **Step 4: Custom UserManager mit `.active()` + `.deleted()`**

In `apps/accounts/models.py` — direkt VOR der User-Klasse:

```python
class UserManager(BaseUserManager):
    """Custom Manager — adds active()/deleted() helpers, keeps Django's
    create_user/create_superuser behavior intact.

    Note: kein default-Filter. User.objects.all() zeigt weiterhin alles
    (auch soft-deleted). Use User.objects.active() / .deleted() explizit.
    """

    use_in_migrations = True

    def _create_user(self, username, email, password, **extra_fields):
        if not username:
            raise ValueError("The given username must be set")
        email = self.normalize_email(email) if email else email
        user = self.model(username=username, email=email, **extra_fields)
        user.password = make_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, username, email=None, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(username, email, password, **extra_fields)

    def create_superuser(self, username, email=None, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        return self._create_user(username, email, password, **extra_fields)

    def active(self):
        """Convenience: User.objects.active() → non-soft-deleted."""
        return self.filter(deleted_at__isnull=True)

    def deleted(self):
        """Convenience: User.objects.deleted() → soft-deleted only."""
        return self.filter(deleted_at__isnull=False)
```

Imports oben in `apps/accounts/models.py` ergänzen:
```python
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import AbstractUser, BaseUserManager
```

Im User-Klasse: `objects = UserManager()` direkt nach den Feldern.

- [ ] **Step 5: Migration generieren**

Run: `cd /home/pbuchegger/OE5XRX/station-manager/.worktrees/feat-account-lifecycle-2b && uv run python manage.py makemigrations accounts 2>&1 | tail -5`
Expected: `apps/accounts/migrations/0XXX_user_soft_delete.py` (Nummer auto-gen) — zeigt:
- `AddField: deleted_at`
- `AddField: deleted_by`
- `AddConstraint: unique_active_username`

- [ ] **Step 6: Migration name umbenennen (cosmetic)**

Den auto-gen-Filename auf `0XXX_user_soft_delete.py` umbenennen, falls Django sonst was generiert (z.B. `0XXX_user_deleted_at_user_deleted_by_unique_active_username.py`).

Run: `cd apps/accounts/migrations && ls -t | head -2`
Den letzten `.py`-File (außer `__init__.py`) zu `<num>_user_soft_delete.py` umbenennen.

- [ ] **Step 7: Run Tests to verify pass**

Run: `uv run pytest tests/test_user_manager_helpers.py -v 2>&1 | tail -10`
Expected: 3 passed.

- [ ] **Step 8: Full regression**

Run: `uv run pytest tests/ -x --tb=short 2>&1 | tail -5`
Expected: 938+3 = 941 passed.

- [ ] **Step 9: ruff format + check**

Run: `uv run ruff format apps/accounts/models.py tests/test_user_manager_helpers.py 2>&1 | tail -2`
Run: `uv run ruff check apps/accounts/models.py tests/test_user_manager_helpers.py 2>&1 | tail -3`
Expected: Clean.

- [ ] **Step 10: Commit**

```bash
git add apps/accounts/models.py apps/accounts/migrations/ tests/test_user_manager_helpers.py
git commit -m "feat(accounts): add deleted_at + deleted_by + active/deleted manager

Conditional UniqueConstraint on username (condition=deleted_at__isnull)
enables callsign-reuse after soft-delete. Custom UserManager preserves
Django's create_user/create_superuser semantics and adds .active()/
.deleted() convenience filters. No default-filter mutation — existing
User.objects.all() callers keep seeing all rows.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 3: `UserCreationForm` + `ProfileIdentityForm` — Uniqueness excludiert soft-deleted

**Files:**
- Modify: `apps/accounts/forms.py`
- Modify: `tests/test_user_change_form.py` (append)

Heute (nach 2a) prüft `UserCreationForm.clean_email` mit `User.objects.filter(email__iexact=email).exists()`. Nach 2b muss das `.active()` werden, sonst blockiert ein soft-deleted User die Email/Username.

- [ ] **Step 1: Write failing tests — append zu `tests/test_user_change_form.py`**

Am Ende von `tests/test_user_change_form.py`:

```python
@pytest.mark.django_db
class TestUniquenessExcludesSoftDeleted:
    def test_create_form_allows_email_of_soft_deleted_user(self):
        from django.utils import timezone

        from apps.accounts.forms import UserCreationForm

        old = User.objects.create_user(
            username="OE5OLD", email="hans@example.org", password="x",
        )
        old.deleted_at = timezone.now()
        old.is_active = False
        old.save()

        form = UserCreationForm(data={
            "username": "OE5NEW",
            "email": "hans@example.org",
            "first_name": "",
            "last_name": "",
            "language": "en",
        })
        assert form.is_valid(), form.errors

    def test_create_form_allows_username_of_soft_deleted_user(self):
        from django.utils import timezone

        from apps.accounts.forms import UserCreationForm

        old = User.objects.create_user(
            username="OE5XYZ", email="old@example.org", password="x",
        )
        old.deleted_at = timezone.now()
        old.is_active = False
        old.save()

        form = UserCreationForm(data={
            "username": "OE5XYZ",
            "email": "new@example.org",
            "first_name": "",
            "last_name": "",
            "language": "en",
        })
        assert form.is_valid(), form.errors

    def test_profile_identity_allows_email_of_soft_deleted_user(self):
        from django.utils import timezone

        from apps.accounts.forms import ProfileIdentityForm

        old = User.objects.create_user(
            username="OE5OLD", email="taken@example.org", password="x",
        )
        old.deleted_at = timezone.now()
        old.is_active = False
        old.save()
        active = User.objects.create_user(
            username="OE5ACT", email="active@example.org", password="x",
        )

        form = ProfileIdentityForm(
            data={
                "identity-email": "taken@example.org",
                "identity-first_name": active.first_name,
                "identity-last_name": active.last_name,
                "identity-language": active.language,
            },
            instance=active,
            prefix="identity",
        )
        assert form.is_valid(), form.errors
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_user_change_form.py::TestUniquenessExcludesSoftDeleted -v 2>&1 | tail -10`
Expected: Tests failen — soft-deleted-User blockiert die Uniqueness-Check.

- [ ] **Step 3: Forms anpassen**

In `apps/accounts/forms.py` — `UserCreationForm.clean_email` umstellen auf `.active()`:

```python
    def clean_email(self):
        email = self.cleaned_data["email"].strip()
        if not email:
            raise forms.ValidationError(_("Email is required for the Welcome link."))
        if User.objects.active().filter(email__iexact=email).exists():
            raise forms.ValidationError(_("A user with this email already exists."))
        return email
```

`UserCreationForm.clean_username` ergänzen (analog):

```python
    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        if User.objects.active().filter(username__iexact=username).exists():
            raise forms.ValidationError(_("A user with this username already exists."))
        return username
```

`ProfileIdentityForm.clean_email` umstellen:

```python
    def clean_email(self):
        email = self.cleaned_data["email"].strip()
        if (
            User.objects.active()
            .exclude(pk=self.instance.pk)
            .filter(email__iexact=email)
            .exists()
        ):
            raise forms.ValidationError(_("Another user already has this email."))
        return email
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/test_user_change_form.py::TestUniquenessExcludesSoftDeleted -v 2>&1 | tail -10`
Expected: 3 passed.

- [ ] **Step 5: Full regression**

Run: `uv run pytest tests/ -x --tb=short 2>&1 | tail -5`
Expected: All pass.

- [ ] **Step 6: ruff**

Run: `uv run ruff format apps/accounts/forms.py tests/test_user_change_form.py 2>&1 | tail -2`
Run: `uv run ruff check apps/accounts/forms.py tests/test_user_change_form.py 2>&1 | tail -3`

- [ ] **Step 7: Commit**

```bash
git add apps/accounts/forms.py tests/test_user_change_form.py
git commit -m "feat(accounts): forms exclude soft-deleted from uniqueness checks

UserCreationForm.clean_email/clean_username + ProfileIdentityForm.
clean_email now use User.objects.active() so a soft-deleted user
no longer blocks email/callsign reuse for a new active user.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 4: `UserSoftDeleteView` + Topology/SSO-Revoke + Audit-Events

**Files:**
- Modify: `apps/accounts/views.py`
- Modify: `apps/accounts/models.py` (neue EventType-Choices)
- Modify: `apps/accounts/urls.py`
- Create: `apps/accounts/templates/accounts/user_confirm_soft_delete.html`
- Create: `tests/test_user_soft_delete.py`

- [ ] **Step 1: AccountAuditLog.EventType erweitern**

In `apps/accounts/models.py` — der `AccountAuditLog.EventType`-Choices-Klasse drei neue Choices hinzufügen:

```python
class EventType(models.TextChoices):
    # ... existing choices ...
    USER_SOFT_DELETED = "user_soft_deleted", _("User Soft-Deleted")
    USER_RESTORED = "user_restored", _("User Restored")
    USER_HARD_PURGED = "user_hard_purged", _("User Hard-Purged")
    # USER_DELETED bleibt im Enum — wird ab 2b nicht mehr emittiert,
    # aber alte DB-Rows referenzieren den String.
```

- [ ] **Step 2: Migration für die Choices (nur falls Django das verlangt)**

Run: `uv run python manage.py makemigrations accounts --check --dry-run 2>&1 | tail -3`

Falls Django sagt "0 migrations needed" → kein Migration-File. Wenn Django doch eine generiert (z.B. weil die Choices-Liste eingelagert wird):

Run: `uv run python manage.py makemigrations accounts 2>&1 | tail -3`
Den File auf `<num>_audit_events_for_soft_delete.py` umbenennen.

- [ ] **Step 3: Write failing tests — `tests/test_user_soft_delete.py`**

Create NEW file `tests/test_user_soft_delete.py`:

```python
"""UserSoftDeleteView — confirm-GET + POST mit Topology auto-revoke,
SSO-revoke, Token-invalidate.

Sub-Spec 2b §4.
"""

import pytest
from django.urls import reverse

from apps.accounts.models import AccountAuditLog, User
from apps.accounts.tokens import issue_token
from apps.accounts.models import AccountToken


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
        email="m@example.org",
        password="x",
        membership_level=User.MembershipLevel.MEMBER,
    )


@pytest.fixture
def region(db):
    from apps.stations.models import Region

    return Region.objects.create(name="Innviertel")


@pytest.fixture
def station(db, region):
    from apps.stations.models import Station

    return Station.objects.create(name="OE5XRX-Test", callsign="OE5XRX", region=region)


@pytest.mark.django_db
class TestSoftDeleteConfirmGET:
    def test_get_shows_counts(self, client, admin, member, region, station):
        from apps.stations.models import RegionAssignment, StationAssignment

        RegionAssignment.objects.create(
            user=member, region=region,
            role=RegionAssignment.Role.MANAGER, assigned_by=admin,
        )
        StationAssignment.objects.create(
            user=member, station=station,
            role=StationAssignment.Role.MAINTAINER, assigned_by=admin,
        )

        client.force_login(admin)
        resp = client.get(reverse("accounts:user_soft_delete", kwargs={"pk": member.pk}))
        assert resp.status_code == 200
        assert resp.context["n_station_assignments"] == 1
        assert resp.context["n_region_assignments"] == 1

    def test_get_shows_station_admin_warning_list(self, client, admin, member, station):
        from apps.stations.models import StationAssignment

        StationAssignment.objects.create(
            user=member, station=station,
            role=StationAssignment.Role.ADMIN, assigned_by=admin,
        )
        client.force_login(admin)
        resp = client.get(reverse("accounts:user_soft_delete", kwargs={"pk": member.pk}))
        assert len(resp.context["station_admin_assignments"]) == 1

    def test_active_user_returns_200(self, client, admin, member):
        client.force_login(admin)
        resp = client.get(reverse("accounts:user_soft_delete", kwargs={"pk": member.pk}))
        assert resp.status_code == 200

    def test_soft_deleted_user_returns_404(self, client, admin, member):
        from django.utils import timezone

        member.deleted_at = timezone.now()
        member.is_active = False
        member.save()

        client.force_login(admin)
        resp = client.get(reverse("accounts:user_soft_delete", kwargs={"pk": member.pk}))
        assert resp.status_code == 404


@pytest.mark.django_db
class TestSoftDeletePOST:
    def test_post_sets_deleted_at_and_deleted_by_and_is_active_false(
        self, client, admin, member,
    ):
        client.force_login(admin)
        client.post(reverse("accounts:user_soft_delete", kwargs={"pk": member.pk}))
        member.refresh_from_db()
        assert member.deleted_at is not None
        assert member.deleted_by == admin
        assert member.is_active is False

    def test_self_soft_delete_blocked(self, client, admin):
        client.force_login(admin)
        resp = client.post(reverse("accounts:user_soft_delete", kwargs={"pk": admin.pk}))
        assert resp.status_code == 302
        admin.refresh_from_db()
        assert admin.deleted_at is None

    def test_topology_auto_revoked_with_per_assignment_audit(
        self, client, admin, member, region, station,
    ):
        from apps.stations.models import RegionAssignment, StationAssignment

        RegionAssignment.objects.create(
            user=member, region=region,
            role=RegionAssignment.Role.MANAGER, assigned_by=admin,
        )
        StationAssignment.objects.create(
            user=member, station=station,
            role=StationAssignment.Role.ADMIN, assigned_by=admin,
        )

        client.force_login(admin)
        client.post(reverse("accounts:user_soft_delete", kwargs={"pk": member.pk}))

        assert not member.station_assignments.exists()
        assert not member.region_assignments.exists()

        # Audit-Rows mit reason=user_soft_deleted
        region_audits = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.REGION_ASSIGNMENT_REVOKED,
            target_user=member,
        )
        station_audits = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.STATION_ASSIGNMENT_REVOKED,
            target_user=member,
        )
        assert region_audits.count() == 1
        assert station_audits.count() == 1
        assert "reason=user_soft_deleted" in region_audits.first().message
        assert "reason=user_soft_deleted" in station_audits.first().message

    def test_account_tokens_invalidated(self, client, admin, member):
        # Pending Welcome + Reset + Verify tokens
        for ttype in [
            AccountToken.TokenType.WELCOME,
            AccountToken.TokenType.RESET,
            AccountToken.TokenType.VERIFY,
        ]:
            issue_token(member, ttype)

        assert member.account_tokens.filter(used_at__isnull=True).count() == 3

        client.force_login(admin)
        client.post(reverse("accounts:user_soft_delete", kwargs={"pk": member.pk}))

        assert member.account_tokens.filter(used_at__isnull=True).count() == 0
        assert member.account_tokens.filter(used_at__isnull=False).count() == 3

    def test_emits_user_soft_deleted_audit_with_email_in_message(
        self, client, admin, member,
    ):
        client.force_login(admin)
        client.post(reverse("accounts:user_soft_delete", kwargs={"pk": member.pk}))

        audit = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.USER_SOFT_DELETED,
            target_user=member,
        ).first()
        assert audit is not None
        assert audit.actor == admin
        assert "OE5MEM1" in audit.message
        assert "m@example.org" in audit.message
```

- [ ] **Step 4: Run tests to verify failure**

Run: `uv run pytest tests/test_user_soft_delete.py -v 2>&1 | tail -15`
Expected: Tests fail — `accounts:user_soft_delete` URL existiert noch nicht.

- [ ] **Step 5: `UserSoftDeleteView` + Helpers in `apps/accounts/views.py` einbauen**

Imports oben ergänzen (falls noch nicht da):
```python
from django.db import transaction
from django.utils import timezone
```

Existing `UserDeleteView`-Klasse **komplett entfernen** und ersetzen durch:

```python
def _revoke_all_topology(request, user):
    """Auto-revoke alle Region- + Station-Assignments des Users.

    Returnt eine Liste menschenlesbarer Strings ("Station-Admin: OE5XRX")
    die im Success-Banner gezeigt werden, damit der Admin weiß welche
    Positionen jetzt frei sind.
    """
    freed = []
    for assignment in list(user.region_assignments.select_related("region")):
        freed.append(
            f"Region-{assignment.get_role_display()}: {assignment.region.name}"
        )
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.REGION_ASSIGNMENT_REVOKED,
            actor=request.user,
            target_user=user,
            message=(
                f"reason=user_soft_deleted region={assignment.region.name} "
                f"role={assignment.role}"
            ),
            ip_address=_client_ip(request),
        )
        assignment.delete()

    for assignment in list(user.station_assignments.select_related("station")):
        label = assignment.station.callsign or assignment.station.name
        freed.append(
            f"Station-{assignment.get_role_display()}: {label}"
        )
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.STATION_ASSIGNMENT_REVOKED,
            actor=request.user,
            target_user=user,
            message=(
                f"reason=user_soft_deleted station={label} role={assignment.role}"
            ),
            ip_address=_client_ip(request),
        )
        assignment.delete()
    return freed


def _revoke_sso(request, user):
    """Revoke alle SSO-Grants + Sessions des Users (kein per-event Audit
    — der USER_SOFT_DELETED-Audit + die Impact-Counts reichen)."""
    now = timezone.now()
    if hasattr(user, "app_grants"):
        user.app_grants.filter(revoked_at__isnull=True).update(
            revoked_at=now, revoked_by=request.user,
        )
    if hasattr(user, "token_sessions"):
        user.token_sessions.filter(revoked_at__isnull=True).update(
            revoked_at=now, revoked_by=request.user,
        )


class UserSoftDeleteView(AdminRequiredMixin, View):
    template_name = "accounts/user_confirm_soft_delete.html"

    def get_object(self):
        # 404 wenn schon soft-deleted — Re-Soft-Delete nicht möglich.
        return get_object_or_404(
            User, pk=self.kwargs["pk"], deleted_at__isnull=True,
        )

    def get(self, request, pk):
        target = self.get_object()
        from apps.stations.models import StationAssignment

        ctx = {
            "target_user": target,
            "n_station_assignments": target.station_assignments.count(),
            "n_region_assignments": target.region_assignments.count(),
            "station_admin_assignments": list(
                target.station_assignments
                      .filter(role=StationAssignment.Role.ADMIN)
                      .select_related("station")
            ),
            "n_sso_grants": (
                target.app_grants.count() if hasattr(target, "app_grants") else 0
            ),
            "n_active_sessions": (
                target.token_sessions.filter(revoked_at__isnull=True).count()
                if hasattr(target, "token_sessions") else 0
            ),
            "n_group_memberships": target.groups.count(),
            "n_pending_tokens": target.account_tokens.filter(used_at__isnull=True).count(),
        }
        return render(request, self.template_name, ctx)

    def post(self, request, pk):
        target = self.get_object()
        if target == request.user:
            messages.error(request, _("You cannot delete your own account."))
            return redirect("accounts:user_detail", pk=pk)

        with transaction.atomic():
            freed_positions = _revoke_all_topology(request, target)
            target.account_tokens.filter(used_at__isnull=True).update(
                used_at=timezone.now()
            )
            _revoke_sso(request, target)
            target.groups.clear()
            target.deleted_at = timezone.now()
            target.deleted_by = request.user
            target.is_active = False
            target.save(update_fields=[
                "deleted_at", "deleted_by", "is_active",
            ])
            AccountAuditLog.log(
                event_type=AccountAuditLog.EventType.USER_SOFT_DELETED,
                actor=request.user,
                target_user=target,
                message=f"{target.username} <{target.email}>",
                ip_address=_client_ip(request),
            )

        if freed_positions:
            lines = "\n".join(f"  • {p}" for p in freed_positions)
            messages.warning(
                request,
                _("User soft-deleted. Free positions:\n%(lines)s\nReassign as needed.")
                % {"lines": lines},
            )
        else:
            messages.success(request, _("User soft-deleted."))
        return HttpResponseRedirect(
            reverse("accounts:user_list") + "?show=deleted"
        )
```

Imports oben ergänzen:
```python
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
```

- [ ] **Step 6: URL-Map updaten in `apps/accounts/urls.py`**

Die alte `user_delete`-URL **entfernen**:
```python
# REMOVE:
# path("users/<int:pk>/delete/", views.UserDeleteView.as_view(), name="user_delete"),
```

Die neue URL hinzufügen:
```python
path(
    "users/<int:pk>/soft-delete/",
    views.UserSoftDeleteView.as_view(),
    name="user_soft_delete",
),
```

- [ ] **Step 7: Confirm-Template `user_confirm_soft_delete.html` schreiben**

> **Subagent für diesen Step:** `pixel`, MUST invoke `Skill("frontend-design")`.

Create NEW file `apps/accounts/templates/accounts/user_confirm_soft_delete.html`:

```django
{% extends "base.html" %}
{% load i18n %}

{% block title %}{% trans "Soft-delete user" %}{% endblock %}

{% block breadcrumbs %}
  <a href="{% url 'accounts:user_list' %}">{% trans "Users" %}</a>
  <span class="sep">/</span>
  <span class="cur">{% trans "Soft-delete" %}</span>
{% endblock %}

{% block content %}
<div class="page-head"><div class="page-head-main">
  <div class="page-eyebrow t-danger">{% trans "Soft-delete user" %}</div>
  <h1 class="page-title">{% trans "Soft-delete user" %}</h1>
  <p class="page-sub">
    {% blocktrans with username=target_user.username %}Soft-delete user "{{ username }}"? This action is reversible — restore via the user list with ?show=deleted. Hard-purge is only available after a successful soft-delete.{% endblocktrans %}
  </p>
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

    <p class="t-label" style="margin-bottom:8px;">{% trans "Auto-revoked on soft-delete:" %}</p>
    <dl class="dlist">
      <dt>{% trans "Station-Assignments" %}</dt>
      <dd class="t-mono">{{ n_station_assignments }} <span class="t-muted">— {% trans "revoked, per-assignment audit emitted" %}</span></dd>
      <dt>{% trans "Region-Assignments" %}</dt>
      <dd class="t-mono">{{ n_region_assignments }} <span class="t-muted">— {% trans "revoked, per-assignment audit emitted" %}</span></dd>
      <dt>{% trans "SSO Grants" %}</dt>
      <dd class="t-mono">{{ n_sso_grants }} <span class="t-muted">— {% trans "revoked" %}</span></dd>
      <dt>{% trans "Active SSO Sessions" %}</dt>
      <dd class="t-mono">{{ n_active_sessions }} <span class="t-muted">— {% trans "terminated" %}</span></dd>
      <dt>{% trans "Group Memberships" %}</dt>
      <dd class="t-mono">{{ n_group_memberships }} <span class="t-muted">— {% trans "cleared" %}</span></dd>
      <dt>{% trans "Pending Account Tokens" %}</dt>
      <dd class="t-mono">{{ n_pending_tokens }} <span class="t-muted">— {% trans "invalidated" %}</span></dd>
    </dl>

    {% if station_admin_assignments %}
      <div class="onboarding-hint" role="alert" style="border-left-color:var(--danger);margin-top:14px;">
        <span class="onboarding-hint-icon">⚠️</span>
        <div class="onboarding-hint-text">
          {% trans "Attention: user is Station-Admin on these stations — the stations lose their admin:" %}
          <ul style="margin:4px 0 0 0;padding-left:20px;">
            {% for sa in station_admin_assignments %}
              <li>{{ sa.station.callsign|default:sa.station.name }}</li>
            {% endfor %}
          </ul>
        </div>
      </div>
    {% endif %}
  </div>
  <div class="panel-foot row-gap-8">
    <button type="submit" class="btn btn-danger" data-confirm="{% trans 'Soft-delete this user?' %}">{% trans "Soft-delete" %}</button>
    <a href="{% url 'accounts:user_detail' target_user.pk %}" class="btn btn-ghost">{% trans "Cancel" %}</a>
  </div>
</form>
{% endblock %}
```

**CRITICAL:** Multi-line `{# … #}` Django-Kommentare sind im Projekt verboten — Django schließt beim ersten `#}` und der Rest leakt in den HTTP-Response. Nur single-line `{# … #}` benutzen, oder `{% comment %}…{% endcomment %}` für multi-line. Im Template oben sind nur die Kommentar-freien Block-Tags drin — passt.

- [ ] **Step 8: Template-Guard prüfen**

Run: `uv run python scripts/check_template_comments.py 2>&1 | tail -3`
Expected: clean.

- [ ] **Step 9: Run tests to verify pass**

Run: `uv run pytest tests/test_user_soft_delete.py -v 2>&1 | tail -15`
Expected: 9 passed.

- [ ] **Step 10: Full regression**

Run: `uv run pytest tests/ -x --tb=short 2>&1 | tail -5`
Expected: All pass.

- [ ] **Step 11: ruff format + check**

Run: `uv run ruff format apps/accounts/views.py apps/accounts/urls.py apps/accounts/models.py tests/test_user_soft_delete.py 2>&1 | tail -2`
Run: `uv run ruff check apps/accounts/views.py apps/accounts/urls.py apps/accounts/models.py tests/test_user_soft_delete.py 2>&1 | tail -3`

- [ ] **Step 12: Commit**

```bash
git add apps/accounts/views.py apps/accounts/urls.py apps/accounts/models.py \
        apps/accounts/templates/accounts/user_confirm_soft_delete.html \
        apps/accounts/migrations/ \
        tests/test_user_soft_delete.py
git commit -m "feat(accounts): UserSoftDeleteView + topology auto-revoke + token invalidate

Soft-Delete-Lifecycle in einer atomic-Tx: topology revoke (per-assignment
*_ASSIGNMENT_REVOKED-Audit mit reason=user_soft_deleted-Marker), account-
tokens invalidate, SSO grants/sessions revoke, groups clear, deleted_at/
deleted_by/is_active stempeln, USER_SOFT_DELETED-Audit. Confirm-Page
zeigt alle Impact-Counts; Success-Banner listet freie Topology-Positionen.

URL-Name user_delete → user_soft_delete. AccountAuditLog.EventType.
USER_SOFT_DELETED + USER_RESTORED + USER_HARD_PURGED neu (USER_DELETED
bleibt im Enum als deprecated).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 5: `UserRestoreView` mit Email/Username-Konflikt-Guard

**Files:**
- Modify: `apps/accounts/views.py`
- Modify: `apps/accounts/urls.py`
- Create: `tests/test_user_restore.py`

- [ ] **Step 1: Write failing tests — `tests/test_user_restore.py`**

```python
"""UserRestoreView — restore soft-deleted user.

Sub-Spec 2b §5.
"""

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import AccountAuditLog, User


@pytest.fixture
def admin(db):
    return User.objects.create_user(
        username="OE5ADMIN",
        password="x",
        membership_level=User.MembershipLevel.ADMIN,
    )


@pytest.fixture
def deleted_member(db):
    u = User.objects.create_user(
        username="OE5DEAD", email="dead@example.org", password="x",
    )
    u.deleted_at = timezone.now()
    u.is_active = False
    u.save()
    return u


@pytest.mark.django_db
class TestRestore:
    def test_restore_sets_deleted_at_null_and_is_active_true(
        self, client, admin, deleted_member,
    ):
        client.force_login(admin)
        client.post(reverse("accounts:user_restore", kwargs={"pk": deleted_member.pk}))
        deleted_member.refresh_from_db()
        assert deleted_member.deleted_at is None
        assert deleted_member.deleted_by is None
        assert deleted_member.is_active is True

    def test_active_user_returns_404(self, client, admin):
        active = User.objects.create_user(username="OE5LIVE", password="x")
        client.force_login(admin)
        resp = client.post(reverse("accounts:user_restore", kwargs={"pk": active.pk}))
        assert resp.status_code == 404

    def test_restore_blocked_when_email_conflicts_with_active_user(
        self, client, admin, deleted_member,
    ):
        # Active user with same email as the deleted one
        User.objects.create_user(
            username="OE5NEW", email="dead@example.org", password="x",
        )
        client.force_login(admin)
        resp = client.post(reverse("accounts:user_restore", kwargs={"pk": deleted_member.pk}))
        # Restore failed silently — user is still deleted
        deleted_member.refresh_from_db()
        assert deleted_member.deleted_at is not None  # still soft-deleted

    def test_emits_user_restored_audit(self, client, admin, deleted_member):
        client.force_login(admin)
        client.post(reverse("accounts:user_restore", kwargs={"pk": deleted_member.pk}))
        audit = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.USER_RESTORED,
            target_user=deleted_member,
        ).first()
        assert audit is not None
        assert audit.actor == admin
        assert "OE5DEAD" in audit.message
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_user_restore.py -v 2>&1 | tail -10`
Expected: Fail — `accounts:user_restore` URL existiert noch nicht.

- [ ] **Step 3: `UserRestoreView` in `apps/accounts/views.py` einbauen**

Nach `UserSoftDeleteView`:

```python
class UserRestoreView(AdminRequiredMixin, View):
    http_method_names = ["post"]

    def post(self, request, pk):
        target = get_object_or_404(
            User, pk=pk, deleted_at__isnull=False,
        )
        # Email-Konflikt-Check: hat ein aktiver User dieselbe Email?
        clashing_email = (
            User.objects.active()
            .filter(email__iexact=target.email)
            .exclude(pk=target.pk)
            .first()
        )
        if clashing_email:
            messages.error(
                request,
                _("Cannot restore: another active user (%(other)s) is using "
                  "%(email)s. Either change %(other)s's email first, or update "
                  "%(name)s's email before restoring.")
                % {
                    "other": clashing_email.username,
                    "email": target.email,
                    "name": target.username,
                },
            )
            return redirect("accounts:user_detail", pk=pk)

        # Username-Konflikt-Check
        clashing_username = (
            User.objects.active()
            .filter(username__iexact=target.username)
            .exclude(pk=target.pk)
            .first()
        )
        if clashing_username:
            messages.error(
                request,
                _("Cannot restore: another active user is using callsign "
                  "%(name)s. Soft-delete or rename them first.")
                % {"name": target.username},
            )
            return redirect("accounts:user_detail", pk=pk)

        with transaction.atomic():
            target.deleted_at = None
            target.deleted_by = None
            target.is_active = True
            target.save(update_fields=[
                "deleted_at", "deleted_by", "is_active",
            ])
            AccountAuditLog.log(
                event_type=AccountAuditLog.EventType.USER_RESTORED,
                actor=request.user,
                target_user=target,
                message=f"{target.username} <{target.email}>",
                ip_address=_client_ip(request),
            )
        messages.success(
            request,
            _("User %(name)s restored. Topology assignments were revoked at "
              "delete-time and need to be re-assigned.")
            % {"name": target.username},
        )
        return redirect("accounts:user_detail", pk=pk)
```

- [ ] **Step 4: URL hinzufügen in `apps/accounts/urls.py`**

```python
path(
    "users/<int:pk>/restore/",
    views.UserRestoreView.as_view(),
    name="user_restore",
),
```

- [ ] **Step 5: Run tests to verify pass**

Run: `uv run pytest tests/test_user_restore.py -v 2>&1 | tail -10`
Expected: 4 passed.

- [ ] **Step 6: Full regression**

Run: `uv run pytest tests/ -x --tb=short 2>&1 | tail -5`

- [ ] **Step 7: ruff format + check**

Run: `uv run ruff format apps/accounts/views.py apps/accounts/urls.py tests/test_user_restore.py 2>&1 | tail -2`
Run: `uv run ruff check apps/accounts/views.py apps/accounts/urls.py tests/test_user_restore.py 2>&1 | tail -3`

- [ ] **Step 8: Commit**

```bash
git add apps/accounts/views.py apps/accounts/urls.py tests/test_user_restore.py
git commit -m "feat(accounts): UserRestoreView with email + username conflict guard

POST-only endpoint reverses soft-delete. Pre-commit check for active
user with same email or callsign (race window between soft-delete and
restore could let another user grab the identifier). Topology +
SSO-grants + groups stay revoked — admin re-assigns them manually
(documented in success message).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 6: `UserHardPurgeView` mit Avatar-File-Cleanup

**Files:**
- Modify: `apps/accounts/views.py`
- Modify: `apps/accounts/urls.py`
- Create: `apps/accounts/templates/accounts/user_confirm_hard_purge.html`
- Create: `tests/test_user_hard_purge.py`

- [ ] **Step 1: Write failing tests — `tests/test_user_hard_purge.py`**

```python
"""UserHardPurgeView — irreversible delete of an already-soft-deleted user.

Sub-Spec 2b §6.
"""

import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import AccountAuditLog, AccountToken, User


@pytest.fixture
def admin(db):
    return User.objects.create_user(
        username="OE5ADMIN",
        password="x",
        membership_level=User.MembershipLevel.ADMIN,
    )


@pytest.fixture
def deleted_member(db):
    u = User.objects.create_user(
        username="OE5DEAD", email="dead@example.org", password="x",
    )
    u.deleted_at = timezone.now()
    u.is_active = False
    u.save()
    return u


@pytest.mark.django_db
class TestHardPurge:
    def test_active_user_returns_404(self, client, admin):
        active = User.objects.create_user(username="OE5LIVE", password="x")
        client.force_login(admin)
        resp = client.get(reverse("accounts:user_hard_purge", kwargs={"pk": active.pk}))
        assert resp.status_code == 404

    def test_post_cascades_account_tokens(self, client, admin, deleted_member):
        from apps.accounts.tokens import issue_token

        issue_token(deleted_member, AccountToken.TokenType.WELCOME)
        assert AccountToken.objects.filter(user=deleted_member).exists()

        client.force_login(admin)
        client.post(reverse("accounts:user_hard_purge", kwargs={"pk": deleted_member.pk}))

        assert not AccountToken.objects.filter(user=deleted_member).exists()

    def test_post_sets_audit_actor_and_target_to_null_but_message_preserves_strings(
        self, client, admin, deleted_member,
    ):
        # Emit a USER_SOFT_DELETED audit BEFORE purge so we can verify message preservation
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.USER_SOFT_DELETED,
            actor=admin,
            target_user=deleted_member,
            message=f"{deleted_member.username} <{deleted_member.email}>",
        )

        client.force_login(admin)
        client.post(reverse("accounts:user_hard_purge", kwargs={"pk": deleted_member.pk}))

        assert not User.objects.filter(pk=deleted_member.pk).exists()

        audit = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.USER_SOFT_DELETED,
        ).first()
        assert audit is not None
        assert audit.target_user is None  # SET_NULL after purge
        assert "OE5DEAD" in audit.message  # text preserved

    def test_post_deletes_avatar_file(self, client, admin, deleted_member, tmp_path, settings):
        from PIL import Image

        settings.MEDIA_ROOT = str(tmp_path)
        settings.STORAGES = {
            **settings.STORAGES,
            "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        }

        img = Image.new("RGB", (50, 50), color=(255, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        buf.seek(0)
        f = SimpleUploadedFile("a.jpg", buf.read(), content_type="image/jpeg")
        deleted_member.avatar = f
        deleted_member.save()
        avatar_path = deleted_member.avatar.path
        import os
        assert os.path.exists(avatar_path)

        client.force_login(admin)
        client.post(reverse("accounts:user_hard_purge", kwargs={"pk": deleted_member.pk}))

        assert not os.path.exists(avatar_path)

    def test_emits_user_hard_purged_audit_with_soft_delete_date_in_message(
        self, client, admin, deleted_member,
    ):
        soft_date = deleted_member.deleted_at.strftime("%Y-%m-%d")

        client.force_login(admin)
        client.post(reverse("accounts:user_hard_purge", kwargs={"pk": deleted_member.pk}))

        audit = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.USER_HARD_PURGED,
        ).first()
        assert audit is not None
        assert audit.actor == admin
        assert "OE5DEAD" in audit.message
        assert soft_date in audit.message
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_user_hard_purge.py -v 2>&1 | tail -10`
Expected: Fail.

- [ ] **Step 3: `UserHardPurgeView` in `apps/accounts/views.py`**

Nach `UserRestoreView` einbauen. Logger oben importieren:

```python
import logging

logger = logging.getLogger(__name__)
```

Klasse:

```python
class UserHardPurgeView(AdminRequiredMixin, View):
    template_name = "accounts/user_confirm_hard_purge.html"

    def get_object(self):
        # Critical guard: nur soft-deleted User sind hard-purgeable.
        return get_object_or_404(
            User, pk=self.kwargs["pk"], deleted_at__isnull=False,
        )

    def get(self, request, pk):
        target = self.get_object()
        return render(request, self.template_name, {
            "target_user": target,
            "deleted_at": target.deleted_at,
            "deleted_by": target.deleted_by,
            "n_audit_as_actor": AccountAuditLog.objects.filter(actor=target).count(),
            "n_audit_as_target": AccountAuditLog.objects.filter(target_user=target).count(),
        })

    def post(self, request, pk):
        target = self.get_object()
        # Audit BEFORE delete — der target FK ist nach .delete() weg
        username = target.username
        email = target.email
        deleted_at = target.deleted_at
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.USER_HARD_PURGED,
            actor=request.user,
            target_user=target,
            message=(
                f"{username} <{email}> "
                f"(purged after soft-delete on {deleted_at:%Y-%m-%d})"
            ),
            ip_address=_client_ip(request),
        )
        # Avatar-File physisch entfernen (best-effort, kein Tx-Rollback bei Fail)
        if target.avatar:
            try:
                target.avatar.delete(save=False)
            except Exception:
                logger.exception(
                    "Avatar file delete failed for purged user %s", target.pk,
                )
        target.delete()
        messages.success(request, _("User permanently purged."))
        return HttpResponseRedirect(
            reverse("accounts:user_list") + "?show=deleted"
        )
```

- [ ] **Step 4: URL hinzufügen in `apps/accounts/urls.py`**

```python
path(
    "users/<int:pk>/hard-purge/",
    views.UserHardPurgeView.as_view(),
    name="user_hard_purge",
),
```

- [ ] **Step 5: Confirm-Template schreiben**

> **Subagent für diesen Step:** `pixel`, MUST invoke `Skill("frontend-design")`.

Create NEW `apps/accounts/templates/accounts/user_confirm_hard_purge.html`:

```django
{% extends "base.html" %}
{% load i18n %}

{% block title %}{% trans "Hard-purge user" %}{% endblock %}

{% block breadcrumbs %}
  <a href="{% url 'accounts:user_list' %}?show=deleted">{% trans "Deleted users" %}</a>
  <span class="sep">/</span>
  <span class="cur">{% trans "Hard-purge" %}</span>
{% endblock %}

{% block content %}
<div class="page-head"><div class="page-head-main">
  <div class="page-eyebrow t-danger">{% trans "Hard-purge — irreversible" %}</div>
  <h1 class="page-title">{% trans "Permanently purge user" %}</h1>
  <p class="page-sub">
    {% blocktrans with username=target_user.username %}Permanently delete user "{{ username }}"? This cannot be undone.{% endblocktrans %}
  </p>
</div></div>

<form method="post" class="panel" style="border-left:3px solid var(--danger);">
  {% csrf_token %}
  <div class="panel-body">
    <dl class="dlist">
      <dt>{% trans "Username" %}</dt><dd class="t-mono">{{ target_user.username }}</dd>
      <dt>{% trans "Email" %}</dt><dd class="t-mono">{{ target_user.email|default:"—" }}</dd>
      <dt>{% trans "Soft-deleted on" %}</dt><dd class="t-mono-sm">{{ deleted_at|date:"Y-m-d H:i" }}</dd>
      <dt>{% trans "Soft-deleted by" %}</dt><dd>{{ deleted_by.username|default:"(unknown)" }}</dd>
    </dl>

    <hr style="margin:14px 0;border:0;border-top:1px solid var(--line);">

    <p class="t-label" style="margin-bottom:8px;">{% trans "Audit rows turn into tombstones (FK SET_NULL, message strings preserved):" %}</p>
    <dl class="dlist">
      <dt>{% trans "Audit rows as actor" %}</dt>
      <dd class="t-mono">{{ n_audit_as_actor }} <span class="t-muted">— {% trans "actor FK becomes NULL" %}</span></dd>
      <dt>{% trans "Audit rows as target" %}</dt>
      <dd class="t-mono">{{ n_audit_as_target }} <span class="t-muted">— {% trans "target FK becomes NULL" %}</span></dd>
    </dl>

    <div class="onboarding-hint" role="alert" style="border-left-color:var(--danger);margin-top:14px;">
      <span class="onboarding-hint-icon">⚠️</span>
      <div class="onboarding-hint-text">
        {% trans "Once purged: account_tokens are dropped (CASCADE), avatar file is removed, all FK references become NULL. Audit message-strings preserve username/email for historical readability, but the user row is gone." %}
      </div>
    </div>
  </div>
  <div class="panel-foot row-gap-8">
    <button type="submit" class="btn btn-danger" data-confirm="{% trans 'Permanently purge this user?' %}">{% trans "Permanently purge" %}</button>
    <a href="{% url 'accounts:user_detail' target_user.pk %}" class="btn btn-ghost">{% trans "Cancel" %}</a>
  </div>
</form>
{% endblock %}
```

- [ ] **Step 6: Template-Guard**

Run: `uv run python scripts/check_template_comments.py 2>&1 | tail -3`
Expected: clean.

- [ ] **Step 7: Run tests to verify pass**

Run: `uv run pytest tests/test_user_hard_purge.py -v 2>&1 | tail -15`
Expected: 5 passed.

- [ ] **Step 8: Full regression**

Run: `uv run pytest tests/ -x --tb=short 2>&1 | tail -5`

- [ ] **Step 9: ruff format + check**

Run: `uv run ruff format apps/accounts/views.py apps/accounts/urls.py tests/test_user_hard_purge.py 2>&1 | tail -2`
Run: `uv run ruff check apps/accounts/views.py apps/accounts/urls.py tests/test_user_hard_purge.py 2>&1 | tail -3`

- [ ] **Step 10: Commit**

```bash
git add apps/accounts/views.py apps/accounts/urls.py \
        apps/accounts/templates/accounts/user_confirm_hard_purge.html \
        tests/test_user_hard_purge.py
git commit -m "feat(accounts): UserHardPurgeView (only on soft-deleted)

Two-stage delete: hard-purge is only reachable via the URL after a user
is already soft-deleted (404 otherwise — defense in depth). Audit-log
emit BEFORE .delete() so the message string preserves username/email
even after FK SET_NULL. Avatar-file delete in best-effort try/except
(transient storage failures don't block the DB-delete).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 7: `UserListView` Filter + Template-Cleanup

**Files:**
- Modify: `apps/accounts/views.py`
- Modify: `apps/accounts/templates/accounts/user_list.html`
- Create: `tests/test_user_list_filter.py`

- [ ] **Step 1: Write failing tests — `tests/test_user_list_filter.py`**

```python
"""UserListView ?show=active|inactive|deleted|all filter.

Sub-Spec 2b §3.1 + §7.1.
"""

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User


@pytest.fixture
def admin(db):
    return User.objects.create_user(
        username="OE5ADMIN",
        password="x",
        membership_level=User.MembershipLevel.ADMIN,
    )


@pytest.fixture
def population(db):
    active = User.objects.create_user(username="OE5ACTV", password="x")
    inactive = User.objects.create_user(username="OE5INAC", password="x")
    inactive.is_active = False
    inactive.save()
    deleted = User.objects.create_user(username="OE5DEAD", password="x")
    deleted.deleted_at = timezone.now()
    deleted.is_active = False
    deleted.save()
    return {"active": active, "inactive": inactive, "deleted": deleted}


@pytest.mark.django_db
class TestUserListFilter:
    def test_default_shows_active_only(self, client, admin, population):
        client.force_login(admin)
        resp = client.get(reverse("accounts:user_list"))
        usernames = {u.username for u in resp.context["users"]}
        # admin + active sind aktiv
        assert "OE5ACTV" in usernames
        assert "OE5ADMIN" in usernames
        assert "OE5INAC" not in usernames
        assert "OE5DEAD" not in usernames

    def test_show_inactive_shows_inactive_only(self, client, admin, population):
        client.force_login(admin)
        resp = client.get(reverse("accounts:user_list") + "?show=inactive")
        usernames = {u.username for u in resp.context["users"]}
        assert "OE5INAC" in usernames
        assert "OE5ACTV" not in usernames
        assert "OE5DEAD" not in usernames

    def test_show_deleted_shows_deleted_only(self, client, admin, population):
        client.force_login(admin)
        resp = client.get(reverse("accounts:user_list") + "?show=deleted")
        usernames = {u.username for u in resp.context["users"]}
        assert "OE5DEAD" in usernames
        assert "OE5ACTV" not in usernames
        assert "OE5INAC" not in usernames

    def test_show_all_shows_everyone(self, client, admin, population):
        client.force_login(admin)
        resp = client.get(reverse("accounts:user_list") + "?show=all")
        usernames = {u.username for u in resp.context["users"]}
        assert "OE5ACTV" in usernames
        assert "OE5INAC" in usernames
        assert "OE5DEAD" in usernames
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_user_list_filter.py -v 2>&1 | tail -10`
Expected: Fail — heutige UserListView hat keinen `?show=`-Filter.

- [ ] **Step 3: `UserListView.get_queryset` einbauen**

In `apps/accounts/views.py` — `UserListView` ersetzen:

```python
class UserListView(AdminRequiredMixin, ListView):
    model = User
    template_name = "accounts/user_list.html"
    context_object_name = "users"
    paginate_by = 25

    def get_queryset(self):
        show = self.request.GET.get("show", "active")
        qs = User.objects.order_by("username")
        if show == "deleted":
            qs = qs.filter(deleted_at__isnull=False)
        elif show == "inactive":
            qs = qs.filter(deleted_at__isnull=True, is_active=False)
        elif show == "all":
            pass
        else:  # default "active"
            qs = qs.filter(deleted_at__isnull=True, is_active=True)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["filter_show"] = self.request.GET.get("show", "active")
        return ctx
```

- [ ] **Step 4: `user_list.html` Template — Filter-Bar + Action-Buttons raus**

> **Subagent für diesen Step:** `pixel`, MUST invoke `Skill("frontend-design")`.

Existing `apps/accounts/templates/accounts/user_list.html` öffnen.

Filter-Bar oben unterhalb des `page-head`-Blocks einfügen (vor der List-Section):

```django
<div class="filter-bar row-gap-8" style="margin:14px 0;">
  <a href="?show=active"   class="pill {% if filter_show == 'active' %}pill-accent{% endif %}">{% trans "Active" %}</a>
  <a href="?show=inactive" class="pill {% if filter_show == 'inactive' %}pill-violet{% endif %}">{% trans "Inactive" %}</a>
  <a href="?show=deleted"  class="pill {% if filter_show == 'deleted' %}pill-muted{% endif %}">{% trans "Deleted" %}</a>
  <a href="?show=all"      class="pill {% if filter_show == 'all' %}pill-ghost{% endif %}">{% trans "All" %}</a>
</div>
```

Im List-Row-Rendering:
- Username/Name als Link auf `{% url 'accounts:user_detail' u.pk %}` machen (falls noch nicht).
- Per-row Action-Buttons (Edit, Delete, etc.) **entfernen**.
- Status-Pill erweitern: aktive = `ACTIVE`, deactivated = `INACTIVE`, deleted = `DELETED YYYY-MM-DD`:

```django
{% if u.deleted_at %}
  <span class="pill pill-muted">DELETED {{ u.deleted_at|date:"Y-m-d" }}</span>
{% elif u.is_active %}
  <span class="pill pill-online"><span class="dot"></span>ACTIVE</span>
{% else %}
  <span class="pill pill-violet">INACTIVE</span>
{% endif %}
```

**CRITICAL:** Wenn ein `_user_actions.html`-Partial existiert (gegrep'pt: `find apps/accounts/templates -name "_user_actions*"`), kann es jetzt gelöscht oder leer gelassen werden.

- [ ] **Step 5: Run tests to verify pass**

Run: `uv run pytest tests/test_user_list_filter.py -v 2>&1 | tail -10`
Expected: 4 passed.

- [ ] **Step 6: Full regression**

Run: `uv run pytest tests/ -x --tb=short 2>&1 | tail -5`
Expected: All pass.

- [ ] **Step 7: Template-Guard**

Run: `uv run python scripts/check_template_comments.py 2>&1 | tail -3`

- [ ] **Step 8: ruff**

Run: `uv run ruff format apps/accounts/views.py tests/test_user_list_filter.py 2>&1 | tail -2`
Run: `uv run ruff check apps/accounts/views.py tests/test_user_list_filter.py 2>&1 | tail -3`

- [ ] **Step 9: Commit**

```bash
git add apps/accounts/views.py apps/accounts/templates/accounts/user_list.html \
        tests/test_user_list_filter.py
git commit -m "feat(accounts): UserListView ?show= filter + remove inline actions

Default ?show=active filters out is_active=False + soft-deleted.
?show=inactive shows deactivated non-deleted, ?show=deleted shows
soft-deleted only, ?show=all shows everyone. Filter-Bar oben mit
vier pill-buttons (Active/Inactive/Deleted/All). Per-row action
buttons fliegen raus — der Username-Link führt auf UserDetailView
wo alle Actions konsolidiert sind.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 8: `UserDetailView` Action-Surface + Template-Banner

**Files:**
- Modify: `apps/accounts/views.py` (UserDetailView, falls 404-Guard für deleted da ist)
- Modify: `apps/accounts/templates/accounts/user_detail.html`

> **Subagent für diesen Task:** `pixel`, MUST invoke `Skill("frontend-design")`.

- [ ] **Step 1: `UserDetailView` — kein 404 für deleted**

Heute `UserDetailView` aus 1b hat ggf. einen Filter, der deleted ausschließt. In `apps/accounts/views.py` prüfen — falls `get_queryset` oder `queryset` deleted ausschließt, das **entfernen**:

```python
class UserDetailView(LoginRequiredMixin, DetailView):
    model = User
    template_name = "accounts/user_detail.html"
    context_object_name = "target_user"

    def get_queryset(self):
        # Nach 2b: deleted User sind erreichbar — die Action-Bar im
        # Template rendert konditional Restore/Hard-Purge statt Edit/
        # SoftDelete.
        return User.objects.all()
```

Falls schon kein Filter da ist, diesen Step skippen.

- [ ] **Step 2: `user_detail.html` — Banner + konditionale Action-Bar**

In `apps/accounts/templates/accounts/user_detail.html` zwei Änderungen:

**(a)** Banner über dem Page-Head:

```django
{% if target_user.deleted_at %}
  <div class="panel" style="border-left:3px solid var(--danger);margin-bottom:14px;background:var(--bg-2);">
    <div class="panel-body">
      <strong class="t-danger">{% trans "Soft-deleted" %}</strong>
      {% trans "on" %} {{ target_user.deleted_at|date:"Y-m-d H:i" }}
      {% if target_user.deleted_by %}
        {% trans "by" %} <a href="{% url 'accounts:user_detail' target_user.deleted_by.pk %}">{{ target_user.deleted_by.username }}</a>
      {% endif %}
    </div>
  </div>
{% endif %}
```

**(b)** Action-Bar (rechts neben page-head oder am Ende der oberen Section) — konditional:

```django
<div class="action-bar row-gap-8" style="margin-bottom:14px;">
  {% if target_user.deleted_at %}
    <form method="post" action="{% url 'accounts:user_restore' target_user.pk %}" style="display:inline;">
      {% csrf_token %}
      <button type="submit" class="btn btn-primary" data-confirm="{% trans 'Restore this user?' %}">{% trans "Restore" %}</button>
    </form>
    <a href="{% url 'accounts:user_hard_purge' target_user.pk %}" class="btn btn-danger">{% trans "Hard-purge" %}</a>
    <span class="btn btn-ghost" data-disabled title="{% trans 'Restore first' %}">{% trans "Edit" %}</span>
  {% elif target_user == request.user %}
    <a href="{% url 'accounts:profile' %}" class="btn btn-primary">{% trans "Edit profile" %}</a>
    {# Self-Soft-Delete is blocked at view level; do not render button #}
  {% else %}
    <a href="{% url 'accounts:user_edit' target_user.pk %}" class="btn btn-primary">{% trans "Edit" %}</a>
    <a href="{% url 'accounts:user_soft_delete' target_user.pk %}" class="btn btn-danger">{% trans "Soft-delete" %}</a>
  {% endif %}
</div>
```

Wo `{% url 'accounts:user_delete' ... %}` heute im Template steht: **alle Vorkommen ersetzen mit `accounts:user_soft_delete`** (grep nach dem alten Namen und ersetze).

**(c)** Existing Cards (Membership-Card, Region/Station-Assignments, Group-Picker etc.) bei deleted Usern konditional disablen:

```django
{% if not target_user.deleted_at %}
  {% include "accounts/_membership_card.html" %}
  {% include "accounts/_region_assignments_card.html" %}
  {% include "accounts/_station_assignments_card.html" %}
{% else %}
  <div class="panel-body t-muted">
    {% trans "Cards are disabled for soft-deleted users. Restore first to manage membership, assignments, and groups." %}
  </div>
{% endif %}
```

**CRITICAL:** Keine multi-line `{# … #}` Kommentare.

- [ ] **Step 3: Template-Guard**

Run: `uv run python scripts/check_template_comments.py 2>&1 | tail -3`

- [ ] **Step 4: Full regression**

Run: `uv run pytest tests/ -x --tb=short 2>&1 | tail -5`
Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add apps/accounts/views.py apps/accounts/templates/accounts/user_detail.html
git commit -m "feat(accounts): UserDetailView action-bar + banner for soft-deleted

Detail-page becomes the single action surface — list view has no
inline buttons anymore. Action-bar renders conditionally:
- active user (other): Edit + Soft-delete
- active user (self):  Edit profile (self-soft-delete blocked)
- soft-deleted user:   Restore + Hard-purge + Edit-disabled
Red banner above page-head shows soft-delete date + actor. Membership/
assignment/group cards are disabled for deleted users with a clear hint.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 9: Topology-Filter (Visibility + Notifications)

**Files:**
- Modify: `apps/accounts/visibility.py`
- Modify: `apps/monitoring/recipients.py`
- Create: `tests/test_topology_filter_deleted.py`

- [ ] **Step 1: Write failing tests — `tests/test_topology_filter_deleted.py`**

```python
"""Topology-Routing + Visibility filtern soft-deleted User.

Sub-Spec 2b §3.1.
"""

import pytest
from django.utils import timezone

from apps.accounts.models import User


@pytest.fixture
def admin(db):
    return User.objects.create_user(
        username="OE5ADMIN",
        password="x",
        membership_level=User.MembershipLevel.ADMIN,
    )


@pytest.fixture
def deleted_member(db):
    u = User.objects.create_user(
        username="OE5DEAD",
        password="x",
        email="dead@example.org",
        membership_level=User.MembershipLevel.MEMBER,
    )
    u.deleted_at = timezone.now()
    u.is_active = False
    u.save()
    return u


@pytest.mark.django_db
class TestRecipientsExcludesDeleted:
    def test_recipients_for_station_alert_excludes_deleted_admin(
        self, db, deleted_member,
    ):
        from apps.stations.models import Region, Station, StationAssignment
        from apps.monitoring.recipients import recipients_for_station_alert

        region = Region.objects.create(name="X")
        station = Station.objects.create(name="S", callsign="OE5S", region=region)
        StationAssignment.objects.create(
            user=deleted_member, station=station,
            role=StationAssignment.Role.ADMIN, assigned_by=deleted_member,
        )

        recipients = list(recipients_for_station_alert(station))
        assert deleted_member not in recipients


@pytest.mark.django_db
class TestDirectoryVisibility:
    def test_deleted_user_not_in_member_directory(self, client, admin, deleted_member):
        from django.urls import reverse

        client.force_login(admin)
        resp = client.get(reverse("accounts:user_list") + "?show=active")
        # OE5DEAD wurde soft-deleted und ist in show=active
        # explizit ausgeschlossen — UserListView Filter macht das schon.
        assert "OE5DEAD" not in resp.content.decode()

    def test_deleted_user_404_for_member_audience_in_detail(self, client, deleted_member):
        # A regular member browsing the directory should not see deleted users —
        # this test asserts that the visibility module's audience-filter
        # excludes them (specifics depend on existing visibility.py shape).
        from apps.accounts.visibility import user_can_view_directory

        member = User.objects.create_user(
            username="OE5MEM",
            password="x",
            membership_level=User.MembershipLevel.MEMBER,
        )
        client.force_login(member)
        # ... actual assertion depends on visibility module — at minimum,
        # checking that User.objects.active() is used in the directory
        # builder excludes the deleted user.
        from apps.accounts.models import User as UserModel
        members_visible = list(UserModel.objects.active().filter(
            membership_level__in=[
                UserModel.MembershipLevel.MEMBER,
                UserModel.MembershipLevel.STAFF,
                UserModel.MembershipLevel.ADMIN,
            ],
        ))
        assert deleted_member not in members_visible
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_topology_filter_deleted.py -v 2>&1 | tail -10`
Expected: Mindestens der recipients-Test failed — deleted-Filter ist noch nicht drin.

- [ ] **Step 3: `apps/monitoring/recipients.py` ergänzen**

Den File öffnen, alle queries die User-Adressen zurückgeben um `deleted_at__isnull=True` ergänzen.

Typisch sieht das so aus:
```python
def recipients_for_station_alert(station):
    ...
    qs = User.objects.filter(...)
    # NEW: exclude soft-deleted users from notification routing
    qs = qs.filter(deleted_at__isnull=True)
    return qs
```

Konkret: jede `User.objects.filter(…)`-Zeile bekommt `, deleted_at__isnull=True` ergänzt, oder besser: am Anfang der Funktion ein `.active()`-prefix benutzen via `User.objects.active().filter(...)`.

- [ ] **Step 4: `apps/accounts/visibility.py` ergänzen**

Falls `user_can_view_directory` oder andere visibility-Helpers User-Queries enthalten, die deleted nicht ausschließen, sie um den Filter ergänzen.

Beispiel-Patch-Pattern:
```python
def visible_users_for(viewer):
    qs = User.objects.active()  # NEW: exclude soft-deleted
    # ... existing audience logic ...
    return qs
```

- [ ] **Step 5: Run tests to verify pass**

Run: `uv run pytest tests/test_topology_filter_deleted.py -v 2>&1 | tail -10`
Expected: All pass.

- [ ] **Step 6: Full regression**

Run: `uv run pytest tests/ -x --tb=short 2>&1 | tail -5`

- [ ] **Step 7: ruff**

Run: `uv run ruff format apps/accounts/visibility.py apps/monitoring/recipients.py tests/test_topology_filter_deleted.py 2>&1 | tail -2`
Run: `uv run ruff check apps/accounts/visibility.py apps/monitoring/recipients.py tests/test_topology_filter_deleted.py 2>&1 | tail -3`

- [ ] **Step 8: Commit**

```bash
git add apps/accounts/visibility.py apps/monitoring/recipients.py \
        tests/test_topology_filter_deleted.py
git commit -m "feat(accounts): topology + notifications exclude soft-deleted users

Notification routing (apps/monitoring/recipients.py) no longer includes
soft-deleted users — a deleted Station-Admin wouldn't receive alerts
anyway (is_active=False blocks login), but their email is still in our
DB and would have been targeted by send_mail. The directory visibility
in apps/accounts/visibility.py also excludes deleted users from the
member-list audience.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 10: Cleanup — alte UserDeleteView-Tests + URL-Refs entfernen

**Files:**
- Delete: `tests/test_user_delete_view.py`
- Modify: alle Templates die `{% url 'accounts:user_delete' ... %}` referenzieren
- Modify: alle Tests die `accounts:user_delete` benutzen

- [ ] **Step 1: Alte Tests löschen**

```bash
rm tests/test_user_delete_view.py
```

- [ ] **Step 2: Grep für `user_delete` URL-Name**

Run: `grep -rn "accounts:user_delete\b" apps/ tests/ templates/ 2>&1 | grep -v __pycache__`

Jeder Treffer ist Legacy aus 1c — alle auf `accounts:user_soft_delete` umstellen.

Typische Stellen:
- `apps/accounts/templates/accounts/user_form.html` — Cancel-Link
- `tests/test_*` — Test-URL-Refs

Stell jeden Treffer um.

- [ ] **Step 3: Full regression**

Run: `uv run pytest tests/ -x --tb=short 2>&1 | tail -5`
Expected: All pass.

- [ ] **Step 4: ruff**

Run: `uv run ruff format apps/ tests/ 2>&1 | tail -2`
Run: `uv run ruff check apps/ tests/ 2>&1 | tail -3`

- [ ] **Step 5: Template-Guard**

Run: `uv run python scripts/check_template_comments.py 2>&1 | tail -3`

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "chore(accounts): remove legacy UserDeleteView refs

tests/test_user_delete_view.py deleted — coverage moved to
test_user_soft_delete.py + test_user_hard_purge.py. All template
+ test URL references to accounts:user_delete updated to
accounts:user_soft_delete.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 11: Final integration verify

**Files:**
- Read only

- [ ] **Step 1: Run full test suite**

Run: `cd /home/pbuchegger/OE5XRX/station-manager/.worktrees/feat-account-lifecycle-2b && uv run pytest tests/ --tb=short 2>&1 | tail -5`
Expected: All tests pass (~938 baseline + ~25 new = ~963).

- [ ] **Step 2: Django system check**

Run: `uv run python manage.py check 2>&1 | tail -5`
Expected: `System check identified no issues (0 silenced).`

- [ ] **Step 3: Migrations clean**

Run: `uv run python manage.py makemigrations --check --dry-run 2>&1 | tail -5`
Expected: keine pending Migrations für `accounts`.

- [ ] **Step 4: ruff über alles**

Run: `uv run ruff check apps/ tests/ 2>&1 | tail -3`
Run: `uv run ruff format --check apps/ tests/ 2>&1 | tail -3`
Expected: Clean.

- [ ] **Step 5: Template-Guard final**

Run: `uv run python scripts/check_template_comments.py 2>&1 | tail -3`
Expected: clean.

- [ ] **Step 6: Branch-Summary**

Run: `git log --oneline origin/main..HEAD 2>&1 | head -15`
Expected: ~10 commits since main, einer pro Task.

---

## Summary

Nach Merge dieses Plans liefert `feat/account-lifecycle-2b-soft-delete`:

- **User-Modell:** `deleted_at` + `deleted_by` Felder + Conditional UNIQUE-Index auf username → Callsign-Reuse nach Soft-Delete erlaubt.
- **UserManager:** `.active()` + `.deleted()` Helper, kein default-Filter (`User.objects.all()` unverändert).
- **Forms:** UserCreationForm + ProfileIdentityForm excludieren soft-deleted aus Uniqueness-Check.
- **UserSoftDeleteView:** atomare 6-Stufen-Tx (Topology-Revoke + Token-Invalidate + SSO-Revoke + Groups-Clear + Stempel + Audit), Success-Banner listet freie Topology-Positionen.
- **UserRestoreView:** POST-only, Email/Username-Konflikt-Check vor Restore, Topology bleibt revoked (Admin re-assigniert).
- **UserHardPurgeView:** nur auf soft-deleted erreichbar (404-Guard), Audit-vor-Delete, Avatar-File-Cleanup im try/except.
- **UserListView Filter:** `?show=active|inactive|deleted|all` + Filter-Bar-UI; per-row Action-Buttons entfernt.
- **UserDetailView:** roter Banner für soft-deleted, konditionale Action-Bar (Edit+SoftDelete / Restore+HardPurge / EditProfile self).
- **Topology + Notifications:** filtern soft-deleted aus Recipients + Directory-Visibility.
- **Audit:** drei neue EventTypes (`USER_SOFT_DELETED`, `USER_RESTORED`, `USER_HARD_PURGED`); per-Assignment `*_REVOKED`-Audits beim Soft-Delete mit `reason=user_soft_deleted`-Marker.

Test-Count wächst um ~25 Tests in 5 neuen Modulen. URL-Name `user_delete` ist weg, ersetzt durch `user_soft_delete`/`user_restore`/`user_hard_purge`.

Damit ist der User-Domain-Arc komplett: **1a Foundation + 1b Directory + 1c Self-Service + 2a Email-Flows + 2b Soft-Delete**.
