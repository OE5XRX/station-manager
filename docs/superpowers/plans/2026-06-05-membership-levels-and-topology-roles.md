# Membership-Levels + Topology-Roles — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a two-axis authorization model — sequential Membership-Levels (applicant/member/staff/admin) on the User, plus a per-Region/per-Station Topology of assignments — and re-route alert notifications through it.

**Architecture:** New `membership_level` enum field on `User` replaces today's group-based role checks. New models `Region`, `StationAssignment` (admin/maintainer), `RegionAssignment` (manager) live in `apps/stations/`. New `AccountAuditLog` parallel to `StationAuditLog` and `SsoAuditLog`. Notification routing moves into a single-purpose helper module `apps/monitoring/recipients.py`. The legacy Django Groups `admin`/`operator`/`member` are dropped at the end of the migration chain.

**Tech Stack:** Django 6.0, django-test-migrations, pytest-django, HTMX, the existing DTL templates + base.html design system.

**Reference spec:** `docs/superpowers/specs/2026-06-05-membership-levels-and-topology-roles-design.md`

---

## PR Strategy (Hybrid Split)

To keep individual PRs review-able, this plan ships in **two PRs**:

### PR-1 (this plan, Tasks 1-10 + Task 23 + Task 24)
**Scope:** Phase 1 (Foundation Models) + Phase 2 (Permission Helpers) + Phase 3 (Refactor existing helpers + call-site sweep).

After PR-1 merge:
- Schemas for `membership_level`, `Region`, `StationAssignment`, `RegionAssignment`, `AccountAuditLog` all exist.
- All call sites are refactored to use `is_internal` / `is_admin` / topology helpers.
- Notification routing still uses `membership_level=ADMIN` (functionally equivalent to today's `groups__name="admin"`) — the new `recipients_for_station_alert` helper is NOT wired yet.
- New schemas (Region etc.) have no UI to populate them — admin via Django-Admin only.
- Legacy Django Groups `admin`/`operator`/`member` are STILL present (migration 0009 deferred to PR-2).

Net effect on production: zero user-visible change, behavior identical to today's group-based check. Foundation is in place.

### PR-2 (separate plan, to be written after PR-1 merge)
**Scope:** Phase 4 (Notification Routing) + Phase 5 (Audit-Log Signals) + Phase 6-8 (UI: User-Detail, Station-Detail, Region-CRUD) + Phase 9 (drop legacy Groups, migration 0009).

This plan deliberately defers writing PR-2 details until PR-1 is merged — the actual function/template names from PR-1 are then ground truth, not a forecast. Phase 4-9 of THIS document is kept as reference (the "what" and "why"), but the executor for PR-1 stops after Task 24.

---

## File Structure

### New files

| Path | Responsibility |
|---|---|
| `apps/accounts/migrations/0004_add_membership_level.py` | Schema migration: add `membership_level` CharField with default APPLICANT to User |
| `apps/accounts/migrations/0005_seed_membership_levels.py` | Data migration: map existing Group membership → membership_level |
| `apps/accounts/migrations/0008_add_account_audit_log.py` | Schema migration: new AccountAuditLog model |
| `apps/accounts/migrations/0009_drop_legacy_role_groups.py` | Data migration: delete the three legacy Django Groups |
| `apps/stations/migrations/0006_add_region_and_station_fk.py` | Schema migration: Region model + Station.region FK |
| `apps/stations/migrations/0007_add_assignments.py` | Schema migration: StationAssignment + RegionAssignment + constraints |
| `apps/monitoring/recipients.py` | Single-responsibility helper: resolve Alert → email recipient set |
| `apps/stations/views_region.py` | Region CRUD views (list/create/edit/delete) |
| `apps/stations/views_assignments.py` | Station/Region assignment CRUD views (htmx) |
| `apps/accounts/views_membership.py` | Membership-level promote/demote view |
| `apps/stations/templates/stations/region_list.html` | Region admin list |
| `apps/stations/templates/stations/region_form.html` | Region create/edit form |
| `apps/stations/templates/stations/region_confirm_delete.html` | Region delete confirm |
| `apps/stations/templates/stations/_station_region_picker.html` | HTMX fragment: region picker on station-detail |
| `apps/stations/templates/stations/_station_admin_picker.html` | HTMX fragment: station-admin picker |
| `apps/stations/templates/stations/_station_maintainer_list.html` | HTMX fragment: maintainer list + add row |
| `apps/accounts/templates/accounts/_membership_level_picker.html` | HTMX fragment: membership-level dropdown |
| `apps/accounts/templates/accounts/_user_region_assignments.html` | HTMX fragment: region-manager assignments |
| `apps/accounts/templates/accounts/_user_station_assignments.html` | HTMX fragment: station assignments |
| `tests/test_membership_levels.py` | Membership-level field + helpers + migration tests |
| `tests/test_topology_models.py` | Region/StationAssignment/RegionAssignment models + constraints |
| `tests/test_topology_permissions.py` | can_administer_station / can_maintain_station / can_use_station |
| `tests/test_alert_recipients.py` | recipients_for_station_alert query logic |
| `tests/test_account_audit_log.py` | AccountAuditLog model + log() helper |
| `tests/test_audit_log_emission.py` | All 7 audit emission paths |
| `tests/test_views_membership.py` | Promote/demote view + permission gates |
| `tests/test_views_assignments.py` | Assignment CRUD views + permission gates |
| `tests/test_views_region.py` | Region CRUD views |
| `tests/test_views_station_topology.py` | Station-detail region picker + admin/maintainer pickers |

### Modified files

| Path | Reason |
|---|---|
| `apps/accounts/models.py` | Add membership_level + new properties; refactor is_admin; remove is_operator/is_staff_member/group_names |
| `apps/stations/models.py` | Add Region, StationAssignment, RegionAssignment; add Station.region FK; new StationAuditLog event types |
| `apps/accounts/forms.py` | Add membership_level to UserCreationForm/UserChangeForm |
| `apps/monitoring/notifications.py` | Switch _send_email_notification to recipients_for_station_alert; send_test_notification to per-user |
| `apps/audit/views.py` | Merge 3 sources (add AccountAuditLog) |
| `tests/conftest.py` | Refactor _user_in_group to set membership_level; add region/assignment fixtures |
| `apps/stations/urls.py` | Add region + assignments routes |
| `apps/accounts/urls.py` | Add membership-promote route |
| `apps/stations/templates/stations/station_detail.html` | New "Region & Verantwortliche" section |
| `apps/accounts/templates/accounts/user_detail.html` | New "Rollen & Zuordnungen" section |
| `apps/stations/views.py:45,106` | Switch to topology helpers |
| `apps/firmware/views.py:27` | is_internal |
| `apps/monitoring/views.py:19,26` | unchanged at class level, but consistency check |
| `apps/audit/views.py:161` | membership_level=ADMIN |
| `apps/deployments/consumers.py:31` | is_internal |
| `apps/tunnel/views.py:24` | is_internal |
| `apps/tunnel/consumers.py:35` | is_internal |
| `apps/api/views.py:130` | is_internal |

---

# Phase 1: Foundation Models (schema-only, no behavior change)

## Task 1: User.membership_level field + migration 0004

**Files:**
- Modify: `apps/accounts/models.py`
- Create: `apps/accounts/migrations/0004_add_membership_level.py`
- Create: `tests/test_membership_levels.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_membership_levels.py`:

```python
"""Tests for User.membership_level field + helpers.

The membership_level is the new vereinsweit role indicator on User.
Replaces today's group-based admin/operator/member detection.
"""

import pytest

from apps.accounts.models import User


@pytest.mark.django_db
def test_membership_level_default_is_applicant():
    """A freshly-created user without explicit level lands on APPLICANT.

    Production today does not have a self-service signup, but the
    APPLICANT default is the safe fallback for SSO-bootstrapped users
    and the future signup flow.
    """
    user = User.objects.create_user(username="nobody", password="x")
    assert user.membership_level == User.MembershipLevel.APPLICANT


@pytest.mark.django_db
def test_membership_level_choices_exist():
    """All four level values are defined as TextChoices."""
    assert User.MembershipLevel.APPLICANT == "applicant"
    assert User.MembershipLevel.MEMBER == "member"
    assert User.MembershipLevel.STAFF == "staff"
    assert User.MembershipLevel.ADMIN == "admin"


@pytest.mark.django_db
def test_membership_level_display_labels():
    """Display labels use the 'Vereins-X' compound form."""
    user = User.objects.create_user(username="u", password="x")
    user.membership_level = User.MembershipLevel.STAFF
    user.save(update_fields=["membership_level"])
    assert user.get_membership_level_display() == "Vereins-Staff"
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/python -m pytest tests/test_membership_levels.py -v
```

Expected: `AttributeError: type object 'User' has no attribute 'MembershipLevel'` on all three.

- [ ] **Step 3: Add field to User model**

In `apps/accounts/models.py`, add inside the `User` class (after the `Language` choices block, before `objects = UserManager()`):

```python
    class MembershipLevel(models.TextChoices):
        APPLICANT = "applicant", _("Vereins-Bewerber")
        MEMBER    = "member",    _("Vereins-Mitglied")
        STAFF     = "staff",     _("Vereins-Staff")
        ADMIN     = "admin",     _("Vereins-Admin")

    membership_level = models.CharField(
        _("membership level"),
        max_length=10,
        choices=MembershipLevel.choices,
        default=MembershipLevel.APPLICANT,
    )
```

- [ ] **Step 4: Generate the migration**

```bash
.venv/bin/python manage.py makemigrations accounts --name add_membership_level
```

Expected: `apps/accounts/migrations/0004_add_membership_level.py` created. Verify the contents — it should be a single `AddField` operation. If Django assigns a different number than 0004, check existing migration numbers and rename.

- [ ] **Step 5: Run the test to verify pass**

```bash
.venv/bin/python -m pytest tests/test_membership_levels.py -v
```

Expected: 3 PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/accounts/models.py apps/accounts/migrations/0004_add_membership_level.py tests/test_membership_levels.py
git commit -m "feat(accounts): add User.membership_level enum field (default APPLICANT)"
```

---

## Task 2: Group → membership_level data migration 0005

**Files:**
- Create: `apps/accounts/migrations/0005_seed_membership_levels.py`
- Create: `tests/test_membership_level_migration.py`

- [ ] **Step 1: Write failing test (django-test-migrations style)**

Create `tests/test_membership_level_migration.py`:

```python
"""Verify the Group → membership_level mapping in migration 0005.

The test uses django-test-migrations to drive migrations to a target
state, mutate data at the historical-model level, then migrate forward
and check the data.
"""

import pytest
from django_test_migrations.migrator import Migrator


@pytest.mark.django_db(transaction=True)
def test_0005_admin_group_maps_to_admin_level():
    migrator = Migrator(database="default")

    old_state = migrator.apply_initial_migration(
        [("accounts", "0004_add_membership_level")]
    )
    User = old_state.apps.get_model("accounts", "User")
    Group = old_state.apps.get_model("auth", "Group")
    admin_group, _ = Group.objects.get_or_create(name="admin")
    u = User.objects.create_user(username="alice", password="x")
    u.groups.add(admin_group)

    new_state = migrator.apply_tested_migration(
        [("accounts", "0005_seed_membership_levels")]
    )
    User = new_state.apps.get_model("accounts", "User")
    alice = User.objects.get(username="alice")
    assert alice.membership_level == "admin"

    migrator.reset()


@pytest.mark.django_db(transaction=True)
def test_0005_operator_group_maps_to_staff_level():
    migrator = Migrator(database="default")
    old_state = migrator.apply_initial_migration(
        [("accounts", "0004_add_membership_level")]
    )
    User = old_state.apps.get_model("accounts", "User")
    Group = old_state.apps.get_model("auth", "Group")
    op_group, _ = Group.objects.get_or_create(name="operator")
    u = User.objects.create_user(username="bob", password="x")
    u.groups.add(op_group)

    new_state = migrator.apply_tested_migration(
        [("accounts", "0005_seed_membership_levels")]
    )
    User = new_state.apps.get_model("accounts", "User")
    assert User.objects.get(username="bob").membership_level == "staff"
    migrator.reset()


@pytest.mark.django_db(transaction=True)
def test_0005_member_group_maps_to_member_level():
    migrator = Migrator(database="default")
    old_state = migrator.apply_initial_migration(
        [("accounts", "0004_add_membership_level")]
    )
    User = old_state.apps.get_model("accounts", "User")
    Group = old_state.apps.get_model("auth", "Group")
    m_group, _ = Group.objects.get_or_create(name="member")
    u = User.objects.create_user(username="carol", password="x")
    u.groups.add(m_group)

    new_state = migrator.apply_tested_migration(
        [("accounts", "0005_seed_membership_levels")]
    )
    User = new_state.apps.get_model("accounts", "User")
    assert User.objects.get(username="carol").membership_level == "member"
    migrator.reset()


@pytest.mark.django_db(transaction=True)
def test_0005_user_with_no_group_stays_applicant():
    migrator = Migrator(database="default")
    old_state = migrator.apply_initial_migration(
        [("accounts", "0004_add_membership_level")]
    )
    User = old_state.apps.get_model("accounts", "User")
    User.objects.create_user(username="dave", password="x")

    new_state = migrator.apply_tested_migration(
        [("accounts", "0005_seed_membership_levels")]
    )
    User = new_state.apps.get_model("accounts", "User")
    # Default from 0004 stays APPLICANT
    assert User.objects.get(username="dave").membership_level == "applicant"
    migrator.reset()


@pytest.mark.django_db(transaction=True)
def test_0005_user_in_multiple_groups_takes_highest():
    """Admin > Staff (operator) > Member precedence."""
    migrator = Migrator(database="default")
    old_state = migrator.apply_initial_migration(
        [("accounts", "0004_add_membership_level")]
    )
    User = old_state.apps.get_model("accounts", "User")
    Group = old_state.apps.get_model("auth", "Group")
    a, _ = Group.objects.get_or_create(name="admin")
    o, _ = Group.objects.get_or_create(name="operator")
    m, _ = Group.objects.get_or_create(name="member")
    u = User.objects.create_user(username="eve", password="x")
    u.groups.add(a, o, m)

    new_state = migrator.apply_tested_migration(
        [("accounts", "0005_seed_membership_levels")]
    )
    User = new_state.apps.get_model("accounts", "User")
    assert User.objects.get(username="eve").membership_level == "admin"
    migrator.reset()
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/python -m pytest tests/test_membership_level_migration.py -v
```

Expected: 5 errors — `Migration accounts.0005_seed_membership_levels does not exist`.

- [ ] **Step 3: Create the migration**

Create `apps/accounts/migrations/0005_seed_membership_levels.py`:

```python
"""Seed User.membership_level from existing Group memberships.

Mapping (highest precedence wins):
  - group 'admin'    -> ADMIN
  - group 'operator' -> STAFF       (rename: operator collides with amateur-radio meaning)
  - group 'member'   -> MEMBER
  - none of the above -> APPLICANT  (= default from 0004)

Reverse is a noop. Re-running forward is idempotent because the mapping
overrides the APPLICANT default; once set, it stays. The legacy Django
groups are NOT touched here — migration 0009 deletes them after the
call-site refactor has landed.
"""

from django.db import migrations

LEVEL_PRECEDENCE = [
    ("admin", "admin"),
    ("operator", "staff"),
    ("member", "member"),
]


def seed_membership_levels(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    Group = apps.get_model("auth", "Group")

    # Cache group ids by name for the loop below
    groups_by_name = {g.name: g for g in Group.objects.filter(
        name__in=[name for name, _ in LEVEL_PRECEDENCE])}

    for user in User.objects.all().order_by("pk"):
        user_group_names = set(user.groups.values_list("name", flat=True))
        for group_name, level in LEVEL_PRECEDENCE:
            if group_name in user_group_names:
                user.membership_level = level
                user.save(update_fields=["membership_level"])
                break
        # No group match -> APPLICANT default from 0004 stays


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0004_add_membership_level"),
    ]

    operations = [
        migrations.RunPython(
            seed_membership_levels,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
```

- [ ] **Step 4: Run tests to verify pass**

```bash
.venv/bin/python -m pytest tests/test_membership_level_migration.py -v
```

Expected: 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/accounts/migrations/0005_seed_membership_levels.py tests/test_membership_level_migration.py
git commit -m "feat(accounts): seed membership_level from legacy Groups (migration 0005)"
```

---

## Task 3: Region model + Station.region FK + migration 0006

**Files:**
- Modify: `apps/stations/models.py`
- Create: `apps/stations/migrations/0006_add_region_and_station_fk.py` (generated)
- Create: `tests/test_topology_models.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_topology_models.py`:

```python
"""Tests for the new topology models: Region, StationAssignment, RegionAssignment."""

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.accounts.models import User
from apps.stations.models import Region, Station


@pytest.mark.django_db
class TestRegion:
    def test_str(self):
        r = Region.objects.create(name="Tirol", slug="tirol")
        assert str(r) == "Tirol"

    def test_unique_name(self):
        Region.objects.create(name="Tirol", slug="tirol-1")
        with pytest.raises(IntegrityError):
            Region.objects.create(name="Tirol", slug="tirol-2")

    def test_unique_slug(self):
        Region.objects.create(name="Tirol", slug="tirol")
        with pytest.raises(IntegrityError):
            Region.objects.create(name="Tirol Süd", slug="tirol")

    def test_description_optional(self):
        r = Region.objects.create(name="Salzburg", slug="sbg")
        assert r.description == ""


@pytest.mark.django_db
class TestStationRegionFK:
    def test_station_can_have_null_region(self):
        s = Station.objects.create(name="OE5XTR", callsign="OE5XTR")
        assert s.region is None

    def test_station_region_set_null_on_delete(self):
        r = Region.objects.create(name="Tirol", slug="tirol")
        s = Station.objects.create(name="OE5XTR", callsign="OE5XTR", region=r)
        r.delete()
        s.refresh_from_db()
        assert s.region is None

    def test_region_stations_reverse_relation(self):
        r = Region.objects.create(name="Tirol", slug="tirol")
        Station.objects.create(name="OE5A", callsign="OE5A", region=r)
        Station.objects.create(name="OE5B", callsign="OE5B", region=r)
        assert r.stations.count() == 2
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/python -m pytest tests/test_topology_models.py -v
```

Expected: ImportError on `Region` (does not exist).

- [ ] **Step 3: Add Region model + Station.region FK**

In `apps/stations/models.py`, add `Region` at the top of the module (after imports, before `StationTag`):

```python
class Region(models.Model):
    """A geographic / organizational grouping of stations.

    Freely manageable via /regions/ admin UI. Provides the scope for
    RegionAssignment (a Region-Manager has operative authority over
    all stations of the region). Station.region is the FK.
    """

    name        = models.CharField(_("name"), max_length=80, unique=True)
    slug        = models.SlugField(_("slug"), unique=True)
    description = models.TextField(_("description"), blank=True)
    created_at  = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("region")
        verbose_name_plural = _("regions")
        ordering = ["name"]

    def __str__(self):
        return self.name
```

Also make sure `from django.utils.translation import gettext_lazy as _` is imported at top of file (likely already is — verify).

Inside the existing `Station` class, add the FK (place near other FKs, typically before `tags`):

```python
    region = models.ForeignKey(
        Region,
        verbose_name=_("region"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="stations",
    )
```

- [ ] **Step 4: Generate migration**

```bash
.venv/bin/python manage.py makemigrations stations --name add_region_and_station_fk
```

Expected: `apps/stations/migrations/0006_add_region_and_station_fk.py` created. Should contain `CreateModel(name='Region', ...)` and `AddField(model_name='station', name='region', ...)`.

- [ ] **Step 5: Run tests to verify pass**

```bash
.venv/bin/python -m pytest tests/test_topology_models.py -v
```

Expected: all 7 PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/stations/models.py apps/stations/migrations/0006_add_region_and_station_fk.py tests/test_topology_models.py
git commit -m "feat(stations): add Region model + Station.region FK (nullable, SET_NULL)"
```

---

## Task 4: StationAssignment + RegionAssignment models + migration 0007

**Files:**
- Modify: `apps/stations/models.py`
- Create: `apps/stations/migrations/0007_add_assignments.py` (generated)
- Modify: `tests/test_topology_models.py` (append assignment tests)

- [ ] **Step 1: Append failing tests**

Append to `tests/test_topology_models.py`:

```python
@pytest.mark.django_db
class TestStationAssignment:
    def _member(self):
        u = User.objects.create_user(username="hans", password="x")
        u.membership_level = User.MembershipLevel.MEMBER
        u.save(update_fields=["membership_level"])
        return u

    def test_role_choices(self):
        from apps.stations.models import StationAssignment
        assert StationAssignment.Role.ADMIN == "admin"
        assert StationAssignment.Role.MAINTAINER == "maintainer"

    def test_create_admin_assignment(self):
        from apps.stations.models import StationAssignment
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        a = StationAssignment.objects.create(
            user=self._member(), station=s,
            role=StationAssignment.Role.ADMIN,
        )
        assert a.assigned_at is not None

    def test_uniq_user_per_station(self):
        from apps.stations.models import StationAssignment
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        u = self._member()
        StationAssignment.objects.create(
            user=u, station=s, role=StationAssignment.Role.MAINTAINER,
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            StationAssignment.objects.create(
                user=u, station=s, role=StationAssignment.Role.ADMIN,
            )

    def test_uniq_admin_per_station(self):
        from apps.stations.models import StationAssignment
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        u1 = self._member()
        u2 = User.objects.create_user(username="franz", password="x")
        u2.membership_level = User.MembershipLevel.MEMBER
        u2.save(update_fields=["membership_level"])

        StationAssignment.objects.create(
            user=u1, station=s, role=StationAssignment.Role.ADMIN,
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            StationAssignment.objects.create(
                user=u2, station=s, role=StationAssignment.Role.ADMIN,
            )

    def test_multiple_maintainers_ok(self):
        from apps.stations.models import StationAssignment
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        u1 = self._member()
        u2 = User.objects.create_user(username="franz", password="x")
        u2.membership_level = User.MembershipLevel.MEMBER
        u2.save(update_fields=["membership_level"])

        StationAssignment.objects.create(
            user=u1, station=s, role=StationAssignment.Role.MAINTAINER,
        )
        StationAssignment.objects.create(
            user=u2, station=s, role=StationAssignment.Role.MAINTAINER,
        )
        assert s.assignments.count() == 2


@pytest.mark.django_db
class TestRegionAssignment:
    def _member(self):
        u = User.objects.create_user(username="lisa", password="x")
        u.membership_level = User.MembershipLevel.MEMBER
        u.save(update_fields=["membership_level"])
        return u

    def test_role_choices(self):
        from apps.stations.models import RegionAssignment
        assert RegionAssignment.Role.MANAGER == "manager"

    def test_create_manager_assignment(self):
        from apps.stations.models import RegionAssignment
        r = Region.objects.create(name="Tirol", slug="tirol")
        a = RegionAssignment.objects.create(
            user=self._member(), region=r,
            role=RegionAssignment.Role.MANAGER,
        )
        assert a.assigned_at is not None

    def test_uniq_user_role_per_region(self):
        from apps.stations.models import RegionAssignment
        r = Region.objects.create(name="Tirol", slug="tirol")
        u = self._member()
        RegionAssignment.objects.create(
            user=u, region=r, role=RegionAssignment.Role.MANAGER,
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            RegionAssignment.objects.create(
                user=u, region=r, role=RegionAssignment.Role.MANAGER,
            )

    def test_multiple_managers_per_region_ok(self):
        from apps.stations.models import RegionAssignment
        r = Region.objects.create(name="Tirol", slug="tirol")
        u1 = self._member()
        u2 = User.objects.create_user(username="lisa2", password="x")
        u2.membership_level = User.MembershipLevel.MEMBER
        u2.save(update_fields=["membership_level"])

        RegionAssignment.objects.create(
            user=u1, region=r, role=RegionAssignment.Role.MANAGER,
        )
        RegionAssignment.objects.create(
            user=u2, region=r, role=RegionAssignment.Role.MANAGER,
        )
        assert r.assignments.count() == 2
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/python -m pytest tests/test_topology_models.py -v
```

Expected: ImportError on `StationAssignment` / `RegionAssignment`.

- [ ] **Step 3: Add the two assignment models**

In `apps/stations/models.py`, add at the BOTTOM (after the existing models). They reference `Region`, `Station`, and the User model:

```python
from django.conf import settings
from django.db.models import Q


class StationAssignment(models.Model):
    """Per-user, per-station role assignment.

    Two roles: ADMIN (max 1 per station, the local "owner") and
    MAINTAINER (N per station, co-helpers). Applicant users cannot
    hold an assignment — enforced via clean()/save().
    """

    class Role(models.TextChoices):
        ADMIN      = "admin",      _("Station-Admin")
        MAINTAINER = "maintainer", _("Station-Maintainer")

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="station_assignments",
        verbose_name=_("user"),
    )
    station = models.ForeignKey(
        Station,
        on_delete=models.CASCADE,
        related_name="assignments",
        verbose_name=_("station"),
    )
    role = models.CharField(_("role"), max_length=12, choices=Role.choices)
    assigned_at = models.DateTimeField(_("assigned at"), auto_now_add=True)
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_station_assignments",
        verbose_name=_("assigned by"),
    )

    class Meta:
        verbose_name = _("station assignment")
        verbose_name_plural = _("station assignments")
        ordering = ["station", "role", "user"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "station"],
                name="uniq_user_per_station_assignment",
            ),
            models.UniqueConstraint(
                fields=["station"],
                condition=Q(role="admin"),
                name="uniq_admin_per_station",
            ),
        ]
        indexes = [
            models.Index(fields=["station", "role"]),
            models.Index(fields=["user"]),
        ]

    def __str__(self):
        return f"{self.user} → {self.station} ({self.get_role_display()})"


class RegionAssignment(models.Model):
    """Per-user, per-region role assignment. One role only: MANAGER."""

    class Role(models.TextChoices):
        MANAGER = "manager", _("Region-Manager")

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="region_assignments",
        verbose_name=_("user"),
    )
    region = models.ForeignKey(
        Region,
        on_delete=models.CASCADE,
        related_name="assignments",
        verbose_name=_("region"),
    )
    role = models.CharField(_("role"), max_length=10, choices=Role.choices)
    assigned_at = models.DateTimeField(_("assigned at"), auto_now_add=True)
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_region_assignments",
        verbose_name=_("assigned by"),
    )

    class Meta:
        verbose_name = _("region assignment")
        verbose_name_plural = _("region assignments")
        ordering = ["region", "user"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "region", "role"],
                name="uniq_user_role_per_region",
            ),
        ]
        indexes = [
            models.Index(fields=["region", "role"]),
            models.Index(fields=["user"]),
        ]

    def __str__(self):
        return f"{self.user} → {self.region} ({self.get_role_display()})"
