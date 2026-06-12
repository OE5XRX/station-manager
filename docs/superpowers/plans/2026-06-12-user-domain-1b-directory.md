# Sub-Spec 1b Member-Directory — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Browse-Surface des Mitgliederverzeichnisses bauen. UserDetailView mit audience-aware Tabs, UserListView audience-aware mit Filter-Bar, Card-Migration aus user_form.html in user_detail.html, Audit-Tab + Global-Filter, Mobile-Polish.

**Architecture:** Read-side für das User-Domain. Backend-Permissions konsumieren das visibility-Modul aus 1a (`audience_for`, `directory_visible_fields`, `user_can_view_directory`). Templates rendern audience-aware aus dem View-Context (`is_admin_view` / `is_self_view` / `is_member_view`). Bestehende HTMX-Endpoints bleiben unverändert — nur die Render-Surface (UserDetailView) zieht um.

**Tech Stack:** Python 3.14, Django 6.0, pytest + pytest-django, ruff, HTMX (bestehend, kein Change).

**Spec:** `docs/superpowers/specs/2026-06-12-user-domain-1b-directory-design.md`
**Overview:** `docs/superpowers/specs/2026-06-09-user-domain-redesign-overview.md`

---

## File Structure

### Files to CREATE

| Pfad | Zweck |
|---|---|
| `apps/accounts/templates/accounts/user_detail.html` | Audience-aware Detail-Page mit Tabs (Overview, Rollen & Topologie, SSO, Audit). |
| `tests/test_user_detail_view.py` | Permission-Matrix + Context-Loading + Audit-Queryset Tests. |
| `tests/test_user_list_view_audience.py` | UserListView Audience-Filter + Filter-Bar Tests. |
| `tests/test_user_card_readonly.py` | Card-Template Readonly-Flag Tests. |
| `tests/test_audit_target_user_filter.py` | Global Audit-Log `?target_user=…` Tests. |

### Files to MODIFY

| Pfad | Änderung |
|---|---|
| `apps/accounts/views.py` | UserListView audience-aware Dispatch + Queryset; UserUpdateView.get_context_data simplification; UserUpdateView/UserCreateView success_url → user_detail; UserDetailView neu. |
| `apps/accounts/urls.py` | URL `users/<int:pk>/` → `UserDetailView`, name `user_detail`. |
| `apps/accounts/templates/accounts/user_list.html` | Audience-aware Refactor (Filter-Bar + Spalten + Row-Link). |
| `apps/accounts/templates/accounts/user_form.html` | Card-Includes entfernen (Cards leben jetzt in user_detail.html). |
| `apps/accounts/templates/accounts/_membership_card.html` | `readonly`-Flag, max-width inline raus. |
| `apps/accounts/templates/accounts/_region_assignments_card.html` | `readonly`-Flag, max-width inline raus. |
| `apps/accounts/templates/accounts/_station_assignments_card.html` | `readonly`-Flag, max-width inline raus. |
| `apps/sso/templates/sso/_sessions_card.html` | `readonly_self`-Flag (zeigt nur eigene Sessions, kein Revoke-aller). |
| `apps/audit/views.py` | `AuditLogFilterMixin.apply_shared_date_filters` akzeptiert `target_user`-Param. |
| `apps/audit/templates/audit/_audit_table.html` | Optional `hide_subject=False` Flag (Subject-Spalte hide). |

### Files unchanged

- Alle HTMX-Endpoints (`views_membership.py`, `views_region_assignments.py`, `views_station_assignments.py`, `apps/sso/views.py` für Grant-Toggle/Session-Revoke/Tag-Toggle).
- Bestehende SSO-Card Templates `_app_grants_card.html`, `_tags_card.html` — bleiben Admin-only via `is_admin_view`.
- `apps/accounts/visibility.py`, `geocoding.py`, `avatars.py` (kamen in 1a).

---

## Tasks

### Task 1: Pre-flight + baseline sanity

**Files:**
- Read only

- [ ] **Step 1: Verify branch + worktree**

Run: `git -C /home/pbuchegger/OE5XRX/station-manager/.worktrees/feat-user-domain-1b-directory branch --show-current`
Expected: `feat/user-domain-1b-directory`

- [ ] **Step 2: Run baseline test suite**

Run: `cd /home/pbuchegger/OE5XRX/station-manager/.worktrees/feat-user-domain-1b-directory && uv run pytest tests/ -x --tb=short 2>&1 | tail -5`
Expected: `719 passed` (alle Tests aus 1a + bestehenden).

- [ ] **Step 3: Verify migrations are up to date**

Run: `uv run python manage.py makemigrations --check --dry-run 2>&1 | tail -5`
Expected: keine pending Migrations für `accounts`.

---

### Task 2: UserDetailView — Class + URL + Permission tests

**Files:**
- Modify: `apps/accounts/views.py` (append `UserDetailView` class at bottom)
- Modify: `apps/accounts/urls.py` (add `users/<int:pk>/` path)
- Create: `tests/test_user_detail_view.py`

- [ ] **Step 1: Write failing permission tests**

Create NEW file `tests/test_user_detail_view.py`:

```python
"""Permission matrix for UserDetailView (Sub-Spec 1b).

Audience tiers come from apps/accounts/visibility.py:
  - Admin sees everyone
  - Self/Applicant sees own detail page
  - Member sees other members (not applicants) when target.is_directory_visible
  - Member sees invisible-target reduced to MINIMAL fields
"""

import pytest
from django.urls import reverse

from apps.accounts.models import User


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


@pytest.mark.django_db
class TestUserDetailViewPermissions:
    """Each request returns 200 / 404 based on Audience tier."""

    def url(self, target):
        return reverse("accounts:user_detail", kwargs={"pk": target.pk})

    def test_admin_sees_any_user(self, client, admin, member):
        client.force_login(admin)
        resp = client.get(self.url(member))
        assert resp.status_code == 200

    def test_admin_sees_applicant(self, client, admin, applicant):
        client.force_login(admin)
        resp = client.get(self.url(applicant))
        assert resp.status_code == 200

    def test_member_sees_other_member(self, client, member, other_member):
        client.force_login(member)
        resp = client.get(self.url(other_member))
        assert resp.status_code == 200

    def test_member_sees_own_detail(self, client, member):
        client.force_login(member)
        resp = client.get(self.url(member))
        assert resp.status_code == 200

    def test_member_cannot_see_applicant(self, client, member, applicant):
        client.force_login(member)
        resp = client.get(self.url(applicant))
        assert resp.status_code == 404

    def test_applicant_sees_own_detail(self, client, applicant):
        client.force_login(applicant)
        resp = client.get(self.url(applicant))
        assert resp.status_code == 200

    def test_applicant_cannot_see_member(self, client, applicant, member):
        client.force_login(applicant)
        resp = client.get(self.url(member))
        assert resp.status_code == 404

    def test_anonymous_redirected_to_login(self, client, member):
        # No login → LoginRequiredMixin redirects (302) to LOGIN_URL.
        resp = client.get(self.url(member))
        assert resp.status_code in (302, 401, 403)


@pytest.mark.django_db
class TestUserDetailViewAudienceFlags:
    """Context exposes audience-aware booleans for the template."""

    def url(self, target):
        return reverse("accounts:user_detail", kwargs={"pk": target.pk})

    def test_admin_view_flag(self, client, admin, member):
        client.force_login(admin)
        resp = client.get(self.url(member))
        ctx = resp.context
        assert ctx["is_admin_view"] is True
        assert ctx["is_self_view"] is False
        assert ctx["is_member_view"] is False

    def test_self_view_flag(self, client, member):
        client.force_login(member)
        resp = client.get(self.url(member))
        ctx = resp.context
        assert ctx["is_admin_view"] is False
        assert ctx["is_self_view"] is True
        assert ctx["is_member_view"] is False

    def test_member_view_flag(self, client, member, other_member):
        client.force_login(member)
        resp = client.get(self.url(other_member))
        ctx = resp.context
        assert ctx["is_admin_view"] is False
        assert ctx["is_self_view"] is False
        assert ctx["is_member_view"] is True

    def test_visible_fields_set_in_context(self, client, member, other_member):
        client.force_login(member)
        resp = client.get(self.url(other_member))
        assert "visible_fields" in resp.context
        # Member sees PUBLIC fields of a directory-visible target.
        assert "username" in resp.context["visible_fields"]
        # Member does NOT see private fields of other members.
        assert "phone" not in resp.context["visible_fields"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_user_detail_view.py -v 2>&1 | tail -30`
Expected: `NoReverseMatch` because `accounts:user_detail` URL doesn't exist yet.

- [ ] **Step 3: Implement UserDetailView**

Edit `apps/accounts/views.py`. Add `Http404` import at top:

```python
from django.http import Http404
from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView
```

Append at the bottom (after `UserDeleteView`):

```python
class UserDetailView(LoginRequiredMixin, DetailView):
    """Audience-aware detail page.

    Permission flows entirely through ``apps.accounts.visibility``:
      - Admin sees any user (incl. Applicants).
      - Self/Applicant sees own detail.
      - Member sees other Members (not Applicants), reduced fields when
        the target has ``is_directory_visible=False``.
      - Everyone else gets 404 (no existence-leak).
    """

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
        return ctx
```

- [ ] **Step 4: Register the URL**

Edit `apps/accounts/urls.py`. Add the path BEFORE `users/<int:pk>/edit/`:

```python
path("users/<int:pk>/", views.UserDetailView.as_view(), name="user_detail"),
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_user_detail_view.py -v 2>&1 | tail -25`
Expected: All 11 tests PASS.

- [ ] **Step 6: Smoke template — create a minimal user_detail.html so 200 responses don't TemplateDoesNotExist**

Create `apps/accounts/templates/accounts/user_detail.html` with a minimal stub for now (full template comes in later tasks):

```django
{% extends "base.html" %}
{% load i18n %}
{% block title %}{{ object.username }} · OE5XRX{% endblock %}
{% block content %}
  <h1>{{ object.username }}</h1>
  <p>Audience: {{ audience }}</p>
{% endblock %}
```

- [ ] **Step 7: Re-run + full regression**

Run: `uv run pytest tests/ -x --tb=short 2>&1 | tail -5`
Expected: All tests pass (was 719 baseline; new tests add ~11).

- [ ] **Step 8: ruff format + check**

Run: `uv run ruff format apps/accounts/views.py apps/accounts/urls.py tests/test_user_detail_view.py 2>&1 | tail -3`
Run: `uv run ruff check apps/accounts/views.py apps/accounts/urls.py tests/test_user_detail_view.py 2>&1 | tail -3`
Expected: Clean.

- [ ] **Step 9: Commit**

```bash
git add apps/accounts/views.py apps/accounts/urls.py \
        apps/accounts/templates/accounts/user_detail.html \
        tests/test_user_detail_view.py
git commit -m "feat(accounts): add UserDetailView with audience-aware permissions

GET users/<pk>/ → audience-aware detail page. Permission tier comes
from apps.accounts.visibility.audience_for(); no existence-leak via
403 (404 on no-access). Template is a stub here — full audience-aware
layout follows in later tasks of this plan."
```

