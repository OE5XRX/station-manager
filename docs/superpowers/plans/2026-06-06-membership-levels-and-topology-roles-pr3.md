# Membership-Levels + Topology-Roles — PR-3: User UI + Cleanup

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. UI tasks MUST invoke superpowers:frontend-design as a sub-skill before writing templates — per CLAUDE.md the pixel agent uses it for all UI work, and the same rule applies here.

**Goal:** Operators can manage user membership-levels + per-user topology assignments (Region-Manager, Station-Admin, Station-Maintainer) via the existing user-edit page instead of needing Django Admin. Includes the membership-level audit-log emission that PR-2 deferred (needs view-level actor context that signals can't provide), plus cleanup of dead group-based rendering left over from PR-1.

**Architecture:** Three HTMX-targeted sections appended to the existing `user_form.html` edit page. Three new view modules (`views_membership.py`, `views_region_assignments.py`, `views_station_assignments.py`) handle the POST endpoints — kept separate from `views.py` so each file stays focused. Permission gates use the existing `AdminRequiredMixin` (Vereins-Admin only — Region-Manager UI editing of own region's stations lands in PR-4).

**Tech Stack:** Django 6.0 class-based views, HTMX 2.0.4 (already in base.html), Django messages framework, `AccountAuditLog.log()` classmethod from PR-1, pytest-django.

**Reference spec:** `docs/superpowers/specs/2026-06-05-membership-levels-and-topology-roles-design.md` §5.1, §5.4

**Reference PR-1 plan:** `docs/superpowers/plans/2026-06-05-membership-levels-and-topology-roles.md` Tasks 15-18 (deferred to PR-3)

**Out of scope (deferred to PR-4):**
- Station-Detail page: Region picker, Station-Admin/Maintainer pickers
- Region-CRUD pages (list/create/edit/delete) — operators continue to use Django Admin for Region management
- Region-Manager edit-permission on stations of own region (currently gated to Vereins-Admin)
- Notification preferences per user, Telegram routing

---

## In-Tree State After PR-2 Merge (verified 2026-06-06)

| Item | Reality |
|---|---|
| User edit URL | `/accounts/users/<pk>/edit/` → `UserUpdateView` → `user_form.html` |
| User list URL | `/accounts/users/` → `UserListView` → `user_list.html` |
| User-detail page | **does NOT exist** — edit-page is the management surface |
| `AdminRequiredMixin` | defined in `apps/accounts/views.py:15` (`is_admin` check) |
| `user_list.html` role rendering | uses `{% for g in u.groups.all %}` — **dead code** (groups dropped in PR-2 migration 0007) |
| `UserListView.queryset` | `User.objects.prefetch_related("groups")` — dead prefetch since rendering will change |
| `user_list.html` sub-text | `"Admins can add, edit, and remove operator & member accounts."` — operator term retired in PR-1 |
| `forms.py` `UserCreationForm` docstring | mentions group membership management — outdated |
| `forms.py` `UserChangeForm` docstring | mentions group membership management — outdated |
| `User.MembershipLevel` choices | APPLICANT / MEMBER / STAFF / ADMIN with `_("Vereins-{X}")` display labels |
| `User.is_admin` / `is_internal` / `is_station_admin(s)` / etc. | all from PR-1, in `apps/accounts/models.py:69-145` |
| `AccountAuditLog` model | `apps/accounts/models.py:158` with EventType (incl. MEMBERSHIP_PROMOTED / MEMBERSHIP_DEMOTED, REGION_ASSIGNMENT_CREATED / REVOKED) and `log()` classmethod |
| `StationAssignment` / `RegionAssignment` | `apps/stations/models.py:473+` with `_ApplicantForbiddenMixin` (clean/full_clean enforces APPLICANT cannot hold assignment) |
| `RegionAssignment` signal emission | already wired in `apps/stations/signals.py` — REGION_ASSIGNMENT_CREATED on post_save, REGION_ASSIGNMENT_REVOKED on post_delete (via `AccountAuditLog.log()`) |
| `StationAssignment` signal emission | already wired — STATION_ASSIGNMENT_CREATED / _REVOKED on `StationAuditLog` |
| MEMBERSHIP audit emission | NOT wired — PR-2 deferred to PR-3 (needs view-level actor) |
| `_build_grants_for_user` | imported in `UserUpdateView` for SSO app-grants card — establishes the pattern of additional context in the edit page |

---

## File Structure

### New files

| Path | Responsibility |
|---|---|
| `apps/accounts/views_membership.py` | `MembershipSetView` (POST endpoint for promote/demote) |
| `apps/accounts/views_region_assignments.py` | `RegionAssignmentCreateView`, `RegionAssignmentRevokeView` |
| `apps/accounts/views_station_assignments.py` | `StationAssignmentCreateView`, `StationAssignmentRevokeView` |
| `apps/accounts/templates/accounts/_membership_card.html` | HTMX fragment: membership-level picker + Vereins-Rolle display |
| `apps/accounts/templates/accounts/_region_assignments_card.html` | HTMX fragment: list + add-form for RegionAssignments |
| `apps/accounts/templates/accounts/_station_assignments_card.html` | HTMX fragment: list + add-form for StationAssignments |
| `tests/test_views_membership.py` | Tests for membership-set view (promote/demote/audit/permission gates) |
| `tests/test_views_region_assignments.py` | Tests for region-assignment views |
| `tests/test_views_station_assignments.py` | Tests for station-assignment views (incl. takeover) |

### Modified files

| Path | Reason |
|---|---|
| `apps/accounts/urls.py` | Add 5 new routes |
| `apps/accounts/views.py` | `UserUpdateView.get_context_data` adds membership/assignment context |
| `apps/accounts/templates/accounts/user_form.html` | Embed the three new cards |
| `apps/accounts/templates/accounts/user_list.html` | Replace `u.groups.all` rendering with `u.get_membership_level_display`; update sub-text |
| `apps/accounts/forms.py` | Docstring cleanup |

---

# Phase 0: Cleanup

## Task 1: Replace dead group-rendering in `user_list.html`

**Files:**
- Modify: `apps/accounts/templates/accounts/user_list.html` (role column + sub-text)
- Modify: `apps/accounts/views.py:49-52` (drop dead prefetch)
- Test: `tests/test_views_user_list_membership_display.py` (new)

This is dead-code cleanup. After PR-2's group drop, `u.groups.all` returns empty for every user — every row currently shows "—" in the Role column. Replace with a pill driven by `get_membership_level_display`. Also drop the now-useless `prefetch_related("groups")`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_views_user_list_membership_display.py`:

```python
"""Smoke test for membership-level pill in user_list."""

import pytest
from django.urls import reverse

from apps.accounts.models import User


@pytest.mark.django_db
def test_user_list_renders_membership_level_pills(
    client, admin_user, member_user, operator_user, applicant_user
):
    """Each user row shows the Vereins-X pill from membership_level."""
    client.force_login(admin_user)
    response = client.get(reverse("accounts:user_list"))

    assert response.status_code == 200
    body = response.content.decode()

    # Pill labels (TextChoices display values)
    assert "Vereins-Admin" in body
    assert "Vereins-Mitglied" in body
    assert "Vereins-Staff" in body
    assert "Vereins-Bewerber" in body


@pytest.mark.django_db
def test_user_list_sub_text_does_not_say_operator(
    client, admin_user
):
    """The page sub-text should not reference the retired 'operator' term."""
    client.force_login(admin_user)
    response = client.get(reverse("accounts:user_list"))

    body = response.content.decode().lower()
    assert "operator" not in body, (
        "Sub-text still mentions 'operator' — retired in PR-1."
    )
```

- [ ] **Step 2: Run to verify failure**

```bash
cd /home/pbuchegger/OE5XRX/station-manager
.venv/bin/python -m pytest tests/test_views_user_list_membership_display.py -v
```

Expected: 2 failures. The first because `groups.all` renders empty → no "Vereins-X" string in body. The second because the sub-text "operator & member accounts" still contains "operator".

- [ ] **Step 3: Edit `apps/accounts/templates/accounts/user_list.html`**

Two changes. (a) Replace the sub-text. Find:

```html
    <p class="page-sub">{% trans "Admins can add, edit, and remove operator & member accounts." %}</p>
```

Replace with:

```html
    <p class="page-sub">{% trans "Admins can add, edit, and remove member, staff, and admin accounts." %}</p>
```

(b) Replace the Role column rendering. Find the existing block:

```html
        <td data-label="{% trans 'Role' %}">
          {% for g in u.groups.all %}
            {% if g.name == "admin" %}<span class="pill pill-accent">ADMIN</span>
            {% elif g.name == "operator" %}<span class="pill pill-violet">OPERATOR</span>
            {% else %}<span class="pill">{{ g.name|upper }}</span>{% endif %}
          {% empty %}<span class="t-muted">—</span>{% endfor %}
        </td>
```

Replace with:

```html
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
```

(Applicant gets the muted pill — visually de-emphasized to highlight that they haven't been promoted yet.)

- [ ] **Step 4: Drop the dead groups prefetch**

In `apps/accounts/views.py`, find the `UserListView.queryset` line (around line 49-52):

```python
    # user_list.html iterates ``u.groups.all`` per row — without the
    # prefetch each row triggers a separate auth_user_groups join.
    queryset = User.objects.prefetch_related("groups").order_by("username")
```

Replace with:

```python
    queryset = User.objects.order_by("username")
```

- [ ] **Step 5: Run tests + ruff**

```bash
.venv/bin/python -m pytest tests/test_views_user_list_membership_display.py -v
.venv/bin/ruff format --check . && .venv/bin/ruff check .
```

Expected: 2 PASS, ruff clean.

- [ ] **Step 6: Commit**

```bash
git add apps/accounts/templates/accounts/user_list.html apps/accounts/views.py tests/test_views_user_list_membership_display.py
git commit -m "cleanup(accounts): replace dead group-rendering with membership_level pills

After PR-2 dropped the legacy Groups (migration 0007), user_list.html
was rendering an empty 'Role' column for every user — the
{% for g in u.groups.all %} loop matches nothing. Replace with a pill
driven by get_membership_level_display: pill-accent for Admin,
pill-violet for Staff, plain for Member, muted for Applicant.

Drop the now-useless prefetch_related('groups') on UserListView.

Sub-text 'operator & member accounts' updated to current terminology
('member, staff, and admin') — operator retired in PR-1."
```

---

## Task 2: Forms.py docstring cleanup

**Files:**
- Modify: `apps/accounts/forms.py` (docstrings on UserCreationForm and UserChangeForm)

Pure docstring fix — no behavior change. The current docstrings still say group membership is managed via Django Admin's `filter_horizontal` widget on Groups. After PR-1+2 that's wrong: `membership_level` is the field, and PR-3 (Tasks 3-4 below) adds inline UI for it. The docstrings will mislead future readers.

- [ ] **Step 1: Update UserCreationForm docstring**

In `apps/accounts/forms.py`, find:

```python
class UserCreationForm(BaseUserCreationForm):
    """Form for admins to create new users.

    Group membership (admin / operator / member) is managed via Django
    Admin's built-in UserAdmin (filter_horizontal widget on the Groups
    M2M), not from this form. Keeping this form lean to the
    bare-minimum identity fields.
    """
```

Replace with:

```python
class UserCreationForm(BaseUserCreationForm):
    """Form for admins to create new users.

    Identity fields only. ``membership_level`` defaults to APPLICANT
    on creation (set in apps/accounts/models.py); the admin promotes
    the user via the membership-card on the edit page after creation.
    Topology assignments (Region-Manager, Station-Admin/Maintainer)
    are managed from the same edit page once the user is at least
    Vereins-Mitglied — the ``_ApplicantForbiddenMixin`` invariant
    rejects assignments for applicants.
    """
```

- [ ] **Step 2: Update UserChangeForm docstring**

Find:

```python
class UserChangeForm(BaseUserChangeForm):
    """Form for admins to edit existing users.

    Group membership is managed via Django Admin (see UserCreationForm
    docstring).
    """
```

Replace with:

```python
class UserChangeForm(BaseUserChangeForm):
    """Form for admins to edit existing users — identity fields only.

    Membership-level promote/demote and topology assignments are NOT
    in this form. They are HTMX-driven cards rendered alongside this
    form in user_form.html (Vereins-Rolle, Region-Manager-Zuordnungen,
    Stations-Zuordnungen) backed by dedicated POST endpoints — see
    apps/accounts/views_membership.py / views_region_assignments.py /
    views_station_assignments.py.
    """
```

- [ ] **Step 3: Ruff format + verify nothing else changed**

```bash
.venv/bin/ruff format apps/accounts/forms.py
.venv/bin/ruff format --check . && .venv/bin/ruff check .
.venv/bin/python -m pytest tests/test_accounts.py -q 2>&1 | tail -3
```

Expected: ruff clean, accounts tests still PASS.

- [ ] **Step 4: Commit**

```bash
git add apps/accounts/forms.py
git commit -m "cleanup(accounts): update form docstrings after PR-1+2 group retirement

UserCreationForm/UserChangeForm docstrings still claimed group
membership was managed via Django Admin's filter_horizontal widget.
After PR-1+2, membership_level is the field and PR-3 adds inline UI
for it. Updated docstrings now point at the membership-card and the
assignment-cards that land in user_form.html (views_membership.py,
views_region_assignments.py, views_station_assignments.py)."
```

---

# Phase 1: Membership-Level Promote/Demote

## Task 3: `MembershipSetView` (POST endpoint)

**Files:**
- Create: `apps/accounts/views_membership.py`
- Modify: `apps/accounts/urls.py`
- Create: `tests/test_views_membership.py`

This is the workhorse view. Accepts a target level via POST, validates four invariants, mutates `membership_level`, emits the audit-log entry. Wired into the template in Task 4.

**Permission contract:**
- Caller must be Vereins-Admin (`AdminRequiredMixin`)
- Cannot promote/demote self
- Cannot demote to APPLICANT if user holds Station- or Region-Assignments (the `_ApplicantForbiddenMixin` would reject the existing assignments retroactively; we surface the conflict)
- Any other level transition is allowed (admin can promote applicant directly to admin)

**Audit emission:**
- Promotion (level index increases): `AccountAuditLog.log(event_type=MEMBERSHIP_PROMOTED, actor=request.user, target_user=target, message=f"{old} → {new}", ip_address=...)`
- Demotion (level index decreases): same with `MEMBERSHIP_DEMOTED`
- No-change saves no audit entry

**Level ordering:** APPLICANT (0) < MEMBER (1) < STAFF (2) < ADMIN (3). Defined as `MEMBERSHIP_ORDER` constant inside the view module.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_views_membership.py`:

```python
"""Tests for MembershipSetView (promote/demote endpoint)."""

import pytest
from django.urls import reverse

from apps.accounts.models import AccountAuditLog, User
from apps.stations.models import (
    Region,
    RegionAssignment,
    Station,
    StationAssignment,
)


def _user(level, username):
    u = User.objects.create_user(
        username=username, password="x", email=f"{username}@x"
    )
    u.membership_level = level
    u.save(update_fields=["membership_level"])
    return u


@pytest.mark.django_db
class TestMembershipSetView:
    def test_admin_can_promote_applicant_to_member(self, client):
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

    def test_promotion_emits_membership_promoted_audit_log(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        target = _user(User.MembershipLevel.APPLICANT, "hans")
        client.force_login(admin)
        client.post(
            reverse("accounts:membership_set", args=[target.pk]),
            {"level": "staff"},
        )
        entry = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.MEMBERSHIP_PROMOTED,
            actor=admin,
            target_user=target,
        ).first()
        assert entry is not None
        assert "applicant" in entry.message.lower()
        assert "staff" in entry.message.lower()

    def test_demotion_emits_membership_demoted_audit_log(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        target = _user(User.MembershipLevel.STAFF, "maria")
        client.force_login(admin)
        client.post(
            reverse("accounts:membership_set", args=[target.pk]),
            {"level": "member"},
        )
        entry = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.MEMBERSHIP_DEMOTED,
            actor=admin,
            target_user=target,
        ).first()
        assert entry is not None
        assert "staff" in entry.message.lower()
        assert "member" in entry.message.lower()

    def test_no_change_does_not_emit_audit_log(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        target = _user(User.MembershipLevel.MEMBER, "hans")
        client.force_login(admin)
        client.post(
            reverse("accounts:membership_set", args=[target.pk]),
            {"level": "member"},
        )
        assert (
            AccountAuditLog.objects.filter(target_user=target).count() == 0
        )

    def test_non_admin_forbidden(self, client):
        staff = _user(User.MembershipLevel.STAFF, "staff")
        target = _user(User.MembershipLevel.MEMBER, "tgt")
        client.force_login(staff)
        response = client.post(
            reverse("accounts:membership_set", args=[target.pk]),
            {"level": "admin"},
        )
        assert response.status_code in (302, 403)
        target.refresh_from_db()
        assert target.membership_level == User.MembershipLevel.MEMBER

    def test_self_forbidden(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        client.force_login(admin)
        response = client.post(
            reverse("accounts:membership_set", args=[admin.pk]),
            {"level": "member"},
        )
        assert response.status_code == 400
        admin.refresh_from_db()
        assert admin.membership_level == User.MembershipLevel.ADMIN

    def test_demote_to_applicant_blocked_when_assignments_exist(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        target = _user(User.MembershipLevel.MEMBER, "hans")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        StationAssignment.objects.create(
            user=target, station=s, role=StationAssignment.Role.ADMIN
        )
        client.force_login(admin)
        response = client.post(
            reverse("accounts:membership_set", args=[target.pk]),
            {"level": "applicant"},
        )
        assert response.status_code == 400
        target.refresh_from_db()
        assert target.membership_level == User.MembershipLevel.MEMBER

    def test_demote_to_applicant_clean_user_ok(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        target = _user(User.MembershipLevel.MEMBER, "hans")
        client.force_login(admin)
        response = client.post(
            reverse("accounts:membership_set", args=[target.pk]),
            {"level": "applicant"},
        )
        assert response.status_code == 200
        target.refresh_from_db()
        assert target.membership_level == User.MembershipLevel.APPLICANT

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

Expected: `NoReverseMatch: 'accounts:membership_set' is not a registered namespace`.

- [ ] **Step 3: Create the view module**

Create `apps/accounts/views_membership.py`:

```python
"""Membership-level set (promote/demote) view.

POST /accounts/users/<pk>/membership/  data: {"level": "<value>"}

Returns 200 on success (HTMX-friendly), 400 on validation error
(invalid level, self-promote/demote, demote-to-applicant blocked by
existing assignments), 403 on permission denied.

Emits AccountAuditLog MEMBERSHIP_PROMOTED / _DEMOTED with the actor
(request.user) — this is the reason promote/demote lives in a view
and not in a model signal: signals don't know who initiated the
change. Same level → no audit entry.
"""

from django.contrib.auth import get_user_model
from django.http import (
    HttpResponseBadRequest,
    HttpResponseForbidden,
    JsonResponse,
)
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext as _
from django.views import View

from apps.accounts.models import AccountAuditLog
from apps.accounts.views import AdminRequiredMixin

User = get_user_model()

# Sequential ordering of membership levels. Index defines "up" (promote)
# vs "down" (demote). Sourced from the TextChoices order in
# apps/accounts/models.py so a single edit-point governs both.
MEMBERSHIP_ORDER = [
    User.MembershipLevel.APPLICANT,
    User.MembershipLevel.MEMBER,
    User.MembershipLevel.STAFF,
    User.MembershipLevel.ADMIN,
]


class MembershipSetView(AdminRequiredMixin, View):
    def post(self, request, pk):
        target = get_object_or_404(User, pk=pk)
        if target.pk == request.user.pk:
            return HttpResponseBadRequest(
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

        # Demote-to-applicant block when assignments exist.
        # _ApplicantForbiddenMixin would otherwise let the demote
        # silently break the existing assignment invariant (only
        # newly-saved assignments check membership_level).
        if new_level == User.MembershipLevel.APPLICANT:
            n_station = target.station_assignments.count()
            n_region = target.region_assignments.count()
            if n_station or n_region:
                return HttpResponseBadRequest(
                    _(
                        "Cannot demote to Applicant: user has %(s)d "
                        "station + %(r)d region assignment(s). "
                        "Remove them first."
                    )
                    % {"s": n_station, "r": n_region}
                )

        target.membership_level = new_level
        target.save(update_fields=["membership_level"])
        User._invalidate_role_cache(target)

        new_index = MEMBERSHIP_ORDER.index(User.MembershipLevel(new_level))
        old_index = MEMBERSHIP_ORDER.index(User.MembershipLevel(old_level))
        is_promote = new_index > old_index
        event = (
            AccountAuditLog.EventType.MEMBERSHIP_PROMOTED
            if is_promote
            else AccountAuditLog.EventType.MEMBERSHIP_DEMOTED
        )
        AccountAuditLog.log(
            event_type=event,
            actor=request.user,
            target_user=target,
            message=f"{old_level} → {new_level}",
            ip_address=request.META.get("REMOTE_ADDR"),
        )

        return JsonResponse({"success": True})
```

- [ ] **Step 4: Wire the URL**

In `apps/accounts/urls.py`, add the import at the top (alongside the existing `from . import views`):

```python
from .views_membership import MembershipSetView
```

Append to `urlpatterns`:

```python
    path(
        "users/<int:pk>/membership/",
        MembershipSetView.as_view(),
        name="membership_set",
    ),
```

- [ ] **Step 5: Ruff format**

```bash
.venv/bin/ruff format apps/accounts/views_membership.py apps/accounts/urls.py tests/test_views_membership.py
.venv/bin/ruff format --check . && .venv/bin/ruff check .
```

- [ ] **Step 6: Run tests to verify pass**

```bash
.venv/bin/python -m pytest tests/test_views_membership.py -v
```

Expected: 9 PASS.

- [ ] **Step 7: Full-suite regression**

```bash
.venv/bin/python -m pytest -q 2>&1 | tail -3
```

Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add apps/accounts/views_membership.py apps/accounts/urls.py tests/test_views_membership.py
git commit -m "feat(accounts): MembershipSetView for promote/demote + audit emission

POST /accounts/users/<pk>/membership/ accepts a target level and
mutates User.membership_level with four invariants:
- AdminRequiredMixin (Vereins-Admin only)
- Cannot promote/demote self (400)
- Cannot demote to APPLICANT if user holds assignments (400)
- Invalid level rejected (400)

Successful change emits AccountAuditLog MEMBERSHIP_PROMOTED or
MEMBERSHIP_DEMOTED with actor=request.user — closes the audit gap
that PR-2 deferred (signals can't know the actor)."
```

---

## Task 4: Membership-card on `user_form.html`

**Files:**
- Create: `apps/accounts/templates/accounts/_membership_card.html`
- Modify: `apps/accounts/templates/accounts/user_form.html`
- Modify: `apps/accounts/views.py` (UserUpdateView context)

UI work — invoke superpowers:frontend-design before writing the template if available (per CLAUDE.md the pixel-agent guideline applies to all UI work).

The card displays current membership level + a `<select>` dropdown with the four levels, posted via HTMX. Form-target is the card itself (`hx-target="this"`, `hx-swap="outerHTML"`) — but since the view returns JSON, we use `hx-on::after-request` to reload the section on success. Simpler alternative: server returns a re-rendered fragment. We go with the fragment approach for clean rerendering — extend `MembershipSetView.post` to render the fragment on the HTMX path.

Actually no — keep `MembershipSetView` JSON-only as Task 3 built it (matches `AlertRuleUpdateView` pattern from existing monitoring code). Use a simple page-reload approach for the card refresh: `hx-on::htmx:after-request="if (event.detail.successful) window.location.reload()"`. Less elegant than fragment rerender but simpler and reliable.

- [ ] **Step 1: Write the failing template test**

Append to `tests/test_views_membership.py`:

```python
@pytest.mark.django_db
def test_user_form_renders_membership_card_for_admin(
    client, admin_user
):
    """Admin viewing another user sees the membership picker."""
    target = User.objects.create_user(
        username="hans", password="x", email="hans@x"
    )
    target.membership_level = User.MembershipLevel.MEMBER
    target.save(update_fields=["membership_level"])

    client.force_login(admin_user)
    response = client.get(
        reverse("accounts:user_edit", args=[target.pk])
    )
    assert response.status_code == 200
    body = response.content.decode()
    # Section header + the dropdown
    assert "Vereins-Rolle" in body or "Vereinsrolle" in body
    assert "<select" in body
    # All four membership-level options should appear by display label
    assert "Vereins-Bewerber" in body
    assert "Vereins-Mitglied" in body
    assert "Vereins-Staff" in body
    assert "Vereins-Admin" in body


@pytest.mark.django_db
def test_user_form_does_not_render_membership_card_on_self(
    client, admin_user
):
    """Admin viewing their own edit page sees no membership picker
    (self-promote/demote is forbidden)."""
    client.force_login(admin_user)
    response = client.get(
        reverse("accounts:user_edit", args=[admin_user.pk])
    )
    body = response.content.decode()
    # Section is hidden on self-view
    assert "Vereins-Rolle" not in body
    assert "Vereinsrolle" not in body
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/python -m pytest tests/test_views_membership.py::test_user_form_renders_membership_card_for_admin tests/test_views_membership.py::test_user_form_does_not_render_membership_card_on_self -v
```

Expected: 2 failures (no membership section in `user_form.html` yet).

- [ ] **Step 3: Create the fragment template**

Create `apps/accounts/templates/accounts/_membership_card.html`:

```html
{% load i18n %}
<section class="panel" id="membership-card" style="max-width:640px;margin-top:16px;">
  <div class="panel-head">
    <div class="panel-title"><span class="dot"></span>{% trans "Vereins-Rolle" %}</div>
  </div>
  <div class="panel-body">
    <p class="t-muted" style="margin-bottom:12px;">
      {% trans "Current:" %}
      <strong>{{ object.get_membership_level_display }}</strong>
    </p>
    <form hx-post="{% url 'accounts:membership_set' object.pk %}"
          hx-on::after-request="if (event.detail.successful) window.location.reload()">
      {% csrf_token %}
      <label class="form-label" for="membership-level-select">
        {% trans "Set membership level" %}:
      </label>
      <select id="membership-level-select" name="level" class="form-select" style="max-width:280px;">
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
  </div>
</section>
```

- [ ] **Step 4: Embed the card + add context**

In `apps/accounts/views.py`, update `UserUpdateView.get_context_data`. Find:

```python
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_title"] = _("Edit User")
        # Local import: avoids loading apps.sso at module-load time
        # (defensive against import-cycle surprises).
        from apps.sso.views import _build_grants_for_user

        context["app_grants_list"] = _build_grants_for_user(self.object)
        return context
```

Replace with:

```python
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_title"] = _("Edit User")
        # Local import: avoids loading apps.sso at module-load time
        # (defensive against import-cycle surprises).
        from apps.sso.views import _build_grants_for_user

        context["app_grants_list"] = _build_grants_for_user(self.object)
        # Membership-level picker uses the model's TextChoices.
        context["membership_level_choices"] = User.MembershipLevel.choices
        return context
```

In `apps/accounts/templates/accounts/user_form.html`, find the existing block at the end of `{% block content %}`:

```html
{% if request.user.is_admin and object %}
  {% include "sso/_app_grants_card.html" with target_user=object applications=app_grants_list %}
{% endif %}
{% endblock %}
```

Replace with:

```html
{% if request.user.is_admin and object and object.pk != request.user.pk %}
  {% include "accounts/_membership_card.html" %}
{% endif %}

{% if request.user.is_admin and object %}
  {% include "sso/_app_grants_card.html" with target_user=object applications=app_grants_list %}
{% endif %}
{% endblock %}
```

The `object.pk != request.user.pk` guard hides the card on self-view, mirroring the view's self-block.

- [ ] **Step 5: Run tests to verify pass**

```bash
.venv/bin/python -m pytest tests/test_views_membership.py -v
```

Expected: 11 PASS (9 from Task 3 + 2 template smokes).

- [ ] **Step 6: Ruff format**

```bash
.venv/bin/ruff format apps/accounts/views.py tests/test_views_membership.py
.venv/bin/ruff format --check . && .venv/bin/ruff check .
```

- [ ] **Step 7: Commit**

```bash
git add apps/accounts/templates/accounts/_membership_card.html apps/accounts/templates/accounts/user_form.html apps/accounts/views.py tests/test_views_membership.py
git commit -m "feat(accounts): membership-card on user-edit page

HTMX-driven membership-level picker rendered above the SSO app-grants
card on the user-edit page. Admin selects target level + clicks Apply;
hx-post calls MembershipSetView; on success the page reloads to show
the new level (no fragment-render needed — JSON response + window
reload mirrors the AlertRuleUpdateView pattern in monitoring).

The card is hidden on self-view (matches the view's self-block at
the POST endpoint) so admins don't see a control they can't use."
```

---

# Phase 2: Region-Assignment Widget

## Task 5: `RegionAssignmentCreateView` + `RevokeView`

**Files:**
- Create: `apps/accounts/views_region_assignments.py`
- Modify: `apps/accounts/urls.py`
- Create: `tests/test_views_region_assignments.py`

POST endpoints for adding/removing RegionAssignments on a target user. Audit-log emission for create+delete is already wired via signals (PR-2 Task 4), so the view doesn't emit — it just creates/deletes the model row and lets the signal fire.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_views_region_assignments.py`:

```python
"""Tests for RegionAssignment HTMX endpoints."""

import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.stations.models import Region, RegionAssignment


def _user(level, username):
    u = User.objects.create_user(
        username=username, password="x", email=f"{username}@x"
    )
    u.membership_level = level
    u.save(update_fields=["membership_level"])
    return u


@pytest.mark.django_db
class TestRegionAssignmentCreateView:
    def test_admin_can_add_region_manager(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        lisa = _user(User.MembershipLevel.MEMBER, "lisa")
        r = Region.objects.create(name="Tirol", slug="tirol")
        client.force_login(admin)
        response = client.post(
            reverse(
                "accounts:region_assignment_create",
                args=[lisa.pk],
            ),
            {"region": r.pk},
        )
        assert response.status_code == 200
        assert RegionAssignment.objects.filter(
            user=lisa, region=r
        ).exists()

    def test_applicant_target_returns_400(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        applicant = _user(User.MembershipLevel.APPLICANT, "newbie")
        r = Region.objects.create(name="Tirol", slug="tirol")
        client.force_login(admin)
        response = client.post(
            reverse(
                "accounts:region_assignment_create",
                args=[applicant.pk],
            ),
            {"region": r.pk},
        )
        assert response.status_code == 400
        assert not RegionAssignment.objects.filter(
            user=applicant
        ).exists()

    def test_non_admin_forbidden(self, client):
        staff = _user(User.MembershipLevel.STAFF, "staff")
        target = _user(User.MembershipLevel.MEMBER, "tgt")
        r = Region.objects.create(name="Tirol", slug="tirol")
        client.force_login(staff)
        response = client.post(
            reverse(
                "accounts:region_assignment_create",
                args=[target.pk],
            ),
            {"region": r.pk},
        )
        assert response.status_code in (302, 403)
        assert not RegionAssignment.objects.filter(
            user=target
        ).exists()

    def test_invalid_region_returns_404(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        target = _user(User.MembershipLevel.MEMBER, "tgt")
        client.force_login(admin)
        response = client.post(
            reverse(
                "accounts:region_assignment_create",
                args=[target.pk],
            ),
            {"region": "99999"},
        )
        assert response.status_code == 404

    def test_duplicate_assignment_returns_400(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        lisa = _user(User.MembershipLevel.MEMBER, "lisa")
        r = Region.objects.create(name="Tirol", slug="tirol")
        RegionAssignment.objects.create(
            user=lisa, region=r, role=RegionAssignment.Role.MANAGER
        )
        client.force_login(admin)
        response = client.post(
            reverse(
                "accounts:region_assignment_create",
                args=[lisa.pk],
            ),
            {"region": r.pk},
        )
        # uniq_user_role_per_region constraint catches it
        assert response.status_code == 400


@pytest.mark.django_db
class TestRegionAssignmentRevokeView:
    def test_admin_can_revoke(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        lisa = _user(User.MembershipLevel.MEMBER, "lisa")
        r = Region.objects.create(name="Tirol", slug="tirol")
        a = RegionAssignment.objects.create(
            user=lisa, region=r, role=RegionAssignment.Role.MANAGER
        )
        client.force_login(admin)
        response = client.post(
            reverse(
                "accounts:region_assignment_revoke",
                args=[a.pk],
            )
        )
        assert response.status_code == 200
        assert not RegionAssignment.objects.filter(pk=a.pk).exists()

    def test_non_admin_forbidden(self, client):
        staff = _user(User.MembershipLevel.STAFF, "staff")
        lisa = _user(User.MembershipLevel.MEMBER, "lisa")
        r = Region.objects.create(name="Tirol", slug="tirol")
        a = RegionAssignment.objects.create(
            user=lisa, region=r, role=RegionAssignment.Role.MANAGER
        )
        client.force_login(staff)
        response = client.post(
            reverse(
                "accounts:region_assignment_revoke",
                args=[a.pk],
            )
        )
        assert response.status_code in (302, 403)
        assert RegionAssignment.objects.filter(pk=a.pk).exists()
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/python -m pytest tests/test_views_region_assignments.py -v
```

Expected: `NoReverseMatch` on the new URL names.

- [ ] **Step 3: Create the view module**

Create `apps/accounts/views_region_assignments.py`:

```python
"""HTMX views for managing RegionAssignments on a target user.

Both endpoints are Vereins-Admin only. Audit-log emission for
create+delete is already wired via signal handlers in
apps/stations/signals.py — these views just create or delete the
ORM row and let the signal fire.

Create path:
  POST /accounts/users/<user_pk>/region_assignments/
       body: {"region": "<region_pk>"}
  Returns 200 on success, 400 on ValidationError (e.g., target is
  APPLICANT or duplicate assignment), 404 if region not found.

Revoke path:
  POST /accounts/region_assignments/<pk>/revoke/
  Returns 200 on success, 404 if assignment not found.
"""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404
from django.views import View

from apps.accounts.views import AdminRequiredMixin
from apps.stations.models import Region, RegionAssignment

User = get_user_model()


class RegionAssignmentCreateView(AdminRequiredMixin, View):
    def post(self, request, user_pk):
        target = get_object_or_404(User, pk=user_pk)
        region_pk = request.POST.get("region")
        region = get_object_or_404(Region, pk=region_pk)
        try:
            RegionAssignment.objects.create(
                user=target,
                region=region,
                role=RegionAssignment.Role.MANAGER,
                assigned_by=request.user,
            )
        except ValidationError as e:
            # _ApplicantForbiddenMixin raises on save()
            return HttpResponseBadRequest(str(e))
        except IntegrityError:
            # uniq_user_role_per_region constraint
            return HttpResponseBadRequest(
                "Assignment already exists."
            )
        return JsonResponse({"success": True})


class RegionAssignmentRevokeView(AdminRequiredMixin, View):
    def post(self, request, pk):
        assignment = get_object_or_404(RegionAssignment, pk=pk)
        assignment.delete()
        return JsonResponse({"success": True})
```

- [ ] **Step 4: Wire the URLs**

In `apps/accounts/urls.py`, add to imports:

```python
from .views_region_assignments import (
    RegionAssignmentCreateView,
    RegionAssignmentRevokeView,
)
```

Append to `urlpatterns`:

```python
    path(
        "users/<int:user_pk>/region_assignments/",
        RegionAssignmentCreateView.as_view(),
        name="region_assignment_create",
    ),
    path(
        "region_assignments/<int:pk>/revoke/",
        RegionAssignmentRevokeView.as_view(),
        name="region_assignment_revoke",
    ),
```

- [ ] **Step 5: Ruff format**

```bash
.venv/bin/ruff format apps/accounts/views_region_assignments.py apps/accounts/urls.py tests/test_views_region_assignments.py
.venv/bin/ruff format --check . && .venv/bin/ruff check .
```

- [ ] **Step 6: Run tests to verify pass**

```bash
.venv/bin/python -m pytest tests/test_views_region_assignments.py -v
```

Expected: 7 PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/accounts/views_region_assignments.py apps/accounts/urls.py tests/test_views_region_assignments.py
git commit -m "feat(accounts): RegionAssignment HTMX endpoints (create + revoke)

POST endpoints for managing region-manager assignments on a target
user. AdminRequiredMixin gates access. Create catches the two model-
level invariants (_ApplicantForbiddenMixin ValidationError +
uniq_user_role_per_region IntegrityError) and surfaces them as 400.
Revoke is a thin .delete() wrapper.

Audit emission for both events is already wired via signal handlers
in apps/stations/signals.py (PR-2 Task 4) — the views just hit the
ORM and let the signal fire."
```

---

## Task 6: Region-assignments card on `user_form.html`

**Files:**
- Create: `apps/accounts/templates/accounts/_region_assignments_card.html`
- Modify: `apps/accounts/templates/accounts/user_form.html`
- Modify: `apps/accounts/views.py` (UserUpdateView context)

UI work — invoke superpowers:frontend-design before writing the template if available.

The card shows the current RegionAssignments as a list (each with a ✕ revoke button) plus a `<select>` of all-regions-not-yet-assigned and an Add button. Applicant users see a disabled card with an explanatory message instead of the form (`_ApplicantForbiddenMixin` would reject the POST anyway, but pre-disabling is better UX).

- [ ] **Step 1: Append failing tests**

Append to `tests/test_views_region_assignments.py`:

```python
@pytest.mark.django_db
class TestRegionAssignmentsCardRendering:
    def test_card_visible_to_admin_for_member(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        lisa = _user(User.MembershipLevel.MEMBER, "lisa")
        Region.objects.create(name="Tirol", slug="tirol")
        Region.objects.create(name="OOe", slug="ooe")
        client.force_login(admin)
        response = client.get(
            reverse("accounts:user_edit", args=[lisa.pk])
        )
        body = response.content.decode()
        assert response.status_code == 200
        assert "Region-Manager" in body
        # The select offers the two regions
        assert "Tirol" in body
        assert "OOe" in body

    def test_card_lists_existing_assignment_with_revoke_button(
        self, client
    ):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        lisa = _user(User.MembershipLevel.MEMBER, "lisa")
        r = Region.objects.create(name="Tirol", slug="tirol")
        a = RegionAssignment.objects.create(
            user=lisa, region=r, role=RegionAssignment.Role.MANAGER
        )
        client.force_login(admin)
        response = client.get(
            reverse("accounts:user_edit", args=[lisa.pk])
        )
        body = response.content.decode()
        # The revoke URL is rendered as the form target
        assert (
            reverse(
                "accounts:region_assignment_revoke", args=[a.pk]
            )
            in body
        )

    def test_card_warns_for_applicant_target(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        applicant = _user(User.MembershipLevel.APPLICANT, "newbie")
        client.force_login(admin)
        response = client.get(
            reverse("accounts:user_edit", args=[applicant.pk])
        )
        body = response.content.decode()
        # The warning mentions the membership-level requirement
        assert "Vereins-Bewerber" in body or "applicant" in body.lower()
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/python -m pytest tests/test_views_region_assignments.py::TestRegionAssignmentsCardRendering -v
```

Expected: 3 failures (no card yet).

- [ ] **Step 3: Create the card template**

Create `apps/accounts/templates/accounts/_region_assignments_card.html`:

```html
{% load i18n %}
<section class="panel" id="region-assignments-card" style="max-width:640px;margin-top:16px;">
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
              <form hx-post="{% url 'accounts:region_assignment_revoke' ra.pk %}"
                    hx-on::after-request="if (event.detail.successful) window.location.reload()"
                    style="display:inline;">
                {% csrf_token %}
                <button type="submit" class="btn btn-ghost btn-sm"
                        title="{% trans 'Revoke assignment' %}">✕</button>
              </form>
            </li>
          {% endfor %}
        </ul>
      {% else %}
        <p class="t-muted" style="margin-bottom:12px;">
          {% trans "No Region-Manager assignments yet." %}
        </p>
      {% endif %}

      {% if available_regions %}
        <form hx-post="{% url 'accounts:region_assignment_create' object.pk %}"
              hx-on::after-request="if (event.detail.successful) window.location.reload()">
          {% csrf_token %}
          <label class="form-label" for="region-add-select">
            {% trans "Add Region-Manager assignment" %}:
          </label>
          <div class="row-gap-8" style="align-items:flex-end;">
            <select id="region-add-select" name="region" class="form-select" style="max-width:280px;">
              {% for r in available_regions %}
                <option value="{{ r.pk }}">{{ r.name }}</option>
              {% endfor %}
            </select>
            <button type="submit" class="btn btn-primary btn-sm">{% trans "Add" %}</button>
          </div>
        </form>
      {% else %}
        <p class="t-muted t-mono-sm">
          {% trans "No remaining regions to assign." %}
        </p>
      {% endif %}

    {% endif %}
  </div>
</section>
```

- [ ] **Step 4: Add context + embed the card**

In `apps/accounts/views.py`, update `UserUpdateView.get_context_data`. The method currently looks like (after Task 4):

```python
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_title"] = _("Edit User")
        from apps.sso.views import _build_grants_for_user

        context["app_grants_list"] = _build_grants_for_user(self.object)
        context["membership_level_choices"] = User.MembershipLevel.choices
        return context
```

Replace with:

```python
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_title"] = _("Edit User")
        from apps.sso.views import _build_grants_for_user

        context["app_grants_list"] = _build_grants_for_user(self.object)
        context["membership_level_choices"] = User.MembershipLevel.choices

        # Region-Assignment card.
        from apps.stations.models import Region

        existing_ra = list(
            self.object.region_assignments.select_related("region")
        )
        context["existing_region_assignments"] = existing_ra
        assigned_region_ids = {ra.region_id for ra in existing_ra}
        context["available_regions"] = Region.objects.exclude(
            pk__in=assigned_region_ids
        ).order_by("name")
        return context
```

In `apps/accounts/templates/accounts/user_form.html`, find the section that now reads:

```html
{% if request.user.is_admin and object and object.pk != request.user.pk %}
  {% include "accounts/_membership_card.html" %}
{% endif %}

{% if request.user.is_admin and object %}
  {% include "sso/_app_grants_card.html" with target_user=object applications=app_grants_list %}
{% endif %}
{% endblock %}
```

Replace with:

```html
{% if request.user.is_admin and object and object.pk != request.user.pk %}
  {% include "accounts/_membership_card.html" %}
{% endif %}

{% if request.user.is_admin and object %}
  {% include "accounts/_region_assignments_card.html" %}
{% endif %}

{% if request.user.is_admin and object %}
  {% include "sso/_app_grants_card.html" with target_user=object applications=app_grants_list %}
{% endif %}
{% endblock %}
```

(The region-card shows on the self-view too — it's harmless to see your own region assignments; the POST endpoints don't have a self-block since admin-on-self is a valid operator scenario.)

- [ ] **Step 5: Run tests to verify pass**

```bash
.venv/bin/python -m pytest tests/test_views_region_assignments.py -v
```

Expected: 10 PASS (7 + 3 new).

- [ ] **Step 6: Ruff format**

```bash
.venv/bin/ruff format apps/accounts/views.py tests/test_views_region_assignments.py
.venv/bin/ruff format --check . && .venv/bin/ruff check .
```

- [ ] **Step 7: Commit**

```bash
git add apps/accounts/templates/accounts/_region_assignments_card.html apps/accounts/templates/accounts/user_form.html apps/accounts/views.py tests/test_views_region_assignments.py
git commit -m "feat(accounts): region-assignments card on user-edit page

HTMX-driven card showing current RegionAssignments + a select-and-add
form for new ones. APPLICANT targets see a disabled explanation
instead of the form (the _ApplicantForbiddenMixin would reject the
POST anyway; pre-disabling is better UX).

Available-regions list excludes already-assigned regions to prevent
duplicate-constraint surprises in the UI."
```

---

# Phase 3: Station-Assignment Widget

## Task 7: `StationAssignmentCreateView` + `RevokeView`

**Files:**
- Create: `apps/accounts/views_station_assignments.py`
- Modify: `apps/accounts/urls.py`
- Create: `tests/test_views_station_assignments.py`

POST endpoints for adding/removing StationAssignments. Additional complexity over RegionAssignment: each station can have at most one ADMIN-role assignment (DB partial-unique-constraint `uniq_admin_per_station`). The view supports a takeover mode where adding a new admin atomically deletes the existing one for that station — but only when the request explicitly sets `takeover=1`. Default behavior on conflict is 409 Conflict (so the UI can show a confirm dialog).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_views_station_assignments.py`:

```python
"""Tests for StationAssignment HTMX endpoints."""

import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.stations.models import Station, StationAssignment


def _user(level, username):
    u = User.objects.create_user(
        username=username, password="x", email=f"{username}@x"
    )
    u.membership_level = level
    u.save(update_fields=["membership_level"])
    return u


@pytest.mark.django_db
class TestStationAssignmentCreateView:
    def test_admin_can_add_station_admin(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        franz = _user(User.MembershipLevel.MEMBER, "franz")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        client.force_login(admin)
        response = client.post(
            reverse(
                "accounts:station_assignment_create",
                args=[franz.pk],
            ),
            {"station": s.pk, "role": "admin"},
        )
        assert response.status_code == 200
        assert StationAssignment.objects.filter(
            user=franz, station=s, role="admin"
        ).exists()

    def test_admin_can_add_station_maintainer(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        hans = _user(User.MembershipLevel.MEMBER, "hans")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        client.force_login(admin)
        response = client.post(
            reverse(
                "accounts:station_assignment_create",
                args=[hans.pk],
            ),
            {"station": s.pk, "role": "maintainer"},
        )
        assert response.status_code == 200
        assert StationAssignment.objects.filter(
            user=hans, station=s, role="maintainer"
        ).exists()

    def test_applicant_target_returns_400(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        a = _user(User.MembershipLevel.APPLICANT, "newbie")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        client.force_login(admin)
        response = client.post(
            reverse(
                "accounts:station_assignment_create",
                args=[a.pk],
            ),
            {"station": s.pk, "role": "admin"},
        )
        assert response.status_code == 400

    def test_invalid_role_returns_400(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        target = _user(User.MembershipLevel.MEMBER, "tgt")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        client.force_login(admin)
        response = client.post(
            reverse(
                "accounts:station_assignment_create",
                args=[target.pk],
            ),
            {"station": s.pk, "role": "warlord"},
        )
        assert response.status_code == 400

    def test_admin_conflict_without_takeover_returns_409(
        self, client
    ):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        franz = _user(User.MembershipLevel.MEMBER, "franz")
        otto = _user(User.MembershipLevel.MEMBER, "otto")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        StationAssignment.objects.create(
            user=franz, station=s, role=StationAssignment.Role.ADMIN
        )
        client.force_login(admin)
        response = client.post(
            reverse(
                "accounts:station_assignment_create",
                args=[otto.pk],
            ),
            {"station": s.pk, "role": "admin"},
        )
        assert response.status_code == 409
        # franz still has the admin role
        assert StationAssignment.objects.filter(
            user=franz, station=s, role="admin"
        ).exists()

    def test_admin_takeover_replaces_existing(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        franz = _user(User.MembershipLevel.MEMBER, "franz")
        otto = _user(User.MembershipLevel.MEMBER, "otto")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        StationAssignment.objects.create(
            user=franz, station=s, role=StationAssignment.Role.ADMIN
        )
        client.force_login(admin)
        response = client.post(
            reverse(
                "accounts:station_assignment_create",
                args=[otto.pk],
            ),
            {"station": s.pk, "role": "admin", "takeover": "1"},
        )
        assert response.status_code == 200
        # franz lost the role, otto has it
        assert not StationAssignment.objects.filter(
            user=franz, station=s, role="admin"
        ).exists()
        assert StationAssignment.objects.filter(
            user=otto, station=s, role="admin"
        ).exists()

    def test_non_admin_forbidden(self, client):
        staff = _user(User.MembershipLevel.STAFF, "staff")
        target = _user(User.MembershipLevel.MEMBER, "tgt")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        client.force_login(staff)
        response = client.post(
            reverse(
                "accounts:station_assignment_create",
                args=[target.pk],
            ),
            {"station": s.pk, "role": "admin"},
        )
        assert response.status_code in (302, 403)


@pytest.mark.django_db
class TestStationAssignmentRevokeView:
    def test_admin_can_revoke(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        franz = _user(User.MembershipLevel.MEMBER, "franz")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        a = StationAssignment.objects.create(
            user=franz,
            station=s,
            role=StationAssignment.Role.MAINTAINER,
        )
        client.force_login(admin)
        response = client.post(
            reverse(
                "accounts:station_assignment_revoke",
                args=[a.pk],
            )
        )
        assert response.status_code == 200
        assert not StationAssignment.objects.filter(
            pk=a.pk
        ).exists()
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/python -m pytest tests/test_views_station_assignments.py -v
```

Expected: `NoReverseMatch` on the new URL names.

- [ ] **Step 3: Create the view module**

Create `apps/accounts/views_station_assignments.py`:

```python
"""HTMX views for managing StationAssignments on a target user.

Both endpoints are Vereins-Admin only.

Create path:
  POST /accounts/users/<user_pk>/station_assignments/
       body: {"station": "<pk>", "role": "admin"|"maintainer",
              "takeover": "1" (optional, admin role only)}

  Special admin-takeover logic: each station has a partial-unique
  constraint allowing at most one ADMIN-role assignment. When the
  request asks to make `target` the admin AND someone else already
  is the admin:
    - if takeover=="1" → atomically delete the existing admin row
      then create the new one (single transaction).
    - else → return 409 Conflict so the UI can show a confirm
      dialog ("Take over from <existing>?") then re-post with
      takeover=1.

Returns: 200 success, 400 validation (applicant target, invalid
role), 409 admin-conflict-without-takeover, 404 station not found.

Audit emission for create+delete is already wired via signals in
apps/stations/signals.py — these views just hit the ORM.
"""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404
from django.views import View

from apps.accounts.views import AdminRequiredMixin
from apps.stations.models import Station, StationAssignment

User = get_user_model()


class StationAssignmentCreateView(AdminRequiredMixin, View):
    def post(self, request, user_pk):
        target = get_object_or_404(User, pk=user_pk)
        station_pk = request.POST.get("station")
        station = get_object_or_404(Station, pk=station_pk)

        role = request.POST.get("role", "").strip()
        if role not in {"admin", "maintainer"}:
            return HttpResponseBadRequest(
                f"Invalid role: {role!r}"
            )

        takeover = request.POST.get("takeover") == "1"

        if role == "admin":
            existing_admin = StationAssignment.objects.filter(
                station=station,
                role=StationAssignment.Role.ADMIN,
            ).first()
            if existing_admin and existing_admin.user_id != target.pk:
                if not takeover:
                    return JsonResponse(
                        {
                            "error": "admin_conflict",
                            "current_admin_username": (
                                existing_admin.user.username
                            ),
                            "current_admin_id": existing_admin.user_id,
                        },
                        status=409,
                    )
                # takeover=1: delete existing, then create new, atomic
                try:
                    with transaction.atomic():
                        existing_admin.delete()
                        StationAssignment.objects.create(
                            user=target,
                            station=station,
                            role=StationAssignment.Role.ADMIN,
                            assigned_by=request.user,
                        )
                except ValidationError as e:
                    return HttpResponseBadRequest(str(e))
                return JsonResponse({"success": True})

        # Plain create path
        try:
            StationAssignment.objects.create(
                user=target,
                station=station,
                role=role,
                assigned_by=request.user,
            )
        except ValidationError as e:
            return HttpResponseBadRequest(str(e))
        except IntegrityError:
            return HttpResponseBadRequest(
                "Assignment already exists."
            )
        return JsonResponse({"success": True})


class StationAssignmentRevokeView(AdminRequiredMixin, View):
    def post(self, request, pk):
        assignment = get_object_or_404(StationAssignment, pk=pk)
        assignment.delete()
        return JsonResponse({"success": True})
```

- [ ] **Step 4: Wire the URLs**

In `apps/accounts/urls.py`, add to imports:

```python
from .views_station_assignments import (
    StationAssignmentCreateView,
    StationAssignmentRevokeView,
)
```

Append to `urlpatterns`:

```python
    path(
        "users/<int:user_pk>/station_assignments/",
        StationAssignmentCreateView.as_view(),
        name="station_assignment_create",
    ),
    path(
        "station_assignments/<int:pk>/revoke/",
        StationAssignmentRevokeView.as_view(),
        name="station_assignment_revoke",
    ),
```

- [ ] **Step 5: Ruff format**

```bash
.venv/bin/ruff format apps/accounts/views_station_assignments.py apps/accounts/urls.py tests/test_views_station_assignments.py
.venv/bin/ruff format --check . && .venv/bin/ruff check .
```

- [ ] **Step 6: Run tests to verify pass**

```bash
.venv/bin/python -m pytest tests/test_views_station_assignments.py -v
```

Expected: 8 PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/accounts/views_station_assignments.py apps/accounts/urls.py tests/test_views_station_assignments.py
git commit -m "feat(accounts): StationAssignment HTMX endpoints with admin-takeover

POST endpoints for managing station-admin/maintainer assignments.
Each station can have at most one ADMIN-role assignment (DB partial
unique). The create-view supports two modes:
- default: 409 Conflict if another user already is the admin (UI
  shows confirm dialog 'Take over from <user>?')
- takeover=1: atomic delete-existing + create-new in a single
  transaction (per uniq_admin_per_station DB constraint).

Maintainer role has no cardinality limit. Both paths catch
_ApplicantForbiddenMixin ValidationError and surface as 400.

Audit emission already wired via PR-2 signals — views just hit ORM."
```

---

## Task 8: Station-assignments card on `user_form.html`

**Files:**
- Create: `apps/accounts/templates/accounts/_station_assignments_card.html`
- Modify: `apps/accounts/templates/accounts/user_form.html`
- Modify: `apps/accounts/views.py` (UserUpdateView context)

UI work — invoke superpowers:frontend-design before writing the template if available.

The card lists current StationAssignments grouped by role (Station-Admin first, then Maintainers) with revoke buttons, plus a two-step picker: pick a station + role. The 409-Conflict admin-takeover UX is handled via a simple JS confirm: on 409, parse the JSON body for the conflicting username, show `confirm()`, on OK resubmit with `takeover=1`. Inline JS in the template keeps the indirection minimal.

- [ ] **Step 1: Append failing tests**

Append to `tests/test_views_station_assignments.py`:

```python
@pytest.mark.django_db
class TestStationAssignmentsCardRendering:
    def test_card_visible_to_admin_for_member(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        franz = _user(User.MembershipLevel.MEMBER, "franz")
        Station.objects.create(name="OE5A", callsign="OE5A")
        Station.objects.create(name="OE5B", callsign="OE5B")
        client.force_login(admin)
        response = client.get(
            reverse("accounts:user_edit", args=[franz.pk])
        )
        body = response.content.decode()
        assert response.status_code == 200
        assert "Station-Zuordnungen" in body or "Station Assignments" in body
        assert "OE5A" in body
        assert "OE5B" in body

    def test_card_lists_existing_with_revoke_button(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        franz = _user(User.MembershipLevel.MEMBER, "franz")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        a = StationAssignment.objects.create(
            user=franz,
            station=s,
            role=StationAssignment.Role.ADMIN,
        )
        client.force_login(admin)
        response = client.get(
            reverse("accounts:user_edit", args=[franz.pk])
        )
        body = response.content.decode()
        assert (
            reverse(
                "accounts:station_assignment_revoke", args=[a.pk]
            )
            in body
        )
        # Display label for the role
        assert "Station-Admin" in body

    def test_card_warns_for_applicant_target(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        applicant = _user(User.MembershipLevel.APPLICANT, "newbie")
        Station.objects.create(name="OE5A", callsign="OE5A")
        client.force_login(admin)
        response = client.get(
            reverse("accounts:user_edit", args=[applicant.pk])
        )
        body = response.content.decode()
        assert (
            "Vereins-Bewerber" in body
            or "applicant" in body.lower()
        )
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/python -m pytest tests/test_views_station_assignments.py::TestStationAssignmentsCardRendering -v
```

Expected: 3 failures (no card yet).

- [ ] **Step 3: Create the card template**

Create `apps/accounts/templates/accounts/_station_assignments_card.html`:

```html
{% load i18n %}
<section class="panel" id="station-assignments-card" style="max-width:640px;margin-top:16px;">
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
              <form hx-post="{% url 'accounts:station_assignment_revoke' sa.pk %}"
                    hx-on::after-request="if (event.detail.successful) window.location.reload()"
                    style="display:inline;">
                {% csrf_token %}
                <button type="submit" class="btn btn-ghost btn-sm"
                        title="{% trans 'Revoke assignment' %}">✕</button>
              </form>
            </li>
          {% endfor %}
        </ul>
      {% else %}
        <p class="t-muted" style="margin-bottom:12px;">
          {% trans "No Station assignments yet." %}
        </p>
      {% endif %}

      {% if all_stations %}
        <form id="station-assignment-add-form"
              hx-post="{% url 'accounts:station_assignment_create' object.pk %}"
              hx-on::after-request="handleStationAssignmentResponse(event)">
          {% csrf_token %}
          <label class="form-label">{% trans "Add Station assignment" %}:</label>
          <div class="row-gap-8" style="align-items:flex-end;flex-wrap:wrap;">
            <select name="station" class="form-select" style="max-width:240px;">
              {% for s in all_stations %}
                <option value="{{ s.pk }}">{{ s.callsign|default:s.name }}</option>
              {% endfor %}
            </select>
            <select name="role" class="form-select" style="max-width:200px;">
              <option value="admin">{% trans "Station-Admin (max 1)" %}</option>
              <option value="maintainer" selected>{% trans "Station-Maintainer" %}</option>
            </select>
            <input type="hidden" name="takeover" value="0">
            <button type="submit" class="btn btn-primary btn-sm">{% trans "Add" %}</button>
          </div>
        </form>

        <script nonce="{{ request.csp_nonce }}">
          function handleStationAssignmentResponse(event) {
            const xhr = event.detail.xhr;
            if (event.detail.successful) {
              window.location.reload();
              return;
            }
            if (xhr.status === 409) {
              let info = {};
              try { info = JSON.parse(xhr.responseText); } catch (_) {}
              const currentAdmin = info.current_admin_username || 'someone';
              const ok = window.confirm(
                "{% trans 'Station already has' %} " + currentAdmin +
                " {% trans 'as Station-Admin. Take over the role?' %}"
              );
              if (!ok) return;
              const form = document.getElementById('station-assignment-add-form');
              form.querySelector('input[name=takeover]').value = '1';
              htmx.trigger(form, 'submit');
              form.querySelector('input[name=takeover]').value = '0';
            }
          }
        </script>
      {% else %}
        <p class="t-muted t-mono-sm">
          {% trans "No stations to assign." %}
        </p>
      {% endif %}

    {% endif %}
  </div>
</section>
```

- [ ] **Step 4: Add context + embed the card**

In `apps/accounts/views.py`, update `UserUpdateView.get_context_data` once more. It currently looks like (after Task 6):

```python
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_title"] = _("Edit User")
        from apps.sso.views import _build_grants_for_user

        context["app_grants_list"] = _build_grants_for_user(self.object)
        context["membership_level_choices"] = User.MembershipLevel.choices

        # Region-Assignment card.
        from apps.stations.models import Region

        existing_ra = list(
            self.object.region_assignments.select_related("region")
        )
        context["existing_region_assignments"] = existing_ra
        assigned_region_ids = {ra.region_id for ra in existing_ra}
        context["available_regions"] = Region.objects.exclude(
            pk__in=assigned_region_ids
        ).order_by("name")
        return context
```

Replace with:

```python
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_title"] = _("Edit User")
        from apps.sso.views import _build_grants_for_user

        context["app_grants_list"] = _build_grants_for_user(self.object)
        context["membership_level_choices"] = User.MembershipLevel.choices

        # Region-Assignment card.
        from apps.stations.models import Region, Station

        existing_ra = list(
            self.object.region_assignments.select_related("region")
        )
        context["existing_region_assignments"] = existing_ra
        assigned_region_ids = {ra.region_id for ra in existing_ra}
        context["available_regions"] = Region.objects.exclude(
            pk__in=assigned_region_ids
        ).order_by("name")

        # Station-Assignment card.
        context["existing_station_assignments"] = list(
            self.object.station_assignments.select_related("station")
        )
        context["all_stations"] = Station.objects.order_by("name")
        return context
```

In `apps/accounts/templates/accounts/user_form.html`, find the now-three-include block at the end of `{% block content %}`:

```html
{% if request.user.is_admin and object and object.pk != request.user.pk %}
  {% include "accounts/_membership_card.html" %}
{% endif %}

{% if request.user.is_admin and object %}
  {% include "accounts/_region_assignments_card.html" %}
{% endif %}

{% if request.user.is_admin and object %}
  {% include "sso/_app_grants_card.html" with target_user=object applications=app_grants_list %}
{% endif %}
{% endblock %}
```

Replace with:

```html
{% if request.user.is_admin and object and object.pk != request.user.pk %}
  {% include "accounts/_membership_card.html" %}
{% endif %}

{% if request.user.is_admin and object %}
  {% include "accounts/_region_assignments_card.html" %}
{% endif %}

{% if request.user.is_admin and object %}
  {% include "accounts/_station_assignments_card.html" %}
{% endif %}

{% if request.user.is_admin and object %}
  {% include "sso/_app_grants_card.html" with target_user=object applications=app_grants_list %}
{% endif %}
{% endblock %}
```

- [ ] **Step 5: Run tests to verify pass**

```bash
.venv/bin/python -m pytest tests/test_views_station_assignments.py -v
```

Expected: 11 PASS (8 + 3 new).

- [ ] **Step 6: Ruff format**

```bash
.venv/bin/ruff format apps/accounts/views.py tests/test_views_station_assignments.py
.venv/bin/ruff format --check . && .venv/bin/ruff check .
```

- [ ] **Step 7: Commit**

```bash
git add apps/accounts/templates/accounts/_station_assignments_card.html apps/accounts/templates/accounts/user_form.html apps/accounts/views.py tests/test_views_station_assignments.py
git commit -m "feat(accounts): station-assignments card on user-edit page

HTMX card listing current StationAssignments + a station/role
picker. Two-step picker means operators can grant Station-Admin or
Station-Maintainer in one submit.

Admin-conflict UX: when the create-view returns 409 with the
existing-admin info, an inline handler shows a confirm dialog,
then resubmits the same form with takeover=1 to atomically replace
the existing admin (per uniq_admin_per_station DB constraint).
APPLICANT targets see a disabled explanation instead of the form."
```

---

# Wrap-Up

## Task 9: Full-suite regression run

**Files:** (none modified)

- [ ] **Step 1: Run full suite + lint**

```bash
.venv/bin/python -m pytest -q 2>&1 | tail -10
.venv/bin/ruff format --check . && .venv/bin/ruff check .
```

Expected: all PASS + ruff clean.

If lint changes anything, run `.venv/bin/ruff format .` and amend the relevant last commit (`git commit --amend --no-edit`).

---

## Task 10: Push branch + create PR

**Files:** (none modified)

- [ ] **Step 1: Push**

```bash
git push -u origin feat/membership-levels-pr3-user-ui
```

(Branch is created by the controller before dispatching Task 1.)

- [ ] **Step 2: Open PR**

```bash
gh pr create --title "feat(accounts): user UI for membership + topology assignments (PR-3)" --body "$(cat <<'EOF'
## Summary

**PR-3 of 3** — User-side UI. Operators can now manage membership-levels
and per-user topology assignments (Region-Manager, Station-Admin/Maintainer)
from the existing user-edit page instead of Django Admin. Closes the
audit-emission gap that PR-2 deferred for membership_level changes
(needs view-level actor that signals cannot provide).

## What this PR does

**Phase 0 — Cleanup:**
- \`user_list.html\` Role column now renders \`get_membership_level_display\` pills instead of the dead \`u.groups.all\` loop (which has returned empty since PR-2's migration 0007 dropped the legacy groups)
- \`user_list.html\` sub-text updated: \"operator & member accounts\" → \"member, staff, and admin accounts\"
- \`UserListView.queryset\` drops the now-useless \`prefetch_related('groups')\`
- \`forms.py\` UserCreationForm + UserChangeForm docstrings updated to point at the new PR-3 cards instead of the retired Django-Admin group-management workflow

**Phase 1 — Membership-level promote/demote:**
- New \`MembershipSetView\` (POST /accounts/users/<pk>/membership/) with four invariants: AdminRequiredMixin, no-self, demote-to-applicant blocked when assignments exist, valid level
- Emits \`AccountAuditLog\` MEMBERSHIP_PROMOTED / MEMBERSHIP_DEMOTED with actor=request.user — closes the audit gap from PR-2
- Membership-card on user_form.html: dropdown of all four levels, HTMX submit, page reload on success; hidden on self-view

**Phase 2 — Region-Assignment widget:**
- New views_region_assignments.py with create + revoke endpoints (AdminRequiredMixin)
- Region-assignments card on user_form.html: list current assignments with revoke (✕), select-and-add form for new ones, APPLICANT targets see disabled explanation
- Audit emission already wired via PR-2 signals — views just hit the ORM

**Phase 3 — Station-Assignment widget:**
- New views_station_assignments.py with create + revoke endpoints; create supports admin-takeover (uniq_admin_per_station constraint)
- Station-assignments card: list current assignments grouped by role, two-step picker (station + role), inline JS confirm dialog for admin-takeover UX (409 → window.confirm → resubmit with takeover=1)

## What this PR does NOT do (deferred to PR-4)

- Station-Detail page: Region picker, Station-Admin/Maintainer pickers — operators continue to manage these from the user-edit page (which has the same data) until the Station-Detail UI lands
- Region-CRUD pages — operators continue using Django Admin for Region create/edit/delete
- Region-Manager edit-permission on stations of own region (currently gated to Vereins-Admin)
- Notification preferences per user, Telegram routing

## Test plan

- [x] Full test suite green
- [x] \`ruff format --check . && ruff check .\` clean
- [ ] Copilot review run
- [ ] Post-merge: \`gh workflow run main.yml --repo OE5XRX/servers\` to deploy
- [ ] Post-merge: verify the three cards on /accounts/users/<pk>/edit/ — membership picker (hidden on self), region-assignments, station-assignments
- [ ] Post-merge: end-to-end test (promote applicant → member → assign to a Region as Manager → verify alert routing reaches them on an offline-alert test)
- [ ] Begin PR-4 (Station-Detail UI + Region-CRUD) plan against the now-real signatures

## Spec + Plan

- Spec: \`docs/superpowers/specs/2026-06-05-membership-levels-and-topology-roles-design.md\` (§5.1, §5.4)
- Plan: \`docs/superpowers/plans/2026-06-06-membership-levels-and-topology-roles-pr3.md\`

## Execution notes

Built via Subagent-Driven Development per CLAUDE.md default. UI tasks invoked superpowers:frontend-design as a sub-skill per the pixel-agent rule.

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
| §5.1 Vereins-Rolle promote/demote on user-detail | Task 3 (view) + Task 4 (UI) — page is user_form, not user_detail (no detail page exists in-tree) |
| §5.1 Region-Manager list/add/revoke | Task 5 (views) + Task 6 (UI) |
| §5.1 Station-Zuordnungen list/add/revoke | Task 7 (views) + Task 8 (UI) |
| §5.1 Demote blocked when assignments exist | Task 3 (post handler) + tested in test_demote_to_applicant_blocked_when_assignments_exist |
| §5.1 Self-promote blocked | Task 3 + test_self_forbidden |
| §5.1 Admin-takeover for Station-Admin | Task 7 + test_admin_takeover_replaces_existing |
| §5.1 Applicant invariant (UI side) | Tasks 6 + 8 disable add-form on APPLICANT target; views 5/7 still 400 on bypassed POST |
| §5.4 Permission matrix (User-Edit row) | AdminRequiredMixin enforces "Vereins-Admin only" on all 5 endpoints; Region-Manager edit-perm deferred to PR-4 |
| §4.6 Membership audit emission with actor | Task 3 — closes the gap PR-2 deferred |
| Out-of-scope: Station-Detail, Region-CRUD | Honored, called out in PR description |

**Placeholder scan:** None — every step shows exact code, file paths, commands, and expected outputs.

**Type / signature consistency:**

- `MembershipSetView` URL kwarg `pk` matches `<int:pk>` in urls.py; reverse called with `args=[target.pk]` in tests.
- `RegionAssignmentCreateView` URL kwarg `user_pk` (matches `<int:user_pk>`); reverse `args=[user.pk]`. Revoke uses `pk` (matches `<int:pk>`); reverse `args=[assignment.pk]`.
- `StationAssignmentCreateView` URL kwarg `user_pk`; revoke `pk`. Symmetric with region.
- `User.MembershipLevel.APPLICANT/MEMBER/STAFF/ADMIN` consistent across views, tests, templates.
- `StationAssignment.Role.ADMIN/MAINTAINER` and `RegionAssignment.Role.MANAGER` consistent.
- `AccountAuditLog.log()` keyword-only signature: `event_type`, `actor`, `target_user`, `region`, `message`, `ip_address` — used identically in Task 3.
- `User._invalidate_role_cache(user)` static method — called in Task 3 after membership_level mutation.
- Template fragment include paths: `accounts/_membership_card.html`, `accounts/_region_assignments_card.html`, `accounts/_station_assignments_card.html` — consistent across Tasks 4, 6, 8.

No spec requirement left without a task.