```

- [ ] **Step 4: Generate migration**

```bash
.venv/bin/python manage.py makemigrations stations --name add_assignments
```

Expected: `apps/stations/migrations/0007_add_assignments.py` created with both `CreateModel` operations.

- [ ] **Step 5: Run tests to verify pass**

```bash
.venv/bin/python -m pytest tests/test_topology_models.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/stations/models.py apps/stations/migrations/0007_add_assignments.py tests/test_topology_models.py
git commit -m "feat(stations): StationAssignment + RegionAssignment models with constraints"
```

---

## Task 5: Applicant invariant on Assignments

**Files:**
- Modify: `apps/stations/models.py` (add clean()+save() to both Assignment models)
- Modify: `tests/test_topology_models.py` (append invariant tests)

- [ ] **Step 1: Append failing tests**

Append to `tests/test_topology_models.py`:

```python
@pytest.mark.django_db
class TestApplicantInvariant:
    def _applicant(self):
        u = User.objects.create_user(username="newbie", password="x")
        # Default level is APPLICANT — no need to set explicitly
        return u

    def test_applicant_cannot_be_station_admin(self):
        from apps.stations.models import StationAssignment
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        a = StationAssignment(
            user=self._applicant(), station=s,
            role=StationAssignment.Role.ADMIN,
        )
        with pytest.raises(ValidationError) as exc:
            a.save()
        assert "Vereins-Bewerber" in str(exc.value)

    def test_applicant_cannot_be_station_maintainer(self):
        from apps.stations.models import StationAssignment
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        a = StationAssignment(
            user=self._applicant(), station=s,
            role=StationAssignment.Role.MAINTAINER,
        )
        with pytest.raises(ValidationError):
            a.save()

    def test_applicant_cannot_be_region_manager(self):
        from apps.stations.models import RegionAssignment
        r = Region.objects.create(name="Tirol", slug="tirol")
        a = RegionAssignment(
            user=self._applicant(), region=r,
            role=RegionAssignment.Role.MANAGER,
        )
        with pytest.raises(ValidationError):
            a.save()

    def test_member_can_be_assigned(self):
        """Sanity: the invariant blocks Applicants only, not Members."""
        from apps.stations.models import StationAssignment
        u = self._applicant()
        u.membership_level = User.MembershipLevel.MEMBER
        u.save(update_fields=["membership_level"])
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        # Should not raise
        StationAssignment.objects.create(
            user=u, station=s, role=StationAssignment.Role.MAINTAINER,
        )
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/python -m pytest tests/test_topology_models.py::TestApplicantInvariant -v
```

Expected: 3 failures (no ValidationError raised — save succeeds).

- [ ] **Step 3: Add clean()/save() to both Assignment models**

In `apps/stations/models.py`, add at the top of the module:

```python
from django.core.exceptions import ValidationError
```

(if not already imported)

Add to `StationAssignment`:

```python
    def clean(self):
        super().clean()
        from apps.accounts.models import User  # avoid import cycle
        if self.user.membership_level == User.MembershipLevel.APPLICANT:
            raise ValidationError({
                "user": _(
                    "Vereins-Bewerber können keine Topology-Rolle haben. "
                    "Den User erst zu Vereins-Mitglied promoten."
                ),
            })

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)
```

Add the same `clean()` + `save()` to `RegionAssignment`.

- [ ] **Step 4: Run tests to verify pass**

```bash
.venv/bin/python -m pytest tests/test_topology_models.py -v
```

Expected: all PASS (including the 4 new invariant tests).

- [ ] **Step 5: Commit**

```bash
git add apps/stations/models.py tests/test_topology_models.py
git commit -m "feat(stations): enforce Applicant cannot hold Topology assignment (clean/save)"
```

---

## Task 6: AccountAuditLog model + migration 0008

**Files:**
- Modify: `apps/accounts/models.py`
- Create: `apps/accounts/migrations/0008_add_account_audit_log.py` (generated)
- Create: `tests/test_account_audit_log.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_account_audit_log.py`:

```python
"""Tests for AccountAuditLog model + log() helper."""

import pytest

from apps.accounts.models import AccountAuditLog, User


@pytest.mark.django_db
def test_event_type_choices():
    assert AccountAuditLog.EventType.MEMBERSHIP_PROMOTED == "membership_promoted"
    assert AccountAuditLog.EventType.MEMBERSHIP_DEMOTED == "membership_demoted"
    assert AccountAuditLog.EventType.REGION_ASSIGNMENT_CREATED == "region_assignment_created"
    assert AccountAuditLog.EventType.REGION_ASSIGNMENT_REVOKED == "region_assignment_revoked"
    assert AccountAuditLog.EventType.REGION_CREATED == "region_created"
    assert AccountAuditLog.EventType.REGION_UPDATED == "region_updated"
    assert AccountAuditLog.EventType.REGION_DELETED == "region_deleted"


@pytest.mark.django_db
def test_log_helper_creates_row():
    actor = User.objects.create_user(username="admin", password="x")
    target = User.objects.create_user(username="hans", password="x")
    entry = AccountAuditLog.log(
        event_type=AccountAuditLog.EventType.MEMBERSHIP_PROMOTED,
        actor=actor, target_user=target,
        message="applicant → member",
    )
    assert entry.pk is not None
    assert entry.actor == actor
    assert entry.target_user == target
    assert entry.created_at is not None


@pytest.mark.django_db
def test_str_format():
    entry = AccountAuditLog.log(
        event_type=AccountAuditLog.EventType.REGION_CREATED,
        message="created: Innviertel",
    )
    assert "Region Created" in str(entry)
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/python -m pytest tests/test_account_audit_log.py -v
```

Expected: ImportError on `AccountAuditLog`.

- [ ] **Step 3: Add AccountAuditLog to apps/accounts/models.py**

At the end of `apps/accounts/models.py`:

```python
from django.conf import settings