---

### Task 3: UserDetailView — Context-loading (Admin + Self) + Audit-Queryset

**Files:**
- Modify: `apps/accounts/views.py` (extend `UserDetailView`)
- Modify: `tests/test_user_detail_view.py` (append)

- [ ] **Step 1: Write failing tests for context-loading**

Append to `tests/test_user_detail_view.py`:

```python
@pytest.mark.django_db
class TestUserDetailViewContextLoading:
    """Admin gets the full management context; Self gets only own helpers."""

    def url(self, target):
        return reverse("accounts:user_detail", kwargs={"pk": target.pk})

    def test_admin_context_has_membership_choices(self, client, admin, member):
        client.force_login(admin)
        resp = client.get(self.url(member))
        assert "membership_level_choices" in resp.context

    def test_admin_context_has_region_assignments(self, client, admin, member):
        client.force_login(admin)
        resp = client.get(self.url(member))
        assert "existing_region_assignments" in resp.context
        assert "available_regions" in resp.context

    def test_admin_context_has_station_assignments(self, client, admin, member):
        client.force_login(admin)
        resp = client.get(self.url(member))
        assert "existing_station_assignments" in resp.context
        assert "all_stations" in resp.context

    def test_admin_context_has_sso_helpers(self, client, admin, member):
        client.force_login(admin)
        resp = client.get(self.url(member))
        assert "app_grants_list" in resp.context
        assert "user_sessions" in resp.context
        assert "tag_entries" in resp.context

    def test_self_context_omits_admin_only(self, client, member):
        client.force_login(member)
        resp = client.get(self.url(member))
        # Self does NOT need the admin-only management context
        assert "available_regions" not in resp.context
        assert "all_stations" not in resp.context
        assert "app_grants_list" not in resp.context
        assert "tag_entries" not in resp.context

    def test_self_context_has_own_sessions(self, client, member):
        client.force_login(member)
        resp = client.get(self.url(member))
        # Self can see own sessions (for self-revoke)
        assert "user_sessions" in resp.context

    def test_member_context_minimal(self, client, member, other_member):
        client.force_login(member)
        resp = client.get(self.url(other_member))
        # Cross-member view has no Admin or SSO helpers
        assert "available_regions" not in resp.context
        assert "app_grants_list" not in resp.context
        assert "user_sessions" not in resp.context
        assert "tag_entries" not in resp.context

    def test_assignment_pills_for_admin(self, client, admin, member):
        client.force_login(admin)
        resp = client.get(self.url(member))
        # Pills available for the topology tab (read-only display)
        assert "region_assignment_pills" in resp.context
        assert "station_assignment_pills" in resp.context

    def test_assignment_pills_for_member_when_target_visible(
        self, client, member, other_member
    ):
        client.force_login(member)
        resp = client.get(self.url(other_member))
        # PUBLIC set includes "region_assignments" + "station_assignments"
        assert "region_assignment_pills" in resp.context
        assert "station_assignment_pills" in resp.context

    def test_no_assignment_pills_for_invisible_target(
        self, client, member, other_member
    ):
        other_member.is_directory_visible = False
        other_member.save()
        client.force_login(member)
        resp = client.get(self.url(other_member))
        # MINIMAL_DIRECTORY_FIELDS does not include assignments → no pills
        assert "region_assignment_pills" not in resp.context
        assert "station_assignment_pills" not in resp.context


@pytest.mark.django_db
class TestUserDetailViewAuditEntries:
    """Audit-Tab entries — Admin + Self get them, Member does not."""

    def url(self, target):
        return reverse("accounts:user_detail", kwargs={"pk": target.pk})

    def test_admin_gets_audit_entries(self, client, admin, member):
        client.force_login(admin)
        resp = client.get(self.url(member))
        assert "user_audit_entries" in resp.context
        # Empty list is OK; the key must exist for the template tab.
        assert resp.context["user_audit_entries"] == [] or isinstance(
            resp.context["user_audit_entries"], list
        )

    def test_self_gets_own_audit_entries(self, client, member):
        client.force_login(member)
        resp = client.get(self.url(member))
        assert "user_audit_entries" in resp.context

    def test_member_does_not_get_audit_entries(self, client, member, other_member):
        client.force_login(member)
        resp = client.get(self.url(other_member))
        assert "user_audit_entries" not in resp.context

    def test_audit_entries_account_and_sso_merged(self, client, admin, member):
        """AccountAuditLog entries on target_user + SsoAuditLog entries
        on target_user or actor are merged and sorted by created_at desc.
        """
        from apps.accounts.models import AccountAuditLog
        from apps.sso.models import SsoAuditLog

        # Mix of entries
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.USER_CREATED,
            target_user=member,
            message="created",
        )
        SsoAuditLog.log(
            event_type=SsoAuditLog.EventType.LOGIN_SUCCESS,
            target_user=member,
            message="login",
        )

        client.force_login(admin)
        resp = client.get(self.url(member))
        entries = resp.context["user_audit_entries"]
        assert len(entries) == 2
        # Each entry is a (category, log_obj) tuple
        categories = {cat for cat, _ in entries}
        assert categories == {"account", "sso"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_user_detail_view.py::TestUserDetailViewContextLoading tests/test_user_detail_view.py::TestUserDetailViewAuditEntries -v 2>&1 | tail -25`
Expected: Most fail with KeyError because context keys don't exist yet.

- [ ] **Step 3: Implement context-loading helpers**

Edit `apps/accounts/views.py`. In `UserDetailView`, replace `get_context_data` with a full version and add the helpers:

```python
    def get_context_data(self, **kwargs):
        from .visibility import Audience, directory_visible_fields

        ctx = super().get_context_data(**kwargs)
        aud = self._audience
        ctx["audience"] = aud.value
        ctx["is_admin_view"] = aud == Audience.ADMIN
        ctx["is_self_view"] = aud in (Audience.SELF, Audience.APPLICANT)
        ctx["is_member_view"] = aud == Audience.MEMBER
        ctx["visible_fields"] = directory_visible_fields(self.request.user, self.object)

        if aud == Audience.ADMIN:
            ctx.update(self._admin_context_data())
        elif aud in (Audience.SELF, Audience.APPLICANT):
            ctx.update(self._self_context_data())

        # Assignment-Pills für Topology-Tab (alle Audiences, sofern Felder visible).
        if "region_assignments" in ctx["visible_fields"]:
            ctx["region_assignment_pills"] = self.object.region_assignments.select_related(
                "region"
            )
        if "station_assignments" in ctx["visible_fields"]:
            ctx["station_assignment_pills"] = self.object.station_assignments.select_related(
                "station"
            )

        # Audit-Tab nur für Self + Admin.
        if aud in (Audience.ADMIN, Audience.SELF, Audience.APPLICANT):
            ctx["user_audit_entries"] = self._build_user_audit(self.object)

        return ctx

    def _admin_context_data(self):
        """Admin sees the full management context — equivalent of the
        old UserUpdateView.get_context_data (which itself moves to a
        slim form-only context in a later task).
        """
        from django.contrib.auth.models import Group

        from apps.sso.views import _active_sessions_for, _build_grants_for_user
        from apps.stations.models import Region, Station

        ctx = {
            "app_grants_list": _build_grants_for_user(self.object),
            "user_sessions": _active_sessions_for(self.object),
            "membership_level_choices": User.MembershipLevel.choices,
        }
        member_ids = set(self.object.groups.values_list("pk", flat=True))
        ctx["tag_entries"] = [
            {"group": g, "is_member": g.pk in member_ids}
            for g in Group.objects.order_by("name")
        ]

        existing_ra = list(self.object.region_assignments.select_related("region"))
        ctx["existing_region_assignments"] = existing_ra
        assigned_region_ids = {ra.region_id for ra in existing_ra}
        ctx["available_regions"] = Region.objects.exclude(pk__in=assigned_region_ids).order_by(
            "name"
        )
        ctx["existing_station_assignments"] = list(
            self.object.station_assignments.select_related("station")
        )
        ctx["all_stations"] = Station.objects.order_by("name")
        return ctx

    def _self_context_data(self):
        """Self only needs own SSO sessions (so the template can show
        the Self-Sessions card with revoke-own).
        """
        from apps.sso.views import _active_sessions_for

        return {"user_sessions": _active_sessions_for(self.object)}

    def _build_user_audit(self, target_user):
        """Merge AccountAuditLog (target_user=...) + SsoAuditLog
        (target_user OR actor matches) into a (category, entry)
        list sorted by created_at desc, capped at the top 50.
        """
        from django.db.models import Q

        from apps.accounts.models import AccountAuditLog
        from apps.sso.models import SsoAuditLog

        MAX_PER_SOURCE = 500

        account_qs = (
            AccountAuditLog.objects.filter(target_user=target_user)
            .select_related("actor", "region")
            .order_by("-created_at")[:MAX_PER_SOURCE]
        )
        sso_qs = (
            SsoAuditLog.objects.filter(Q(target_user=target_user) | Q(actor=target_user))
            .select_related("actor", "target_user", "application")
            .order_by("-created_at")[:MAX_PER_SOURCE]
        )
        merged = [("account", e) for e in account_qs] + [("sso", e) for e in sso_qs]
        merged.sort(key=lambda pair: pair[1].created_at, reverse=True)
        return merged[:50]
```

- [ ] **Step 4: Run new tests to verify they pass**

Run: `uv run pytest tests/test_user_detail_view.py -v 2>&1 | tail -30`
Expected: All tests in TestUserDetailViewContextLoading + TestUserDetailViewAuditEntries PASS, plus earlier tests still PASS.

- [ ] **Step 5: Full regression**

Run: `uv run pytest tests/ -x --tb=short 2>&1 | tail -5`
Expected: All tests pass.

- [ ] **Step 6: ruff format + check**

Run: `uv run ruff format apps/accounts/views.py tests/test_user_detail_view.py 2>&1 | tail -3`
Run: `uv run ruff check apps/accounts/views.py tests/test_user_detail_view.py 2>&1 | tail -3`
Expected: Clean.

- [ ] **Step 7: Commit**

```bash
git add apps/accounts/views.py tests/test_user_detail_view.py
git commit -m "feat(accounts): UserDetailView context-loading + audit queryset

_admin_context_data loads the full management context (assignments,
SSO grants, sessions, tags, membership-level choices) — the same
shape UserUpdateView used to compute. _self_context_data loads only
own SSO sessions. _build_user_audit merges AccountAuditLog +
SsoAuditLog entries about target_user, sorted by created_at desc."
```

---

### Task 4: `hide_subject` flag on `_audit_table.html` + `target_user` filter

**Files:**
- Modify: `apps/audit/templates/audit/_audit_table.html`
- Modify: `apps/audit/views.py`
- Create: `tests/test_audit_target_user_filter.py`