class AccountAuditLog(models.Model):
    """System-wide audit trail for account-management and topology events.

    Parallel to StationAuditLog (per-station) and SsoAuditLog (SSO/OIDC).
    The apps/audit/ listing view merges all three into a single feed.
    """

    class EventType(models.TextChoices):
        MEMBERSHIP_PROMOTED       = "membership_promoted",       _("Membership Promoted")
        MEMBERSHIP_DEMOTED        = "membership_demoted",        _("Membership Demoted")
        REGION_ASSIGNMENT_CREATED = "region_assignment_created", _("Region Assignment Created")
        REGION_ASSIGNMENT_REVOKED = "region_assignment_revoked", _("Region Assignment Revoked")
        REGION_CREATED            = "region_created",            _("Region Created")
        REGION_UPDATED            = "region_updated",            _("Region Updated")
        REGION_DELETED            = "region_deleted",            _("Region Deleted")

    event_type = models.CharField(
        _("event type"), max_length=32, choices=EventType.choices,
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="account_audit_logs_as_actor",
        verbose_name=_("actor"),
    )
    target_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="account_audit_logs_as_target",
        verbose_name=_("target user"),
    )
    region = models.ForeignKey(
        "stations.Region", null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_logs",
        verbose_name=_("region"),
    )
    message = models.TextField(_("message"), blank=True)
    ip_address = models.GenericIPAddressField(_("IP address"), null=True, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("account audit log")
        verbose_name_plural = _("account audit logs")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["-created_at"]),
            models.Index(fields=["event_type", "-created_at"]),
            models.Index(fields=["target_user", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.get_event_type_display()} @ {self.created_at}"

    @classmethod
    def log(cls, *, event_type, actor=None, target_user=None,
            region=None, message="", ip_address=None):
        return cls.objects.create(
            event_type=event_type, actor=actor, target_user=target_user,
            region=region, message=message, ip_address=ip_address,
        )
```

- [ ] **Step 4: Generate migration**

```bash
.venv/bin/python manage.py makemigrations accounts --name add_account_audit_log
```

Expected: `apps/accounts/migrations/0008_add_account_audit_log.py` created.

Note: migration number depends on what came before. If accounts already has 0006/0007 from other concurrent work, the new file gets the next available number. Adjust references in subsequent tasks if needed.

- [ ] **Step 5: Run tests to verify pass**

```bash
.venv/bin/python -m pytest tests/test_account_audit_log.py -v
```

Expected: 3 PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/accounts/models.py apps/accounts/migrations/0008_add_account_audit_log.py tests/test_account_audit_log.py
git commit -m "feat(accounts): AccountAuditLog model for membership + region events"
```

---

# Phase 2: Permission Helpers

## Task 7: User.is_internal + topology-helper methods

**Files:**
- Modify: `apps/accounts/models.py`
- Create: `tests/test_topology_permissions.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_topology_permissions.py`:

```python
"""Tests for User permission-helpers: is_internal + topology lookups."""

import pytest

from apps.accounts.models import User
from apps.stations.models import (
    Region, RegionAssignment, Station, StationAssignment,
)


def _user(level=User.MembershipLevel.MEMBER):
    u = User.objects.create_user(username=f"u{User.objects.count()}", password="x")
    u.membership_level = level
    u.save(update_fields=["membership_level"])
    return u


@pytest.mark.django_db
class TestIsInternal:
    def test_admin_is_internal(self):
        assert _user(User.MembershipLevel.ADMIN).is_internal is True

    def test_staff_is_internal(self):
        assert _user(User.MembershipLevel.STAFF).is_internal is True

    def test_member_is_not_internal(self):
        assert _user(User.MembershipLevel.MEMBER).is_internal is False

    def test_applicant_is_not_internal(self):
        assert _user(User.MembershipLevel.APPLICANT).is_internal is False


@pytest.mark.django_db
class TestIsStationAdmin:
    def test_returns_true_when_assignment_exists(self):
        u = _user()
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        StationAssignment.objects.create(
            user=u, station=s, role=StationAssignment.Role.ADMIN,
        )
        assert u.is_station_admin(s) is True

    def test_returns_false_when_only_maintainer(self):
        u = _user()
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        StationAssignment.objects.create(
            user=u, station=s, role=StationAssignment.Role.MAINTAINER,
        )
        assert u.is_station_admin(s) is False

    def test_returns_false_for_other_station(self):
        u = _user()
        s1 = Station.objects.create(name="OE5A", callsign="OE5A")
        s2 = Station.objects.create(name="OE5B", callsign="OE5B")
        StationAssignment.objects.create(
            user=u, station=s1, role=StationAssignment.Role.ADMIN,
        )
        assert u.is_station_admin(s2) is False


@pytest.mark.django_db
class TestIsStationMaintainer:
    def test_returns_true_when_assignment_exists(self):
        u = _user()
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        StationAssignment.objects.create(
            user=u, station=s, role=StationAssignment.Role.MAINTAINER,
        )
        assert u.is_station_maintainer(s) is True

    def test_returns_false_when_only_admin(self):
        u = _user()
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        StationAssignment.objects.create(
            user=u, station=s, role=StationAssignment.Role.ADMIN,
        )
        # is_station_admin only — not maintainer
        assert u.is_station_maintainer(s) is False


@pytest.mark.django_db
class TestIsRegionManager:
    def test_returns_true_when_assignment_exists(self):
        u = _user()
        r = Region.objects.create(name="Tirol", slug="tirol")
        RegionAssignment.objects.create(
            user=u, region=r, role=RegionAssignment.Role.MANAGER,
        )
        assert u.is_region_manager(r) is True

    def test_returns_false_for_other_region(self):
        u = _user()
        r1 = Region.objects.create(name="Tirol", slug="tirol")
        r2 = Region.objects.create(name="OÖ", slug="ooe")
        RegionAssignment.objects.create(
            user=u, region=r1, role=RegionAssignment.Role.MANAGER,
        )
        assert u.is_region_manager(r2) is False

    def test_returns_false_for_none_region(self):
        u = _user()
        assert u.is_region_manager(None) is False
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/python -m pytest tests/test_topology_permissions.py -v
```

Expected: AttributeError on `is_internal`, `is_station_admin`, etc.

- [ ] **Step 3: Add helpers to User**

In `apps/accounts/models.py`, add inside the `User` class (alongside the existing `is_admin` cached property):

```python
    @cached_property
    def is_internal(self):
        """True iff Vereins-Staff or Vereins-Admin.

        Replaces the pre-refactor ``is_staff_member``. Renamed because
        Django's built-in ``is_staff`` (admin-backend access) is a
        related but distinct concept — keeping both names increases
        confusion. ``is_internal`` reads cleanly at call sites:
        "is this user a member of the internal operations team?"
        """
        return self.membership_level in (
            self.MembershipLevel.STAFF,
            self.MembershipLevel.ADMIN,
        )

    def is_station_admin(self, station):
        return self.station_assignments.filter(
            station=station, role="admin",
        ).exists()

    def is_station_maintainer(self, station):
        return self.station_assignments.filter(
            station=station, role="maintainer",
        ).exists()

    def is_region_manager(self, region):
        if region is None:
            return False
        return self.region_assignments.filter(
            region=region, role="manager",
        ).exists()
```

Update `_invalidate_role_cache` to include `is_internal`:

```python
    @staticmethod
    def _invalidate_role_cache(user):
        """Delete cached role properties after groups/membership mutation."""
        for attr in ("is_admin", "is_internal", "is_operator",
                     "is_staff_member", "group_names"):
            user.__dict__.pop(attr, None)
```

(Note: keep the old attr names in this list — they are removed in Task 10 once call sites are migrated.)

- [ ] **Step 4: Run tests to verify pass**

```bash
.venv/bin/python -m pytest tests/test_topology_permissions.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/accounts/models.py tests/test_topology_permissions.py
git commit -m "feat(accounts): User.is_internal + is_station_admin/maintainer + is_region_manager"
```

---

## Task 8: can_administer_station / can_maintain_station / can_use_station

**Files:**
- Modify: `apps/accounts/models.py`
- Modify: `tests/test_topology_permissions.py` (append)

- [ ] **Step 1: Append failing tests**

Append to `tests/test_topology_permissions.py`:

```python
@pytest.mark.django_db
class TestCanAdministerStation:
    def test_admin_can_administer_any_station(self):
        u = _user(User.MembershipLevel.ADMIN)
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        assert u.can_administer_station(s) is True

    def test_staff_can_administer_any_station(self):
        u = _user(User.MembershipLevel.STAFF)
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        assert u.can_administer_station(s) is True

    def test_station_admin_can_administer_own_station(self):
        u = _user()
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        StationAssignment.objects.create(
            user=u, station=s, role=StationAssignment.Role.ADMIN,
        )
        assert u.can_administer_station(s) is True

    def test_station_admin_cannot_administer_other_station(self):
        u = _user()
        s1 = Station.objects.create(name="OE5A", callsign="OE5A")
        s2 = Station.objects.create(name="OE5B", callsign="OE5B")
        StationAssignment.objects.create(
            user=u, station=s1, role=StationAssignment.Role.ADMIN,
        )
        assert u.can_administer_station(s2) is False

    def test_region_manager_can_administer_stations_in_region(self):
        u = _user()
        r = Region.objects.create(name="Tirol", slug="tirol")
        s = Station.objects.create(name="OE5A", callsign="OE5A", region=r)
        RegionAssignment.objects.create(
            user=u, region=r, role=RegionAssignment.Role.MANAGER,
        )
        assert u.can_administer_station(s) is True

    def test_region_manager_cannot_administer_stations_in_other_region(self):
        u = _user()
        r1 = Region.objects.create(name="Tirol", slug="tirol")
        r2 = Region.objects.create(name="OÖ", slug="ooe")
        s = Station.objects.create(name="OE5A", callsign="OE5A", region=r2)
        RegionAssignment.objects.create(
            user=u, region=r1, role=RegionAssignment.Role.MANAGER,
        )
        assert u.can_administer_station(s) is False

    def test_member_without_assignment_cannot_administer(self):
        u = _user(User.MembershipLevel.MEMBER)
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        assert u.can_administer_station(s) is False

    def test_applicant_cannot_administer(self):
        u = _user(User.MembershipLevel.APPLICANT)
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        assert u.can_administer_station(s) is False


@pytest.mark.django_db
class TestCanMaintainStation:
    def test_includes_can_administer(self):
        u = _user(User.MembershipLevel.STAFF)
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        assert u.can_maintain_station(s) is True

    def test_includes_station_maintainer(self):
        u = _user()
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        StationAssignment.objects.create(
            user=u, station=s, role=StationAssignment.Role.MAINTAINER,
        )
        assert u.can_maintain_station(s) is True

    def test_member_without_assignment_cannot_maintain(self):
        u = _user(User.MembershipLevel.MEMBER)
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        assert u.can_maintain_station(s) is False


@pytest.mark.django_db
class TestCanUseStation:
    def test_applicant_cannot_use(self):
        u = _user(User.MembershipLevel.APPLICANT)
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        assert u.can_use_station(s) is False

    def test_member_can_use(self):
        u = _user(User.MembershipLevel.MEMBER)
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        assert u.can_use_station(s) is True

    def test_staff_can_use(self):
        u = _user(User.MembershipLevel.STAFF)
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        assert u.can_use_station(s) is True

    def test_admin_can_use(self):
        u = _user(User.MembershipLevel.ADMIN)
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        assert u.can_use_station(s) is True
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/python -m pytest tests/test_topology_permissions.py -v
```

Expected: AttributeError on `can_administer_station` etc.

- [ ] **Step 3: Add the composed permission methods to User**

In `apps/accounts/models.py`, add inside the `User` class (after the topology lookup methods from Task 7):

```python
    def can_administer_station(self, station):
        """Full operative authority on `station`:
        Vereins-Admin OR Vereins-Staff OR Station-Admin of `station`
        OR Region-Manager of station.region.
        """
        if self.is_internal:
            return True
        if self.is_station_admin(station):
            return True
        if self.is_region_manager(station.region):
            return True
        return False

    def can_maintain_station(self, station):
        """can_administer_station OR Station-Maintainer of `station`.
        Lower bar than administer: maintenance + operational acks,
        but not structural changes (image release, station rename).
        """
        if self.can_administer_station(station):
            return True
        return self.is_station_maintainer(station)

    def can_use_station(self, station):
        """Future hook for radio operation (Funken über die Station).

        Today: every non-Applicant user passes. The Funk-Stack does not
        yet exist; the permission is defined now so its consumers can
        be written against a stable contract. Per-station restriction
        (e.g., only Region-Members may funken on Region-Stations) can
        be added later without changing the signature.
        """
        return self.membership_level != self.MembershipLevel.APPLICANT
```

- [ ] **Step 4: Run tests to verify pass**

```bash
.venv/bin/python -m pytest tests/test_topology_permissions.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/accounts/models.py tests/test_topology_permissions.py
git commit -m "feat(accounts): can_administer_station / can_maintain_station / can_use_station"
```

---

# Phase 3: Refactor existing helpers + call sites (atomic)

## Task 9: Refactor User.is_admin to read membership_level + conftest fixtures

**Files:**
- Modify: `apps/accounts/models.py:60-62` (`is_admin` property)
- Modify: `tests/conftest.py:71-96` (`_user_in_group` helper + 3 fixtures)

This task is atomic — changing `is_admin` semantics without changing fixtures would break every test using `admin_user`/`operator_user`/`member_user`.

- [ ] **Step 1: Read existing conftest to know exact lines**

```bash
grep -n "_user_in_group\|admin_user\|operator_user\|member_user" tests/conftest.py | head
```

- [ ] **Step 2: Refactor is_admin in User model**

In `apps/accounts/models.py`, replace the existing `is_admin` property:

```python
    @cached_property
    def is_admin(self):
        """True iff Vereins-Admin (membership_level=ADMIN).

        Backwards-compat: kept the same name as the pre-refactor
        group-based check — call-site semantics are unchanged.
        """
        return self.membership_level == self.MembershipLevel.ADMIN
```

- [ ] **Step 3: Refactor `_user_in_group` in conftest.py**

Replace the existing `_user_in_group` helper and the three role fixtures (`admin_user`, `operator_user`, `member_user`) with:

```python
def _user_with_level(username, password, level):
    """Create a user with the given membership_level.

    Replaces the pre-PR _user_in_group helper. The group-based
    test-setup of the old role model is gone; the same role
    semantics are now expressed via membership_level.
    """
    from apps.accounts.models import User
    user = User.objects.create_user(username=username, password=password)
    user.membership_level = level
    user.save(update_fields=["membership_level"])
    User._invalidate_role_cache(user)
    return user


@pytest.fixture
def admin_user(db):
    from apps.accounts.models import User
    return _user_with_level("admin", "testpass123", User.MembershipLevel.ADMIN)


@pytest.fixture
def operator_user(db):
    """Kept fixture name for test-suite continuity. The membership
    level is STAFF (formerly the 'operator' group).
    """
    from apps.accounts.models import User
    return _user_with_level("operator", "testpass123", User.MembershipLevel.STAFF)


@pytest.fixture
def member_user(db):
    from apps.accounts.models import User
    return _user_with_level("member", "testpass123", User.MembershipLevel.MEMBER)


@pytest.fixture
def applicant_user(db):
    from apps.accounts.models import User
    return _user_with_level("applicant", "testpass123", User.MembershipLevel.APPLICANT)
```

Also remove `from django.contrib.auth.models import Group` from conftest.py if it's no longer used elsewhere in the file.

- [ ] **Step 4: Run the full test suite**

```bash
.venv/bin/python -m pytest -q 2>&1 | tail -20
```

Expected: all PASS. The is_admin call sites (across the codebase) still pass because the new is_admin semantics match the old behavior for users who were in the `admin` group (now have membership_level=ADMIN).

If test failures surface — likely because some test directly assigns Django groups expecting `is_admin` to read groups — fix those individual tests by setting membership_level instead.

- [ ] **Step 5: Commit**

```bash
git add apps/accounts/models.py tests/conftest.py
git commit -m "refactor(accounts): User.is_admin reads membership_level; fixtures follow

The pre-PR is_admin was a Django-group-membership query. Now it reads
the new membership_level field directly. Semantics identical for all
users seeded by migration 0005 (admin-group -> ADMIN-level).

conftest fixtures admin_user/operator_user/member_user are switched to
set membership_level directly. The pre-PR _user_in_group helper that
manipulated Django groups is gone — the group-based role-model is on
its way out (migration 0009 deletes the groups themselves)."
```

---

## Task 10: Remove legacy is_operator + is_staff_member + group_names; refactor all 13 call sites

**Files:**
- Modify: `apps/accounts/models.py` (remove 3 properties + clean up _invalidate_role_cache)
- Modify: `apps/stations/views.py:45,106`
- Modify: `apps/firmware/views.py:27`
- Modify: `apps/monitoring/views.py:19,26`
- Modify: `apps/audit/views.py:161`
- Modify: `apps/deployments/consumers.py:31`
- Modify: `apps/tunnel/views.py:24`
- Modify: `apps/tunnel/consumers.py:35`
- Modify: `apps/api/views.py:130`
- Modify: `apps/accounts/views.py:19`
- Modify: `apps/sso/views.py:50`

- [ ] **Step 1: Inventory the current call sites**

```bash
grep -rn "is_operator\|is_staff_member\|group_names\|groups__name=\"admin\"\|groups__name='admin'" apps/ --include="*.py" | grep -v __pycache__ | grep -v migrations
```

Expected output covers ~13 sites listed in the spec Section 6.

- [ ] **Step 2: Refactor each call site**

Per the spec table, with exact replacements:

| File | Before | After |
|---|---|---|
| `apps/stations/views.py:45` | `return self.request.user.is_staff_member` | `return self.request.user.is_internal` |
| `apps/stations/views.py:106` | `if self.request.user.is_admin:` | unchanged (semantics preserved) |
| `apps/firmware/views.py:27` | `self.request.user.is_admin or self.request.user.is_operator` | `self.request.user.is_internal` |
| `apps/monitoring/views.py:19` | `return self.request.user.is_admin` | unchanged |
| `apps/monitoring/views.py:26` | `return self.request.user.is_staff_member` | `return self.request.user.is_internal` |
| `apps/audit/views.py:161` | `User.objects.filter(groups__name="admin").distinct().order_by("username")` | `User.objects.filter(membership_level=User.MembershipLevel.ADMIN).order_by("username")` |
| `apps/deployments/consumers.py:31` | `is_staff_member = await database_sync_to_async(lambda: user.is_staff_member)()` | `is_internal = await database_sync_to_async(lambda: user.is_internal)()` (also update the variable name on line 32) |
| `apps/tunnel/views.py:24` | `... and request.user.is_staff_member` | `... and request.user.is_internal` |
| `apps/tunnel/consumers.py:35` | `is_staff = await database_sync_to_async(lambda: user.is_staff_member)()` | `is_internal = await database_sync_to_async(lambda: user.is_internal)()` (also update conditional below) |
| `apps/api/views.py:130` | `if not request.user.is_staff_member:` | `if not request.user.is_internal:` |
| `apps/accounts/views.py:19` | `return self.request.user.is_admin` | unchanged |
| `apps/sso/views.py:50` | `return getattr(self.request.user, "is_admin", False)` | unchanged |

For `apps/monitoring/notifications.py` (both 16 and 108) — the notification helper changes substantially in Task 12, so leave it for now. Just update the membership_level filter at line 16:

```python
# apps/monitoring/notifications.py:16 — temporary state, replaced fully in Task 12
admins = User.objects.filter(
    membership_level=User.MembershipLevel.ADMIN,
).distinct()
```

Same temporary update for line 108.

- [ ] **Step 3: Remove the legacy properties from User model**

In `apps/accounts/models.py`, delete:
- `is_operator` (lines 64-66 approx)
- `is_staff_member` (lines 68-71 approx)
- `group_names` (lines 73-81 approx)

Update `_invalidate_role_cache` to remove the dropped attrs:

```python
    @staticmethod
    def _invalidate_role_cache(user):
        """Delete cached role properties after membership_level mutation."""
        for attr in ("is_admin", "is_internal"):
            user.__dict__.pop(attr, None)
```

Also update the User-class-level docstring to reflect the new state (delete the mention of `is_operator`/`is_staff_member`/`group_names`; explain that the role model is now `membership_level`-based + topology assignments).

- [ ] **Step 4: Run full test suite**

```bash
.venv/bin/python -m pytest -q 2>&1 | tail -20
```

Expected: all PASS.

If any test fails because it directly references `is_operator`, `is_staff_member`, or `group_names`: refactor that test to use `is_internal` or `membership_level` directly.

- [ ] **Step 5: Lint**

```bash
ruff format . && ruff check .
```

- [ ] **Step 6: Commit**

```bash
git add apps/ tests/
git commit -m "refactor: replace is_operator/is_staff_member/group_names with is_internal

13 call sites across apps/{stations,firmware,monitoring,audit,
deployments,tunnel,api,accounts,sso} switched to the new
membership_level-aware helpers. is_operator and is_staff_member
removed from User (term collides with amateur-radio 'operator';
is_staff_member is too close to Django's built-in is_staff).
group_names removed (no Django-group role-model anymore)."
```

---

# Phase 4-9 — Deferred to PR-2

> **Stop here for PR-1 execution.** The phases below are kept in this document as reference for the architecture — the PR-2 plan will be written after PR-1 merges, when the actual signatures and template paths from PR-1 are in-tree and can be cited exactly instead of forecast. Do NOT execute Tasks 11-22 in this session.

---

# Phase 4: Notification Routing (PR-2)

## Task 11: recipients_for_station_alert helper

**Files:**
- Create: `apps/monitoring/recipients.py`
- Create: `tests/test_alert_recipients.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_alert_recipients.py`:

```python
"""Tests for recipients_for_station_alert.

Pins the routing contract: who gets an alert email for a station.
"""

import pytest

from apps.accounts.models import User
from apps.monitoring.recipients import recipients_for_station_alert
from apps.stations.models import (
    Region, RegionAssignment, Station, StationAssignment,
)


def _user(level, email="x@example.com", username=None):
    username = username or f"u{User.objects.count()}"
    u = User.objects.create_user(username=username, password="x", email=email)
    u.membership_level = level
    u.save(update_fields=["membership_level"])
    return u


@pytest.mark.django_db
class TestRecipientsForStationAlert:
    def test_admin_always_recipient(self):
        admin = _user(User.MembershipLevel.ADMIN)
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        assert admin in list(recipients_for_station_alert(s))

    def test_region_manager_in_set_for_own_region(self):
        mgr = _user(User.MembershipLevel.MEMBER)
        r = Region.objects.create(name="Tirol", slug="tirol")
        RegionAssignment.objects.create(user=mgr, region=r,
                                        role=RegionAssignment.Role.MANAGER)
        s = Station.objects.create(name="OE5A", callsign="OE5A", region=r)
        assert mgr in list(recipients_for_station_alert(s))

    def test_region_manager_not_in_set_for_other_region(self):
        mgr = _user(User.MembershipLevel.MEMBER)
        r1 = Region.objects.create(name="Tirol", slug="tirol")
        r2 = Region.objects.create(name="OÖ", slug="ooe")
        RegionAssignment.objects.create(user=mgr, region=r1,
                                        role=RegionAssignment.Role.MANAGER)
        s = Station.objects.create(name="OE5A", callsign="OE5A", region=r2)
        assert mgr not in list(recipients_for_station_alert(s))

    def test_station_admin_in_set(self):
        u = _user(User.MembershipLevel.MEMBER)
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        StationAssignment.objects.create(user=u, station=s,
                                         role=StationAssignment.Role.ADMIN)
        assert u in list(recipients_for_station_alert(s))

    def test_station_admin_not_in_set_for_other_station(self):
        u = _user(User.MembershipLevel.MEMBER)
        s1 = Station.objects.create(name="OE5A", callsign="OE5A")
        s2 = Station.objects.create(name="OE5B", callsign="OE5B")
        StationAssignment.objects.create(user=u, station=s1,
                                         role=StationAssignment.Role.ADMIN)
        assert u not in list(recipients_for_station_alert(s2))

    def test_station_maintainer_in_set(self):
        u = _user(User.MembershipLevel.MEMBER)
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        StationAssignment.objects.create(user=u, station=s,
                                         role=StationAssignment.Role.MAINTAINER)
        assert u in list(recipients_for_station_alert(s))

    def test_staff_not_recipient_without_topology(self):
        staff = _user(User.MembershipLevel.STAFF)
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        assert staff not in list(recipients_for_station_alert(s))

    def test_member_without_assignments_not_recipient(self):
        m = _user(User.MembershipLevel.MEMBER)
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        assert m not in list(recipients_for_station_alert(s))

    def test_applicant_never_recipient(self):
        a = _user(User.MembershipLevel.APPLICANT)
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        # Applicants cannot hold assignments by invariant (Task 5);
        # the recipient query excludes them as defense-in-depth.
        assert a not in list(recipients_for_station_alert(s))

    def test_dedup_same_user_multiple_roles(self):
        u = _user(User.MembershipLevel.ADMIN)
        r = Region.objects.create(name="Tirol", slug="tirol")
        s = Station.objects.create(name="OE5A", callsign="OE5A", region=r)
        RegionAssignment.objects.create(user=u, region=r,
                                        role=RegionAssignment.Role.MANAGER)
        StationAssignment.objects.create(user=u, station=s,
                                         role=StationAssignment.Role.ADMIN)
        recipients = list(recipients_for_station_alert(s))
        assert recipients.count(u) == 1

    def test_inactive_user_excluded(self):
        admin = _user(User.MembershipLevel.ADMIN)
        admin.is_active = False
        admin.save(update_fields=["is_active"])
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        assert admin not in list(recipients_for_station_alert(s))

    def test_user_without_email_excluded(self):
        admin = _user(User.MembershipLevel.ADMIN, email="")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        assert admin not in list(recipients_for_station_alert(s))

    def test_no_region_only_admin_and_station_assignments(self):
        admin = _user(User.MembershipLevel.ADMIN, username="admin")
        # An orphan region-manager exists but the station has no region
        mgr = _user(User.MembershipLevel.MEMBER, username="mgr")
        r = Region.objects.create(name="Tirol", slug="tirol")
        RegionAssignment.objects.create(user=mgr, region=r,
                                        role=RegionAssignment.Role.MANAGER)
        s = Station.objects.create(name="OE5A", callsign="OE5A", region=None)
        rcp = list(recipients_for_station_alert(s))
        assert admin in rcp
        assert mgr not in rcp
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/python -m pytest tests/test_alert_recipients.py -v
```

Expected: ImportError on `apps.monitoring.recipients`.

- [ ] **Step 3: Create the helper module**

Create `apps/monitoring/recipients.py`:

```python
"""Resolve email recipients for a station alert.

Single-responsibility helper. Lives in a dedicated module so the
notification dispatch in apps/monitoring/notifications.py stays
focused on SMTP delivery, and the routing logic is unit-testable in
isolation from email-backend mocking.

Routing contract (see docs/superpowers/specs/2026-06-05-membership-
levels-and-topology-roles-design.md §4.7):
  - Vereins-Admins (membership_level=ADMIN), vereinsweit
  - Region-Manager der zugeh. Region (sofern station.region gesetzt)
  - Station-Admin dieser Station
  - Station-Maintainer dieser Station

Excludes Vereins-Staff (operative role, not escalation inbox),
Applicants (defense-in-depth — invariant blocks them anyway), and
inactive / no-email users.
"""

from django.contrib.auth import get_user_model
from django.db.models import Q

User = get_user_model()


def recipients_for_station_alert(station):
    q = Q(membership_level=User.MembershipLevel.ADMIN)

    if station.region_id is not None:
        q |= Q(
            region_assignments__region_id=station.region_id,
            region_assignments__role="manager",
        )

    q |= Q(
        station_assignments__station=station,
        station_assignments__role__in=["admin", "maintainer"],
    )

    return (
        User.objects
        .filter(q)
        .exclude(email="")
        .exclude(is_active=False)
        .exclude(membership_level=User.MembershipLevel.APPLICANT)
        .distinct()
    )
```

- [ ] **Step 4: Run tests to verify pass**

```bash
.venv/bin/python -m pytest tests/test_alert_recipients.py -v
```

Expected: all 13 PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/monitoring/recipients.py tests/test_alert_recipients.py
git commit -m "feat(monitoring): recipients_for_station_alert helper

Replaces the hardcoded User.objects.filter(groups__name='admin')
in _send_email_notification (wired in Task 12). Routes to:
Vereins-Admin OR Region-Manager-of-region OR Station-Admin OR
Station-Maintainer. Excludes Staff, Applicant, inactive, and
no-email users."
```

---

## Task 12: Wire recipients helper into _send_email_notification + test-email-to-self

**Files:**
- Modify: `apps/monitoring/notifications.py`
- Modify: `apps/monitoring/views.py` (TestNotificationView passes request.user)
- Create: `tests/test_notification_dispatch.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_notification_dispatch.py`:

```python
"""Tests for the notification dispatch wiring.

Verifies _send_email_notification routes through recipients_for_station_alert
and that the test-email endpoint sends to the requesting user only.
"""

import pytest
from django.core import mail
from django.urls import reverse

from apps.accounts.models import User
from apps.monitoring.models import Alert, AlertRule
from apps.monitoring.notifications import send_alert_notifications
from apps.stations.models import Station, StationAssignment


def _user(level, email, username):
    u = User.objects.create_user(username=username, password="x", email=email)
    u.membership_level = level
    u.save(update_fields=["membership_level"])
    return u


@pytest.mark.django_db
def test_alert_email_goes_to_station_admin_and_vereins_admin(settings):
    settings.ALERT_EMAIL_ENABLED = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

    admin = _user(User.MembershipLevel.ADMIN, "admin@x", "admin")
    station_admin = _user(User.MembershipLevel.MEMBER, "franz@x", "franz")

    s = Station.objects.create(name="OE5A", callsign="OE5A")
    StationAssignment.objects.create(
        user=station_admin, station=s,
        role=StationAssignment.Role.ADMIN,
    )
    rule = AlertRule.objects.get(alert_type=AlertRule.AlertType.STATION_OFFLINE)
    alert = Alert.objects.create(
        station=s, alert_rule=rule, severity="critical",
        title="Test", message="m",
    )

    send_alert_notifications(alert)

    assert len(mail.outbox) == 1
    sent = mail.outbox[0]
    assert set(sent.to) == {"admin@x", "franz@x"}


@pytest.mark.django_db
def test_test_email_goes_only_to_requesting_admin(client, settings):
    settings.ALERT_EMAIL_ENABLED = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

    admin1 = _user(User.MembershipLevel.ADMIN, "admin1@x", "admin1")
    _user(User.MembershipLevel.ADMIN, "admin2@x", "admin2")

    client.force_login(admin1)
    response = client.post(reverse("monitoring:test_email"))
    assert response.status_code == 200
    assert response.json()["success"] is True

    assert len(mail.outbox) == 1
    assert list(mail.outbox[0].to) == ["admin1@x"]


@pytest.mark.django_db
def test_no_recipients_logs_warning_and_does_not_send(settings, caplog):
    import logging
    settings.ALERT_EMAIL_ENABLED = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

    s = Station.objects.create(name="OE5A", callsign="OE5A")
    rule = AlertRule.objects.get(alert_type=AlertRule.AlertType.STATION_OFFLINE)
    alert = Alert.objects.create(
        station=s, alert_rule=rule, severity="critical",
        title="Test", message="m",
    )

    with caplog.at_level(logging.WARNING, logger="apps.monitoring.notifications"):
        send_alert_notifications(alert)

    assert len(mail.outbox) == 0
    assert any("no recipients" in rec.message.lower() for rec in caplog.records)
```

- [ ] **Step 2: Refactor `_send_email_notification`**

In `apps/monitoring/notifications.py`, replace the function body:

```python
def _send_email_notification(alert, recipients_qs=None):
    """Send alert email through the topology-based recipient set.

    `recipients_qs` is optional, defaults to recipients_for_station_alert
    for the alert's station. The override is used by the test-email
    path to scope to a single user.
    """
    if recipients_qs is None:
        from apps.monitoring.recipients import recipients_for_station_alert
        recipients_qs = recipients_for_station_alert(alert.station)

    recipient_list = list(recipients_qs.values_list("email", flat=True))
    if not recipient_list:
        region = alert.station.region.name if alert.station.region else None
        logger.warning(
            "Alert %s on station %s (region=%s) has no recipients. "
            "Configure Station-Admin, Region-Manager, or ensure a "
            "Vereins-Admin has an email set.",
            alert.pk, alert.station.name, region,
        )
        return

    subject = f"[OE5XRX] {alert.get_severity_display()}: {alert.title}"
    body = (
        f"Station: {alert.station.name}\n"
        f"Severity: {alert.get_severity_display()}\n"
        f"Alert: {alert.title}\n\n"
        f"{alert.message}\n\n"
        f"Time: {alert.created_at}\n"
    )
    try:
        send_mail(
            subject=subject, message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=recipient_list,
            fail_silently=False,
        )
        logger.info("Alert email sent to %d recipient(s).", len(recipient_list))
    except Exception:
        logger.exception("Failed to send alert email.")
```

Update `send_alert_notifications(alert)` to drop the admin-list pre-fetch:

```python
def send_alert_notifications(alert):
    """Dispatch alert via configured channels."""
    if getattr(settings, "ALERT_EMAIL_ENABLED", False):
        _send_email_notification(alert)
    if getattr(settings, "ALERT_TELEGRAM_ENABLED", False):
        _send_telegram_notification(alert)
```

Update `_test_email()` signature to accept the requesting user:

```python
def _test_email(requesting_user=None):
    """Send a test email to verify SMTP wiring.

    If `requesting_user` is given, the mail goes only to that user's
    email (= the admin who clicked the Send-Test-Email button). This
    avoids cross-notification noise when several admins are configured.
    """
    if not getattr(settings, "ALERT_EMAIL_ENABLED", False):
        return False, "Email notifications are not enabled (ALERT_EMAIL_ENABLED)."

    if requesting_user is not None and requesting_user.email:
        recipient_list = [requesting_user.email]
    else:
        from apps.accounts.models import User as UserModel
        recipient_list = list(
            UserModel.objects.filter(
                membership_level=UserModel.MembershipLevel.ADMIN
            ).exclude(email="").values_list("email", flat=True)
        )

    if not recipient_list:
        return False, "No recipient — set your user's email or configure a Vereins-Admin with email."

    try:
        send_mail(
            subject="[OE5XRX] Test notification",
            message=(
                f"This is a test notification from OE5XRX Station Manager.\n"
                f"Sent at: {timezone.now()}\n\n"
                f"If you received this, email notifications are working correctly."
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=recipient_list,
            fail_silently=False,
        )
        return True, ""
    except Exception as e:
        return False, str(e)
```

Update `send_test_notification`:

```python
def send_test_notification(channel, requesting_user=None):
    if channel == "email":
        return _test_email(requesting_user=requesting_user)
    elif channel == "telegram":
        return _test_telegram()
    return False, f"Unknown channel: {channel}"
```

- [ ] **Step 3: Wire requesting_user in the view**

In `apps/monitoring/views.py`, modify `TestNotificationView`:

```python
class TestNotificationView(AdminRequiredMixin, View):
    def post(self, request, channel):
        success, error_message = send_test_notification(
            channel, requesting_user=request.user,
        )
        return JsonResponse({"success": success, "error": error_message})
```

- [ ] **Step 4: Run tests to verify pass**

```bash
.venv/bin/python -m pytest tests/test_notification_dispatch.py -v
```

Expected: 3 PASS.

- [ ] **Step 5: Run the full suite as regression-guard**

```bash
.venv/bin/python -m pytest -q 2>&1 | tail -5
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/monitoring/notifications.py apps/monitoring/views.py tests/test_notification_dispatch.py
git commit -m "feat(monitoring): wire recipients helper + scope test-email to requester

_send_email_notification now resolves recipients via the topology
helper. send_test_notification(email) targets only the requesting
admin (resolves the multi-admin cross-notification annoyance)."
```

---

# Phase 5: Audit Log Integration

## Task 13: Add StationAuditLog event types + emission helpers

**Files:**
- Modify: `apps/stations/models.py` (extend StationAuditLog.EventType + emission helpers)
- Create: `tests/test_audit_log_emission.py`

- [ ] **Step 1: Inspect existing StationAuditLog.EventType**

```bash
grep -A 20 "class EventType" apps/stations/models.py | head -25
```

Note the existing choices so the new ones append cleanly.

- [ ] **Step 2: Append new event types**

In `apps/stations/models.py`, inside `StationAuditLog.EventType`, add three new choices:

```python
        STATION_ASSIGNMENT_CREATED = "station_assignment_created", _("Station Assignment Created")
        STATION_ASSIGNMENT_REVOKED = "station_assignment_revoked", _("Station Assignment Revoked")
        STATION_REGION_CHANGED     = "station_region_changed",     _("Station Region Changed")
```

(Adjust placement to keep the enum logically grouped.)

- [ ] **Step 3: Write failing test**

Create `tests/test_audit_log_emission.py`:

```python
"""Tests that each topology mutation emits the right audit-log entry."""

import pytest

from apps.accounts.models import AccountAuditLog, User
from apps.stations.models import (
    Region, RegionAssignment, Station, StationAssignment, StationAuditLog,
)


def _admin():
    u = User.objects.create_user(username="admin", password="x", email="a@x")
    u.membership_level = User.MembershipLevel.ADMIN
    u.save(update_fields=["membership_level"])
    return u


def _member(name):
    u = User.objects.create_user(username=name, password="x", email=f"{name}@x")
    u.membership_level = User.MembershipLevel.MEMBER
    u.save(update_fields=["membership_level"])
    return u


# Promotion / Demotion emission is exercised via the view in Task 16.
# Here we test the topology-mutation emission paths.


@pytest.mark.django_db
def test_station_assignment_create_emits_audit_log():
    admin = _admin()
    franz = _member("franz")
    s = Station.objects.create(name="OE5A", callsign="OE5A")
    StationAssignment.objects.create(
        user=franz, station=s,
        role=StationAssignment.Role.ADMIN,
        assigned_by=admin,
    )
    entry = StationAuditLog.objects.filter(
        event_type=StationAuditLog.EventType.STATION_ASSIGNMENT_CREATED,
        station=s,
    ).first()
    assert entry is not None
    assert entry.user == franz
    assert "admin" in entry.message.lower()


@pytest.mark.django_db
def test_station_assignment_revoke_emits_audit_log():
    admin = _admin()
    franz = _member("franz")
    s = Station.objects.create(name="OE5A", callsign="OE5A")
    a = StationAssignment.objects.create(
        user=franz, station=s,
        role=StationAssignment.Role.MAINTAINER,
        assigned_by=admin,
    )
    a.delete()  # Triggers post_delete signal
    entry = StationAuditLog.objects.filter(
        event_type=StationAuditLog.EventType.STATION_ASSIGNMENT_REVOKED,
        station=s,
    ).first()
    assert entry is not None


@pytest.mark.django_db
def test_station_region_change_emits_audit_log():
    s = Station.objects.create(name="OE5A", callsign="OE5A")
    r1 = Region.objects.create(name="Tirol", slug="tirol")
    r2 = Region.objects.create(name="OÖ", slug="ooe")
    s.region = r1
    s.save()
    s.region = r2
    s.save()
    # At least one CHANGED event present
    entry = StationAuditLog.objects.filter(
        event_type=StationAuditLog.EventType.STATION_REGION_CHANGED,
        station=s,
    ).first()
    assert entry is not None
    assert "Tirol" in entry.message or "OÖ" in entry.message


@pytest.mark.django_db
def test_region_assignment_create_emits_audit_log():
    admin = _admin()
    lisa = _member("lisa")
    r = Region.objects.create(name="Tirol", slug="tirol")
    RegionAssignment.objects.create(
        user=lisa, region=r,
        role=RegionAssignment.Role.MANAGER,
        assigned_by=admin,
    )
    entry = AccountAuditLog.objects.filter(
        event_type=AccountAuditLog.EventType.REGION_ASSIGNMENT_CREATED,
        target_user=lisa,
        region=r,
    ).first()
    assert entry is not None


@pytest.mark.django_db
def test_region_assignment_revoke_emits_audit_log():
    admin = _admin()
    lisa = _member("lisa")
    r = Region.objects.create(name="Tirol", slug="tirol")
    a = RegionAssignment.objects.create(
        user=lisa, region=r,
        role=RegionAssignment.Role.MANAGER,
        assigned_by=admin,
    )
    a.delete()
    entry = AccountAuditLog.objects.filter(
        event_type=AccountAuditLog.EventType.REGION_ASSIGNMENT_REVOKED,
        target_user=lisa,
    ).first()
    assert entry is not None


@pytest.mark.django_db
def test_region_create_update_delete_emits_audit_log():
    r = Region.objects.create(name="Innviertel", slug="innv")
    assert AccountAuditLog.objects.filter(
        event_type=AccountAuditLog.EventType.REGION_CREATED, region=r,
    ).exists()

    r.name = "Innviertel-West"
    r.save()
    assert AccountAuditLog.objects.filter(
        event_type=AccountAuditLog.EventType.REGION_UPDATED, region=r,
    ).exists()

    r_id = r.id
    r.delete()
    # After delete, region FK becomes NULL; query by event_type only
    assert AccountAuditLog.objects.filter(
        event_type=AccountAuditLog.EventType.REGION_DELETED,
    ).exists()
```

- [ ] **Step 4: Run to verify failure**

```bash
.venv/bin/python -m pytest tests/test_audit_log_emission.py -v
```

Expected: failures across all tests (no signals wired yet).

- [ ] **Step 5: Add signal handlers**

Create `apps/stations/signals.py`:

```python
"""Signal handlers that emit audit-log entries for topology mutations.

The signal-based design keeps audit emission decoupled from view code:
direct ORM mutations (Django Admin, migrations, shell) are also
captured. Per spec §4.6, the data migration 0005 itself does NOT emit
entries — no signal handlers are registered yet during migrations.
"""

from django.db.models.signals import post_save, post_delete, pre_save
from django.dispatch import receiver

from apps.accounts.models import AccountAuditLog
from apps.stations.models import (
    Region, RegionAssignment, Station, StationAssignment, StationAuditLog,
)


@receiver(post_save, sender=StationAssignment)
def _on_station_assignment_save(sender, instance, created, **kwargs):
    if not created:
        return
    StationAuditLog.objects.create(
        event_type=StationAuditLog.EventType.STATION_ASSIGNMENT_CREATED,
        station=instance.station,
        user=instance.assigned_by,
        message=f"{instance.user} → {instance.get_role_display()}",
    )


@receiver(post_delete, sender=StationAssignment)
def _on_station_assignment_delete(sender, instance, **kwargs):
    StationAuditLog.objects.create(
        event_type=StationAuditLog.EventType.STATION_ASSIGNMENT_REVOKED,
        station=instance.station,
        user=None,
        message=f"{instance.user} ({instance.get_role_display()}) entfernt",
    )


@receiver(pre_save, sender=Station)
def _on_station_pre_save(sender, instance, **kwargs):
    if not instance.pk:
        return
    try:
        old = Station.objects.only("region_id").get(pk=instance.pk)
    except Station.DoesNotExist:
        return
    if old.region_id != instance.region_id:
        instance._pending_region_change = (old.region_id, instance.region_id)


@receiver(post_save, sender=Station)
def _on_station_save(sender, instance, created, **kwargs):
    change = getattr(instance, "_pending_region_change", None)
    if not change:
        return
    old_id, new_id = change
    old_name = Region.objects.filter(pk=old_id).values_list("name", flat=True).first() if old_id else None
    new_name = Region.objects.filter(pk=new_id).values_list("name", flat=True).first() if new_id else None
    StationAuditLog.objects.create(
        event_type=StationAuditLog.EventType.STATION_REGION_CHANGED,
        station=instance,
        user=None,
        message=f"{old_name or '∅'} → {new_name or '∅'}",
    )
    del instance._pending_region_change


@receiver(post_save, sender=RegionAssignment)
def _on_region_assignment_save(sender, instance, created, **kwargs):
    if not created:
        return
    AccountAuditLog.log(
        event_type=AccountAuditLog.EventType.REGION_ASSIGNMENT_CREATED,
        actor=instance.assigned_by,
        target_user=instance.user,
        region=instance.region,
        message=f"role={instance.get_role_display()}",
    )


@receiver(post_delete, sender=RegionAssignment)
def _on_region_assignment_delete(sender, instance, **kwargs):
    AccountAuditLog.log(
        event_type=AccountAuditLog.EventType.REGION_ASSIGNMENT_REVOKED,
        target_user=instance.user,
        region=instance.region,
        message=f"role={instance.get_role_display()} entfernt",
    )


@receiver(post_save, sender=Region)
def _on_region_save(sender, instance, created, **kwargs):
    if created:
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.REGION_CREATED,
            region=instance, message=f"created: {instance.name}",
        )
    else:
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.REGION_UPDATED,
            region=instance, message=f"updated: {instance.name}",
        )