- [ ] **Step 1: Write failing tests for `target_user` filter**

Create NEW file `tests/test_audit_target_user_filter.py`:

```python
"""Global Audit-Log filter: ?target_user=<pk> narrows AccountAuditLog
and SsoAuditLog entries to the given user (subject or actor).

The filter is consumed by the "Open in global audit log" link from
the per-user audit tab (UserDetailView).
"""

import pytest
from django.urls import reverse

from apps.accounts.models import AccountAuditLog, User
from apps.sso.models import SsoAuditLog


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
        membership_level=User.MembershipLevel.MEMBER,
    )


@pytest.fixture
def other_member(db):
    return User.objects.create_user(
        username="OE5MEM2",
        password="x",
        membership_level=User.MembershipLevel.MEMBER,
    )


@pytest.mark.django_db
class TestTargetUserFilter:
    """?target_user=<pk> narrows the merged feed."""

    def url(self, target_user):
        return reverse("audit:audit_list") + f"?category=account&target_user={target_user.pk}"

    def test_account_filter_matches_target(self, client, admin, member, other_member):
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.USER_CREATED,
            target_user=member,
            message="created member",
        )
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.USER_CREATED,
            target_user=other_member,
            message="created other",
        )

        client.force_login(admin)
        resp = client.get(self.url(member))
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "created member" in body
        assert "created other" not in body

    def test_sso_filter_matches_target(self, client, admin, member, other_member):
        SsoAuditLog.log(
            event_type=SsoAuditLog.EventType.LOGIN_SUCCESS,
            target_user=member,
            message="member login",
        )
        SsoAuditLog.log(
            event_type=SsoAuditLog.EventType.LOGIN_SUCCESS,
            target_user=other_member,
            message="other login",
        )

        client.force_login(admin)
        resp = client.get(
            reverse("audit:audit_list") + f"?category=sso&target_user={member.pk}"
        )
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "member login" in body
        assert "other login" not in body

    def test_no_target_user_param_shows_all(self, client, admin, member, other_member):
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.USER_CREATED,
            target_user=member,
            message="entry-a",
        )
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.USER_CREATED,
            target_user=other_member,
            message="entry-b",
        )

        client.force_login(admin)
        resp = client.get(reverse("audit:audit_list") + "?category=account")
        body = resp.content.decode()
        assert "entry-a" in body
        assert "entry-b" in body


@pytest.mark.django_db
class TestAuditTableHideSubject:
    """_audit_table.html hides the Subject column when hide_subject=True."""

    def test_global_feed_shows_subject_header(self, client, admin, member):
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.USER_CREATED,
            target_user=member,
            message="x",
        )
        client.force_login(admin)
        resp = client.get(reverse("audit:audit_list") + "?category=account")
        # Global feed renders with hide_subject=False → "Subject" column header present
        assert "Subject" in resp.content.decode()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_audit_target_user_filter.py -v 2>&1 | tail -20`
Expected: Filter tests fail because `target_user` GET-param has no effect yet.

- [ ] **Step 3: Extend `AuditLogFilterMixin.apply_shared_date_filters` in `apps/audit/views.py`**

Find the existing `apply_shared_date_filters` method (around line 56) and add `target_user` support:

```python
    def apply_shared_date_filters(self, queryset, params):
        """Shared date-filter helper used by all feeds except ``station``."""
        from django.db.models import Q

        from apps.sso.models import SsoAuditLog

        date_from = params.get("date_from")
        if date_from:
            queryset = queryset.filter(created_at__date__gte=date_from)
        date_to = params.get("date_to")
        if date_to:
            queryset = queryset.filter(created_at__date__lte=date_to)
        # Per-user filter — consumed by the "Open in global audit log" link
        # from the UserDetailView audit-tab. For AccountAuditLog the user is
        # always the target (`target_user`). For SsoAuditLog the per-user
        # audit-tab matches `target_user OR actor` (e.g. LOGIN_SUCCESS fires
        # with the user as actor); the global filter mirrors that contract.
        target_user = params.get("target_user")
        if target_user:
            if queryset.model is SsoAuditLog:
                queryset = queryset.filter(
                    Q(target_user_id=target_user) | Q(actor_id=target_user)
                )
            else:
                queryset = queryset.filter(target_user_id=target_user)
        return queryset
```

- [ ] **Step 4: Add `hide_subject` flag to `_audit_table.html`**

Edit `apps/audit/templates/audit/_audit_table.html`. Wrap the `<th>{% trans "Subject" %}</th>` and the corresponding `<td>` cells with `{% if not hide_subject %}…{% endif %}` so they render by default but disappear when the include passes `hide_subject=True`.

Specifically, find:
```html
        <th>{% trans "Subject" %}</th>
```
Replace with:
```html
        {% if not hide_subject %}<th>{% trans "Subject" %}</th>{% endif %}
```

And for each of the 3 category branches (`station`, `account`, sso-else) find the `<td data-label="{% trans 'Subject' %}">…</td>` and wrap:
```html
        {% if not hide_subject %}
        <td data-label="{% trans 'Subject' %}">
          …
        </td>
        {% endif %}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_audit_target_user_filter.py -v 2>&1 | tail -20`
Expected: All tests PASS.

- [ ] **Step 6: Verify the global audit page still works**

Run: `uv run pytest tests/test_audit_log_emission.py tests/test_account_audit_log.py tests/test_sso_audit.py 2>&1 | tail -5`
Expected: No regression.

- [ ] **Step 7: ruff format + check**

Run: `uv run ruff format apps/audit/views.py tests/test_audit_target_user_filter.py 2>&1 | tail -3`
Run: `uv run ruff check apps/audit/views.py tests/test_audit_target_user_filter.py 2>&1 | tail -3`
Expected: Clean.

- [ ] **Step 8: Commit**

```bash
git add apps/audit/views.py apps/audit/templates/audit/_audit_table.html \
        tests/test_audit_target_user_filter.py
git commit -m "feat(audit): add target_user filter + hide_subject template flag

Global audit log accepts ?target_user=<pk> to filter AccountAuditLog
and SsoAuditLog rows where target_user (or actor, for SSO) matches.
Consumed by the 'Open in global audit log →' link on the per-user
audit tab from UserDetailView.

_audit_table.html grows an optional hide_subject=False flag — when
True the Subject column is omitted (used by the per-user audit tab
where the subject is redundantly always the page's User)."
```

---

### Task 5: Card-Templates — `readonly` flag + max-width cleanup

**Files:**
- Modify: `apps/accounts/templates/accounts/_membership_card.html`
- Modify: `apps/accounts/templates/accounts/_region_assignments_card.html`
- Modify: `apps/accounts/templates/accounts/_station_assignments_card.html`
- Modify: `apps/sso/templates/sso/_sessions_card.html`
- Create: `tests/test_user_card_readonly.py`

> **Subagent:** `pixel`. MUST invoke `Skill("frontend-design")` before any HTML edit.

- [ ] **Step 1: Write failing tests for the readonly flag behavior**

Create NEW file `tests/test_user_card_readonly.py`:

```python
"""Card templates accept a readonly=True flag that hides Add/Revoke
forms. Tests render the templates standalone and assert HTML markers.
"""

import pytest
from django.template.loader import render_to_string

from apps.accounts.models import User
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
        membership_level=User.MembershipLevel.MEMBER,
    )


@pytest.fixture
def region(db):
    return Region.objects.create(name="Innviertel")


@pytest.fixture
def station(db, region):
    return Station.objects.create(name="OE5XRX-Test", callsign="OE5XRX", region=region)


@pytest.mark.django_db
class TestMembershipCardReadonly:
    def test_admin_mode_has_apply_button(self, member, admin):
        html = render_to_string(
            "accounts/_membership_card.html",
            {
                "object": member,
                "membership_level_choices": User.MembershipLevel.choices,
                "readonly": False,
                "request": _request(admin),
            },
        )
        assert "Apply" in html or "submit" in html

    def test_readonly_mode_has_no_form(self, member, admin):
        html = render_to_string(
            "accounts/_membership_card.html",
            {
                "object": member,
                "membership_level_choices": User.MembershipLevel.choices,
                "readonly": True,
                "request": _request(admin),
            },
        )
        # No POST form, no Apply button
        assert "<form" not in html
        assert "Apply" not in html


@pytest.mark.django_db
class TestRegionAssignmentsCardReadonly:
    def test_admin_mode_renders_add_form(self, member, region, admin):
        html = render_to_string(
            "accounts/_region_assignments_card.html",
            {
                "object": member,
                "existing_region_assignments": [],
                "available_regions": Region.objects.all(),
                "readonly": False,
                "request": _request(admin),
            },
        )
        assert "Add Region-Manager assignment" in html or "<form" in html

    def test_readonly_mode_omits_add_form(self, member, region, admin):
        html = render_to_string(
            "accounts/_region_assignments_card.html",
            {
                "object": member,
                "existing_region_assignments": [],
                "available_regions": Region.objects.all(),
                "readonly": True,
                "request": _request(admin),
            },
        )
        assert "<form" not in html

    def test_readonly_mode_keeps_existing_pills(self, member, region, admin):
        RegionAssignment.objects.create(
            user=member, region=region, role=RegionAssignment.Role.MANAGER,
            assigned_by=admin,
        )
        existing = list(member.region_assignments.select_related("region"))
        html = render_to_string(
            "accounts/_region_assignments_card.html",
            {
                "object": member,
                "existing_region_assignments": existing,
                "available_regions": Region.objects.none(),
                "readonly": True,
                "request": _request(admin),
            },
        )
        # Pill text present, but no ✕ revoke button
        assert "Innviertel" in html
        assert "✕" not in html


@pytest.mark.django_db
class TestStationAssignmentsCardReadonly:
    def test_admin_mode_renders_add_form(self, member, station, admin):
        html = render_to_string(
            "accounts/_station_assignments_card.html",
            {
                "object": member,
                "existing_station_assignments": [],
                "all_stations": Station.objects.all(),
                "readonly": False,
                "request": _request(admin),
            },
        )
        assert "Add Station assignment" in html or "<form" in html

    def test_readonly_mode_omits_forms(self, member, station, admin):
        html = render_to_string(
            "accounts/_station_assignments_card.html",
            {
                "object": member,
                "existing_station_assignments": [],
                "all_stations": Station.objects.all(),
                "readonly": True,
                "request": _request(admin),
            },
        )
        assert "<form" not in html


def _request(user):
    """Build a fake request object exposing the bits the templates read."""
    from django.test import RequestFactory
    req = RequestFactory().get("/")
    req.user = user
    req.csp_nonce = ""
    return req
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_user_card_readonly.py -v 2>&1 | tail -20`
Expected: Readonly tests fail because the cards always render the forms.

- [ ] **Step 3: Refactor `_membership_card.html`**

Replace `apps/accounts/templates/accounts/_membership_card.html`:

```django
{% load i18n %}
<section class="panel" id="membership-card">
  <div class="panel-head">
    <div class="panel-title"><span class="dot"></span>{% trans "Vereins-Rolle" %}</div>
  </div>
  <div class="panel-body">
    <p class="t-muted" style="margin-bottom:12px;">
      {% trans "Current:" %}
      <strong>{{ object.get_membership_level_display }}</strong>
    </p>
    {% if not readonly %}
    <form hx-post="{% url 'accounts:membership_set' object.pk %}"
          hx-on::after-request="if (event.detail.successful) window.location.reload()">
      {% csrf_token %}
      <label class="form-label" for="membership-level-select">
        {% trans "Set membership level" %}:
      </label>
      <select id="membership-level-select" name="level" class="form-select">
        {% for value, label in membership_level_choices %}
          <option value="{{ value }}" {% if object.membership_level == value %}selected{% endif %}>
            {{ label }}
          </option>
        {% endfor %}
      </select>
      <div class="row-gap-8" style="margin-top:12px;">
        <button type="submit" class="btn btn-primary btn-sm">{% trans "Apply" %}</button>
      </div>
      <p class="t-muted t-mono-sm" style="margin-top:8px;">
        {% trans "Demote to Vereins-Bewerber requires removing all topology assignments first." %}
      </p>
    </form>
    {% endif %}
  </div>
</section>
```

(Changes: inline `max-width:640px;margin-top:16px;` and `style="max-width:280px;"` on the select are removed. Add/Apply form wrapped in `{% if not readonly %}`.)

- [ ] **Step 4: Refactor `_region_assignments_card.html`**

Replace `apps/accounts/templates/accounts/_region_assignments_card.html`:

```django
{% load i18n %}
<section class="panel" id="region-assignments-card">
  <div class="panel-head">
    <div class="panel-title"><span class="dot"></span>{% trans "Region-Manager" %}</div>
  </div>
  <div class="panel-body">

    {% if object.membership_level == "applicant" %}
      <p class="t-muted">
        {% trans "Vereins-Bewerber cannot hold a Region-Manager assignment. Promote the user to Vereins-Mitglied first." %}
      </p>
    {% else %}

      {% if existing_region_assignments %}
        <ul class="stack-gap-2" style="margin-bottom:12px;list-style:none;padding-left:0;">
          {% for ra in existing_region_assignments %}
            <li class="row-gap-8" style="align-items:center;">
              <span class="pill pill-muted">{{ ra.region.name }}</span>
              {% if not readonly %}
              <form hx-post="{% url 'accounts:region_assignment_revoke' ra.pk %}"
                    hx-on::after-request="if (event.detail.successful) window.location.reload()"
                    style="display:inline;">
                {% csrf_token %}
                <button type="submit" class="btn btn-ghost btn-sm"
                        title="{% trans 'Revoke assignment' %}">✕</button>
              </form>
              {% endif %}
            </li>
          {% endfor %}
        </ul>
      {% else %}
        <p class="t-muted" style="margin-bottom:12px;">
          {% trans "No Region-Manager assignments yet." %}
        </p>
      {% endif %}

      {% if not readonly and available_regions %}
        <form hx-post="{% url 'accounts:region_assignment_create' object.pk %}"
              hx-on::after-request="if (event.detail.successful) window.location.reload()">
          {% csrf_token %}
          <label class="form-label" for="region-add-select">
            {% trans "Add Region-Manager assignment" %}:
          </label>
          <div class="row-gap-8" style="align-items:flex-end;">
            <select id="region-add-select" name="region" class="form-select">
              {% for r in available_regions %}
                <option value="{{ r.pk }}">{{ r.name }}</option>
              {% endfor %}
            </select>
            <button type="submit" class="btn btn-primary btn-sm">{% trans "Add" %}</button>
          </div>
        </form>
      {% elif not readonly %}
        <p class="t-muted t-mono-sm">
          {% trans "No remaining regions to assign." %}
        </p>
      {% endif %}

    {% endif %}
  </div>
</section>
```

(Changes: inline `style="max-width:640px;margin-top:16px;"` and `style="max-width:280px;"` removed. Revoke `<form>` per pill wrapped in `{% if not readonly %}`. Add-form wrapped in `{% if not readonly and available_regions %}`.)

- [ ] **Step 5: Refactor `_station_assignments_card.html`**

Edit `apps/accounts/templates/accounts/_station_assignments_card.html`. Remove the section-level inline `style="max-width:640px;margin-top:16px;"`. Remove the `style="max-width:240px;"` and `style="max-width:200px;"` on the two selects. Wrap the Revoke-form per pill in `{% if not readonly %}…{% endif %}`. Wrap the Add-form `<form id="station-assignment-add-form" ...>` plus its inline `<script>` block in `{% if not readonly %}…{% endif %}`.

The structure should be:

```django
{% load i18n %}
<section class="panel" id="station-assignments-card">
  <div class="panel-head">
    <div class="panel-title"><span class="dot"></span>{% trans "Station-Zuordnungen" %}</div>
  </div>
  <div class="panel-body">
    {% if object.membership_level == "applicant" %}
      <p class="t-muted">
        {% trans "Vereins-Bewerber cannot hold a Station-Admin or Maintainer assignment. Promote the user to Vereins-Mitglied first." %}
      </p>
    {% else %}
      {% if existing_station_assignments %}
        <ul class="stack-gap-2" style="margin-bottom:12px;list-style:none;padding-left:0;">
          {% for sa in existing_station_assignments %}
            <li class="row-gap-8" style="align-items:center;">
              <span class="pill pill-muted">{{ sa.station.callsign|default:sa.station.name }}</span>
              <span class="t-mono-sm">{{ sa.get_role_display }}</span>
              {% if not readonly %}
              <form hx-post="{% url 'accounts:station_assignment_revoke' sa.pk %}"
                    hx-on::after-request="if (event.detail.successful) window.location.reload()"
                    style="display:inline;">
                {% csrf_token %}
                <button type="submit" class="btn btn-ghost btn-sm"
                        title="{% trans 'Revoke assignment' %}">✕</button>
              </form>
              {% endif %}
            </li>
          {% endfor %}
        </ul>
      {% else %}
        <p class="t-muted" style="margin-bottom:12px;">
          {% trans "No Station assignments yet." %}
        </p>
      {% endif %}

      {% if not readonly and all_stations %}
        <form id="station-assignment-add-form"
              hx-post="{% url 'accounts:station_assignment_create' object.pk %}"
              hx-on::after-request="handleStationAssignmentResponse(event)">
          {% csrf_token %}
          <label class="form-label">{% trans "Add Station assignment" %}:</label>
          <div class="row-gap-8" style="align-items:flex-end;flex-wrap:wrap;">
            <select name="station" class="form-select">
              {% for s in all_stations %}
                <option value="{{ s.pk }}">{{ s.callsign|default:s.name }}</option>
              {% endfor %}
            </select>
            <select name="role" class="form-select">
              <option value="admin">{% trans "Station-Admin (max 1)" %}</option>
              <option value="maintainer" selected>{% trans "Station-Maintainer" %}</option>
            </select>
            <input type="hidden" name="takeover" value="0">
            <button type="submit" class="btn btn-primary btn-sm">{% trans "Add" %}</button>
          </div>
        </form>

        <script nonce="{{ csp_nonce }}">
          function handleStationAssignmentResponse(event) {
            const xhr = event.detail.xhr;
            if (event.detail.successful) {
              window.location.reload();
              return;
            }
            const form = document.getElementById('station-assignment-add-form');
            form.querySelector('input[name=takeover]').value = '0';
            if (xhr.status === 409) {
              let info = {};
              try { info = JSON.parse(xhr.responseText); } catch (_) {}
              const currentAdmin = info.current_admin_username || 'someone';
              const ok = window.confirm(
                "{% trans 'Station already has' %} " + currentAdmin +
                " {% trans 'as Station-Admin. Take over the role?' %}"
              );
              if (!ok) return;
              form.querySelector('input[name=takeover]').value = '1';
              htmx.trigger(form, 'submit');
            }
          }
        </script>
      {% elif not readonly %}
        <p class="t-muted t-mono-sm">
          {% trans "No stations to assign." %}
        </p>
      {% endif %}

    {% endif %}
  </div>
</section>
```

- [ ] **Step 6: Add `readonly_self` flag in `_sessions_card.html`**

Edit `apps/sso/templates/sso/_sessions_card.html`. Find the `<section class="panel" id="sessions-card" style="max-width:960px;margin-top:16px;">` — remove the inline style.

For now we keep the Revoke button on Self-Sessions (users CAN revoke their own sessions — same endpoint), so the readonly_self flag doesn't change rendering of the per-row Revoke button. The flag's primary job is signalling intent for the template-level title/description (and for future cross-user views in 1c).

Replace the opening section line:
```html
<section class="panel" id="sessions-card" style="max-width:960px;margin-top:16px;">
```
with:
```html
<section class="panel" id="sessions-card">
```

Add a panel-foot note when `readonly_self=True`:
Find the existing `<div class="panel-foot row-split">` and change the inner content to:
```html
<div class="panel-foot row-split">
  <span class="t-mono" style="font-size:11px;letter-spacing:0.08em;color:var(--ink-3);">
    {% if readonly_self %}
      {% trans "SSO · your own live token sessions" %}
    {% else %}
      {% trans "SSO · live token sessions" %}
    {% endif %}
  </span>
  <a class="btn btn-sm btn-ghost" href="{% url 'sso:dashboard' %}">
    {% trans "SSO dashboard" %} →
  </a>
</div>
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/test_user_card_readonly.py -v 2>&1 | tail -25`
Expected: All tests PASS.

- [ ] **Step 8: Verify HTMX-endpoint tests still pass**

Run: `uv run pytest tests/test_views_membership.py tests/test_views_region_assignments.py tests/test_views_station_assignments.py 2>&1 | tail -5`
Expected: No regression.

- [ ] **Step 9: Full regression**

Run: `uv run pytest tests/ -x --tb=short 2>&1 | tail -5`
Expected: All pass.

- [ ] **Step 10: ruff check**

Run: `uv run ruff check tests/test_user_card_readonly.py 2>&1 | tail -3`
Expected: Clean.

- [ ] **Step 11: Commit**

```bash
git add apps/accounts/templates/accounts/_membership_card.html \
        apps/accounts/templates/accounts/_region_assignments_card.html \
        apps/accounts/templates/accounts/_station_assignments_card.html \
        apps/sso/templates/sso/_sessions_card.html \
        tests/test_user_card_readonly.py
git commit -m "feat(accounts): readonly flag on management cards + inline-width cleanup

Membership / Region-Assignments / Station-Assignments cards accept a
readonly=True context flag that suppresses the Add/Revoke forms. The
existing pills/labels stay rendered. SSO sessions card gets a
readonly_self flag (label-only for now; same Revoke endpoint).

Inline 'max-width' styles (640/960/280/240/200) removed — width now
comes from the surrounding grid in user_detail.html."
```