@receiver(post_delete, sender=Region)
def _on_region_delete(sender, instance, **kwargs):
    AccountAuditLog.log(
        event_type=AccountAuditLog.EventType.REGION_DELETED,
        region=None,  # FK is gone after delete
        message=f"deleted: {instance.name}",
    )
```

Check `apps/stations/apps.py`:

```python
from django.apps import AppConfig


class StationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.stations"

    def ready(self):
        from apps.stations import signals  # noqa: F401
```

(Add the `ready()` method if it doesn't exist.)

- [ ] **Step 6: Run tests to verify pass**

```bash
.venv/bin/python -m pytest tests/test_audit_log_emission.py -v
```

Expected: all 6 PASS.

- [ ] **Step 7: Run full suite**

```bash
.venv/bin/python -m pytest -q 2>&1 | tail -5
```

Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add apps/stations/models.py apps/stations/signals.py apps/stations/apps.py tests/test_audit_log_emission.py
git commit -m "feat(stations): audit-log signals for topology mutations

StationAuditLog: STATION_ASSIGNMENT_CREATED/REVOKED + STATION_REGION_CHANGED
AccountAuditLog: REGION_ASSIGNMENT_CREATED/REVOKED + REGION_CREATED/UPDATED/DELETED

Signal-based emission catches all paths: views, Django Admin, shell,
direct ORM. Promotion/demotion audit emission is wired separately
in the views (Task 16) because it needs request-context for actor."
```