---

### Task 6: `user_detail.html` full template

**Files:**
- Modify: `apps/accounts/templates/accounts/user_detail.html` (replace stub)

> **Subagent:** `pixel`. MUST invoke `Skill("frontend-design")` before any HTML edit.

This task delivers the full audience-aware Detail-Page. Use the existing `stations/station_detail.html` as the pattern reference (Page-Head + Summary-Bar + `data-tabs` block + `data-tab-panel="<name>"` blocks).

- [ ] **Step 1: Write failing template tests**

Append to `tests/test_user_detail_view.py`:

```python
@pytest.mark.django_db
class TestUserDetailViewTemplateRendering:
    """High-level HTML smoke tests for the four audience modes."""

    def url(self, target):
        return reverse("accounts:user_detail", kwargs={"pk": target.pk})

    def test_admin_view_renders_4_tabs(self, client, admin, member):
        client.force_login(admin)
        resp = client.get(self.url(member))
        body = resp.content.decode()
        assert 'data-tab="overview"' in body
        assert 'data-tab="topology"' in body
        assert 'data-tab="sso"' in body
        assert 'data-tab="audit"' in body

    def test_admin_view_has_edit_and_delete_buttons(self, client, admin, member):
        client.force_login(admin)
        resp = client.get(self.url(member))
        body = resp.content.decode()
        assert reverse("accounts:user_edit", kwargs={"pk": member.pk}) in body
        assert reverse("accounts:user_delete", kwargs={"pk": member.pk}) in body

    def test_admin_self_view_omits_edit_delete(self, client, admin):
        """Admin viewing own detail page does NOT see Edit/Delete — self-edit
        goes through accounts:profile (1c), and self-delete is blocked."""
        client.force_login(admin)
        resp = client.get(self.url(admin))
        body = resp.content.decode()
        # The Detail-Page does not show self-Edit/Delete buttons (deferred to profile)
        assert reverse("accounts:user_edit", kwargs={"pk": admin.pk}) not in body
        assert reverse("accounts:user_delete", kwargs={"pk": admin.pk}) not in body

    def test_self_view_has_profile_edit_action(self, client, member):
        client.force_login(member)
        resp = client.get(self.url(member))
        body = resp.content.decode()
        assert reverse("accounts:profile") in body

    def test_member_view_renders_2_tabs(self, client, member, other_member):
        client.force_login(member)
        resp = client.get(self.url(other_member))
        body = resp.content.decode()
        assert 'data-tab="overview"' in body
        assert 'data-tab="topology"' in body
        assert 'data-tab="sso"' not in body
        assert 'data-tab="audit"' not in body

    def test_member_view_hides_private_fields(self, client, member, other_member):
        other_member.phone = "+43 1 23456"
        other_member.address = "Geheimstraße 7"
        other_member.email = "secret@example.org"
        other_member.save()
        client.force_login(member)
        resp = client.get(self.url(other_member))
        body = resp.content.decode()
        # Phone and address must NOT appear for cross-member view.
        assert "+43 1 23456" not in body
        assert "Geheimstraße 7" not in body
        # Email is in PUBLIC_PROFILE_FIELDS, so it CAN show.

    def test_member_view_invisible_target_minimal(self, client, member, other_member):
        other_member.bio = "Should not appear"
        other_member.qth_name = "Should not appear either"
        other_member.is_directory_visible = False
        other_member.save()
        client.force_login(member)
        resp = client.get(self.url(other_member))
        body = resp.content.decode()
        assert "Should not appear" not in body
        # Membership pill and username still show
        assert other_member.username in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_user_detail_view.py::TestUserDetailViewTemplateRendering -v 2>&1 | tail -20`
Expected: Most fail because the stub template doesn't have tabs / actions / cards.

- [ ] **Step 3: Build the full user_detail.html**

Replace `apps/accounts/templates/accounts/user_detail.html` with the full audience-aware template. Follow the structure of `apps/stations/templates/stations/station_detail.html`. Required sections:

```django
{% extends "base.html" %}
{% load i18n %}

{% block title %}{{ object.username }} · OE5XRX{% endblock %}

{% block breadcrumbs %}
  <a href="{% url 'dashboard:index' %}">OE5XRX</a>
  <span class="sep">/</span>
  {% if is_member_view %}
    <a href="{% url 'accounts:user_list' %}">{% trans "Mitglieder" %}</a>
  {% else %}
    <a href="{% url 'accounts:user_list' %}">{% trans "Users" %}</a>
  {% endif %}
  <span class="sep">/</span>
  <span class="cur">{{ object.username }}</span>
{% endblock %}

{% block content %}
<div class="page-head">
  <div class="page-head-main">
    <div class="page-eyebrow">
      {% if is_member_view %}
        {% trans "Verein" %} · {% trans "Mitglied" %}
      {% else %}
        {% trans "User" %} · #{{ object.pk|stringformat:"03d" }}
      {% endif %}
    </div>
    <h1 class="page-title">
      <div class="sb-avatar" style="width:48px;height:48px;line-height:48px;font-size:20px;">
        {% if object.avatar %}<img src="{{ object.avatar.url }}" alt="{{ object.username }}">
        {% else %}{{ object.username|slice:":1"|upper }}{% endif %}
      </div>
      <span>{{ object.username }}</span>
      <span class="t-muted" style="font-family:var(--font-body);font-weight:500;">
        {{ object.get_full_name|default:"—" }}
      </span>
    </h1>
    <div class="row-gap-8 mt-10">
      {# Membership pill: always #}
      {% if object.membership_level == "admin" %}
        <span class="pill pill-accent">{{ object.get_membership_level_display }}</span>
      {% elif object.membership_level == "staff" %}
        <span class="pill pill-violet">{{ object.get_membership_level_display }}</span>
      {% elif object.membership_level == "member" %}
        <span class="pill">{{ object.get_membership_level_display }}</span>
      {% else %}
        <span class="pill pill-muted">{{ object.get_membership_level_display }}</span>
      {% endif %}
      {# is_active pill — Admin or Self, and only when not active #}
      {% if not object.is_active and is_admin_view or not object.is_active and is_self_view %}
        <span class="pill pill-offline">INACTIVE</span>
      {% endif %}
      {# language pill — Self + Admin #}
      {% if is_admin_view or is_self_view %}
        <span class="pill pill-muted">{{ object.get_language_display }}</span>
      {% endif %}
      {# qth pill if visible + set #}
      {% if "qth_name" in visible_fields and object.qth_name %}
        <span class="pill pill-muted">QTH {{ object.qth_name }}</span>
      {% endif %}
    </div>
  </div>
  <div class="page-head-actions">
    {% if is_admin_view and object.pk != request.user.pk %}
      <a href="{% url 'accounts:user_edit' object.pk %}" class="btn btn-ghost">{% trans "Edit identity" %}</a>
      <a href="{% url 'accounts:user_delete' object.pk %}" class="btn btn-danger">{% trans "Delete" %}</a>
    {% elif is_self_view %}
      <a href="{% url 'accounts:profile' %}" class="btn btn-ghost">{% trans "Edit profile" %}</a>
    {% endif %}
  </div>
</div>

{# Empty-state shortcut for Member-viewing-invisible-target #}
{% if is_member_view and not object.is_directory_visible %}
  <section class="panel">
    <div class="panel-body">
      <div class="empty">
        <div class="empty-title">
          {% trans "Dieses Mitglied hat sein Profil im Verzeichnis verborgen." %}
        </div>
      </div>
    </div>
  </section>
{% else %}

<div class="summary-bar mb-24">
  {% if "email" in visible_fields and object.email %}
    <div class="summary-item">
      <div class="summary-key">{% trans "Email" %}</div>
      <div class="summary-val mono">{{ object.email }}</div>
    </div>
  {% endif %}
  {% if "locator" in visible_fields and object.locator %}
    <div class="summary-item">
      <div class="summary-key">{% trans "Locator" %}</div>
      <div class="summary-val mono">{{ object.locator }}</div>
    </div>
  {% endif %}
  {% if "qth_name" in visible_fields and object.qth_name %}
    <div class="summary-item">
      <div class="summary-key">QTH</div>
      <div class="summary-val mono">{{ object.qth_name }}</div>
    </div>
  {% endif %}
  {% if "date_joined_year" in visible_fields %}
    <div class="summary-item">
      <div class="summary-key">{% trans "Mitglied seit" %}</div>
      <div class="summary-val mono">{{ object.date_joined|date:"Y" }}</div>
    </div>
  {% endif %}
  {% if is_admin_view and object.last_login %}
    <div class="summary-item">
      <div class="summary-key">{% trans "Last login" %}</div>
      <div class="summary-val mono">{{ object.last_login|date:"Y-m-d H:i" }}</div>
    </div>
  {% endif %}
</div>

<div data-tabs>
  <div class="tabs">
    <button type="button" class="tab active" data-tab="overview" aria-selected="true">
      {% trans "Overview" %}
    </button>
    <button type="button" class="tab" data-tab="topology" aria-selected="false">
      {% trans "Rollen & Topologie" %}
    </button>
    {% if is_admin_view or is_self_view %}
      <button type="button" class="tab" data-tab="sso" aria-selected="false">
        {% trans "Single Sign-On" %}
      </button>
      <button type="button" class="tab" data-tab="audit" aria-selected="false">
        {% trans "Audit" %}
      </button>
    {% endif %}
  </div>
</div>

<div data-tab-panel="overview">
  <div class="grid grid-main">
    <section class="panel">
      <div class="panel-head">
        <div class="panel-title"><span class="dot"></span>{% trans "Identity" %}</div>
      </div>
      <div class="panel-body">
        <dl class="dlist">
          <dt>{% trans "Callsign" %}</dt><dd class="t-mono">{{ object.username }}</dd>
          {% if "first_name" in visible_fields or "last_name" in visible_fields %}
            <dt>{% trans "Name" %}</dt><dd>{{ object.get_full_name|default:"—" }}</dd>
          {% endif %}
          {% if "bio" in visible_fields and object.bio %}
            <dt>{% trans "Bio" %}</dt><dd>{{ object.bio|linebreaksbr }}</dd>
          {% endif %}
          {% if "email" in visible_fields and object.email %}
            <dt>{% trans "Email" %}</dt><dd class="t-mono">{{ object.email }}</dd>
          {% endif %}
          {% if "phone" in visible_fields and object.phone %}
            <dt>{% trans "Phone" %}</dt><dd class="t-mono">{{ object.phone }}</dd>
          {% endif %}
          {% if "address" in visible_fields and object.address %}
            <dt>{% trans "Address" %}</dt><dd>{{ object.address|linebreaksbr }}</dd>
          {% endif %}
          {% if "qth_name" in visible_fields and object.qth_name %}
            <dt>QTH</dt><dd>{{ object.qth_name }}</dd>
          {% endif %}
          {% if "locator" in visible_fields and object.locator %}
            <dt>{% trans "Locator" %}</dt><dd class="t-mono">{{ object.locator }}</dd>
          {% endif %}
          {% if "qrz_url" in visible_fields and object.qrz_url %}
            <dt>QRZ</dt><dd><a href="{{ object.qrz_url }}" target="_blank" rel="noopener">{{ object.qrz_url }}</a></dd>
          {% endif %}
          {% if "language" in visible_fields %}
            <dt>{% trans "Language" %}</dt><dd>{{ object.get_language_display }}</dd>
          {% endif %}
          {% if "date_joined_year" in visible_fields %}
            <dt>{% trans "Mitglied seit" %}</dt><dd class="t-mono">{{ object.date_joined|date:"Y" }}</dd>
          {% endif %}
          {% if "last_login" in visible_fields and object.last_login %}
            <dt>{% trans "Last login" %}</dt><dd class="t-mono-sm">{{ object.last_login|date:"Y-m-d H:i" }}</dd>
          {% endif %}
          {% if "is_active" in visible_fields %}
            <dt>{% trans "Active" %}</dt>
            <dd>
              {% if object.is_active %}<span class="pill pill-online"><span class="dot"></span>ACTIVE</span>
              {% else %}<span class="pill pill-offline">INACTIVE</span>{% endif %}
            </dd>
          {% endif %}
          {% if is_admin_view %}
            {% if object.latitude is not None or object.longitude is not None %}
              <dt>{% trans "Lat/Lon (debug)" %}</dt>
              <dd class="t-mono-sm t-muted">{{ object.latitude }}, {{ object.longitude }}</dd>
            {% endif %}
          {% endif %}
          {% if "is_directory_visible" in visible_fields %}
            <dt>{% trans "Directory visible" %}</dt>
            <dd>{% if object.is_directory_visible %}{% trans "yes" %}{% else %}{% trans "no" %}{% endif %}</dd>
          {% endif %}
        </dl>
      </div>
    </section>

    <aside class="stack-gap-14">
      {% if object.avatar %}
      <section class="panel">
        <div class="panel-body" style="text-align:center;">
          <img src="{{ object.avatar.url }}" alt="{{ object.username }}"
               style="max-width:128px;max-height:128px;border-radius:8px;">
        </div>
      </section>
      {% endif %}

      <section class="panel">
        <div class="panel-head">
          <div class="panel-title"><span class="dot"></span>{% trans "Status" %}</div>
        </div>
        <div class="panel-body">
          <dl class="dlist">
            {% if region_assignment_pills is not None %}
              <dt>{% trans "Regions" %}</dt>
              <dd>
                {% for ra in region_assignment_pills %}
                  <span class="pill pill-muted">{{ ra.region.name }}</span>
                {% empty %}—{% endfor %}
              </dd>
            {% endif %}
            {% if station_assignment_pills is not None %}
              <dt>{% trans "Stations" %}</dt>
              <dd>
                {% for sa in station_assignment_pills %}
                  <span class="pill pill-muted">{{ sa.station.callsign|default:sa.station.name }} · {{ sa.get_role_display }}</span>
                {% empty %}—{% endfor %}
              </dd>
            {% endif %}
          </dl>
        </div>
      </section>
    </aside>
  </div>
</div>

<div data-tab-panel="topology" hidden>
  {% if is_admin_view %}
    {% include "accounts/_membership_card.html" with readonly=False %}
    <div class="grid grid-main">
      {% include "accounts/_region_assignments_card.html" with readonly=False %}
      {% include "accounts/_station_assignments_card.html" with readonly=False %}
    </div>
  {% else %}
    {% include "accounts/_membership_card.html" with readonly=True %}
    <div class="grid grid-main">
      {% include "accounts/_region_assignments_card.html" with readonly=True %}
      {% include "accounts/_station_assignments_card.html" with readonly=True %}
    </div>
  {% endif %}
</div>

{% if is_admin_view %}
<div data-tab-panel="sso" hidden>
  {% include "sso/_app_grants_card.html" with target_user=object applications=app_grants_list %}
  {% include "sso/_sessions_card.html" with target_user=object sessions=user_sessions %}
  {% include "sso/_tags_card.html" with target_user=object tag_entries=tag_entries %}
</div>
{% elif is_self_view %}
<div data-tab-panel="sso" hidden>
  {% include "sso/_sessions_card.html" with target_user=object sessions=user_sessions readonly_self=True %}
</div>
{% endif %}

{% if is_admin_view or is_self_view %}
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
        <div class="empty">
          <div class="empty-title">{% trans "No audit entries yet" %}</div>
        </div>
      {% endif %}
    </div>
    <div class="panel-foot row-split">
      <span class="t-mono">{% trans "Top 50 events — older entries via global audit log" %}</span>
      {% if is_admin_view %}
        <a class="btn btn-sm btn-ghost"
           href="{% url 'audit:list' %}?target_user={{ object.pk }}">
          {% trans "Open in global audit log" %} →
        </a>
      {% endif %}
    </div>
  </section>
</div>
{% endif %}

{% endif %}{# end of "not invisible-target for member" branch #}
{% endblock %}
```

Notes:
- Each card include passes the `readonly` flag. Admin gets `readonly=False`, Self/Applicant/Member get `readonly=True`.
- `region_assignment_pills` and `station_assignment_pills` are checked with `is not None` — they only land in the context when `visible_fields` allows them.
- The `data-tabs` JS in `static/js/app.js` line 98 ff. attaches the click handlers automatically.

- [ ] **Step 4: Run template tests to verify they pass**

Run: `uv run pytest tests/test_user_detail_view.py -v 2>&1 | tail -30`
Expected: All tests PASS.

- [ ] **Step 5: Manual smoke check (optional)**

If desired, start a dev server (`uv run python manage.py runserver 0.0.0.0:8000`) and check `/accounts/users/<admin-pk>/` and `/accounts/users/<member-pk>/` render without errors.

- [ ] **Step 6: Full regression**

Run: `uv run pytest tests/ -x --tb=short 2>&1 | tail -5`
Expected: All pass.

- [ ] **Step 7: Commit**

```bash
git add apps/accounts/templates/accounts/user_detail.html tests/test_user_detail_view.py
git commit -m "feat(accounts): build audience-aware user_detail.html

Page-Head + Summary-Bar + 4-Tab structure (Overview / Rollen &
Topologie / SSO / Audit) following the stations/station_detail
pattern. Admin sees all four tabs with full management cards; Self
sees the same minus Admin-only SSO Grants/Tags; Member sees only
Overview + Topology (read-only); Member viewing an invisible
target gets an Empty-State."
```

---

### Task 7: `user_form.html` cleanup + `UserUpdateView.get_context_data` simplification

**Files:**
- Modify: `apps/accounts/views.py` (UserUpdateView)
- Modify: `apps/accounts/templates/accounts/user_form.html`

> **Subagent:** `pixel`. MUST invoke `Skill("frontend-design")` before any HTML edit.

- [ ] **Step 1: Write failing test for user_form.html scope**

Append to `tests/test_user_detail_view.py`:

```python
@pytest.mark.django_db
class TestUserFormCardCleanup:
    """user_form.html no longer renders management cards."""

    def test_edit_form_omits_cards(self, client, admin, member):
        client.force_login(admin)
        resp = client.get(reverse("accounts:user_edit", kwargs={"pk": member.pk}))
        body = resp.content.decode()
        # Cards have moved to user_detail.html.
        assert "membership-card" not in body
        assert "region-assignments-card" not in body
        assert "station-assignments-card" not in body
        assert "sso-grants-card" not in body
        assert "sessions-card" not in body
        assert "tags-card" not in body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_user_detail_view.py::TestUserFormCardCleanup -v 2>&1 | tail -10`
Expected: Fails because user_form.html still includes the cards.

- [ ] **Step 3: Simplify UserUpdateView.get_context_data**

Edit `apps/accounts/views.py`. In `UserUpdateView`, replace `get_context_data` with the slim version:

```python
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_title"] = _("Edit User")
        return context
```

(The Admin management context — region/station assignments, SSO grants/sessions/tags, membership-level choices — moved to `UserDetailView._admin_context_data` in Task 3.)

- [ ] **Step 4: Strip card includes from user_form.html**

Edit `apps/accounts/templates/accounts/user_form.html`. Remove all 6 trailing `{% include %}` blocks (lines ~38-60). The file should end with the closing `</form>` and `{% endblock %}`:

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
<div class="page-head"><div class="page-head-main">
  <div class="page-eyebrow">{% trans "Administration" %}</div>
  <h1 class="page-title">{{ form_title|default:_("Edit user") }}</h1>
</div></div>

<form method="post" style="max-width:640px;">
  {% csrf_token %}
  <section class="panel">
    <div class="panel-body">
      {% for field in form %}
      <div class="form-group">
        <label class="form-label" for="{{ field.id_for_label }}">{{ field.label }}</label>
        {{ field }}
        {% if field.help_text %}<div class="form-help">{{ field.help_text|safe }}</div>{% endif %}
        {% if field.errors %}<div class="form-error">{{ field.errors|join:", " }}</div>{% endif %}
      </div>
      {% endfor %}
    </div>
    <div class="panel-foot row-gap-8">
      <button type="submit" class="btn btn-primary">{% trans "Save user" %}</button>
      <a href="{% url 'accounts:user_list' %}" class="btn btn-ghost">{% trans "Cancel" %}</a>
    </div>
  </section>
</form>
{% endblock %}
```

(The inline `max-width:640px;` on the form is left for 1c to address as part of the form mobile-redesign — out of 1b's scope.)

- [ ] **Step 5: Verify test passes**

Run: `uv run pytest tests/test_user_detail_view.py::TestUserFormCardCleanup -v 2>&1 | tail -10`
Expected: PASS.

- [ ] **Step 6: Full regression**

Run: `uv run pytest tests/ -x --tb=short 2>&1 | tail -5`
Expected: All pass.

- [ ] **Step 7: Commit**

```bash
git add apps/accounts/views.py apps/accounts/templates/accounts/user_form.html \
        tests/test_user_detail_view.py
git commit -m "refactor(accounts): move management cards out of user_form.html

Cards (Membership / Region / Station / SSO Grants / SSO Sessions /
SSO Tags) now live in user_detail.html under the Rollen & Topologie
+ SSO tabs. UserUpdateView.get_context_data slims down to form_title
only; the heavy load is now UserDetailView._admin_context_data."
```

---

### Task 8: `UserListView` audience-aware

**Files:**
- Modify: `apps/accounts/views.py` (UserListView)
- Create: `tests/test_user_list_view_audience.py`

- [ ] **Step 1: Write failing tests**

Create NEW file `tests/test_user_list_view_audience.py`:

```python
"""UserListView audience-aware: dispatch, queryset filter, get-params."""

import pytest
from django.urls import reverse

from apps.accounts.models import User


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