---

## Task 14: Update apps/audit/views.py to merge 3 sources

**Files:**
- Modify: `apps/audit/views.py`
- Modify: `apps/audit/templates/audit/_audit_table.html` (render the new source rows)
- Modify: existing test for the merged feed (if present) or add a new sanity test

- [ ] **Step 1: Add merged-feed test**

In `tests/test_audit_log_emission.py`, append:

```python
@pytest.mark.django_db
def test_audit_events_visible_in_merged_feed(client):
    admin = _admin()
    # Generate one event in each of the 3 sources
    s = Station.objects.create(name="OE5A", callsign="OE5A")
    r = Region.objects.create(name="Tirol", slug="tirol")  # AccountAuditLog REGION_CREATED
    StationAuditLog.log_action(
        station=s, user=admin, event_type=StationAuditLog.EventType.NOTE_ADDED, message="n",
    )  # if NOTE_ADDED exists, otherwise pick a real event_type

    client.force_login(admin)
    response = client.get("/audit/")
    assert response.status_code == 200
    body = response.content.decode()
    assert "Tirol" in body  # REGION_CREATED entry surfaces
```

(Note: replace `NOTE_ADDED` with an actual existing `StationAuditLog.EventType` if `NOTE_ADDED` isn't defined; the test only needs ANY existing event to ensure all 3 sources merge.)

- [ ] **Step 2: Read existing apps/audit/views.py to understand merge structure**

```bash
sed -n '60,130p' apps/audit/views.py
```

- [ ] **Step 3: Extend the merge to include AccountAuditLog**

In `apps/audit/views.py`:

```python
from apps.accounts.models import AccountAuditLog
```

In `AuditLogListView.get_queryset()` / the merge logic, add a third source:

```python
# Pseudocode — adapt to the existing structure
account_logs = AccountAuditLog.objects.select_related(
    "actor", "target_user", "region",
).order_by("-created_at")[:MERGE_FEED_CAP]

merged = sorted(
    [("station", e) for e in station_logs]
    + [("sso", e) for e in sso_logs]
    + [("account", e) for e in account_logs],
    key=lambda pair: pair[1].created_at,
    reverse=True,
)
```

- [ ] **Step 4: Update template to render account rows**

In `apps/audit/templates/audit/_audit_table.html`, add a third branch alongside the existing station / sso renderers:

```html
{% elif category == "account" %}
  <tr>
    <td>{{ entry.created_at|date:"Y-m-d H:i" }}</td>
    <td>{{ entry.get_event_type_display }}</td>
    <td>{{ entry.actor|default:"—" }}</td>
    <td>{{ entry.target_user|default:"—" }}</td>
    <td>{{ entry.region|default:"—" }}</td>
    <td>{{ entry.message }}</td>
  </tr>
{% endif %}
```

(Adjust column layout to match the existing table.)

- [ ] **Step 5: Run tests**

```bash
.venv/bin/python -m pytest tests/test_audit_log_emission.py -v
```

Expected: all PASS, including the new merged-feed test.

- [ ] **Step 6: Commit**

```bash
git add apps/audit/views.py apps/audit/templates/audit/_audit_table.html tests/test_audit_log_emission.py
git commit -m "feat(audit): merge AccountAuditLog into the /audit/ feed (3 sources now)"
```

---

# Phase 6: UI — User-Detail

## Task 15: Read existing user_detail template

**Files:** (none modified — exploratory)

- [ ] **Step 1: Inspect user_detail.html**

```bash
cat apps/accounts/templates/accounts/user_detail.html 2>/dev/null || find apps -name "user_detail.html"
```

If no `user_detail.html` exists yet, find how user-detail is currently rendered:

```bash
grep -rn "user_detail\|UserDetail" apps/accounts/ apps/sso/ --include="*.py" | head -10
```

- [ ] **Step 2: Determine where to add the "Rollen & Zuordnungen" section**

Identify the file (likely `apps/sso/templates/sso/user_detail.html` since SSO has a user-detail view per the existing AppGrants pattern, or `apps/accounts/templates/accounts/user_detail.html` if accounts owns it). Confirm before proceeding to Task 16.

- [ ] **Step 3: Note the file path and section structure in the implementer's report.**

No commit for this exploratory step.

---

## Task 16: Membership-level dropdown (promote/demote with audit log)

**Files:**
- Create: `apps/accounts/views_membership.py`
- Modify: `apps/accounts/urls.py`
- Create: `apps/accounts/templates/accounts/_membership_level_picker.html`
- Modify: user_detail.html (path determined in Task 15)
- Create: `tests/test_views_membership.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_views_membership.py`:

```python
"""Tests for the membership-level set view (promote/demote)."""

import pytest
from django.urls import reverse

from apps.accounts.models import AccountAuditLog, User
from apps.stations.models import StationAssignment, Station


def _user(level, username):
    u = User.objects.create_user(username=username, password="x", email=f"{username}@x")
    u.membership_level = level
    u.save(update_fields=["membership_level"])
    return u


@pytest.mark.django_db
class TestMembershipSetView:
    def test_admin_can_promote(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        target = _user(User.MembershipLevel.APPLICANT, "hans")
        client.force_login(admin)
        response = client.post(
            reverse("accounts:membership_set", args=[target.pk]),
            {"level": "member"},
        )
        assert response.status_code == 200
        target.refresh_from_db()
        assert target.membership_level == User.MembershipLevel.MEMBER
        assert AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.MEMBERSHIP_PROMOTED,
            actor=admin, target_user=target,
        ).exists()

    def test_admin_can_demote(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        target = _user(User.MembershipLevel.STAFF, "maria")
        client.force_login(admin)
        response = client.post(
            reverse("accounts:membership_set", args=[target.pk]),
            {"level": "member"},
        )
        assert response.status_code == 200
        target.refresh_from_db()
        assert target.membership_level == User.MembershipLevel.MEMBER
        assert AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.MEMBERSHIP_DEMOTED,
        ).exists()

    def test_non_admin_forbidden(self, client):
        staff = _user(User.MembershipLevel.STAFF, "staff")
        target = _user(User.MembershipLevel.MEMBER, "tgt")
        client.force_login(staff)
        response = client.post(
            reverse("accounts:membership_set", args=[target.pk]),
            {"level": "admin"},
        )
        assert response.status_code == 403

    def test_self_forbidden(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        client.force_login(admin)
        response = client.post(
            reverse("accounts:membership_set", args=[admin.pk]),
            {"level": "member"},
        )
        assert response.status_code == 403

    def test_demote_to_applicant_blocked_when_assignments_exist(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        target = _user(User.MembershipLevel.MEMBER, "hans")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        StationAssignment.objects.create(
            user=target, station=s, role=StationAssignment.Role.ADMIN,
        )
        client.force_login(admin)
        response = client.post(
            reverse("accounts:membership_set", args=[target.pk]),
            {"level": "applicant"},
        )
        assert response.status_code == 400
        target.refresh_from_db()
        # Level unchanged
        assert target.membership_level == User.MembershipLevel.MEMBER

    def test_invalid_level_returns_400(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        target = _user(User.MembershipLevel.MEMBER, "hans")
        client.force_login(admin)
        response = client.post(
            reverse("accounts:membership_set", args=[target.pk]),
            {"level": "godlike"},
        )
        assert response.status_code == 400
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/python -m pytest tests/test_views_membership.py -v
```

Expected: NoReverseMatch on `accounts:membership_set`.

- [ ] **Step 3: Create the view**

Create `apps/accounts/views_membership.py`:

```python
"""Membership-level set (promote/demote) view.

POST /accounts/<user_pk>/membership/  data: {"level": "<value>"}
Returns 200 on success (HTMX-friendly), 400 on validation error,
403 on permission denied.
"""

from django.contrib.auth import get_user_model
from django.http import JsonResponse, HttpResponseForbidden, HttpResponseBadRequest
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext as _
from django.views import View

from apps.accounts.models import AccountAuditLog

User = get_user_model()


class MembershipSetView(View):
    def post(self, request, pk):
        if not request.user.is_authenticated:
            return HttpResponseForbidden()
        if not request.user.is_admin:
            return HttpResponseForbidden()

        target = get_object_or_404(User, pk=pk)
        if target.pk == request.user.pk:
            return HttpResponseForbidden(
                _("Cannot change your own membership level.")
            )

        new_level = request.POST.get("level", "").strip()
        valid_levels = {x.value for x in User.MembershipLevel}
        if new_level not in valid_levels:
            return HttpResponseBadRequest(
                _("Invalid level: %s") % new_level
            )

        old_level = target.membership_level
        if new_level == old_level:
            return JsonResponse({"success": True, "unchanged": True})

        # Demote-to-applicant block when assignments exist
        if new_level == User.MembershipLevel.APPLICANT:
            n_station = target.station_assignments.count()
            n_region = target.region_assignments.count()
            if n_station or n_region:
                return HttpResponseBadRequest(
                    _("Cannot demote to Applicant: user has %d station + "
                      "%d region assignment(s). Remove them first.")
                    % (n_station, n_region)
                )

        target.membership_level = new_level
        target.save(update_fields=["membership_level"])
        User._invalidate_role_cache(target)

        is_promote = list(User.MembershipLevel).index(User.MembershipLevel(new_level)) \
            > list(User.MembershipLevel).index(User.MembershipLevel(old_level))
        event = (AccountAuditLog.EventType.MEMBERSHIP_PROMOTED if is_promote
                 else AccountAuditLog.EventType.MEMBERSHIP_DEMOTED)
        AccountAuditLog.log(
            event_type=event, actor=request.user, target_user=target,
            message=f"{old_level} → {new_level}",
            ip_address=request.META.get("REMOTE_ADDR"),
        )

        return JsonResponse({"success": True})
```

- [ ] **Step 4: Wire the URL**

In `apps/accounts/urls.py`, add:

```python
from apps.accounts.views_membership import MembershipSetView

urlpatterns += [
    path("<int:pk>/membership/", MembershipSetView.as_view(),
         name="membership_set"),
]
```

(Adapt the import / app_name pattern to match the existing file.)

- [ ] **Step 5: Run tests to verify pass**

```bash
.venv/bin/python -m pytest tests/test_views_membership.py -v
```

Expected: all PASS.

- [ ] **Step 6: Create the HTMX widget**

Create `apps/accounts/templates/accounts/_membership_level_picker.html`:

```html
{% load i18n %}
<form hx-post="{% url 'accounts:membership_set' user_obj.pk %}"
      hx-target="this" hx-swap="outerHTML">
  {% csrf_token %}
  <label>{% trans "Vereins-Rolle" %}:
    <select name="level" data-submit-on-change>
      {% for value, label in level_choices %}
        <option value="{{ value }}"
                {% if user_obj.membership_level == value %}selected{% endif %}>
          {{ label }}
        </option>
      {% endfor %}
    </select>
  </label>
</form>
```

- [ ] **Step 7: Embed in user_detail template**

At the appropriate location in the user-detail template (path identified in Task 15):

```html
{% if request.user.is_admin and request.user.pk != user_obj.pk %}
  {% include "accounts/_membership_level_picker.html" %}
{% else %}
  <span>{{ user_obj.get_membership_level_display }}</span>
{% endif %}
```

And in the view that renders user-detail, add `level_choices = User.MembershipLevel.choices` to the context.

- [ ] **Step 8: Commit**

```bash
git add apps/accounts/views_membership.py apps/accounts/urls.py apps/accounts/templates/accounts/_membership_level_picker.html apps/accounts/templates/accounts/user_detail.html apps/<wherever_view_lives> tests/test_views_membership.py
git commit -m "feat(accounts): membership-level set view + HTMX picker"
```

---

## Task 17: Region-assignment widget (per-User)

**Files:**
- Create: `apps/stations/views_assignments.py`
- Modify: `apps/stations/urls.py`
- Create: `apps/accounts/templates/accounts/_user_region_assignments.html`
- Modify: user_detail.html
- Create: `tests/test_views_assignments.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_views_assignments.py`:

```python
"""Tests for the topology-assignment views (region + station)."""

import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.stations.models import (
    Region, RegionAssignment, Station, StationAssignment,
)


def _user(level, username):
    u = User.objects.create_user(username=username, password="x", email=f"{username}@x")
    u.membership_level = level
    u.save(update_fields=["membership_level"])
    return u


@pytest.mark.django_db
class TestRegionAssignmentViews:
    def test_admin_can_add_region_manager(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        lisa = _user(User.MembershipLevel.MEMBER, "lisa")
        r = Region.objects.create(name="Tirol", slug="tirol")
        client.force_login(admin)
        response = client.post(
            reverse("stations:region_assignment_create", args=[lisa.pk]),
            {"region": r.pk},
        )
        assert response.status_code == 200
        assert RegionAssignment.objects.filter(user=lisa, region=r).exists()

    def test_admin_can_remove_region_manager(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        lisa = _user(User.MembershipLevel.MEMBER, "lisa")
        r = Region.objects.create(name="Tirol", slug="tirol")
        a = RegionAssignment.objects.create(
            user=lisa, region=r, role=RegionAssignment.Role.MANAGER,
        )
        client.force_login(admin)
        response = client.post(
            reverse("stations:region_assignment_revoke", args=[a.pk]),
        )
        assert response.status_code == 200
        assert not RegionAssignment.objects.filter(pk=a.pk).exists()

    def test_non_admin_forbidden_to_add(self, client):
        staff = _user(User.MembershipLevel.STAFF, "staff")
        target = _user(User.MembershipLevel.MEMBER, "tgt")
        r = Region.objects.create(name="Tirol", slug="tirol")
        client.force_login(staff)
        response = client.post(
            reverse("stations:region_assignment_create", args=[target.pk]),
            {"region": r.pk},
        )
        assert response.status_code == 403

    def test_applicant_target_returns_400(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        applicant = _user(User.MembershipLevel.APPLICANT, "newbie")
        r = Region.objects.create(name="Tirol", slug="tirol")
        client.force_login(admin)
        response = client.post(
            reverse("stations:region_assignment_create", args=[applicant.pk]),
            {"region": r.pk},
        )
        assert response.status_code == 400
        assert not RegionAssignment.objects.filter(user=applicant).exists()
```

- [ ] **Step 2: Create the views module**

Create `apps/stations/views_assignments.py`:

```python
"""HTMX views for creating/revoking topology assignments."""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.http import (
    JsonResponse, HttpResponseForbidden, HttpResponseBadRequest,
)
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext as _
from django.views import View

from apps.stations.models import (
    Region, RegionAssignment, Station, StationAssignment,
)

User = get_user_model()


def _admin_required(request):
    if not request.user.is_authenticated or not request.user.is_admin:
        return HttpResponseForbidden()
    return None


class RegionAssignmentCreateView(View):
    def post(self, request, user_pk):
        denied = _admin_required(request)
        if denied:
            return denied
        target = get_object_or_404(User, pk=user_pk)
        region_pk = request.POST.get("region")
        region = get_object_or_404(Region, pk=region_pk)
        try:
            RegionAssignment.objects.create(
                user=target, region=region,
                role=RegionAssignment.Role.MANAGER,
                assigned_by=request.user,
            )
        except ValidationError as e:
            return HttpResponseBadRequest(str(e))
        return JsonResponse({"success": True})


class RegionAssignmentRevokeView(View):
    def post(self, request, pk):
        denied = _admin_required(request)
        if denied:
            return denied
        a = get_object_or_404(RegionAssignment, pk=pk)
        a.delete()
        return JsonResponse({"success": True})
```

- [ ] **Step 3: Wire URLs**

In `apps/stations/urls.py`:

```python
from apps.stations.views_assignments import (
    RegionAssignmentCreateView, RegionAssignmentRevokeView,
)

urlpatterns += [
    path("regions/assign/<int:user_pk>/",
         RegionAssignmentCreateView.as_view(),
         name="region_assignment_create"),
    path("regions/revoke/<int:pk>/",
         RegionAssignmentRevokeView.as_view(),
         name="region_assignment_revoke"),
]
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/python -m pytest tests/test_views_assignments.py::TestRegionAssignmentViews -v
```

Expected: PASS.

- [ ] **Step 5: Create the HTMX template fragment**

Create `apps/accounts/templates/accounts/_user_region_assignments.html`:

```html
{% load i18n %}
<section id="region-assignments">
  <h3>{% trans "Region-Manager für" %}</h3>
  <ul>
    {% for a in region_assignments %}
      <li>
        {{ a.region.name }}
        {% if request.user.is_admin %}
          <form hx-post="{% url 'stations:region_assignment_revoke' a.pk %}"
                hx-target="#region-assignments" hx-swap="outerHTML"
                style="display:inline">
            {% csrf_token %}
            <button type="submit">✕</button>
          </form>
        {% endif %}
      </li>
    {% empty %}
      <li class="t-muted">{% trans "Keine Region-Zuordnung." %}</li>
    {% endfor %}
  </ul>
  {% if request.user.is_admin %}
    <form hx-post="{% url 'stations:region_assignment_create' user_obj.pk %}"
          hx-target="#region-assignments" hx-swap="outerHTML">
      {% csrf_token %}
      <select name="region">
        {% for r in all_regions %}
          <option value="{{ r.pk }}">{{ r.name }}</option>
        {% endfor %}
      </select>
      <button type="submit">{% trans "+ Region hinzufügen" %}</button>
    </form>
  {% endif %}
</section>
```

(The view that renders user_detail must populate `region_assignments` + `all_regions` in context.)

- [ ] **Step 6: Embed in user_detail template + view context**

In the user_detail view, add:

```python
context["region_assignments"] = user_obj.region_assignments.select_related("region")
context["all_regions"] = Region.objects.all().order_by("name")
```

In user_detail template:

```html
{% include "accounts/_user_region_assignments.html" %}
```

- [ ] **Step 7: Commit**

```bash
git add apps/stations/views_assignments.py apps/stations/urls.py apps/accounts/templates/accounts/_user_region_assignments.html tests/test_views_assignments.py apps/<view-file>
git commit -m "feat(stations): region-assignment HTMX widget on user-detail"
```

---

## Task 18: Station-assignment widget (per-User) — analogous to Task 17

**Files:**
- Modify: `apps/stations/views_assignments.py` (add StationAssignment views)
- Modify: `apps/stations/urls.py`
- Create: `apps/accounts/templates/accounts/_user_station_assignments.html`
- Modify: `tests/test_views_assignments.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/test_views_assignments.py`:

```python
@pytest.mark.django_db
class TestStationAssignmentViews:
    def test_admin_can_add_station_admin(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        franz = _user(User.MembershipLevel.MEMBER, "franz")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        client.force_login(admin)
        response = client.post(
            reverse("stations:station_assignment_create", args=[franz.pk]),
            {"station": s.pk, "role": "admin"},
        )
        assert response.status_code == 200
        assert StationAssignment.objects.filter(
            user=franz, station=s, role="admin").exists()

    def test_admin_can_add_station_maintainer(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        hans = _user(User.MembershipLevel.MEMBER, "hans")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        client.force_login(admin)
        response = client.post(
            reverse("stations:station_assignment_create", args=[hans.pk]),
            {"station": s.pk, "role": "maintainer"},
        )
        assert response.status_code == 200

    def test_taking_over_admin_replaces_existing(self, client):
        """Cannot have 2 admins per station — replace expected."""
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        franz = _user(User.MembershipLevel.MEMBER, "franz")
        new_admin = _user(User.MembershipLevel.MEMBER, "new")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        StationAssignment.objects.create(
            user=franz, station=s, role=StationAssignment.Role.ADMIN,
        )
        client.force_login(admin)
        response = client.post(
            reverse("stations:station_assignment_create", args=[new_admin.pk]),
            {"station": s.pk, "role": "admin", "takeover": "true"},
        )
        assert response.status_code == 200
        # Old admin removed, new in place
        assert not StationAssignment.objects.filter(user=franz, station=s).exists()
        assert StationAssignment.objects.filter(user=new_admin, station=s, role="admin").exists()

    def test_taking_over_without_confirm_returns_409(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        franz = _user(User.MembershipLevel.MEMBER, "franz")
        new_admin = _user(User.MembershipLevel.MEMBER, "new")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        StationAssignment.objects.create(
            user=franz, station=s, role=StationAssignment.Role.ADMIN,
        )
        client.force_login(admin)
        response = client.post(
            reverse("stations:station_assignment_create", args=[new_admin.pk]),
            {"station": s.pk, "role": "admin"},
        )
        # 409 Conflict: existing admin must be confirmed-replaced
        assert response.status_code == 409

    def test_admin_can_revoke_station_assignment(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        franz = _user(User.MembershipLevel.MEMBER, "franz")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        a = StationAssignment.objects.create(
            user=franz, station=s, role=StationAssignment.Role.MAINTAINER,
        )
        client.force_login(admin)
        response = client.post(
            reverse("stations:station_assignment_revoke", args=[a.pk]),
        )
        assert response.status_code == 200
        assert not StationAssignment.objects.filter(pk=a.pk).exists()
```

- [ ] **Step 2: Add views**

In `apps/stations/views_assignments.py`, append:

```python
class StationAssignmentCreateView(View):
    def post(self, request, user_pk):
        denied = _admin_required(request)
        if denied:
            return denied
        target = get_object_or_404(User, pk=user_pk)
        station_pk = request.POST.get("station")
        station = get_object_or_404(Station, pk=station_pk)
        role = request.POST.get("role", "").strip()
        if role not in {"admin", "maintainer"}:
            return HttpResponseBadRequest(_("Invalid role: %s") % role)

        # Admin uniqueness handling
        if role == "admin":
            existing = StationAssignment.objects.filter(
                station=station, role="admin"
            ).first()
            if existing and existing.user != target:
                takeover = request.POST.get("takeover", "").lower() == "true"
                if not takeover:
                    return JsonResponse(
                        {"success": False, "conflict": "existing_admin",
                         "current_admin": str(existing.user)},
                        status=409,
                    )
                existing.delete()

        try:
            StationAssignment.objects.create(
                user=target, station=station, role=role,
                assigned_by=request.user,
            )
        except ValidationError as e:
            return HttpResponseBadRequest(str(e))
        return JsonResponse({"success": True})


class StationAssignmentRevokeView(View):
    def post(self, request, pk):
        denied = _admin_required(request)
        if denied:
            return denied
        a = get_object_or_404(StationAssignment, pk=pk)
        a.delete()
        return JsonResponse({"success": True})
```

- [ ] **Step 3: Wire URLs**

In `apps/stations/urls.py`, append:

```python
from apps.stations.views_assignments import (
    StationAssignmentCreateView, StationAssignmentRevokeView,
)
urlpatterns += [
    path("assignments/create/<int:user_pk>/",
         StationAssignmentCreateView.as_view(),
         name="station_assignment_create"),
    path("assignments/revoke/<int:pk>/",
         StationAssignmentRevokeView.as_view(),
         name="station_assignment_revoke"),
]
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/python -m pytest tests/test_views_assignments.py::TestStationAssignmentViews -v
```

Expected: all PASS.

- [ ] **Step 5: Create template fragment**

Create `apps/accounts/templates/accounts/_user_station_assignments.html`:

```html
{% load i18n %}
<section id="station-assignments">
  <h3>{% trans "Station-Zuordnungen" %}</h3>
  <ul>
    {% for a in station_assignments %}
      <li>
        {{ a.station.name }} — <em>{{ a.get_role_display }}</em>
        {% if request.user.is_admin %}
          <form hx-post="{% url 'stations:station_assignment_revoke' a.pk %}"
                hx-target="#station-assignments" hx-swap="outerHTML"
                style="display:inline">
            {% csrf_token %}
            <button type="submit">✕</button>
          </form>
        {% endif %}
      </li>
    {% empty %}
      <li class="t-muted">{% trans "Keine Station-Zuordnung." %}</li>
    {% endfor %}
  </ul>
  {% if request.user.is_admin %}
    <form hx-post="{% url 'stations:station_assignment_create' user_obj.pk %}"
          hx-target="#station-assignments" hx-swap="outerHTML">
      {% csrf_token %}
      <select name="station">
        {% for s in all_stations %}<option value="{{ s.pk }}">{{ s.name }}</option>{% endfor %}
      </select>
      <select name="role">
        <option value="admin">{% trans "Station-Admin" %}</option>
        <option value="maintainer">{% trans "Station-Maintainer" %}</option>
      </select>
      <button type="submit">+</button>
    </form>
  {% endif %}
</section>
```

- [ ] **Step 6: Embed in user_detail + view context**

```python
context["station_assignments"] = user_obj.station_assignments.select_related("station")
context["all_stations"] = Station.objects.all().order_by("name")
```

```html
{% include "accounts/_user_station_assignments.html" %}
```

- [ ] **Step 7: Commit**

```bash
git add apps/stations/views_assignments.py apps/stations/urls.py apps/accounts/templates/accounts/_user_station_assignments.html tests/test_views_assignments.py apps/<view-file>
git commit -m "feat(stations): station-assignment HTMX widget + admin-takeover flow"
```

---

# Phase 7: UI — Station-Detail

## Task 19: Region picker on Station-Detail

**Files:**
- Modify: `apps/stations/views.py` (StationDetailView context)
- Modify: `apps/stations/templates/stations/station_detail.html`
- Create: `apps/stations/templates/stations/_station_region_picker.html`
- Create: `tests/test_views_station_topology.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_views_station_topology.py`:

```python
"""Tests for the Station-Detail topology widgets."""

import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.stations.models import Region, Station


def _admin():
    u = User.objects.create_user(username="admin", password="x", email="a@x")
    u.membership_level = User.MembershipLevel.ADMIN
    u.save(update_fields=["membership_level"])
    return u


@pytest.mark.django_db
class TestStationRegionPicker:
    def test_admin_can_set_region(self, client):
        admin = _admin()
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        r = Region.objects.create(name="Tirol", slug="tirol")
        client.force_login(admin)
        response = client.post(
            reverse("stations:station_set_region", args=[s.pk]),
            {"region": r.pk},
        )
        assert response.status_code == 200
        s.refresh_from_db()
        assert s.region == r

    def test_admin_can_clear_region(self, client):
        admin = _admin()
        r = Region.objects.create(name="Tirol", slug="tirol")
        s = Station.objects.create(name="OE5A", callsign="OE5A", region=r)
        client.force_login(admin)
        response = client.post(
            reverse("stations:station_set_region", args=[s.pk]),
            {"region": ""},
        )
        assert response.status_code == 200
        s.refresh_from_db()
        assert s.region is None
```

- [ ] **Step 2: Add view**

In `apps/stations/views_assignments.py`, append:

```python
class StationSetRegionView(View):
    def post(self, request, pk):
        denied = _admin_required(request)
        if denied:
            return denied
        s = get_object_or_404(Station, pk=pk)
        region_pk = request.POST.get("region", "").strip()
        if region_pk:
            s.region = get_object_or_404(Region, pk=region_pk)
        else:
            s.region = None
        s.save(update_fields=["region"])
        # The pre_save signal in apps/stations/signals.py emits the
        # STATION_REGION_CHANGED audit entry.
        return JsonResponse({"success": True})
```

In urls.py:

```python
path("<int:pk>/region/", StationSetRegionView.as_view(),
     name="station_set_region"),
```

- [ ] **Step 3: Add template fragment**

Create `apps/stations/templates/stations/_station_region_picker.html`:

```html
{% load i18n %}
<form id="station-region-picker"
      hx-post="{% url 'stations:station_set_region' station.pk %}"
      hx-target="this" hx-swap="outerHTML">
  {% csrf_token %}
  <label>{% trans "Region" %}:
    <select name="region" data-submit-on-change>
      <option value="">{% trans "— keine —" %}</option>
      {% for r in all_regions %}
        <option value="{{ r.pk }}" {% if station.region == r %}selected{% endif %}>
          {{ r.name }}
        </option>
      {% endfor %}
    </select>
  </label>
</form>
```

- [ ] **Step 4: Embed in station_detail.html + view context**

Add `all_regions = Region.objects.all()` to the StationDetail view context.

In the template (in the existing structure):

```html
<section class="panel">
  <div class="panel-head"><div class="panel-title">{% trans "Region & Verantwortliche" %}</div></div>
  <div class="panel-body">
    {% if request.user.is_admin %}
      {% include "stations/_station_region_picker.html" %}
    {% else %}
      <p>{% trans "Region" %}: {{ station.region|default:"—" }}</p>
    {% endif %}
  </div>
</section>
```

- [ ] **Step 5: Run tests**

```bash
.venv/bin/python -m pytest tests/test_views_station_topology.py::TestStationRegionPicker -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/stations/views_assignments.py apps/stations/urls.py apps/stations/templates/stations/_station_region_picker.html apps/stations/templates/stations/station_detail.html tests/test_views_station_topology.py apps/<view-file>
git commit -m "feat(stations): station-region picker (admin) on station-detail"
```

---

## Task 20: Station-Admin + Station-Maintainer widgets on Station-Detail

**Files:**
- Append to: `apps/stations/views_assignments.py`
- Append to: `apps/stations/urls.py`
- Create: `apps/stations/templates/stations/_station_admin_picker.html`
- Create: `apps/stations/templates/stations/_station_maintainer_list.html`
- Modify: `apps/stations/templates/stations/station_detail.html`
- Append to: `tests/test_views_station_topology.py`

(See Task 18 for the pattern. The endpoints reuse the same view classes as Task 18 — Station-Detail just embeds the widgets with `station=station` context, and the widget templates filter by station, not user. Permission gates extend to Region-Manager / Station-Admin scope per spec §3.4.)

- [ ] **Step 1: Append tests for permission gates**

```python
@pytest.mark.django_db
class TestStationDetailPermissionGates:
    def test_region_manager_can_edit_stations_in_own_region(self, client):
        mgr = User.objects.create_user(username="mgr", password="x", email="m@x")
        mgr.membership_level = User.MembershipLevel.MEMBER
        mgr.save(update_fields=["membership_level"])
        r = Region.objects.create(name="Tirol", slug="tirol")
        from apps.stations.models import RegionAssignment, StationAssignment
        RegionAssignment.objects.create(
            user=mgr, region=r, role=RegionAssignment.Role.MANAGER,
        )
        s = Station.objects.create(name="OE5A", callsign="OE5A", region=r)
        franz = User.objects.create_user(username="franz", password="x", email="f@x")
        franz.membership_level = User.MembershipLevel.MEMBER
        franz.save(update_fields=["membership_level"])
        client.force_login(mgr)
        response = client.post(
            reverse("stations:station_assignment_create", args=[franz.pk]),
            {"station": s.pk, "role": "maintainer"},
        )
        # Region-Manager can edit maintainers within own region — expect 200
        assert response.status_code == 200

    def test_region_manager_cannot_edit_other_region(self, client):
        mgr = User.objects.create_user(username="mgr", password="x", email="m@x")
        mgr.membership_level = User.MembershipLevel.MEMBER
        mgr.save(update_fields=["membership_level"])
        r1 = Region.objects.create(name="Tirol", slug="tirol")
        r2 = Region.objects.create(name="OÖ", slug="ooe")
        from apps.stations.models import RegionAssignment
        RegionAssignment.objects.create(
            user=mgr, region=r1, role=RegionAssignment.Role.MANAGER,
        )
        s = Station.objects.create(name="OE5A", callsign="OE5A", region=r2)
        franz = User.objects.create_user(username="franz", password="x", email="f@x")
        franz.membership_level = User.MembershipLevel.MEMBER
        franz.save(update_fields=["membership_level"])
        client.force_login(mgr)
        response = client.post(
            reverse("stations:station_assignment_create", args=[franz.pk]),
            {"station": s.pk, "role": "maintainer"},
        )
        assert response.status_code == 403
```

- [ ] **Step 2: Tighten the permission check in assignment views**

In `apps/stations/views_assignments.py`, replace `_admin_required` with a station-scoped variant for assignments-of-station ops:

```python
def _can_edit_station(request, station):
    if not request.user.is_authenticated:
        return False
    if request.user.is_admin:
        return True
    if request.user.is_region_manager(station.region):
        return True
    return False


def _can_edit_station_admin(request, station):
    """Stricter: only Vereins-Admin and Region-Manager can change admin.
    Station-Admin can edit maintainers but not their own admin slot.
    """
    return _can_edit_station(request, station)


def _can_edit_station_maintainer(request, station):
    if _can_edit_station(request, station):
        return True
    if request.user.is_station_admin(station):
        return True
    return False
```

Update `StationAssignmentCreateView.post` to use these gates:

```python
class StationAssignmentCreateView(View):
    def post(self, request, user_pk):
        if not request.user.is_authenticated:
            return HttpResponseForbidden()
        target = get_object_or_404(User, pk=user_pk)
        station_pk = request.POST.get("station")
        station = get_object_or_404(Station, pk=station_pk)
        role = request.POST.get("role", "").strip()
        if role not in {"admin", "maintainer"}:
            return HttpResponseBadRequest(_("Invalid role: %s") % role)

        if role == "admin":
            if not _can_edit_station_admin(request, station):
                return HttpResponseForbidden()
        else:
            if not _can_edit_station_maintainer(request, station):
                return HttpResponseForbidden()

        # ... rest of the existing logic (admin uniqueness etc.)
```

- [ ] **Step 3: Run tests**

```bash
.venv/bin/python -m pytest tests/test_views_station_topology.py tests/test_views_assignments.py -v
```

Expected: all PASS.

- [ ] **Step 4: Embed widgets in station_detail.html**

Inside the same panel as the region picker:

```html
<h4>{% trans "Station-Admin" %}</h4>
{% include "stations/_station_admin_picker.html" %}
<h4>{% trans "Station-Maintainers" %}</h4>
{% include "stations/_station_maintainer_list.html" %}
```

Both fragments mirror Task 18's user-side widgets but with station as the anchor (look up by `station.assignments.filter(role=...)`, etc.). Wire into the StationDetail view context.

- [ ] **Step 5: Commit**

```bash
git add apps/stations/views_assignments.py apps/stations/templates/stations/_station_admin_picker.html apps/stations/templates/stations/_station_maintainer_list.html apps/stations/templates/stations/station_detail.html tests/test_views_station_topology.py
git commit -m "feat(stations): station-admin + maintainer pickers on station-detail with scope gates"
```

---

# Phase 8: UI — Region CRUD

## Task 21: Region list / create / edit / delete

**Files:**
- Create: `apps/stations/views_region.py`
- Modify: `apps/stations/urls.py`
- Create: 3 templates: `region_list.html`, `region_form.html`, `region_confirm_delete.html`
- Create: `tests/test_views_region.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_views_region.py`:

```python
"""Tests for the Region CRUD views (admin-only)."""

import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.stations.models import Region, Station


def _admin():
    u = User.objects.create_user(username="admin", password="x", email="a@x")
    u.membership_level = User.MembershipLevel.ADMIN
    u.save(update_fields=["membership_level"])
    return u


@pytest.mark.django_db
class TestRegionCrud:
    def test_list_admin_only(self, client):
        admin = _admin()
        client.force_login(admin)
        Region.objects.create(name="Tirol", slug="tirol")
        response = client.get(reverse("stations:region_list"))
        assert response.status_code == 200
        assert b"Tirol" in response.content

    def test_list_forbidden_for_member(self, client):
        m = User.objects.create_user(username="m", password="x")
        m.membership_level = User.MembershipLevel.MEMBER
        m.save(update_fields=["membership_level"])
        client.force_login(m)
        response = client.get(reverse("stations:region_list"))
        assert response.status_code == 403

    def test_create_region(self, client):
        admin = _admin()
        client.force_login(admin)
        response = client.post(
            reverse("stations:region_create"),
            {"name": "Innviertel", "slug": "innv", "description": ""},
        )
        assert response.status_code in (200, 302)
        assert Region.objects.filter(name="Innviertel").exists()

    def test_update_region(self, client):
        admin = _admin()
        r = Region.objects.create(name="OOe", slug="ooe")
        client.force_login(admin)
        response = client.post(
            reverse("stations:region_update", args=[r.pk]),
            {"name": "OÖ", "slug": "ooe", "description": ""},
        )
        assert response.status_code in (200, 302)
        r.refresh_from_db()
        assert r.name == "OÖ"

    def test_delete_region_with_no_stations(self, client):
        admin = _admin()
        r = Region.objects.create(name="Salzburg", slug="sbg")
        client.force_login(admin)
        response = client.post(
            reverse("stations:region_delete", args=[r.pk]),
        )
        assert response.status_code in (200, 302)
        assert not Region.objects.filter(pk=r.pk).exists()

    def test_delete_region_with_stations_set_null(self, client):
        admin = _admin()
        r = Region.objects.create(name="Tirol", slug="tirol")
        s = Station.objects.create(name="OE5A", callsign="OE5A", region=r)
        client.force_login(admin)
        response = client.post(
            reverse("stations:region_delete", args=[r.pk]),
        )
        assert response.status_code in (200, 302)
        s.refresh_from_db()
        assert s.region is None
```

- [ ] **Step 2: Create views**

Create `apps/stations/views_region.py`:

```python
from django.urls import reverse_lazy
from django.views.generic import (
    ListView, CreateView, UpdateView, DeleteView,
)

from apps.accounts.views import AdminRequiredMixin
from apps.stations.models import Region


class RegionListView(AdminRequiredMixin, ListView):
    model = Region
    template_name = "stations/region_list.html"
    context_object_name = "regions"


class RegionCreateView(AdminRequiredMixin, CreateView):
    model = Region
    fields = ["name", "slug", "description"]
    template_name = "stations/region_form.html"
    success_url = reverse_lazy("stations:region_list")


class RegionUpdateView(AdminRequiredMixin, UpdateView):
    model = Region
    fields = ["name", "slug", "description"]
    template_name = "stations/region_form.html"
    success_url = reverse_lazy("stations:region_list")


class RegionDeleteView(AdminRequiredMixin, DeleteView):
    model = Region
    template_name = "stations/region_confirm_delete.html"
    success_url = reverse_lazy("stations:region_list")
```

- [ ] **Step 3: URLs**

```python
from apps.stations.views_region import (
    RegionListView, RegionCreateView, RegionUpdateView, RegionDeleteView,
)
urlpatterns += [
    path("regions/", RegionListView.as_view(), name="region_list"),
    path("regions/new/", RegionCreateView.as_view(), name="region_create"),
    path("regions/<int:pk>/edit/", RegionUpdateView.as_view(), name="region_update"),
    path("regions/<int:pk>/delete/", RegionDeleteView.as_view(), name="region_delete"),
]
```

- [ ] **Step 4: Create templates**

`apps/stations/templates/stations/region_list.html`:

```html
{% extends "base.html" %}
{% load i18n %}
{% block content %}
<h1>{% trans "Regionen" %}</h1>
<table>
  <thead><tr><th>{% trans "Name" %}</th><th>{% trans "Slug" %}</th><th>{% trans "Stationen" %}</th><th></th></tr></thead>
  <tbody>
  {% for r in regions %}
    <tr>
      <td>{{ r.name }}</td>
      <td>{{ r.slug }}</td>
      <td>{{ r.stations.count }}</td>
      <td>
        <a href="{% url 'stations:region_update' r.pk %}">{% trans "Bearbeiten" %}</a>
        <a href="{% url 'stations:region_delete' r.pk %}">✕</a>
      </td>
    </tr>
  {% endfor %}
  </tbody>
</table>
<a href="{% url 'stations:region_create' %}">{% trans "+ Neue Region" %}</a>
{% endblock %}
```

`region_form.html`:

```html
{% extends "base.html" %}
{% load i18n %}
{% block content %}
<h1>{% if object %}{% trans "Region bearbeiten" %}{% else %}{% trans "Neue Region" %}{% endif %}</h1>
<form method="post">{% csrf_token %}
  {{ form.as_p }}
  <button type="submit">{% trans "Speichern" %}</button>
  <a href="{% url 'stations:region_list' %}">{% trans "Abbrechen" %}</a>
</form>
{% endblock %}
```

`region_confirm_delete.html`:

```html
{% extends "base.html" %}
{% load i18n %}
{% block content %}
<h1>{% trans "Region löschen?" %}</h1>
<p>{% blocktrans with name=object.name count=object.stations.count %}
  Wenn du "{{ name }}" löschst, verlieren {{ count }} Stationen ihre Region-Zuordnung
  (die Stationen selbst bleiben).
{% endblocktrans %}</p>
<form method="post">{% csrf_token %}
  <button type="submit">{% trans "Endgültig löschen" %}</button>
  <a href="{% url 'stations:region_list' %}">{% trans "Abbrechen" %}</a>
</form>
{% endblock %}
```

- [ ] **Step 5: Run tests**

```bash
.venv/bin/python -m pytest tests/test_views_region.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/stations/views_region.py apps/stations/urls.py apps/stations/templates/stations/region_*.html tests/test_views_region.py
git commit -m "feat(stations): Region CRUD admin pages (list/create/update/delete)"
```

---

# Phase 9: Drop legacy groups

## Task 22: Migration 0009 — delete legacy Django groups

**Files:**
- Create: `apps/accounts/migrations/0009_drop_legacy_role_groups.py`
- Append to: `tests/test_membership_level_migration.py`

- [ ] **Step 1: Append failing test**

Append to `tests/test_membership_level_migration.py`:

```python
@pytest.mark.django_db(transaction=True)
def test_0009_drops_legacy_groups():
    migrator = Migrator(database="default")
    old_state = migrator.apply_initial_migration(
        [("accounts", "0008_add_account_audit_log")]
    )
    Group = old_state.apps.get_model("auth", "Group")
    # Pre-state: legacy groups exist
    for name in ("admin", "operator", "member"):
        Group.objects.get_or_create(name=name)
    assert Group.objects.filter(name__in=["admin", "operator", "member"]).count() == 3

    new_state = migrator.apply_tested_migration(
        [("accounts", "0009_drop_legacy_role_groups")]
    )
    Group = new_state.apps.get_model("auth", "Group")
    assert Group.objects.filter(name__in=["admin", "operator", "member"]).count() == 0
    migrator.reset()


@pytest.mark.django_db(transaction=True)
def test_0009_reverse_recreates_groups():
    migrator = Migrator(database="default")
    old_state = migrator.apply_initial_migration(
        [("accounts", "0008_add_account_audit_log")]
    )
    Group = old_state.apps.get_model("auth", "Group")
    for name in ("admin", "operator", "member"):
        Group.objects.get_or_create(name=name)

    new_state = migrator.apply_tested_migration(
        [("accounts", "0009_drop_legacy_role_groups")]
    )
    Group = new_state.apps.get_model("auth", "Group")
    assert Group.objects.filter(name__in=["admin", "operator", "member"]).count() == 0

    # Now reverse
    final_state = migrator.apply_tested_migration(
        [("accounts", "0008_add_account_audit_log")]
    )
    Group = final_state.apps.get_model("auth", "Group")
    assert Group.objects.filter(name__in=["admin", "operator", "member"]).count() == 3
    migrator.reset()
```

- [ ] **Step 2: Create the migration**

Create `apps/accounts/migrations/0009_drop_legacy_role_groups.py`:

```python
"""Delete the legacy Django role-groups (admin/operator/member).

After Phase 3 (call-site refactor + is_admin re-implementation) and
data migration 0005 (Group → membership_level seed), the groups
themselves serve no further purpose. They are removed here.

Reverse re-creates the groups via idempotent get_or_create so that a
rollback within the 30-day backup window leaves the database
recoverable. The reversed groups are EMPTY — membership reconstruction
is not attempted (cardinal: rollback inside the backup window relies
on the pre-cutover backup itself, not on reverse-migration round-trip
fidelity).
"""

from django.db import migrations

LEGACY_GROUPS = ("admin", "operator", "member")


def drop_legacy_groups(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.filter(name__in=LEGACY_GROUPS).delete()


def recreate_legacy_groups(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    for name in LEGACY_GROUPS:
        Group.objects.get_or_create(name=name)


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0008_add_account_audit_log"),
    ]

    operations = [
        migrations.RunPython(
            drop_legacy_groups,
            reverse_code=recreate_legacy_groups,
        ),
    ]
```

- [ ] **Step 3: Run tests**

```bash
.venv/bin/python -m pytest tests/test_membership_level_migration.py -v
```

Expected: all PASS (including the 2 new tests).

- [ ] **Step 4: Run full suite**

```bash
.venv/bin/python -m pytest -q 2>&1 | tail -5
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/accounts/migrations/0009_drop_legacy_role_groups.py tests/test_membership_level_migration.py
git commit -m "feat(accounts): drop legacy admin/operator/member Django Groups (migration 0009)

Cutover complete: membership_level is now the single source of truth
for vereinsweit role state. Legacy groups removed. Reverse-code
recreates empty groups for 30-day backup-window rollback safety."
```

---

# Phase 10: PR + Verification

## Task 23: Full-suite regression run

**Files:** (none modified)

- [ ] **Step 1: Run full suite**

```bash
.venv/bin/python -m pytest -q 2>&1 | tail -10
```

Expected: all PASS.

- [ ] **Step 2: Lint**

```bash
ruff format --check . && ruff check .
```

Expected: clean. If `ruff format --check .` reports drift, run `ruff format .` and amend the previous commit.

- [ ] **Step 3: No commit if everything green**

If lint changes anything, amend the last commit (`git commit --amend --no-edit`).

---

## Task 24: Push branch + create PR

**Files:** (none modified)

- [ ] **Step 1: Push**

```bash
git push -u origin feat/membership-levels-and-topology-roles
```

- [ ] **Step 2: Open PR**

```bash
gh pr create --title "feat(accounts+stations): membership-level + topology foundation (PR-1)" --body "$(cat <<'EOF'
## Summary

**PR-1 of 2** — Foundation only. Behavior-preserving refactor that introduces the data model + permission helpers for the new two-axis authorization (Membership-Level + Topology). Routing, audit-log emission, UI, and legacy-group cleanup all come in PR-2.

## What this PR does

- New \`User.membership_level\` field (CharField TextChoices: applicant/member/staff/admin), default APPLICANT.
- Data migration 0005 maps existing Django-Groups → membership_level (admin → ADMIN, operator → STAFF, member → MEMBER).
- New \`Region\` model + nullable \`Station.region\` FK (no UI yet, only Django-Admin / ORM-level access).
- New \`StationAssignment\` (admin/maintainer with uniqueness constraints) + \`RegionAssignment\` (manager) models. Applicant invariant enforced via clean()/save().
- New \`AccountAuditLog\` model (no signals emitting yet — that's PR-2).
- New permission helpers on \`User\`: \`is_internal\`, \`is_station_admin(s)\`, \`is_station_maintainer(s)\`, \`is_region_manager(r)\`, \`can_administer_station(s)\`, \`can_maintain_station(s)\`, \`can_use_station(s)\`.
- Refactor: \`User.is_admin\` now reads \`membership_level\` instead of group membership. \`is_operator\`, \`is_staff_member\`, \`group_names\` removed.
- ~13 call-sites refactored across \`apps/{stations,firmware,monitoring,audit,deployments,tunnel,api,accounts,sso}\` to use the new helpers.
- conftest fixtures (\`admin_user\`, \`operator_user\`, \`member_user\`) switched from Django-Group assignment to membership_level setting.

## What this PR does NOT do (deferred to PR-2)

- Notification routing rewrite (\`recipients_for_station_alert\` helper + dispatch wiring + test-email-to-self).
- Audit-log emission signals.
- Any new UI (User-Detail rollen-section, Station-Detail topology widgets, Region-CRUD admin page).
- Deletion of the legacy \`admin\`/\`operator\`/\`member\` Django-Groups (migration 0009).

## Migrations

| # | Migration | What |
|---|---|---|
| 0004 | accounts: add_membership_level | CharField TextChoices, default APPLICANT |
| 0005 | accounts: seed_membership_levels | data: Group → membership_level mapping |
| 0006 | stations: add_region_and_station_fk | Region model + Station.region FK (nullable, SET_NULL) |
| 0007 | stations: add_assignments | StationAssignment + RegionAssignment + constraints |
| 0008 | accounts: add_account_audit_log | AccountAuditLog model (no consumers yet) |

All data migrations have safe reverses (noop for 0005).

## Spec + Plan

- Spec: \`docs/superpowers/specs/2026-06-05-membership-levels-and-topology-roles-design.md\`
- Plan: \`docs/superpowers/plans/2026-06-05-membership-levels-and-topology-roles.md\` (Phase 1-3 covered here; Phase 4-9 documented for context, executed in PR-2)

## Pre-merge

- [x] Full test suite green
- [x] ruff format/check clean
- [ ] Copilot review run

## Post-merge

- [ ] \`gh workflow run main.yml --repo OE5XRX/servers\` to deploy — the new migrations land in the init container; no user-visible behavior change expected.
- [ ] Verify \`/de/monitoring/settings/\` thresholds still display (regression-guard).
- [ ] Trigger synthetic offline alert — verify the existing admin-group recipient set still receives the email (the new recipient helper is NOT wired yet; PR-2 brings that).
- [ ] Begin PR-2 plan after merge.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Return the PR URL.**

---

## Self-Review

**Spec coverage:**

| Spec section | Implementing task |
|---|---|
| §3.1 Membership-Level (4 levels, sequential) | T1, T2 |
| §3.2 Topology (Region, StationAssignment, RegionAssignment) | T3, T4 |
| §3.3 Permission-Composition (can_*_station) | T7, T8 |
| §3.4 Naming Conventions (TextChoices + display labels) | T1, T3, T4 |
| §4.1 User.membership_level field | T1 |
| §4.2 Region model | T3 |
| §4.3 StationAssignment | T4 |
| §4.4 RegionAssignment | T4 |
| §4.5 Applicant invariant (model+demote+query) | T5 (model), T16 (demote), T11 (query exclude) |
| §4.6 AccountAuditLog | T6 (model), T13 (signals), T16 (membership wiring) |
| §4.7 Notification Routing | T11 (helper), T12 (dispatch + test-email) |
| §5.1 User-Detail UI | T15 (read), T16 (membership picker), T17 (region), T18 (station) |
| §5.2 Station-Detail UI | T19 (region), T20 (admin/maintainer + scope gates) |
| §5.3 Region-CRUD UI | T21 |
| §5.4 Permission Matrix | T20 (station-scoped gates), T17/T18 (admin gate) |
| §6 Migrations 0004-0009 + call-site refactor | T1-T6, T10, T22 |
| §6 Test infrastructure refactor (conftest) | T9 |
| §7 Tests | Inline per task |
| §8 Out-of-scope | Honored (no Telegram routing change, no signup, no per-severity, etc.) |
| §9 Known Limitations | Honored — migration 0005 no audit (per design), Region-Manager hierarchy (per UI permission gates) |

All spec sections map to at least one task.

**Placeholder scan:**

- All file paths are exact (no `<wherever>`-style placeholders). Two known soft spots: Task 15 explicitly asks the implementer to *identify* the user_detail template path before Task 16, because the user-detail rendering location depends on whether SSO or accounts owns it — this is research-then-act, not a placeholder. Task 16's commit step references the path as `apps/<wherever_view_lives>` for the same reason; implementer fills in.
- No "TBD" / "TODO" / "fill in details" anywhere.
- Test code blocks contain executable Python; production code blocks contain executable Python/HTML/SQL.

**Type / signature consistency:**

- `User.MembershipLevel` (TextChoices with APPLICANT/MEMBER/STAFF/ADMIN) used identically across T1, T6, T7, T8, T10, T11, T12, T16, T17, T18.
- `StationAssignment.Role.{ADMIN,MAINTAINER}` consistent T4 → T11/T13/T18/T20.
- `RegionAssignment.Role.MANAGER` consistent T4 → T11/T13/T17.
- `recipients_for_station_alert(station)` signature: T11 defines, T12 consumes.
- `_send_email_notification(alert, recipients_qs=None)` signature: T12 defines, no other consumer.
- `send_test_notification(channel, requesting_user=None)` signature: T12 defines, T12 consumes from view.
- `AccountAuditLog.log(*, event_type, actor=None, target_user=None, region=None, message="", ip_address=None)` signature: T6 defines, T13 + T16 + T17 consume.
- `User._invalidate_role_cache(user)`: T7 extends the attr list to include `is_internal`, T10 prunes back to `(is_admin, is_internal)` only after dropping the legacy properties.
- URL names: `accounts:membership_set`, `stations:region_assignment_create`, `stations:region_assignment_revoke`, `stations:station_assignment_create`, `stations:station_assignment_revoke`, `stations:station_set_region`, `stations:region_{list,create,update,delete}` — defined and consumed consistently.

**No spec requirement without a task.** Implementation plan is complete.