@pytest.mark.django_db
class TestUserListPermissions:
    def url(self):
        return reverse("accounts:user_list")

    def test_admin_can_access(self, client, admin):
        client.force_login(admin)
        resp = client.get(self.url())
        assert resp.status_code == 200

    def test_member_can_access(self, client, member):
        client.force_login(member)
        resp = client.get(self.url())
        assert resp.status_code == 200

    def test_applicant_cannot_access(self, client, applicant):
        client.force_login(applicant)
        resp = client.get(self.url())
        assert resp.status_code == 404

    def test_anonymous_redirected(self, client):
        resp = client.get(self.url())
        assert resp.status_code in (302, 401, 403)


@pytest.mark.django_db
class TestUserListAudienceFilter:
    """Member sees no applicants; Admin sees all by default."""

    def url(self, **params):
        u = reverse("accounts:user_list")
        if params:
            from urllib.parse import urlencode
            u += "?" + urlencode(params)
        return u

    def test_admin_sees_applicants(self, client, admin, member, applicant):
        client.force_login(admin)
        resp = client.get(self.url())
        usernames = [u.username for u in resp.context["users"]]
        assert applicant.username in usernames
        assert member.username in usernames

    def test_member_does_not_see_applicants(self, client, member, applicant, other_member):
        client.force_login(member)
        resp = client.get(self.url())
        usernames = [u.username for u in resp.context["users"]]
        assert applicant.username not in usernames
        assert other_member.username in usernames

    def test_role_filter_member(self, client, admin, member, other_member, applicant):
        client.force_login(admin)
        resp = client.get(self.url(role="member"))
        usernames = [u.username for u in resp.context["users"]]
        assert member.username in usernames
        assert other_member.username in usernames
        assert applicant.username not in usernames
        assert admin.username not in usernames

    def test_role_filter_applicant_admin_only(self, client, admin, member, applicant):
        client.force_login(admin)
        resp = client.get(self.url(role="applicant"))
        usernames = [u.username for u in resp.context["users"]]
        assert applicant.username in usernames
        assert member.username not in usernames

    def test_member_cannot_filter_to_applicants(self, client, member, applicant, other_member):
        """Even if a member tries ?role=applicant, the queryset excludes
        applicants because the audience filter applies first."""
        client.force_login(member)
        resp = client.get(self.url(role="applicant"))
        usernames = [u.username for u in resp.context["users"]]
        assert applicant.username not in usernames

    def test_search_filter_q(self, client, admin, member):
        member.email = "specialhandle@example.org"
        member.save()
        client.force_login(admin)
        resp = client.get(self.url(q="specialhandle"))
        usernames = [u.username for u in resp.context["users"]]
        assert member.username in usernames

    def test_admin_status_filter_inactive(self, client, admin, member, other_member):
        other_member.is_active = False
        other_member.save()
        client.force_login(admin)
        resp = client.get(self.url(status="inactive"))
        usernames = [u.username for u in resp.context["users"]]
        assert other_member.username in usernames
        assert member.username not in usernames

    def test_member_status_param_ignored(self, client, member, other_member):
        """Member tries ?status=inactive — the param is ignored (no admin)."""
        other_member.is_active = False
        other_member.save()
        client.force_login(member)
        resp = client.get(self.url(status="inactive"))
        usernames = [u.username for u in resp.context["users"]]
        # other_member.is_active=False but is_directory_visible=True →
        # still appears for member (status filter not applied).
        assert other_member.username in usernames
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_user_list_view_audience.py -v 2>&1 | tail -20`
Expected: Tests fail because UserListView is still AdminRequiredMixin + no filter logic.

- [ ] **Step 3: Refactor UserListView**

Edit `apps/accounts/views.py`. Add `Q` import:

```python
from django.db.models import Q
```

Replace `UserListView`:

```python
class UserListView(LoginRequiredMixin, ListView):
    """Audience-aware list. Admin sees everyone (incl. Applicants),
    Member sees everyone except Applicants, Applicants get 404.
    Filter-bar params (q, role, status) are applied on the audience-filtered
    queryset.
    """

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

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["is_admin_view"] = self.request.user.is_admin
        ctx["is_member_view"] = not self.request.user.is_admin
        ctx["filter_q"] = self.request.GET.get("q", "")
        ctx["filter_role"] = self.request.GET.get("role", "")
        ctx["filter_status"] = self.request.GET.get("status", "")
        return ctx
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_user_list_view_audience.py -v 2>&1 | tail -25`
Expected: All tests PASS.

- [ ] **Step 5: Full regression**

Run: `uv run pytest tests/ -x --tb=short 2>&1 | tail -5`
Expected: All pass.

- [ ] **Step 6: ruff format + check**

Run: `uv run ruff format apps/accounts/views.py tests/test_user_list_view_audience.py 2>&1 | tail -3`
Run: `uv run ruff check apps/accounts/views.py tests/test_user_list_view_audience.py 2>&1 | tail -3`
Expected: Clean.

- [ ] **Step 7: Commit**

```bash
git add apps/accounts/views.py tests/test_user_list_view_audience.py
git commit -m "feat(accounts): UserListView audience-aware + filter-bar

LoginRequiredMixin + user_can_view_directory gate (Applicants 404).
Member-view excludes Applicants from the queryset. Filter params:
?q= (username/email/name icontains), ?role= (admin/staff/member,
also applicant for Admin), ?status=active|inactive (Admin only).
Context exposes is_admin_view/is_member_view + the current filter
values for the template's filter-bar."
```

---

### Task 9: `user_list.html` audience-aware refactor

**Files:**
- Modify: `apps/accounts/templates/accounts/user_list.html`

> **Subagent:** `pixel`. MUST invoke `Skill("frontend-design")` before any HTML edit.

- [ ] **Step 1: Write failing template tests**

Append to `tests/test_user_list_view_audience.py`:

```python
@pytest.mark.django_db
class TestUserListTemplate:
    def url(self, **params):
        u = reverse("accounts:user_list")
        if params:
            from urllib.parse import urlencode
            u += "?" + urlencode(params)
        return u

    def test_admin_sees_new_user_button(self, client, admin):
        client.force_login(admin)
        resp = client.get(self.url())
        body = resp.content.decode()
        assert reverse("accounts:user_create") in body

    def test_member_does_not_see_new_user_button(self, client, member):
        client.force_login(member)
        resp = client.get(self.url())
        body = resp.content.decode()
        assert reverse("accounts:user_create") not in body

    def test_member_sees_only_view_button(self, client, member, other_member):
        client.force_login(member)
        resp = client.get(self.url())
        body = resp.content.decode()
        assert reverse("accounts:user_detail", kwargs={"pk": other_member.pk}) in body
        assert reverse("accounts:user_edit", kwargs={"pk": other_member.pk}) not in body
        assert reverse("accounts:user_delete", kwargs={"pk": other_member.pk}) not in body

    def test_admin_sees_view_edit_delete(self, client, admin, member):
        client.force_login(admin)
        resp = client.get(self.url())
        body = resp.content.decode()
        assert reverse("accounts:user_detail", kwargs={"pk": member.pk}) in body
        assert reverse("accounts:user_edit", kwargs={"pk": member.pk}) in body
        assert reverse("accounts:user_delete", kwargs={"pk": member.pk}) in body

    def test_filter_bar_role_options_admin(self, client, admin):
        client.force_login(admin)
        resp = client.get(self.url())
        body = resp.content.decode()
        # Admin sees Applicant option in role select
        assert "applicant" in body.lower() or "Bewerber" in body

    def test_member_view_hides_invisible_other_fields(
        self, client, member, other_member
    ):
        other_member.email = "secret@example.org"
        other_member.is_directory_visible = False
        other_member.save()
        client.force_login(member)
        resp = client.get(self.url())
        body = resp.content.decode()
        # Email of an invisible member must NOT show up in the list table
        assert "secret@example.org" not in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_user_list_view_audience.py::TestUserListTemplate -v 2>&1 | tail -15`
Expected: Fail because the current template is Admin-only and doesn't differentiate.

- [ ] **Step 3: Refactor user_list.html**

Replace `apps/accounts/templates/accounts/user_list.html` with the audience-aware version:

```django
{% extends "base.html" %}
{% load i18n %}

{% block title %}
  {% if is_member_view %}{% trans "Mitglieder" %}{% else %}{% trans "Users" %}{% endif %} · OE5XRX
{% endblock %}

{% block breadcrumbs %}
  <a href="{% url 'dashboard:index' %}">OE5XRX</a>
  <span class="sep">/</span>
  <span class="cur">
    {% if is_member_view %}{% trans "Mitglieder" %}{% else %}{% trans "Users" %}{% endif %}
  </span>
{% endblock %}

{% block content %}
<div class="page-head">
  <div class="page-head-main">
    <div class="page-eyebrow">
      {% if is_admin_view %}
        {% trans "Administration" %} · {% trans "People" %}
      {% else %}
        {% trans "Verein" %} · {% trans "Mitgliederverzeichnis" %}
      {% endif %}
    </div>
    <h1 class="page-title">
      {% if is_admin_view %}{% trans "Users" %}{% else %}{% trans "Mitglieder" %}{% endif %}
    </h1>
    <p class="page-sub">
      {% if is_admin_view %}
        {% trans "Add, view, edit, and remove member, staff, and admin accounts." %}
      {% else %}
        {% trans "Vereinsmitglieder mit Kontaktdaten und Funktionen." %}
      {% endif %}
    </p>
  </div>
  {% if is_admin_view %}
  <div class="page-head-actions">
    <a href="{% url 'accounts:user_create' %}" class="btn btn-primary">
      <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>
      {% trans "New user" %}
    </a>
  </div>
  {% endif %}
</div>

<form method="get" class="filter-bar mb-24">
  <input type="search" name="q" value="{{ filter_q }}"
         placeholder="{% trans 'Search callsign, email, name' %}"
         class="form-control">
  <select name="role" class="form-select">
    <option value="">{% trans "All roles" %}</option>
    {% if is_admin_view %}
      <option value="applicant" {% if filter_role == "applicant" %}selected{% endif %}>{% trans "Vereins-Bewerber" %}</option>
    {% endif %}
    <option value="member" {% if filter_role == "member" %}selected{% endif %}>{% trans "Vereins-Mitglied" %}</option>
    <option value="staff" {% if filter_role == "staff" %}selected{% endif %}>{% trans "Vereins-Staff" %}</option>
    <option value="admin" {% if filter_role == "admin" %}selected{% endif %}>{% trans "Vereins-Admin" %}</option>
  </select>
  {% if is_admin_view %}
    <select name="status" class="form-select">
      <option value="">{% trans "All status" %}</option>
      <option value="active" {% if filter_status == "active" %}selected{% endif %}>{% trans "Active" %}</option>
      <option value="inactive" {% if filter_status == "inactive" %}selected{% endif %}>{% trans "Inactive" %}</option>
    </select>
  {% endif %}
  <button type="submit" class="btn btn-primary btn-sm">{% trans "Filter" %}</button>
  {% if filter_q or filter_role or filter_status %}
    <a href="{% url 'accounts:user_list' %}" class="btn btn-ghost btn-sm">{% trans "Reset" %}</a>
  {% endif %}
</form>

<div class="table-wrap" data-mobile-cards>
  <table class="t-table">
    <thead>
      <tr>
        <th>{% trans "User" %}</th>
        <th>{% trans "Role" %}</th>
        <th>{% trans "Email" %}</th>
        {% if is_member_view %}<th>QTH</th>{% endif %}
        <th>{% trans "Topology" %}</th>
        {% if is_admin_view %}
          <th>{% trans "Last login" %}</th>
          <th>{% trans "Status" %}</th>
        {% endif %}
        <th></th>
      </tr>
    </thead>
    <tbody>
      {% for u in users %}
      {% with is_visible=u.is_directory_visible %}
      <tr>
        <td data-primary>
          <a href="{% url 'accounts:user_detail' u.pk %}" style="color:inherit;text-decoration:none;">
            <div class="row-gap-8">
              <div class="sb-avatar">
                {% if u.avatar %}<img src="{{ u.avatar.url }}" alt="{{ u.username }}">
                {% else %}{{ u.username|slice:":1"|upper }}{% endif %}
              </div>
              <div class="stack-gap-2">
                <span style="font-weight:600;color:var(--ink-0);">{{ u.username }}</span>
                {% if is_admin_view or is_visible %}
                  <span class="t-mono-sm t-muted">{{ u.get_full_name|default:"" }}</span>
                {% endif %}
              </div>
            </div>
          </a>
        </td>
        <td data-label="{% trans 'Role' %}">
          {% if u.membership_level == "admin" %}
            <span class="pill pill-accent">{{ u.get_membership_level_display }}</span>
          {% elif u.membership_level == "staff" %}
            <span class="pill pill-violet">{{ u.get_membership_level_display }}</span>
          {% elif u.membership_level == "member" %}
            <span class="pill">{{ u.get_membership_level_display }}</span>
          {% else %}
            <span class="pill pill-muted">{{ u.get_membership_level_display }}</span>
          {% endif %}
        </td>
        <td class="t-mono" data-label="{% trans 'Email' %}">
          {% if is_admin_view or is_visible %}{{ u.email|default:"—" }}{% else %}—{% endif %}
        </td>
        {% if is_member_view %}
        <td class="t-mono" data-label="QTH">
          {% if is_visible %}{{ u.qth_name|default:"—" }}{% else %}—{% endif %}
        </td>
        {% endif %}
        <td data-label="{% trans 'Topology' %}">
          {% if is_admin_view or is_visible %}
            <span class="pill pill-muted">{{ u.region_assignments.count }}·{{ u.station_assignments.count }}</span>
          {% else %}—{% endif %}
        </td>
        {% if is_admin_view %}
          <td class="t-mono-sm t-muted" data-label="{% trans 'Last login' %}">
            {{ u.last_login|date:"Y-m-d H:i"|default:"never" }}
          </td>
          <td data-label="{% trans 'Status' %}">
            {% if u.is_active %}<span class="pill pill-online"><span class="dot"></span>ACTIVE</span>
            {% else %}<span class="pill pill-offline">INACTIVE</span>{% endif %}
          </td>
        {% endif %}
        <td class="actions">
          <a href="{% url 'accounts:user_detail' u.pk %}" class="btn btn-sm btn-primary">{% trans "View" %}</a>
          {% if is_admin_view %}
            <a href="{% url 'accounts:user_edit' u.pk %}" class="btn btn-sm btn-ghost">{% trans "Edit" %}</a>
            {% if u.pk != user.pk %}
            <a href="{% url 'accounts:user_delete' u.pk %}" class="btn btn-sm btn-danger">{% trans "Delete" %}</a>
            {% endif %}
          {% endif %}
        </td>
      </tr>
      {% endwith %}
      {% endfor %}
    </tbody>
  </table>
</div>
{% include "includes/pagination.html" %}
{% endblock %}
```

Notes:
- The `{% with is_visible=u.is_directory_visible %}` block lets the row decide per-user whether to render details for Member viewers.
- Admin sees everything regardless of the visibility flag (admin row never collapses).
- [View] is now a `btn-primary`; Edit and Delete are `btn-ghost` / `btn-danger` and Admin-only.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_user_list_view_audience.py -v 2>&1 | tail -25`
Expected: All tests PASS.

- [ ] **Step 5: Full regression**

Run: `uv run pytest tests/ -x --tb=short 2>&1 | tail -5`
Expected: All pass.

- [ ] **Step 6: Commit**

```bash
git add apps/accounts/templates/accounts/user_list.html \
        tests/test_user_list_view_audience.py
git commit -m "feat(accounts): audience-aware user_list.html refactor

Header/eyebrow/title switch between 'Users / Administration · People'
(Admin) and 'Mitglieder / Verein · Mitgliederverzeichnis' (Member).
Filter-bar replaces the static table — q + role + status, with the
Admin-only status filter and Applicant role-option. Table columns
adapt per audience (Admin: Last login + Status, Member: QTH).
[View] is the primary action; Edit/Delete are Admin-only. Row cells
hide content for invisible-directory members."
```

---

### Task 10: Success-Redirects for UserCreateView + UserUpdateView

**Files:**
- Modify: `apps/accounts/views.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_user_detail_view.py`:

```python
@pytest.mark.django_db
class TestSuccessRedirects:
    """Create/Update redirect to the user_detail page of the affected user."""

    def test_create_redirects_to_detail(self, client, admin):
        client.force_login(admin)
        resp = client.post(
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
            follow=False,
        )
        assert resp.status_code == 302
        created = User.objects.get(username="OE5NEW1")
        assert resp.url == reverse("accounts:user_detail", kwargs={"pk": created.pk})

    def test_update_redirects_to_detail(self, client, admin, member):
        client.force_login(admin)
        resp = client.post(
            reverse("accounts:user_edit", kwargs={"pk": member.pk}),
            {
                "username": member.username,
                "email": "updated@example.org",
                "first_name": "Updated",
                "last_name": "",
                "language": "en",
                "is_active": "on",
            },
            follow=False,
        )
        assert resp.status_code == 302
        assert resp.url == reverse("accounts:user_detail", kwargs={"pk": member.pk})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_user_detail_view.py::TestSuccessRedirects -v 2>&1 | tail -10`
Expected: Both fail because current `success_url` points to `user_list`.

- [ ] **Step 3: Replace static `success_url` with `get_success_url`**

Edit `apps/accounts/views.py`.

For `UserCreateView`, remove the `success_url = reverse_lazy(...)` line and add:

```python
    def get_success_url(self):
        return reverse("accounts:user_detail", kwargs={"pk": self.object.pk})
```

For `UserUpdateView`, same — remove `success_url = reverse_lazy(...)` and add:

```python
    def get_success_url(self):
        return reverse("accounts:user_detail", kwargs={"pk": self.object.pk})
```

Add the `reverse` import at the top if not already present:

```python
from django.urls import reverse, reverse_lazy
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_user_detail_view.py::TestSuccessRedirects -v 2>&1 | tail -10`
Expected: PASS.

- [ ] **Step 5: Verify the existing UserDeleteView still redirects to list (unchanged behavior)**

Run: `uv run pytest tests/ -k delete -x --tb=short 2>&1 | tail -10`
Expected: PASS.

- [ ] **Step 6: Full regression**

Run: `uv run pytest tests/ -x --tb=short 2>&1 | tail -5`
Expected: All pass.

- [ ] **Step 7: ruff check**

Run: `uv run ruff format apps/accounts/views.py 2>&1 | tail -2 && uv run ruff check apps/accounts/views.py 2>&1 | tail -3`
Expected: Clean.

- [ ] **Step 8: Commit**

```bash
git add apps/accounts/views.py tests/test_user_detail_view.py
git commit -m "feat(accounts): redirect Create/Update to user_detail

UserCreateView.get_success_url and UserUpdateView.get_success_url
return the user_detail URL for the affected user. UserDeleteView
keeps redirecting to user_list (unchanged)."
```

---

### Task 11: Final integration verify (inline)

**Files:**
- Read only

- [ ] **Step 1: Run the entire test suite**

Run: `uv run pytest tests/ --tb=short 2>&1 | tail -5`
Expected: All tests pass.

- [ ] **Step 2: System check**

Run: `uv run python manage.py check 2>&1 | tail -5`
Expected: `System check identified no issues (0 silenced).`

- [ ] **Step 3: Migrations clean**

Run: `uv run python manage.py makemigrations --check --dry-run 2>&1 | tail -5`
Expected: `No changes detected` for accounts/audit (the `images` app may have unrelated drift; ignore).

- [ ] **Step 4: ruff check on the whole tree**

Run: `uv run ruff check apps/accounts/ apps/audit/ tests/ 2>&1 | tail -5`
Run: `uv run ruff format --check apps/accounts/ apps/audit/ tests/ 2>&1 | tail -5`
Expected: Clean.

- [ ] **Step 5: Branch summary**

Run: `git log --oneline origin/main..HEAD 2>&1 | head -15`
Expected: ~10 commits since main (one per task, with TDD pattern).

- [ ] **Step 6: Manual smoke (optional)**

Start dev server (`uv run python manage.py runserver 0.0.0.0:8000`) and exercise:
- Admin: `/accounts/users/` → list with filter-bar + Edit/Delete buttons. Click a user → detail page with 4 tabs.
- Member: `/accounts/users/` → list as „Mitglieder", no Edit/Delete. Click own → detail with 4 tabs (sso, audit visible). Click another member → 2 tabs (no SSO/Audit).
- Applicant: `/accounts/users/` → 404. `/accounts/users/<self-pk>/` → 200, own detail.

---

## Summary

After this plan executes, the branch `feat/user-domain-1b-directory` delivers:

- `UserDetailView` audience-aware with 4 tabs (Overview, Rollen & Topologie, SSO, Audit).
- `UserListView` audience-aware (Applicants gated, Member sees filtered list).
- Card-Migration: 3 accounts management cards + 1 SSO sessions card got a `readonly` flag and lost their inline `max-width` styles.
- `user_form.html` slimmed down to the form only — management cards live in `user_detail.html` now.
- `user_list.html` refactored to audience-aware rendering with a filter-bar.
- Global Audit log accepts `?target_user=<pk>` for cross-link from the per-user audit tab.
- `_audit_table.html` grows an optional `hide_subject` flag.
- `UserCreateView` / `UserUpdateView` redirect to the affected user's detail page.

Test count grows by ~50 new tests across 4 new files. No regression in existing tests. All HTMX endpoints stay unchanged (UI moves; backend doesn't).

The next PR (Sub-Spec 1c — Self-Service) will refactor `UserUpdateView` form to include the new profile fields, complete `ProfileView` with multi-panel forms, add the password-change view, and bring Onboarding hints.
