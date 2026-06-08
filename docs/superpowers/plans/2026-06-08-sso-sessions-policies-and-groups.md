# SSO Sessions, App Policies and Group Propagation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the existing SSO/OIDC provider in `station-manager` with per-application access policies (auto-grant for "open" apps), per-session lifecycle tracking with GeoIP-resolved location and admin-revoke, and a populated `groups` claim derived from `membership_level` + Station/RegionAssignments + Django auth.Group "tags".

**Architecture:** Two new models in `apps/sso` (`ApplicationPolicy`, `TokenSession`) sit as sidecars next to DOT's tables — no DOT model swap. The `user_can_access` gate becomes policy-aware. A `save_bearer_token` validator override records sessions, including GeoIP lookup via the `geoip2` library against a db-ip.com Free database that lives in a Docker volume and is refreshed daily by a GitHub-Actions cron. The `groups` claim is synthesised at token-issue time from the existing structured role data — Django auth.Group is repurposed as ad-hoc "tags" with a `tag:` prefix. All new mutating actions write to the existing `SsoAuditLog`. Servers-repo work (Docker mount, cron workflows, composite action) is Phase 8.

**Tech Stack:** Django 6.0, django-oauth-toolkit (DOT), `geoip2` (new), pytest-django, HTMX for partial swaps. PostgreSQL. Existing patterns: `AdminOnlyMixin` for permissions, `AppGrant`-style toggle views, `SsoAuditLog.log` for audit emission.

**Spec:** `docs/superpowers/specs/2026-06-08-sso-sessions-policies-and-groups-design.md`.

**Out of scope (deferred per Spec §11):** per-app tag-filter, mass-revoke on policy change, end-user-self-service session list, WebSocket live updates, MaxMind GeoIP, UA-parser library, group→permission elevation in station-manager itself.

---

## File Structure (locks decomposition decisions)

**Created files (station-manager):**
- `apps/sso/migrations/0004_application_policy.py` — ApplicationPolicy schema.
- `apps/sso/migrations/0005_token_session.py` — TokenSession schema.
- `apps/sso/migrations/0006_extend_audit_event_types.py` — AlterField on SsoAuditLog.event_type choices.
- `apps/sso/geoip.py` — thread-safe singleton wrapper around `geoip2.database.Reader`, lookup helper.
- `apps/sso/management/commands/update_geoip_db.py` — daily cron entrypoint.
- `apps/sso/management/commands/prune_token_sessions.py` — daily cron entrypoint.
- `apps/sso/templates/sso/_sessions_card.html` — HTMX-swap partial for the active-sessions card.
- `apps/sso/templates/sso/_tags_card.html` — HTMX-swap partial for tag toggles.
- `apps/sso/templates/sso/tag_list.html` — tag management listing.
- `apps/sso/templates/sso/tag_detail.html` — per-tag membership editor.
- `tests/test_sso_policy.py` — policy matrix (4 active levels × 5 policies × grant present/absent).
- `tests/test_sso_sessions.py` — TokenSession lifecycle (issue, refresh-rotate, admin-revoke, cascade).
- `tests/test_sso_geoip.py` — GeoIP wrapper happy/fail/fallback paths.
- `tests/test_sso_tags.py` — tag CRUD and membership toggle.
- `tests/fixtures/dbip-city-lite-test.mmdb` — tiny test fixture (~5KB, 2 known IPs).

**Modified files (station-manager):**
- `apps/sso/models.py` — add `ApplicationPolicy` and `TokenSession`; extend `SsoAuditLog.EventType`.
- `apps/sso/permissions.py` — `user_can_access` policy-aware; `SsoOAuth2Validator.save_bearer_token` override.
- `apps/sso/oidc_claims.py` — replace `groups` line with synthesis.
- `apps/sso/signals.py` — cascade TokenSession revoke on user-deactivate + grant-revoke.
- `apps/sso/views.py` — add `SessionRevokeView`, `ApplicationPolicyUpdateView`, tag views; extend `SsoDashboardView` + `ApplicationDetailView`.
- `apps/sso/urls.py` — wire up new view URLs.
- `apps/sso/templates/sso/dashboard.html` — KPI tile + policy column.
- `apps/sso/templates/sso/application_detail.html` — policy selector + group propagation section + recent sessions.
- `apps/accounts/templates/accounts/user_detail.html` — embed sessions-card and tags-card.
- `config/settings/base.py` — `GEOIP_DB_PATH`.
- `requirements/base.txt` — `geoip2>=4.8`.
- `tests/test_sso_claims.py` — extend for the synthesised-groups matrix.
- `tests/test_sso_audit.py` — extend for new event types.
- `tests/test_sso_views.py` — extend for new views.
- `tests/test_sso_flow.py` — extend E2E: session-row + GeoIP fields end-to-end.
- `README.md` — GeoIP setup blurb.

**Created files (servers repo, Phase 8):**
- `.github/actions/open-failure-issue/action.yml`
- `.github/workflows/update-geoip-db.yml`
- `.github/workflows/prune-token-sessions.yml`

**Modified files (servers repo, Phase 8):**
- `services/station_manager/docker-compose.yml`
- `.github/workflows/backup.yml`
- `services/station_manager/README.md` (if it exists; otherwise create)

---

## Phase 1 — Data Models

Three additive migrations, no data backfill, no DOT-model swap.

### Task 1.1: ApplicationPolicy model

**Files:**
- Modify: `apps/sso/models.py` (append new model)
- Create: `apps/sso/migrations/0004_application_policy.py`
- Test: `tests/test_sso_policy.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_sso_policy.py`:

```python
"""Tests for ApplicationPolicy model.

The model is a 1:1 sidecar to DOT's Application. Missing rows are
equivalent to GRANT_REQUIRED (the pre-existing behaviour). Stored
rows can express auto-approval policies tied to membership_level.
"""

import pytest
from oauth2_provider.models import Application

from apps.sso.models import ApplicationPolicy


@pytest.fixture
def app(db):
    return Application.objects.create(
        name="InvenTree",
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://example.org/oidc/callback/",
    )


def test_application_policy_default_is_grant_required(db, app):
    policy = ApplicationPolicy.objects.create(application=app)
    assert policy.access_policy == ApplicationPolicy.AccessPolicy.GRANT_REQUIRED


def test_application_policy_choices_include_all_five(db):
    choices = {value for value, _ in ApplicationPolicy.AccessPolicy.choices}
    assert choices == {
        "grant_required",
        "open_to_all",
        "open_to_members",
        "open_to_internal",
        "open_to_admins",
    }


def test_application_policy_is_one_to_one_with_application(db, app):
    ApplicationPolicy.objects.create(application=app)
    with pytest.raises(Exception):  # IntegrityError; sqlite/pg-agnostic
        ApplicationPolicy.objects.create(application=app)


def test_application_policy_modified_by_is_optional(db, app):
    policy = ApplicationPolicy.objects.create(application=app)
    assert policy.modified_by is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sso_policy.py -v`
Expected: `ImportError: cannot import name 'ApplicationPolicy' from 'apps.sso.models'`.

- [ ] **Step 3: Add the model**

Append to `apps/sso/models.py`:

```python
class ApplicationPolicy(models.Model):
    """Per-App access policy. 1:1 zu DOT's Application.

    Wenn keine Row existiert -> Policy ist implizit GRANT_REQUIRED
    (Spec §3.1).
    """

    class AccessPolicy(models.TextChoices):
        GRANT_REQUIRED = "grant_required", _("Grant required (default)")
        OPEN_TO_ALL = "open_to_all", _("Open to all (incl. applicants)")
        OPEN_TO_MEMBERS = "open_to_members", _("Open to members and above")
        OPEN_TO_INTERNAL = "open_to_internal", _("Open to staff and admins")
        OPEN_TO_ADMINS = "open_to_admins", _("Open to admins only")

    application = models.OneToOneField(
        "oauth2_provider.Application",
        on_delete=models.CASCADE,
        related_name="sso_policy",
        verbose_name=_("application"),
    )
    access_policy = models.CharField(
        _("access policy"),
        max_length=32,
        choices=AccessPolicy.choices,
        default=AccessPolicy.GRANT_REQUIRED,
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)
    modified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="modified_app_policies",
        verbose_name=_("modified by"),
    )

    class Meta:
        verbose_name = _("application policy")
        verbose_name_plural = _("application policies")

    def __str__(self):
        return f"{self.application.name} -> {self.get_access_policy_display()}"
```

Generate the migration: `python manage.py makemigrations sso --name application_policy`

The output should be `apps/sso/migrations/0004_application_policy.py`. Open it and confirm it matches the model fields above.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_sso_policy.py -v`
Expected: 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/sso/models.py apps/sso/migrations/0004_application_policy.py tests/test_sso_policy.py
git commit -m "feat(sso): ApplicationPolicy model with 5 access policies"
```

---

### Task 1.2: TokenSession model

**Files:**
- Modify: `apps/sso/models.py` (append)
- Create: `apps/sso/migrations/0005_token_session.py`
- Test: `tests/test_sso_sessions.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_sso_sessions.py`:

```python
"""Tests for TokenSession model — schema only in this task.

Lifecycle (validator hook, signals, admin-revoke) is covered in
later tasks.
"""

from datetime import timedelta

import pytest
from django.utils import timezone
from oauth2_provider.models import Application, RefreshToken

from apps.accounts.models import User
from apps.sso.models import TokenSession


@pytest.fixture
def app(db):
    return Application.objects.create(
        name="InvenTree",
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://example.org/oidc/callback/",
    )


@pytest.fixture
def user(db):
    return User.objects.create_user(username="peter", password="x")


def test_token_session_minimal_fields(db, user, app):
    s = TokenSession.objects.create(user=user, application=app)
    assert s.revoked_at is None
    assert s.ip_address is None
    assert s.user_agent == ""
    assert s.country_code == ""
    assert s.city == ""
    assert s.parent is None
    assert s.revoked_by is None


def test_token_session_revoke_reason_choices(db):
    choices = {value for value, _ in TokenSession.RevokeReason.choices}
    assert choices == {
        "admin_revoke",
        "user_logout",
        "user_deactivated",
        "grant_revoked",
        "rotated",
    }


def test_token_session_parent_self_reference(db, user, app):
    parent = TokenSession.objects.create(user=user, application=app)
    child = TokenSession.objects.create(user=user, application=app, parent=parent)
    assert child.parent == parent
    assert list(parent.children.all()) == [child]


def test_token_session_is_active_property(db, user, app):
    s = TokenSession.objects.create(user=user, application=app)
    # No refresh_token attached: not active.
    assert s.is_active is False

    # Revoked: not active.
    s.revoked_at = timezone.now()
    s.save(update_fields=["revoked_at"])
    assert s.is_active is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sso_sessions.py -v`
Expected: `ImportError: cannot import name 'TokenSession' from 'apps.sso.models'`.

- [ ] **Step 3: Add the model**

Append to `apps/sso/models.py`:

```python
class TokenSession(models.Model):
    """1:1 zu jeder RefreshToken-Issuance (inkl. Rotations-Chain).

    Spec §4.1.
    """

    class RevokeReason(models.TextChoices):
        ADMIN_REVOKE = "admin_revoke", _("Admin revoke")
        USER_LOGOUT = "user_logout", _("User logout")
        USER_DEACTIVATED = "user_deactivated", _("User deactivated")
        GRANT_REVOKED = "grant_revoked", _("Grant revoked")
        ROTATED = "rotated", _("Rotated (refresh)")

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="token_sessions",
        verbose_name=_("user"),
    )
    application = models.ForeignKey(
        "oauth2_provider.Application",
        on_delete=models.CASCADE,
        related_name="token_sessions",
        verbose_name=_("application"),
    )
    refresh_token = models.OneToOneField(
        "oauth2_provider.RefreshToken",
        on_delete=models.CASCADE,
        related_name="sso_session",
        null=True,
        blank=True,
        verbose_name=_("refresh token"),
    )
    parent = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="children",
        verbose_name=_("parent session"),
    )

    ip_address = models.GenericIPAddressField(_("IP address"), null=True, blank=True)
    user_agent = models.CharField(_("user agent"), max_length=512, blank=True)
    country_code = models.CharField(_("country code"), max_length=2, blank=True)
    city = models.CharField(_("city"), max_length=100, blank=True)

    issued_at = models.DateTimeField(_("issued at"), auto_now_add=True)
    last_seen_at = models.DateTimeField(_("last seen at"), auto_now_add=True)

    revoked_at = models.DateTimeField(_("revoked at"), null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="revoked_sessions",
        verbose_name=_("revoked by"),
    )
    revoke_reason = models.CharField(
        _("revoke reason"),
        max_length=32,
        choices=RevokeReason.choices,
        blank=True,
    )

    class Meta:
        verbose_name = _("token session")
        verbose_name_plural = _("token sessions")
        ordering = ("-issued_at",)
        indexes = [
            models.Index(fields=["user", "-issued_at"]),
            models.Index(fields=["application", "-issued_at"]),
            models.Index(fields=["revoked_at"]),
        ]

    @property
    def is_active(self) -> bool:
        """Lebende Session: nicht revoked, RefreshToken intakt, nicht
        ueber die Refresh-Lifetime hinaus."""
        from datetime import timedelta

        if self.revoked_at is not None:
            return False
        rt = self.refresh_token
        if rt is None or rt.revoked is not None:
            return False
        max_lifetime = timedelta(
            seconds=settings.OAUTH2_PROVIDER.get(
                "REFRESH_TOKEN_EXPIRE_SECONDS", 14 * 24 * 3600
            )
        )
        from django.utils import timezone

        return self.issued_at + max_lifetime > timezone.now()

    def __str__(self):
        status = "revoked" if self.revoked_at else "active"
        return f"{self.user} @ {self.application} ({status})"
```

Generate the migration: `python manage.py makemigrations sso --name token_session`

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_sso_sessions.py -v`
Expected: 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/sso/models.py apps/sso/migrations/0005_token_session.py tests/test_sso_sessions.py
git commit -m "feat(sso): TokenSession model with rotation chain + revoke metadata"
```

---

### Task 1.3: Extend SsoAuditLog.EventType

**Files:**
- Modify: `apps/sso/models.py` (existing `SsoAuditLog.EventType` enum)
- Create: `apps/sso/migrations/0006_extend_audit_event_types.py` (auto-generated `AlterField`)
- Test: `tests/test_sso_audit.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_sso_audit.py`:

```python
from apps.sso.models import SsoAuditLog


def test_audit_event_type_includes_session_revoked():
    assert SsoAuditLog.EventType.SESSION_REVOKED == "session_revoked"


def test_audit_event_type_includes_app_policy_changed():
    assert SsoAuditLog.EventType.APP_POLICY_CHANGED == "app_policy_changed"


def test_audit_event_type_includes_group_membership_changed():
    assert SsoAuditLog.EventType.GROUP_MEMBERSHIP_CHANGED == "group_membership_changed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sso_audit.py::test_audit_event_type_includes_session_revoked -v`
Expected: `AttributeError: SESSION_REVOKED`.

- [ ] **Step 3: Extend the enum**

In `apps/sso/models.py`, update `SsoAuditLog.EventType`:

```python
class EventType(models.TextChoices):
    APP_REGISTERED = "app_registered", _("App Registered")
    APP_DELETED = "app_deleted", _("App Deleted")
    GRANT_GIVEN = "grant_given", _("Grant Given")
    GRANT_REVOKED = "grant_revoked", _("Grant Revoked")
    LOGIN_SUCCESS = "login_success", _("Login Success")
    LOGIN_DENIED_NO_GRANT = "login_denied_no_grant", _("Login Denied — No Grant")
    LOGIN_DENIED_INACTIVE = "login_denied_inactive", _("Login Denied — Inactive User")
    TOKEN_REVOKED = "token_revoked", _("Token Revoked")
    # NEU:
    SESSION_REVOKED = "session_revoked", _("Session Revoked (admin)")
    APP_POLICY_CHANGED = "app_policy_changed", _("App Policy Changed")
    GROUP_MEMBERSHIP_CHANGED = "group_membership_changed", _("Group Membership Changed")
```

Generate the migration: `python manage.py makemigrations sso --name extend_audit_event_types`

The migration should be an `AlterField` on `SsoAuditLog.event_type.choices`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_sso_audit.py -v`
Expected: all PASS (new tests + existing).

- [ ] **Step 5: Commit**

```bash
git add apps/sso/models.py apps/sso/migrations/0006_extend_audit_event_types.py tests/test_sso_audit.py
git commit -m "feat(sso): three new audit event types for sessions/policy/tags"
```

---

## Phase 2 — Policy Gate + Group Synthesis

### Task 2.1: Policy-aware `user_can_access`

**Files:**
- Modify: `apps/sso/permissions.py:user_can_access`
- Test: `tests/test_sso_policy.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_sso_policy.py`:

```python
from apps.accounts.models import User
from apps.sso.models import AppGrant, ApplicationPolicy
from apps.sso.permissions import user_can_access


def _make_user(level: str, *, active: bool = True) -> User:
    u = User.objects.create_user(username=f"u-{level}-{int(active)}", password="x")
    u.membership_level = level
    u.is_active = active
    u.save(update_fields=["membership_level", "is_active"])
    User._invalidate_role_cache(u)
    return u


@pytest.mark.parametrize(
    "policy,level,is_active,has_grant,expected",
    [
        # GRANT_REQUIRED: existing behaviour, no policy row needed
        ("grant_required", "applicant", True, True, True),
        ("grant_required", "applicant", True, False, False),
        ("grant_required", "member", True, True, True),
        ("grant_required", "admin", False, True, False),
        # OPEN_TO_ALL: every active user, including applicants
        ("open_to_all", "applicant", True, False, True),
        ("open_to_all", "applicant", False, True, False),
        ("open_to_all", "member", True, False, True),
        # OPEN_TO_MEMBERS: applicant out, member+ in
        ("open_to_members", "applicant", True, False, False),
        ("open_to_members", "member", True, False, True),
        ("open_to_members", "staff", True, False, True),
        ("open_to_members", "admin", True, False, True),
        # OPEN_TO_INTERNAL: staff + admin
        ("open_to_internal", "member", True, False, False),
        ("open_to_internal", "staff", True, False, True),
        ("open_to_internal", "admin", True, False, True),
        # OPEN_TO_ADMINS: admin only
        ("open_to_admins", "staff", True, False, False),
        ("open_to_admins", "admin", True, False, True),
    ],
)
def test_user_can_access_matrix(db, app, policy, level, is_active, has_grant, expected):
    user = _make_user(level, active=is_active)
    if policy != "grant_required":
        ApplicationPolicy.objects.create(application=app, access_policy=policy)
    if has_grant:
        AppGrant.objects.create(user=user, application=app)

    assert user_can_access(user, app) is expected


def test_inactive_user_never_allowed_even_with_open_to_all(db, app):
    user = _make_user("admin", active=False)
    ApplicationPolicy.objects.create(application=app, access_policy="open_to_all")
    assert user_can_access(user, app) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sso_policy.py::test_user_can_access_matrix -v`
Expected: many cases FAIL because `user_can_access` currently only knows AppGrant.

- [ ] **Step 3: Update the gate**

Replace the body of `user_can_access` in `apps/sso/permissions.py`:

```python
def user_can_access(user, application) -> bool:
    """Return True iff user is active AND policy/grant allows access.

    Spec §3.2: rote Linie ist inactive=False; alle 5 Policies haben das
    als Grundvoraussetzung. Wenn keine ApplicationPolicy-Row existiert,
    faellt der Code auf das pre-existierende GRANT_REQUIRED-Verhalten
    zurueck (abwaertskompatibel).
    """
    if not getattr(user, "is_active", False):
        return False

    # Local imports keep the validator/permissions module free of an
    # import cycle on settings loading (see existing pattern below).
    from .models import AppGrant, ApplicationPolicy

    policy = ApplicationPolicy.AccessPolicy.GRANT_REQUIRED
    pol_obj = getattr(application, "sso_policy", None)
    if pol_obj is not None:
        policy = pol_obj.access_policy

    AP = ApplicationPolicy.AccessPolicy
    if policy == AP.OPEN_TO_ALL:
        return True
    if policy == AP.OPEN_TO_MEMBERS:
        return user.membership_level != user.MembershipLevel.APPLICANT
    if policy == AP.OPEN_TO_INTERNAL:
        return user.is_internal
    if policy == AP.OPEN_TO_ADMINS:
        return user.is_admin

    # GRANT_REQUIRED — pre-existing behaviour
    return AppGrant.objects.filter(
        user=user, application=application, revoked_at__isnull=True,
    ).exists()
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_sso_policy.py tests/test_sso_permissions.py -v`
Expected: all PASS (existing permission tests should not regress).

- [ ] **Step 5: Commit**

```bash
git add apps/sso/permissions.py tests/test_sso_policy.py
git commit -m "feat(sso): user_can_access consults ApplicationPolicy first"
```

---

### Task 2.2: Group synthesis in `oidc_claims.py`

**Files:**
- Modify: `apps/sso/oidc_claims.py`
- Test: `tests/test_sso_claims.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_sso_claims.py`:

```python
from django.contrib.auth.models import Group
from oauth2_provider.models import Application

from apps.accounts.models import User
from apps.sso.oidc_claims import _build_groups, add_claims
from apps.stations.models import Region, RegionAssignment, Station, StationAssignment


@pytest.fixture
def member(db):
    u = User.objects.create_user(username="peter", password="x", email="p@ex.org")
    u.membership_level = User.MembershipLevel.MEMBER
    u.save(update_fields=["membership_level"])
    User._invalidate_role_cache(u)
    return u


@pytest.fixture
def applicant(db):
    u = User.objects.create_user(username="anna", password="x")
    u.membership_level = User.MembershipLevel.APPLICANT
    u.save(update_fields=["membership_level"])
    User._invalidate_role_cache(u)
    return u


def test_build_groups_applicant_only_membership(db, applicant):
    assert _build_groups(applicant) == ["applicant"]


def test_build_groups_member_with_no_assignments(db, member):
    assert _build_groups(member) == ["member"]


def test_build_groups_includes_station_assignment(db, member):
    s = Station.objects.create(name="OE5XRX-1", callsign="OE5XRX")
    s.slug = "oe5xrx-1"  # if your Station doesn't auto-slug, set explicitly
    s.save()
    StationAssignment.objects.create(user=member, station=s, role="admin")
    groups = _build_groups(member)
    assert "member" in groups
    assert "station:oe5xrx-1:admin" in groups


def test_build_groups_includes_region_assignment(db, member):
    r = Region.objects.create(name="Wien", slug="wien")
    RegionAssignment.objects.create(user=member, region=r, role="manager")
    groups = _build_groups(member)
    assert "region:wien:manager" in groups


def test_build_groups_includes_tag_prefix_for_django_groups(db, member):
    g1 = Group.objects.create(name="kontakt-team")
    g2 = Group.objects.create(name="buehne-techniker")
    member.groups.add(g1, g2)
    groups = _build_groups(member)
    assert "tag:kontakt-team" in groups
    assert "tag:buehne-techniker" in groups


def test_build_groups_is_sorted_and_deduplicated(db, member):
    g = Group.objects.create(name="kontakt-team")
    member.groups.add(g)
    groups = _build_groups(member)
    assert groups == sorted(set(groups))


def test_add_claims_uses_synthesized_groups(db, member):
    g = Group.objects.create(name="kontakt-team")
    member.groups.add(g)
    claims = add_claims({}, member, request=None)
    assert "member" in claims["groups"]
    assert "tag:kontakt-team" in claims["groups"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sso_claims.py -v -k "build_groups or add_claims_uses_synth"`
Expected: `ImportError: cannot import name '_build_groups'`.

- [ ] **Step 3: Rewrite `oidc_claims.py`**

Replace the body of `apps/sso/oidc_claims.py`:

```python
"""Custom OIDC claims emitted in ID tokens and UserInfo responses.

Two DOT hooks funnel through the same function: ID-token claims via
``SsoOAuth2Validator.get_additional_claims`` and UserInfo claims via
``OAUTH2_PROVIDER["OIDC_USERINFO_HOOK"]``. Keeping both paths through
this one function means RPs see identical data regardless of which
endpoint they use.

The ``groups`` claim is synthesised from four sources (Spec §5):
- ``membership_level`` -> ``"applicant"``/``"member"``/``"staff"``/``"admin"``
- ``StationAssignment`` -> ``"station:<slug>:<role>"``
- ``RegionAssignment``  -> ``"region:<slug>:<role>"``
- Django ``auth.Group``  -> ``"tag:<name>"``
"""


def _build_groups(user) -> list[str]:
    """Synthetische groups-Liste -- siehe Spec §5.2.

    Determinismus: die Liste wird sortiert+dedupliziert zurueckgegeben,
    damit Test-Stabilitaet und RP-Diff-Sauberkeit gegeben sind.
    """
    groups: list[str] = []

    # 1. Membership-Level: alle vier Werte werden propagiert. Spec §5.1
    #    Use-Case (Applicant-Einsteiger-Trainings).
    groups.append(user.membership_level)

    # 2. StationAssignments
    for assignment in user.station_assignments.select_related("station"):
        groups.append(f"station:{assignment.station.slug}:{assignment.role}")

    # 3. RegionAssignments
    for assignment in user.region_assignments.select_related("region"):
        groups.append(f"region:{assignment.region.slug}:{assignment.role}")

    # 4. Freie Django auth.Group-Tags
    for name in user.groups.values_list("name", flat=True):
        groups.append(f"tag:{name}")

    return sorted(set(groups))


def add_claims(claims, user, request):
    """Merge OE5XRX-specific claims into the OIDC payload."""
    claims["preferred_username"] = user.username
    claims["email"] = user.email or ""
    claims["email_verified"] = bool(user.email)
    claims["name"] = user.get_full_name() or user.username
    claims["locale"] = getattr(user, "language", "en") or "en"
    claims["groups"] = _build_groups(user)
    return claims
```

Note: `Station.slug` — check that Station has a `slug` field. If it has `name` instead, swap. (Looking at the migrations history, stations have slugs via tags and regions have slugs; verify station's slug field in `apps/stations/models.py` before running tests. If Station has no `slug`, fall back to `str(assignment.station.pk)` or `assignment.station.name.lower().replace(" ", "-")` — Plan-Phase note for the engineer: confirm and stay consistent with how Region's slug is read.)

- [ ] **Step 4: Verify Station has a slug field**

Run: `grep -n "slug" apps/stations/models.py | head -20`

If Station has no `slug` field, modify Task 2.2 Step 3 to use `assignment.station.pk` for the station segment (`f"station:{assignment.station.pk}:{assignment.role}"`) and update the test accordingly.

- [ ] **Step 5: Run tests to verify pass**

Run: `pytest tests/test_sso_claims.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/sso/oidc_claims.py tests/test_sso_claims.py
git commit -m "feat(sso): synthesize groups claim from membership_level + topology + tags"
```

---

## Phase 3 — GeoIP

### Task 3.1: GeoIP wrapper module

**Files:**
- Create: `apps/sso/geoip.py`
- Create: `tests/fixtures/dbip-city-lite-test.mmdb` (download or fabricate small mmdb)
- Test: `tests/test_sso_geoip.py`

- [ ] **Step 1: Generate the test fixture**

Build a tiny mmdb test fixture by running this one-off script (don't commit the script — only the resulting fixture):

```bash
python <<'PY'
# Generates a 5KB mmdb file with two known IPs for tests.
# Uses the maxminddb-writer library; install if not present.
import subprocess, sys
try:
    import mmdb_writer
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "mmdb-writer"])
    import mmdb_writer
from netaddr import IPSet, IPNetwork
w = mmdb_writer.MMDBWriter(ip_version=4, database_type="DBIP-City-Lite-Test",
                            languages=["en"])
w.insert_network(IPSet([IPNetwork("89.207.4.0/24")]),
                  {"country": {"iso_code": "AT", "names": {"en": "Austria"}},
                   "city": {"names": {"en": "Linz"}}})
w.insert_network(IPSet([IPNetwork("89.207.5.0/24")]),
                  {"country": {"iso_code": "AT", "names": {"en": "Austria"}},
                   "city": {"names": {"en": "Wien"}}})
import os
os.makedirs("tests/fixtures", exist_ok=True)
w.to_db_file("tests/fixtures/dbip-city-lite-test.mmdb")
print("wrote tests/fixtures/dbip-city-lite-test.mmdb")
PY
```

Verify the file exists and is small: `ls -lh tests/fixtures/dbip-city-lite-test.mmdb` (expect ~5–15 KB).

- [ ] **Step 2: Write the failing test**

Create `tests/test_sso_geoip.py`:

```python
"""GeoIP wrapper: happy path with fixture, missing-DB fallback, bad-IP fallback."""

from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "dbip-city-lite-test.mmdb"


def _reset_singleton():
    """Force the geoip module to re-read settings on next call."""
    from apps.sso import geoip
    geoip._reader = None
    geoip._reader_load_failed = False


def test_lookup_known_ip_returns_country_and_city(settings, tmp_path):
    target = tmp_path / "test.mmdb"
    target.write_bytes(FIXTURE.read_bytes())
    settings.GEOIP_DB_PATH = str(target)
    _reset_singleton()
    from apps.sso.geoip import lookup_location
    country, city = lookup_location("89.207.4.5")
    assert country == "AT"
    assert city == "Linz"


def test_lookup_unknown_ip_returns_none(settings, tmp_path):
    target = tmp_path / "test.mmdb"
    target.write_bytes(FIXTURE.read_bytes())
    settings.GEOIP_DB_PATH = str(target)
    _reset_singleton()
    from apps.sso.geoip import lookup_location
    assert lookup_location("203.0.113.1") == (None, None)


def test_lookup_when_db_missing_returns_none(settings, tmp_path):
    settings.GEOIP_DB_PATH = str(tmp_path / "does-not-exist.mmdb")
    _reset_singleton()
    from apps.sso.geoip import lookup_location
    assert lookup_location("89.207.4.5") == (None, None)


def test_lookup_with_none_ip_returns_none(settings):
    _reset_singleton()
    from apps.sso.geoip import lookup_location
    assert lookup_location(None) == (None, None)
    assert lookup_location("") == (None, None)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_sso_geoip.py -v`
Expected: `ImportError: cannot import name 'lookup_location' from 'apps.sso.geoip'`.

- [ ] **Step 4: Add `geoip2` to requirements**

Append to `requirements/base.txt`:

```
geoip2>=4.8
```

Install: `pip install -r requirements/base.txt`. (If the project uses `uv`, run `uv pip install -r requirements/base.txt` instead.)

- [ ] **Step 5: Add the GEOIP_DB_PATH setting**

In `config/settings/base.py`, after the existing `OIDC_RSA_KEY_PATH` block:

```python
# db-ip.com City Lite database path. Refreshed daily by the
# update-geoip-db GitHub-Actions cron (see servers repo). If the file
# is missing, lookups silently return (None, None) — session rows get
# empty country/city, token issuance is never blocked.
GEOIP_DB_PATH = os.environ.get(
    "GEOIP_DB_PATH",
    str(BASE_DIR / "geoip_db" / "dbip-city-lite.mmdb"),
)
```

- [ ] **Step 6: Implement the wrapper**

Create `apps/sso/geoip.py`:

```python
"""Thin wrapper around geoip2.database.Reader.

Singleton reader (geoip2 is threadsafe). If the DB file is missing or
the lookup itself fails for any reason, ``lookup_location`` returns
``(None, None)`` — never raises. Token issuance must not block on
GeoIP being broken.

DB-file location is ``settings.GEOIP_DB_PATH`` (Docker volume
``/app/geoip_db`` in prod).
"""

import logging
import threading
from pathlib import Path

import geoip2.database
import geoip2.errors
from django.conf import settings

logger = logging.getLogger(__name__)

_reader = None
_reader_lock = threading.Lock()
_reader_load_failed = False


def _get_reader():
    global _reader, _reader_load_failed
    if _reader is not None:
        return _reader
    if _reader_load_failed:
        return None
    with _reader_lock:
        if _reader is not None:
            return _reader
        path = Path(settings.GEOIP_DB_PATH)
        if not path.exists():
            logger.warning("GeoIP DB not found at %s -- lookups disabled", path)
            _reader_load_failed = True
            return None
        try:
            _reader = geoip2.database.Reader(str(path))
        except Exception:
            logger.exception("GeoIP DB reader could not be initialised")
            _reader_load_failed = True
            return None
    return _reader


def lookup_location(ip):
    """Return (country_code, city_name) for the IP, or (None, None)."""
    if not ip:
        return None, None
    reader = _get_reader()
    if reader is None:
        return None, None
    try:
        resp = reader.city(ip)
    except geoip2.errors.AddressNotFoundError:
        return None, None
    except Exception:
        logger.exception("GeoIP lookup failed for %s", ip)
        return None, None
    return resp.country.iso_code, resp.city.name
```

- [ ] **Step 7: Run tests to verify pass**

Run: `pytest tests/test_sso_geoip.py -v`
Expected: 4 tests PASS.

- [ ] **Step 8: Commit**

```bash
git add apps/sso/geoip.py tests/test_sso_geoip.py tests/fixtures/dbip-city-lite-test.mmdb \
        config/settings/base.py requirements/base.txt
git commit -m "feat(sso): GeoIP wrapper module with singleton reader + fail-closed lookup"
```

---

### Task 3.2: `update_geoip_db` management command

**Files:**
- Create: `apps/sso/management/commands/update_geoip_db.py`
- Test: `tests/test_sso_geoip.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_sso_geoip.py`:

```python
from unittest.mock import MagicMock, patch

from django.core.management import call_command


def test_update_geoip_db_writes_target_when_current_month_ok(settings, tmp_path):
    target = tmp_path / "dbip.mmdb"
    settings.GEOIP_DB_PATH = str(target)
    fake_gz_bytes = _gzip_bytes(FIXTURE.read_bytes())

    class FakeResp:
        def __init__(self, payload): self._p = payload
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self, n=-1):
            if n < 0: data, self._p = self._p, b""; return data
            data, self._p = self._p[:n], self._p[n:]; return data

    with patch(
        "apps.sso.management.commands.update_geoip_db.urllib.request.urlopen",
        return_value=FakeResp(fake_gz_bytes),
    ):
        call_command("update_geoip_db")

    assert target.exists()
    assert target.stat().st_size > 0


def test_update_geoip_db_falls_back_to_previous_month_on_404(settings, tmp_path):
    import urllib.error
    target = tmp_path / "dbip.mmdb"
    settings.GEOIP_DB_PATH = str(target)
    fake_gz_bytes = _gzip_bytes(FIXTURE.read_bytes())

    class FakeResp404:
        def __enter__(self): raise urllib.error.HTTPError(
            "url", 404, "Not Found", {}, None,
        )
        def __exit__(self, *a): pass

    class FakeRespOk:
        def __init__(self, payload): self._p = payload
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self, n=-1):
            if n < 0: data, self._p = self._p, b""; return data
            data, self._p = self._p[:n], self._p[n:]; return data

    call_count = {"n": 0}

    def fake_urlopen(url, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return FakeRespOk(fake_gz_bytes)

    with patch(
        "apps.sso.management.commands.update_geoip_db.urllib.request.urlopen",
        side_effect=fake_urlopen,
    ):
        call_command("update_geoip_db")

    assert call_count["n"] == 2  # current month tried, then previous
    assert target.exists()


def test_update_geoip_db_raises_when_both_months_404(settings, tmp_path):
    import urllib.error
    settings.GEOIP_DB_PATH = str(tmp_path / "dbip.mmdb")

    def fake_urlopen(url, *args, **kwargs):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    with patch(
        "apps.sso.management.commands.update_geoip_db.urllib.request.urlopen",
        side_effect=fake_urlopen,
    ):
        with pytest.raises(SystemExit):
            call_command("update_geoip_db")


def _gzip_bytes(payload: bytes) -> bytes:
    import gzip, io
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write(payload)
    return buf.getvalue()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sso_geoip.py::test_update_geoip_db_writes_target_when_current_month_ok -v`
Expected: `CommandError: Unknown command: 'update_geoip_db'`.

- [ ] **Step 3: Implement the command**

Create `apps/sso/management/commands/update_geoip_db.py`:

```python
"""Download + atomic replace of the db-ip.com City Lite DB.

Spec §6.3: daily cron, current-month-first with previous-month fallback,
fails workflow only when BOTH months 404. Existing DB on disk stays
untouched on failure -- lookups keep returning the previous month's
data instead of erroring out.
"""

from datetime import date
from pathlib import Path
import gzip
import shutil
import tempfile
import urllib.error
import urllib.request

from django.conf import settings
from django.core.management.base import BaseCommand


DBIP_URL_TEMPLATE = "https://download.db-ip.com/free/dbip-city-lite-{year_month}.mmdb.gz"


def _previous_month(today: date) -> date:
    if today.month == 1:
        return today.replace(year=today.year - 1, month=12, day=1)
    return today.replace(month=today.month - 1, day=1)


class Command(BaseCommand):
    help = "Download db-ip.com City Lite DB; atomic replace into GEOIP_DB_PATH."

    def handle(self, *args, **opts):
        target = Path(settings.GEOIP_DB_PATH)
        target.parent.mkdir(parents=True, exist_ok=True)

        today = date.today()
        candidates = [
            today.strftime("%Y-%m"),
            _previous_month(today).strftime("%Y-%m"),
        ]

        downloaded_from = None
        for year_month in candidates:
            url = DBIP_URL_TEMPLATE.format(year_month=year_month)
            try:
                self._download(url, target)
                downloaded_from = year_month
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    self.stdout.write(self.style.WARNING(
                        f"{year_month} not yet published (404), trying previous"
                    ))
                    continue
                raise

        if downloaded_from is None:
            raise SystemExit(
                f"Both {candidates[0]} and {candidates[1]} return 404 -- "
                f"db-ip.com release schedule changed? Manual check needed."
            )

        self.stdout.write(self.style.SUCCESS(
            f"Updated {target} from db-ip.com {downloaded_from}"
        ))

        # Reset the in-process singleton so the next lookup picks up the
        # fresh DB without a worker restart. Other gunicorn workers
        # still hold their stale reader until next restart -- acceptable.
        from apps.sso import geoip
        geoip._reader = None
        geoip._reader_load_failed = False

    def _download(self, url: str, target: Path) -> None:
        with tempfile.NamedTemporaryFile(delete=False, dir=target.parent) as tmp:
            tmp_path = Path(tmp.name)
        try:
            self.stdout.write(f"Download {url} ...")
            with urllib.request.urlopen(url) as resp, gzip.GzipFile(fileobj=resp) as gz:
                with tmp_path.open("wb") as out:
                    shutil.copyfileobj(gz, out)
            tmp_path.replace(target)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_sso_geoip.py -v`
Expected: all PASS (4 wrapper + 3 command tests).

- [ ] **Step 5: Commit**

```bash
git add apps/sso/management/commands/update_geoip_db.py tests/test_sso_geoip.py
git commit -m "feat(sso): update_geoip_db command with previous-month fallback"
```

---

## Phase 4 — TokenSession Lifecycle

### Task 4.1: `SsoOAuth2Validator.save_bearer_token` override

**Files:**
- Modify: `apps/sso/permissions.py:SsoOAuth2Validator`
- Test: `tests/test_sso_sessions.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_sso_sessions.py`:

```python
from types import SimpleNamespace
from unittest.mock import patch

from oauth2_provider.models import AccessToken, Application, RefreshToken

from apps.sso.models import SsoAuditLog, TokenSession
from apps.sso.permissions import SsoOAuth2Validator


def _make_dot_tokens(user, app):
    """Create AccessToken + RefreshToken via DOT's models as if save_bearer_token
    had just run super(). The validator hook attaches metadata afterwards."""
    from django.utils import timezone
    from datetime import timedelta

    at = AccessToken.objects.create(
        user=user, application=app,
        token="atok-123",
        expires=timezone.now() + timedelta(hours=1),
        scope="openid",
    )
    rt = RefreshToken.objects.create(
        user=user, application=app, token="rtok-456",
        access_token=at,
    )
    return at, rt


def test_save_bearer_token_creates_token_session(db, user, app):
    _, rt = _make_dot_tokens(user, app)
    token = {"access_token": "atok-123", "refresh_token": "rtok-456",
             "expires_in": 3600, "token_type": "Bearer"}
    request = SimpleNamespace(
        headers={"X-Forwarded-For": "89.207.4.5", "User-Agent": "TestUA/1.0"},
        refresh_token_instance=None,
    )

    validator = SsoOAuth2Validator()
    with patch.object(SsoOAuth2Validator, "save_bearer_token", autospec=True,
                       side_effect=lambda self, t, r, *a, **kw:
                           SsoOAuth2Validator._record_token_session(self, t, r)):
        validator.save_bearer_token(token, request)

    s = TokenSession.objects.get(refresh_token=rt)
    assert s.user == user
    assert s.application == app
    assert s.ip_address == "89.207.4.5"
    assert s.user_agent == "TestUA/1.0"
    assert s.parent is None
    assert s.revoked_at is None


def test_save_bearer_token_emits_login_success_audit(db, user, app):
    _, rt = _make_dot_tokens(user, app)
    token = {"access_token": "atok-123", "refresh_token": "rtok-456"}
    request = SimpleNamespace(
        headers={"X-Real-IP": "89.207.4.5"},
        refresh_token_instance=None,
    )
    validator = SsoOAuth2Validator()
    validator._record_token_session(token, request)

    log = SsoAuditLog.objects.filter(
        event_type=SsoAuditLog.EventType.LOGIN_SUCCESS,
        target_user=user, application=app,
    ).first()
    assert log is not None
    assert log.ip_address == "89.207.4.5"


def test_save_bearer_token_refresh_rotation_chains_parent(db, user, app):
    _, parent_rt = _make_dot_tokens(user, app)
    validator = SsoOAuth2Validator()
    # First issuance:
    validator._record_token_session(
        {"refresh_token": "rtok-456"},
        SimpleNamespace(headers={"X-Real-IP": "89.207.4.5", "User-Agent": "UA1"},
                        refresh_token_instance=None),
    )
    parent_session = TokenSession.objects.get(refresh_token=parent_rt)

    # Simulate rotation: DOT creates a new RefreshToken; we feed it in.
    from django.utils import timezone
    from datetime import timedelta
    at2 = AccessToken.objects.create(
        user=user, application=app, token="atok-789",
        expires=timezone.now() + timedelta(hours=1), scope="openid",
    )
    rt2 = RefreshToken.objects.create(
        user=user, application=app, token="rtok-789", access_token=at2,
    )

    validator._record_token_session(
        {"refresh_token": "rtok-789"},
        SimpleNamespace(headers={"X-Real-IP": "89.207.4.5", "User-Agent": "UA1"},
                        refresh_token_instance=parent_rt),
    )

    child_session = TokenSession.objects.get(refresh_token=rt2)
    assert child_session.parent == parent_session

    parent_session.refresh_from_db()
    assert parent_session.revoked_at is not None
    assert parent_session.revoke_reason == TokenSession.RevokeReason.ROTATED


def test_save_bearer_token_geoip_fallback_writes_empty_fields(db, user, app):
    """When GeoIP DB is missing, country/city stay empty -- session row is
    still created, login is not blocked."""
    _, rt = _make_dot_tokens(user, app)
    request = SimpleNamespace(headers={"X-Real-IP": "203.0.113.99"},
                              refresh_token_instance=None)
    # Force-disable GeoIP for this test
    from apps.sso import geoip
    geoip._reader = None
    geoip._reader_load_failed = True

    validator = SsoOAuth2Validator()
    validator._record_token_session({"refresh_token": "rtok-456"}, request)
    s = TokenSession.objects.get(refresh_token=rt)
    assert s.country_code == ""
    assert s.city == ""
    assert s.ip_address == "203.0.113.99"

    geoip._reader_load_failed = False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sso_sessions.py::test_save_bearer_token_creates_token_session -v`
Expected: `AttributeError: SsoOAuth2Validator has no attribute '_record_token_session'`.

- [ ] **Step 3: Implement the override**

In `apps/sso/permissions.py`, append to the `SsoOAuth2Validator` class:

```python
    def save_bearer_token(self, token, request, *args, **kwargs):
        """Override DOT hook to record a TokenSession after token-issue.

        Spec §4.2. Session tracking is observability, NOT a security gate
        -- a DB error here must NEVER block token issuance. All work is
        wrapped in try/except with logger.exception.
        """
        super().save_bearer_token(token, request, *args, **kwargs)
        try:
            self._record_token_session(token, request)
        except Exception:
            logger.exception("TokenSession recording failed")

    def _record_token_session(self, token, request):
        from oauth2_provider.models import RefreshToken
        from .geoip import lookup_location
        from .models import SsoAuditLog, TokenSession

        refresh_value = token.get("refresh_token") if isinstance(token, dict) else None
        if not refresh_value:
            return  # No refresh -> no session row (e.g. client_credentials)
        rt = RefreshToken.objects.filter(token=refresh_value).first()
        if rt is None:
            return

        # Refresh-rotation detection: when DOT calls save_bearer_token in
        # the refresh path, the previous RefreshToken instance is reachable
        # via request.refresh_token_instance (oauthlib attaches it before
        # invoking the validator hook). For initial issuance the attribute
        # is None.
        parent_session = None
        old_refresh = getattr(request, "refresh_token_instance", None)
        if old_refresh is not None:
            parent_session = TokenSession.objects.filter(
                refresh_token=old_refresh,
            ).first()
            if parent_session is not None:
                from django.utils import timezone
                parent_session.last_seen_at = timezone.now()
                parent_session.revoked_at = timezone.now()
                parent_session.revoke_reason = TokenSession.RevokeReason.ROTATED
                parent_session.save(update_fields=[
                    "last_seen_at", "revoked_at", "revoke_reason",
                ])

        ip = self._extract_ip(request)
        ua = ""
        if getattr(request, "headers", None):
            ua = (request.headers.get("User-Agent") or "")[:512]
        country, city = lookup_location(ip)

        TokenSession.objects.create(
            user=rt.user,
            application=rt.application,
            refresh_token=rt,
            parent=parent_session,
            ip_address=ip,
            user_agent=ua,
            country_code=country or "",
            city=city or "",
        )

        # LOGIN_SUCCESS audit only on initial issuance, not on every
        # refresh-rotation (would be noisy and not actionable).
        if parent_session is None:
            SsoAuditLog.log(
                event_type=SsoAuditLog.EventType.LOGIN_SUCCESS,
                target_user=rt.user,
                application=rt.application,
                message=f"Token issued. UA={ua[:80]} City={city or 'unknown'}",
                ip_address=ip,
            )

    @staticmethod
    def _extract_ip(request):
        headers = getattr(request, "headers", None) or {}
        xff = headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()
        return headers.get("X-Real-IP")
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_sso_sessions.py -v`
Expected: all PASS (model tests from 1.2 + 4 new lifecycle tests).

- [ ] **Step 5: Commit**

```bash
git add apps/sso/permissions.py tests/test_sso_sessions.py
git commit -m "feat(sso): record TokenSession on every token issue + LOGIN_SUCCESS audit"
```

---

### Task 4.2: Cascade TokenSession revoke in `signals.py`

**Files:**
- Modify: `apps/sso/signals.py`
- Test: `tests/test_sso_signals.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_sso_signals.py`:

```python
import pytest
from oauth2_provider.models import AccessToken, Application, RefreshToken

from apps.accounts.models import User
from apps.sso.models import AppGrant, TokenSession


@pytest.fixture
def app(db):
    return Application.objects.create(
        name="InvenTree",
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://example.org/oidc/callback/",
    )


def _make_session(user, app):
    from django.utils import timezone
    from datetime import timedelta
    at = AccessToken.objects.create(
        user=user, application=app, token=f"at-{user.pk}",
        expires=timezone.now() + timedelta(hours=1), scope="openid",
    )
    rt = RefreshToken.objects.create(
        user=user, application=app, token=f"rt-{user.pk}", access_token=at,
    )
    return TokenSession.objects.create(
        user=user, application=app, refresh_token=rt,
    )


def test_user_deactivation_marks_sessions_revoked(db, app):
    user = User.objects.create_user(username="petra", password="x")
    s = _make_session(user, app)
    user.is_active = False
    user.save(update_fields=["is_active"])
    s.refresh_from_db()
    assert s.revoked_at is not None
    assert s.revoke_reason == TokenSession.RevokeReason.USER_DEACTIVATED


def test_grant_revoke_marks_sessions_for_that_app_only(db, app):
    user = User.objects.create_user(username="petra", password="x")
    other_app = Application.objects.create(
        name="Grafana",
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://other.example.org/oidc/callback/",
    )
    grant = AppGrant.objects.create(user=user, application=app)
    s1 = _make_session(user, app)
    s2 = _make_session(user, other_app)

    from django.utils import timezone
    grant.revoked_at = timezone.now()
    grant.save(update_fields=["revoked_at"])

    s1.refresh_from_db()
    s2.refresh_from_db()
    assert s1.revoked_at is not None
    assert s1.revoke_reason == TokenSession.RevokeReason.GRANT_REVOKED
    assert s2.revoked_at is None  # unrelated app
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sso_signals.py::test_user_deactivation_marks_sessions_revoked -v`
Expected: FAIL — sessions are not marked revoked.

- [ ] **Step 3: Add cascade helper + wire up**

In `apps/sso/signals.py`, add a helper after the imports:

```python
def _mark_sessions_revoked(user, *, application=None, reason):
    """Mark all active TokenSessions of a user revoked. Optional app
    filter for grant-revoke cascade (Spec §4.3)."""
    from django.utils import timezone

    from .models import TokenSession

    qs = TokenSession.objects.filter(user=user, revoked_at__isnull=True)
    if application is not None:
        qs = qs.filter(application=application)
    qs.update(revoked_at=timezone.now(), revoke_reason=reason)
```

Then in `_revoke_tokens_on_user_deactivation` (existing handler), after the `RefreshToken.objects.filter(...).update(...)` line, add:

```python
        from .models import TokenSession
        _mark_sessions_revoked(
            instance, reason=TokenSession.RevokeReason.USER_DEACTIVATED,
        )
```

And in `_revoke_tokens_for_user_and_app` (existing helper), after the `RefreshToken` update line, add:

```python
    from .models import TokenSession
    _mark_sessions_revoked(
        user, application=application,
        reason=TokenSession.RevokeReason.GRANT_REVOKED,
    )
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_sso_signals.py tests/test_sso_sessions.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/sso/signals.py tests/test_sso_signals.py
git commit -m "feat(sso): cascade TokenSession revoke on user-deactivate + grant-revoke"
```

---

### Task 4.3: `prune_token_sessions` management command

**Files:**
- Create: `apps/sso/management/commands/prune_token_sessions.py`
- Test: `tests/test_sso_sessions.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_sso_sessions.py`:

```python
from datetime import timedelta

from django.core.management import call_command
from django.utils import timezone


def test_prune_keeps_recent_revoked_sessions(db, user, app):
    s = TokenSession.objects.create(user=user, application=app)
    s.revoked_at = timezone.now() - timedelta(days=5)
    s.save(update_fields=["revoked_at"])
    call_command("prune_token_sessions")
    assert TokenSession.objects.filter(pk=s.pk).exists()


def test_prune_deletes_old_revoked_sessions(db, user, app):
    s = TokenSession.objects.create(user=user, application=app)
    s.revoked_at = timezone.now() - timedelta(days=40)
    s.save(update_fields=["revoked_at"])
    call_command("prune_token_sessions")
    assert not TokenSession.objects.filter(pk=s.pk).exists()


def test_prune_is_idempotent(db, user, app):
    s = TokenSession.objects.create(user=user, application=app)
    s.revoked_at = timezone.now() - timedelta(days=40)
    s.save(update_fields=["revoked_at"])
    call_command("prune_token_sessions")
    call_command("prune_token_sessions")  # no error second time
    assert not TokenSession.objects.filter(pk=s.pk).exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sso_sessions.py::test_prune_keeps_recent_revoked_sessions -v`
Expected: `CommandError: Unknown command: 'prune_token_sessions'`.

- [ ] **Step 3: Implement the command**

Create `apps/sso/management/commands/prune_token_sessions.py`:

```python
"""Delete TokenSession rows whose RefreshToken has been revoked or
expired for more than 30 days (Spec §4.5).

Idempotent: re-running is safe; rows that don't match the cutoff stay.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from apps.sso.models import TokenSession


CUTOFF_DAYS = 30


class Command(BaseCommand):
    help = "Delete TokenSession rows older than CUTOFF_DAYS days (revoked or expired)."

    def handle(self, *args, **opts):
        cutoff = timezone.now() - timedelta(days=CUTOFF_DAYS)
        qs = TokenSession.objects.filter(
            Q(refresh_token__isnull=True)
            | Q(refresh_token__revoked__lt=cutoff)
            | Q(revoked_at__lt=cutoff),
        )
        n = qs.count()
        qs.delete()
        self.stdout.write(f"Pruned {n} TokenSession row(s).")
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_sso_sessions.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/sso/management/commands/prune_token_sessions.py tests/test_sso_sessions.py
git commit -m "feat(sso): prune_token_sessions management command (30d retention)"
```

---

## Phase 5 — Admin Views

### Task 5.1: `SessionRevokeView`

**Files:**
- Modify: `apps/sso/views.py` (append)
- Modify: `apps/sso/urls.py` (append)
- Test: `tests/test_sso_views.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_sso_views.py`:

```python
import pytest
from django.urls import reverse
from oauth2_provider.models import AccessToken, Application, RefreshToken

from apps.accounts.models import User
from apps.sso.models import SsoAuditLog, TokenSession


@pytest.fixture
def admin(db):
    u = User.objects.create_user(username="admin", password="x")
    u.membership_level = User.MembershipLevel.ADMIN
    u.save(update_fields=["membership_level"])
    User._invalidate_role_cache(u)
    return u


@pytest.fixture
def session_row(db):
    from django.utils import timezone
    from datetime import timedelta
    user = User.objects.create_user(username="target", password="x")
    app = Application.objects.create(
        name="InvenTree",
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://x.example.org/cb/",
    )
    at = AccessToken.objects.create(
        user=user, application=app, token="at1",
        expires=timezone.now() + timedelta(hours=1), scope="openid",
    )
    rt = RefreshToken.objects.create(
        user=user, application=app, token="rt1", access_token=at,
    )
    return TokenSession.objects.create(user=user, application=app, refresh_token=rt)


def test_session_revoke_view_requires_admin(db, client, session_row):
    user = User.objects.create_user(username="nonadmin", password="x")
    client.force_login(user)
    resp = client.post(reverse("sso:session_revoke", kwargs={"pk": session_row.pk}))
    assert resp.status_code == 403


def test_session_revoke_view_revokes(db, client, admin, session_row):
    client.force_login(admin)
    resp = client.post(reverse("sso:session_revoke", kwargs={"pk": session_row.pk}))
    assert resp.status_code in (200, 302)

    session_row.refresh_from_db()
    assert session_row.revoked_at is not None
    assert session_row.revoked_by == admin
    assert session_row.revoke_reason == TokenSession.RevokeReason.ADMIN_REVOKE

    rt = session_row.refresh_token
    rt.refresh_from_db()
    assert rt.revoked is not None

    log = SsoAuditLog.objects.filter(
        event_type=SsoAuditLog.EventType.SESSION_REVOKED,
        actor=admin, target_user=session_row.user,
    ).first()
    assert log is not None


def test_session_revoke_view_is_idempotent(db, client, admin, session_row):
    client.force_login(admin)
    client.post(reverse("sso:session_revoke", kwargs={"pk": session_row.pk}))
    # Second call: no second audit row, no error.
    client.post(reverse("sso:session_revoke", kwargs={"pk": session_row.pk}))
    log_count = SsoAuditLog.objects.filter(
        event_type=SsoAuditLog.EventType.SESSION_REVOKED,
        target_user=session_row.user,
    ).count()
    assert log_count == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sso_views.py::test_session_revoke_view_revokes -v`
Expected: `NoReverseMatch: 'session_revoke' is not a valid view function or pattern name`.

- [ ] **Step 3: Implement the view**

Append to `apps/sso/views.py`:

```python
class SessionRevokeView(AdminOnlyMixin, View):
    """POST-only: revoke a single TokenSession + its RefreshToken.

    Idempotent: a second POST on an already-revoked session is a no-op
    (no extra audit row, no extra DOT-token mutation). Spec §4.4.
    """

    def post(self, request, pk):
        from datetime import timedelta

        from django.db import transaction
        from django.utils import timezone
        from oauth2_provider.models import AccessToken

        from .models import SsoAuditLog, TokenSession

        session = get_object_or_404(TokenSession, pk=pk)
        if session.revoked_at is None:
            with transaction.atomic():
                rt = session.refresh_token
                if rt is not None and rt.revoked is None:
                    rt.revoked = timezone.now()
                    rt.save(update_fields=["revoked"])
                    AccessToken.objects.filter(
                        source_refresh_token=rt,
                    ).update(expires=timezone.now() - timedelta(seconds=1))

                session.revoked_at = timezone.now()
                session.revoked_by = request.user
                session.revoke_reason = TokenSession.RevokeReason.ADMIN_REVOKE
                session.save(update_fields=[
                    "revoked_at", "revoked_by", "revoke_reason",
                ])

            SsoAuditLog.log(
                event_type=SsoAuditLog.EventType.SESSION_REVOKED,
                actor=request.user,
                target_user=session.user,
                application=session.application,
                message=(
                    f"Session {session.pk} revoked. "
                    f"Issued {session.issued_at.isoformat()} "
                    f"from {session.ip_address} ({session.city or 'unknown'})"
                ),
                ip_address=_client_ip(request),
            )

        # HTMX vs. standard browser response.
        if getattr(request, "htmx", False):
            return render(request, "sso/_sessions_card.html", {
                "target_user": session.user,
                "sessions": _active_sessions_for(session.user),
            })
        return HttpResponseRedirect(
            request.META.get("HTTP_REFERER", reverse("sso:dashboard")),
        )


def _active_sessions_for(user):
    """Active TokenSessions for a user, newest first. Used by template + HTMX
    swap response from SessionRevokeView."""
    from .models import TokenSession
    return TokenSession.objects.filter(
        user=user, revoked_at__isnull=True,
    ).select_related("application").order_by("-last_seen_at")
```

In `apps/sso/urls.py`, add a URL:

```python
    path(
        "sessions/<int:pk>/revoke/",
        views.SessionRevokeView.as_view(),
        name="session_revoke",
    ),
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_sso_views.py -v`
Expected: 3 new tests PASS, existing view tests still PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/sso/views.py apps/sso/urls.py tests/test_sso_views.py
git commit -m "feat(sso): SessionRevokeView with admin-only access + idempotent revoke"
```

---

### Task 5.2: `ApplicationPolicyUpdateView`

**Files:**
- Modify: `apps/sso/views.py`
- Modify: `apps/sso/urls.py`
- Test: `tests/test_sso_views.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_sso_views.py`:

```python
from apps.sso.models import ApplicationPolicy


def test_app_policy_update_creates_row_if_missing(db, client, admin, session_row):
    app = session_row.application
    client.force_login(admin)
    resp = client.post(
        reverse("sso:app_policy_update", kwargs={"pk": app.pk}),
        data={"access_policy": "open_to_members"},
    )
    assert resp.status_code in (200, 302)
    pol = ApplicationPolicy.objects.get(application=app)
    assert pol.access_policy == "open_to_members"
    assert pol.modified_by == admin


def test_app_policy_update_emits_audit_with_old_and_new(db, client, admin, session_row):
    app = session_row.application
    ApplicationPolicy.objects.create(application=app, access_policy="grant_required")
    client.force_login(admin)
    client.post(
        reverse("sso:app_policy_update", kwargs={"pk": app.pk}),
        data={"access_policy": "open_to_all"},
    )
    log = SsoAuditLog.objects.filter(
        event_type=SsoAuditLog.EventType.APP_POLICY_CHANGED,
        application=app,
    ).first()
    assert log is not None
    assert "grant_required" in log.message
    assert "open_to_all" in log.message


def test_app_policy_update_rejects_unknown_policy(db, client, admin, session_row):
    app = session_row.application
    client.force_login(admin)
    resp = client.post(
        reverse("sso:app_policy_update", kwargs={"pk": app.pk}),
        data={"access_policy": "not-a-real-policy"},
    )
    assert resp.status_code == 400


def test_app_policy_update_requires_admin(db, client, session_row):
    app = session_row.application
    user = User.objects.create_user(username="member-only", password="x")
    client.force_login(user)
    resp = client.post(
        reverse("sso:app_policy_update", kwargs={"pk": app.pk}),
        data={"access_policy": "open_to_all"},
    )
    assert resp.status_code == 403
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sso_views.py::test_app_policy_update_creates_row_if_missing -v`
Expected: `NoReverseMatch: 'app_policy_update'`.

- [ ] **Step 3: Implement the view**

Append to `apps/sso/views.py`:

```python
class ApplicationPolicyUpdateView(AdminOnlyMixin, View):
    """POST-only: set or update the ApplicationPolicy for an Application.

    Auto-creates the policy row on first set; existing-row update emits
    APP_POLICY_CHANGED audit. Spec §3.4.
    """

    def post(self, request, pk):
        from .models import ApplicationPolicy, SsoAuditLog, TokenSession

        application = get_object_or_404(Application, pk=pk)
        new_policy = request.POST.get("access_policy", "")
        valid_choices = {v for v, _ in ApplicationPolicy.AccessPolicy.choices}
        if new_policy not in valid_choices:
            return HttpResponseBadRequest("invalid access_policy value")

        pol, created = ApplicationPolicy.objects.get_or_create(
            application=application,
            defaults={"access_policy": new_policy, "modified_by": request.user},
        )
        old_policy = pol.access_policy if not created else "grant_required"

        if not created and pol.access_policy != new_policy:
            pol.access_policy = new_policy
            pol.modified_by = request.user
            pol.save(update_fields=["access_policy", "modified_by", "updated_at"])

        active_session_count = TokenSession.objects.filter(
            application=application, revoked_at__isnull=True,
        ).count()
        SsoAuditLog.log(
            event_type=SsoAuditLog.EventType.APP_POLICY_CHANGED,
            actor=request.user,
            application=application,
            message=(
                f"Policy {old_policy} -> {new_policy}. "
                f"{active_session_count} active session(s) at the time of change."
            ),
            ip_address=_client_ip(request),
        )

        return HttpResponseRedirect(
            reverse("sso:application_detail", kwargs={"pk": application.pk}),
        )
```

In `apps/sso/urls.py`:

```python
    path(
        "applications/<int:pk>/policy/",
        views.ApplicationPolicyUpdateView.as_view(),
        name="app_policy_update",
    ),
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_sso_views.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/sso/views.py apps/sso/urls.py tests/test_sso_views.py
git commit -m "feat(sso): ApplicationPolicyUpdateView with audit + active-session count"
```

---

### Task 5.3: Tag management views

**Files:**
- Modify: `apps/sso/views.py`
- Modify: `apps/sso/urls.py`
- Test: `tests/test_sso_tags.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_sso_tags.py`:

```python
"""Tests for the custom tag-management views (Django auth.Group repurposed)."""

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts.models import User
from apps.sso.models import SsoAuditLog


@pytest.fixture
def admin(db):
    u = User.objects.create_user(username="admin", password="x")
    u.membership_level = User.MembershipLevel.ADMIN
    u.save(update_fields=["membership_level"])
    User._invalidate_role_cache(u)
    return u


@pytest.fixture
def target_user(db):
    return User.objects.create_user(username="target", password="x")


def test_tag_list_requires_admin(db, client):
    user = User.objects.create_user(username="nonadmin", password="x")
    client.force_login(user)
    resp = client.get(reverse("sso:tag_list"))
    assert resp.status_code == 403


def test_tag_list_shows_existing_groups(db, client, admin):
    Group.objects.create(name="kontakt-team")
    Group.objects.create(name="funkdienst")
    client.force_login(admin)
    resp = client.get(reverse("sso:tag_list"))
    assert resp.status_code == 200
    assert b"kontakt-team" in resp.content
    assert b"funkdienst" in resp.content


def test_tag_create_rejects_invalid_slug(db, client, admin):
    client.force_login(admin)
    resp = client.post(reverse("sso:tag_create"), data={"name": "Kontakt Team"})
    assert resp.status_code == 400  # space not allowed in tag name


def test_tag_create_accepts_valid_slug_and_audits(db, client, admin):
    client.force_login(admin)
    resp = client.post(reverse("sso:tag_create"), data={"name": "kontakt-team"})
    assert resp.status_code in (200, 302)
    assert Group.objects.filter(name="kontakt-team").exists()


def test_tag_membership_toggle_adds_then_removes(db, client, admin, target_user):
    g = Group.objects.create(name="kontakt-team")
    client.force_login(admin)
    url = reverse("sso:tag_toggle", kwargs={"user_id": target_user.pk, "group_id": g.pk})

    client.post(url)
    assert target_user.groups.filter(pk=g.pk).exists()
    assert SsoAuditLog.objects.filter(
        event_type=SsoAuditLog.EventType.GROUP_MEMBERSHIP_CHANGED,
        target_user=target_user,
        message__icontains="added: kontakt-team",
    ).exists()

    client.post(url)
    assert not target_user.groups.filter(pk=g.pk).exists()
    assert SsoAuditLog.objects.filter(
        target_user=target_user,
        message__icontains="removed: kontakt-team",
    ).exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sso_tags.py -v`
Expected: `NoReverseMatch: 'tag_list'`.

- [ ] **Step 3: Implement the views**

Append to `apps/sso/views.py`:

```python
import re

_TAG_NAME_RE = re.compile(r"^[a-z0-9-]+$")


class TagListView(AdminOnlyMixin, ListView):
    template_name = "sso/tag_list.html"
    context_object_name = "tags"

    def get_queryset(self):
        from django.contrib.auth.models import Group
        return Group.objects.annotate(member_count=Count("user")).order_by("name")


class TagCreateView(AdminOnlyMixin, View):
    """POST-only: create a Django auth.Group with a slug-safe name.

    Spec §12 open question default: enforce slug format so the synthesised
    'tag:<name>' string in OIDC tokens stays predictable (no spaces, no
    case sensitivity surprises).
    """

    def post(self, request):
        from django.contrib.auth.models import Group

        name = (request.POST.get("name") or "").strip()
        if not _TAG_NAME_RE.match(name):
            return HttpResponseBadRequest(
                "Tag name must match [a-z0-9-]+",
            )
        Group.objects.get_or_create(name=name)
        return HttpResponseRedirect(reverse("sso:tag_list"))


class TagDetailView(AdminOnlyMixin, DetailView):
    template_name = "sso/tag_detail.html"
    context_object_name = "tag"

    def get_queryset(self):
        from django.contrib.auth.models import Group
        return Group.objects.all()

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["members"] = self.object.user_set.order_by("username")
        ctx["non_members"] = User.objects.exclude(
            pk__in=self.object.user_set.values_list("pk"),
        ).order_by("username")
        return ctx


class TagMembershipToggleView(AdminOnlyMixin, View):
    """POST-only: toggle a user's membership in a tag (Django auth.Group)."""

    def post(self, request, user_id, group_id):
        from django.contrib.auth.models import Group

        from .models import SsoAuditLog

        target = get_object_or_404(User, pk=user_id)
        group = get_object_or_404(Group, pk=group_id)

        if target.groups.filter(pk=group.pk).exists():
            target.groups.remove(group)
            verb = "removed"
        else:
            target.groups.add(group)
            verb = "added"

        SsoAuditLog.log(
            event_type=SsoAuditLog.EventType.GROUP_MEMBERSHIP_CHANGED,
            actor=request.user,
            target_user=target,
            message=f"{verb}: {group.name}",
            ip_address=_client_ip(request),
        )

        return HttpResponseRedirect(
            reverse("sso:tag_detail", kwargs={"pk": group.pk}),
        )
```

In `apps/sso/urls.py`:

```python
    path("tags/", views.TagListView.as_view(), name="tag_list"),
    path("tags/create/", views.TagCreateView.as_view(), name="tag_create"),
    path("tags/<int:pk>/", views.TagDetailView.as_view(), name="tag_detail"),
    path(
        "tags/toggle/<int:user_id>/<int:group_id>/",
        views.TagMembershipToggleView.as_view(),
        name="tag_toggle",
    ),
```

- [ ] **Step 4: Add minimal templates so the GET-based tests work**

Create `apps/sso/templates/sso/tag_list.html`:

```html
{% extends "base.html" %}
{% block content %}
<h1>Tags</h1>
<form method="post" action="{% url 'sso:tag_create' %}">
  {% csrf_token %}
  <input type="text" name="name" placeholder="kontakt-team" required>
  <button type="submit">Create tag</button>
</form>
<table class="table">
  <thead><tr><th>Name</th><th>Members</th></tr></thead>
  <tbody>
    {% for tag in tags %}
      <tr>
        <td><a href="{% url 'sso:tag_detail' pk=tag.pk %}">{{ tag.name }}</a></td>
        <td>{{ tag.member_count }}</td>
      </tr>
    {% endfor %}
  </tbody>
</table>
{% endblock %}
```

Create `apps/sso/templates/sso/tag_detail.html`:

```html
{% extends "base.html" %}
{% block content %}
<h1>Tag: {{ tag.name }}</h1>
<h2>Members</h2>
<ul>
  {% for member in members %}
    <li>
      {{ member.username }}
      <form method="post" action="{% url 'sso:tag_toggle' user_id=member.pk group_id=tag.pk %}" style="display:inline">
        {% csrf_token %}
        <button type="submit">Remove</button>
      </form>
    </li>
  {% endfor %}
</ul>
<h2>Non-members</h2>
<ul>
  {% for u in non_members %}
    <li>
      {{ u.username }}
      <form method="post" action="{% url 'sso:tag_toggle' user_id=u.pk group_id=tag.pk %}" style="display:inline">
        {% csrf_token %}
        <button type="submit">Add</button>
      </form>
    </li>
  {% endfor %}
</ul>
{% endblock %}
```

- [ ] **Step 5: Run tests to verify pass**

Run: `pytest tests/test_sso_tags.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/sso/views.py apps/sso/urls.py \
        apps/sso/templates/sso/tag_list.html apps/sso/templates/sso/tag_detail.html \
        tests/test_sso_tags.py
git commit -m "feat(sso): tag-management views (list/create/detail/toggle) + audit"
```

---

### Task 5.4: Dashboard KPI tile + policy column

**Files:**
- Modify: `apps/sso/views.py:SsoDashboardView`
- Modify: `apps/sso/templates/sso/dashboard.html`
- Test: `tests/test_sso_views.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_sso_views.py`:

```python
def test_dashboard_shows_active_sessions_count(db, client, admin, session_row):
    client.force_login(admin)
    resp = client.get(reverse("sso:dashboard"))
    assert resp.status_code == 200
    assert b"Active sessions" in resp.content
    # The fixture creates exactly one session row.
    assert b">1<" in resp.content or b">1 " in resp.content


def test_dashboard_shows_policy_badge(db, client, admin, session_row):
    app = session_row.application
    ApplicationPolicy.objects.create(application=app, access_policy="open_to_members")
    client.force_login(admin)
    resp = client.get(reverse("sso:dashboard"))
    assert resp.status_code == 200
    assert b"Open to members" in resp.content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sso_views.py::test_dashboard_shows_active_sessions_count -v`
Expected: FAIL — "Active sessions" not in body.

- [ ] **Step 3: Extend the view + template**

In `apps/sso/views.py`, update `SsoDashboardView.get_context_data`:

```python
    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        from .models import TokenSession

        ctx["active_grants_total"] = AppGrant.objects.filter(
            revoked_at__isnull=True,
        ).count()
        ctx["active_sessions_total"] = TokenSession.objects.filter(
            revoked_at__isnull=True,
        ).count()
        ctx["active_sessions_apps"] = TokenSession.objects.filter(
            revoked_at__isnull=True,
        ).values("application").distinct().count()
        return ctx
```

In `apps/sso/views.py`, update `SsoDashboardView.get_queryset` to annotate active sessions + policy:

```python
    def get_queryset(self):
        from .models import TokenSession
        return (
            Application.objects.annotate(
                active_grant_count=Count(
                    "grants",
                    filter=Q(grants__revoked_at__isnull=True),
                ),
                active_session_count=Count(
                    "token_sessions",
                    filter=Q(token_sessions__revoked_at__isnull=True),
                ),
            )
            .order_by("name")
        )
```

In `apps/sso/templates/sso/dashboard.html`, add (inside the existing card grid or KPI section — exact integration is template-style dependent, keep consistency with existing tiles):

```html
<div class="card">
  <h5>Active sessions</h5>
  <p class="kpi-value">{{ active_sessions_total }}</p>
  <p class="kpi-note">across {{ active_sessions_apps }} app(s)</p>
</div>
```

And in the app-table:

```html
<th>Policy</th>
...
<td>
  {% with pol=app.sso_policy.access_policy|default:"grant_required" %}
    {% if pol == "grant_required" %}
      <span class="badge bg-secondary">Grant required</span>
    {% elif pol == "open_to_all" %}
      <span class="badge bg-success">Open to all</span>
    {% elif pol == "open_to_members" %}
      <span class="badge bg-success">Open to members</span>
    {% elif pol == "open_to_internal" %}
      <span class="badge bg-success">Open to internal</span>
    {% elif pol == "open_to_admins" %}
      <span class="badge bg-success">Open to admins</span>
    {% endif %}
  {% endwith %}
</td>
```

Add an "Active sessions" column too.

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_sso_views.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/sso/views.py apps/sso/templates/sso/dashboard.html tests/test_sso_views.py
git commit -m "feat(sso): dashboard KPI tile for active sessions + per-app policy badge"
```

---

## Phase 6 — Templates and User-Detail Integration

### Task 6.1: `_sessions_card.html` partial

**Files:**
- Create: `apps/sso/templates/sso/_sessions_card.html`
- No new test (rendered by existing view tests; visual smoke covered there)

- [ ] **Step 1: Create the partial**

Create `apps/sso/templates/sso/_sessions_card.html`:

```html
{% load i18n %}
<div id="sessions-card" class="card">
  <div class="card-header">
    <h5>{% trans "Active sessions" %} — {{ target_user.username }}</h5>
  </div>
  <div class="card-body">
    {% if sessions %}
      <table class="table table-sm">
        <thead>
          <tr>
            <th>{% trans "App" %}</th>
            <th>{% trans "Issued" %}</th>
            <th>{% trans "Last seen" %}</th>
            <th>{% trans "Location" %}</th>
            <th>{% trans "Device" %}</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {% for s in sessions %}
            <tr>
              <td>{{ s.application.name }}</td>
              <td>{{ s.issued_at|date:"d M H:i" }}</td>
              <td>{{ s.last_seen_at|date:"d M H:i" }}</td>
              <td>
                {% if s.country_code %}{{ s.country_code }} {{ s.city }}{% else %}Unknown{% endif %}
              </td>
              <td>{{ s.user_agent|truncatechars:30 }}</td>
              <td>
                <form method="post"
                      action="{% url 'sso:session_revoke' pk=s.pk %}"
                      hx-post="{% url 'sso:session_revoke' pk=s.pk %}"
                      hx-target="#sessions-card" hx-swap="outerHTML">
                  {% csrf_token %}
                  <button class="btn btn-sm btn-outline-danger" type="submit">
                    {% trans "Revoke" %}
                  </button>
                </form>
              </td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    {% else %}
      <p class="text-muted">{% trans "No active sessions." %}</p>
    {% endif %}
  </div>
</div>
```

- [ ] **Step 2: Commit**

```bash
git add apps/sso/templates/sso/_sessions_card.html
git commit -m "feat(sso): _sessions_card.html partial for user-detail + HTMX swap"
```

---

### Task 6.2: User-detail card integration

**Files:**
- Modify: `apps/accounts/templates/accounts/user_detail.html` (or wherever the existing AppGrant card is rendered — check)

- [ ] **Step 1: Locate the user-detail template**

Run: `grep -rln "AppGrant\|app_grants\|_app_grants_card" apps/accounts/templates/`

Pick the file (likely `apps/accounts/templates/accounts/user_detail.html`). If multiple templates render user detail, modify all of them — but typically there is one.

- [ ] **Step 2: Embed the sessions card**

In the template found above, add inside the user's main content area, near the existing AppGrant card:

```django
{% include "sso/_sessions_card.html" with target_user=target_user sessions=user_sessions %}
```

In the view that renders this template (find via `grep -rn "user_detail.html" apps/`), inject `user_sessions` into the context:

```python
def get_context_data(self, **kwargs):
    ctx = super().get_context_data(**kwargs)
    from apps.sso.views import _active_sessions_for
    ctx["user_sessions"] = _active_sessions_for(self.object)
    return ctx
```

- [ ] **Step 3: Smoke test via existing test**

Run: `pytest tests/test_views_user_list_membership_display.py -v` (or the closest existing test) plus a quick manual `python manage.py runserver` if you want to eyeball. No new test file needed — the existing user-detail rendering test should pick up the new section.

- [ ] **Step 4: Commit**

```bash
git add apps/accounts/templates/accounts/user_detail.html apps/accounts/views.py
git commit -m "feat(accounts): embed _sessions_card on user detail page"
```

---

### Task 6.3: User-detail Tags card

**Files:**
- Create: `apps/sso/templates/sso/_tags_card.html`
- Modify: user-detail template + view (same file as 6.2)

- [ ] **Step 1: Create the partial**

Create `apps/sso/templates/sso/_tags_card.html`:

```html
{% load i18n %}
<div id="tags-card" class="card">
  <div class="card-header"><h5>{% trans "Tags" %}</h5></div>
  <div class="card-body">
    {% for entry in tag_entries %}
      <form method="post" action="{% url 'sso:tag_toggle' user_id=target_user.pk group_id=entry.group.pk %}"
            hx-post="{% url 'sso:tag_toggle' user_id=target_user.pk group_id=entry.group.pk %}"
            hx-target="#tags-card" hx-swap="outerHTML"
            style="display:inline-block; margin-right:0.5em">
        {% csrf_token %}
        <button type="submit" class="btn btn-sm {% if entry.is_member %}btn-success{% else %}btn-outline-secondary{% endif %}">
          {% if entry.is_member %}✓{% else %}+{% endif %} {{ entry.group.name }}
        </button>
      </form>
    {% endfor %}
  </div>
</div>
```

- [ ] **Step 2: Inject context in user-detail view**

In the user-detail view, extend `get_context_data`:

```python
def get_context_data(self, **kwargs):
    ctx = super().get_context_data(**kwargs)
    from django.contrib.auth.models import Group
    from apps.sso.views import _active_sessions_for

    ctx["user_sessions"] = _active_sessions_for(self.object)
    member_ids = set(self.object.groups.values_list("pk", flat=True))
    ctx["tag_entries"] = [
        {"group": g, "is_member": g.pk in member_ids}
        for g in Group.objects.order_by("name")
    ]
    return ctx
```

- [ ] **Step 3: Embed the partial**

Add to the user-detail template:

```django
{% include "sso/_tags_card.html" with target_user=target_user tag_entries=tag_entries %}
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_sso_tags.py tests/test_views_user_list_membership_display.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/sso/templates/sso/_tags_card.html apps/accounts/templates/accounts/user_detail.html \
        apps/accounts/views.py
git commit -m "feat(sso): _tags_card on user detail with HTMX toggle"
```

---

### Task 6.4: App-detail policy selector + group propagation section

**Files:**
- Modify: `apps/sso/views.py:ApplicationDetailView.get_context_data`
- Modify: `apps/sso/templates/sso/application_detail.html`

- [ ] **Step 1: Extend the view context**

In `ApplicationDetailView.get_context_data`, append:

```python
        from django.contrib.auth.models import Group
        from .models import ApplicationPolicy, TokenSession

        ctx["policy"] = getattr(self.object, "sso_policy", None)
        ctx["policy_choices"] = ApplicationPolicy.AccessPolicy.choices
        ctx["current_policy_value"] = (
            ctx["policy"].access_policy if ctx["policy"] else "grant_required"
        )

        # Preview list of all groups currently propagated for any user
        # in the system. Used for the "Group propagation" section.
        from apps.stations.models import RegionAssignment, StationAssignment
        membership_levels = ["applicant", "member", "staff", "admin"]
        station_groups = [
            f"station:{a.station.slug}:{a.role}"
            for a in StationAssignment.objects.select_related("station").distinct()
        ]
        region_groups = [
            f"region:{a.region.slug}:{a.role}"
            for a in RegionAssignment.objects.select_related("region").distinct()
        ]
        tag_groups = [f"tag:{n}" for n in Group.objects.values_list("name", flat=True)]
        ctx["propagated_group_strings"] = sorted(set(
            membership_levels + station_groups + region_groups + tag_groups
        ))

        # Recent sessions on this app
        ctx["recent_sessions"] = TokenSession.objects.filter(
            application=self.object,
        ).select_related("user").order_by("-issued_at")[:50]
```

- [ ] **Step 2: Extend the template**

Add to `apps/sso/templates/sso/application_detail.html`:

```html
<section class="card mt-3">
  <div class="card-header"><h5>Access policy</h5></div>
  <div class="card-body">
    <form method="post" action="{% url 'sso:app_policy_update' pk=application.pk %}">
      {% csrf_token %}
      <select name="access_policy">
        {% for value, label in policy_choices %}
          <option value="{{ value }}" {% if value == current_policy_value %}selected{% endif %}>
            {{ label }}
          </option>
        {% endfor %}
      </select>
      <button type="submit" class="btn btn-primary">Save</button>
    </form>
  </div>
</section>

<section class="card mt-3">
  <div class="card-header"><h5>Group propagation</h5></div>
  <div class="card-body">
    <p>The OIDC token sent to this app contains a <code>groups</code> claim
       built from the strings below. Configure your RP-side mapping accordingly.</p>
    <pre>{% for s in propagated_group_strings %}{{ s }}
{% endfor %}</pre>
  </div>
</section>

<section class="card mt-3">
  <div class="card-header"><h5>Recent sessions (last 50)</h5></div>
  <div class="card-body">
    <table class="table table-sm">
      <thead><tr><th>User</th><th>Issued</th><th>Status</th><th>Location</th></tr></thead>
      <tbody>
        {% for s in recent_sessions %}
          <tr>
            <td>{{ s.user.username }}</td>
            <td>{{ s.issued_at|date:"d M H:i" }}</td>
            <td>{% if s.revoked_at %}revoked{% else %}active{% endif %}</td>
            <td>{% if s.country_code %}{{ s.country_code }} {{ s.city }}{% else %}—{% endif %}</td>
          </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</section>
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_sso_views.py -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add apps/sso/views.py apps/sso/templates/sso/application_detail.html
git commit -m "feat(sso): app detail page shows policy selector + group preview + recent sessions"
```

---

## Phase 7 — Integration + Docs

### Task 7.1: Extend E2E flow test

**Files:**
- Modify: `tests/test_sso_flow.py`

- [ ] **Step 1: Add a flow assertion at the end of the existing happy-path test**

Find the existing happy-path test in `tests/test_sso_flow.py` (the one that walks the full authorization-code + token exchange). After the token is issued, add:

```python
    from apps.sso.models import TokenSession
    session = TokenSession.objects.filter(user=user, application=app).first()
    assert session is not None
    assert session.parent is None
    assert session.user_agent != ""  # the test client sends a UA
    # country_code/city stay empty -- test environment has no GeoIP DB
```

If the existing test mocks the request headers (or uses Django's test client which sets `HTTP_USER_AGENT`), `session.user_agent` will be set. If not, the assertion should be `>= 0` length or removed.

- [ ] **Step 2: Add a refresh-rotation test**

First, read the existing happy-path test in `tests/test_sso_flow.py` to understand the test setup and its fixture/helper functions. Then add a new test function modelled on that pattern.

Skeleton — adapt the fixture/helper names to whatever the existing happy-path test uses (typically `app`, `user`, `client`, plus a helper that mints an auth code):

```python
def test_refresh_rotation_chains_token_sessions(db, client, app, user):
    """After exchanging the auth code, then exchanging the refresh token,
    expect two TokenSession rows: parent revoked with reason=ROTATED,
    child active with parent FK set."""
    from apps.sso.models import TokenSession

    # 1. Drive the happy-path auth-code exchange via the same helper the
    #    existing happy-path test uses. The helper returns the token-
    #    response dict; capture refresh_token from it.
    token_response = _exchange_auth_code_for_token(client, app, user)  # existing helper
    refresh_value = token_response["refresh_token"]

    parent = TokenSession.objects.get(user=user, application=app, parent__isnull=True)
    assert parent.refresh_token.token == refresh_value

    # 2. Exchange the refresh token for a new access+refresh pair.
    resp = client.post("/sso/token/", data={
        "grant_type": "refresh_token",
        "refresh_token": refresh_value,
        "client_id": app.client_id,
        # client_secret omitted for public clients; if app is confidential,
        # set "client_secret": app.client_secret.
    })
    assert resp.status_code == 200
    new_refresh = resp.json()["refresh_token"]
    assert new_refresh != refresh_value  # rotation must change the value

    # 3. Verify chain: parent now revoked=ROTATED, child has parent FK.
    parent.refresh_from_db()
    assert parent.revoked_at is not None
    assert parent.revoke_reason == TokenSession.RevokeReason.ROTATED

    child = TokenSession.objects.get(parent=parent)
    assert child.refresh_token.token == new_refresh
    assert child.revoked_at is None
```

If the existing happy-path test in `test_sso_flow.py` does NOT have a reusable helper for the code exchange (it's all inline), copy that inline code into the new test instead — DRY is secondary to having the new test be self-contained.

- [ ] **Step 3: Run E2E tests**

Run: `pytest tests/test_sso_flow.py -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/test_sso_flow.py
git commit -m "test(sso): E2E flow asserts TokenSession created + refresh-rotation chained"
```

---

### Task 7.2: README update

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add a "GeoIP DB" section**

Insert into `README.md` near the "Initial setup" / "Bootstrap" section (find via `grep -n "setup_oidc_keys\|oidc_keys" README.md` for the right anchor):

```markdown
### GeoIP database

The `station-manager` resolves session IPs to country + city for the
admin-facing "Active sessions" view. The lookup uses the free
[db-ip.com City Lite](https://db-ip.com/db/lite.php) database (no API key
required). On a fresh deployment:

```bash
docker compose run --rm web python manage.py update_geoip_db
```

In production, this command runs daily via the
[`update-geoip-db` workflow](https://github.com/OE5XRX/servers/actions/workflows/update-geoip-db.yml)
in the `servers` repo. If the DB file is missing or a lookup fails, the
session row keeps an empty country/city — token issuance is never
blocked.
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: README section for GeoIP DB initial setup + daily cron"
```

---

## Phase 8 — `servers` repo (separate PR)

> **Note:** This phase is a separate PR in the [`servers`](https://github.com/OE5XRX/servers) repo, not in `station-manager`. Create a feature branch in `servers` (`feat/sso-sessions-geoip-deployment`) and follow the tasks below. The PR can land in either order relative to the station-manager PR; preferred order is servers-first (see Spec §14.5).

### Task 8.1: Composite action `open-failure-issue`

**Files (in `servers` repo):**
- Create: `.github/actions/open-failure-issue/action.yml`

- [ ] **Step 1: Create the composite action**

```yaml
name: Open or update failure issue
description: >
  Creates a GitHub issue when the calling workflow fails, or comments on
  an existing open issue carrying the same label.
inputs:
  label:
    description: GitHub label that identifies the existing open issue.
    required: true
  title-prefix:
    description: Issue title prefix (followed by "@ <ISO timestamp>").
    required: true
  log-file:
    description: Optional path to a log file whose last 50 lines are
                 included in the issue body. Empty -> body has only the
                 workflow run link.
    required: false
    default: ""
runs:
  using: composite
  steps:
    - uses: actions/github-script@v9
      with:
        script: |
          const fs = require('fs');
          const label = '${{ inputs.label }}';
          const titlePrefix = '${{ inputs.title-prefix }}';
          const logFile = '${{ inputs.log-file }}';

          let logTail = '';
          if (logFile) {
            try {
              const log = fs.readFileSync(logFile, 'utf-8');
              logTail = log.split('\n').slice(-50).join('\n');
            } catch (e) {
              core.warning(`could not read log ${logFile}: ${e.message}`);
              logTail = `(log collection failed: ${e.message})`;
            }
          }

          const runUrl = `${context.serverUrl}/${context.repo.owner}/${context.repo.repo}/actions/runs/${context.runId}`;
          const body = logTail
            ? `Workflow run: ${runUrl}\n\nLast 50 log lines:\n\n\`\`\`\n${logTail}\n\`\`\``
            : `Workflow run: ${runUrl}`;

          try {
            const existing = await github.rest.issues.listForRepo({
              owner: context.repo.owner,
              repo: context.repo.repo,
              state: 'open',
              labels: label,
              per_page: 5,
            });
            if (existing.data.length > 0) {
              await github.rest.issues.createComment({
                owner: context.repo.owner,
                repo: context.repo.repo,
                issue_number: existing.data[0].number,
                body: `Another failure at ${new Date().toISOString()}\n\n${body}`,
              });
            } else {
              await github.rest.issues.create({
                owner: context.repo.owner,
                repo: context.repo.repo,
                title: `${titlePrefix} @ ${new Date().toISOString()}`,
                labels: [label, 'auto-generated'],
                body,
              });
            }
          } catch (e) {
            core.error(`failed to file ${label} issue: ${e.message}. Check the workflow logs directly.`);
          }
```

- [ ] **Step 2: Commit**

```bash
git add .github/actions/open-failure-issue/action.yml
git commit -m "feat(ci): composite action for failure-issue creation"
```

---

### Task 8.2: docker-compose.yml — `geoip_db` bind-mount + env

**Files:**
- Modify: `services/station_manager/docker-compose.yml`

- [ ] **Step 1: Extend `prepare-volumes`**

Change the `prepare-volumes` command from:

```yaml
      - 'install -d -m 0700 -o 1000 -g 1000 /target/oidc_keys && echo "oidc_keys ready"'
```

to:

```yaml
      - >
        install -d -m 0700 -o 1000 -g 1000 /target/oidc_keys &&
        install -d -m 0750 -o 1000 -g 1000 /target/geoip_db &&
        echo "volumes ready"
```

- [ ] **Step 2: Extend the `web` service**

In the `web` service's `environment` block (`*station-manager-env` anchor — or directly under `environment:`), add:

```yaml
      GEOIP_DB_PATH: /app/geoip_db/dbip-city-lite.mmdb
```

In the `web` service's `volumes`, add:

```yaml
      - /opt/oe5xrx-data/station_manager/geoip_db:/app/geoip_db
```

(Below the existing `oidc_keys` mount.)

- [ ] **Step 3: Commit**

```bash
git add services/station_manager/docker-compose.yml
git commit -m "feat(station_manager): geoip_db bind-mount + GEOIP_DB_PATH env"
```

---

### Task 8.3: `update-geoip-db.yml` cron workflow

**Files:**
- Create: `.github/workflows/update-geoip-db.yml`

- [ ] **Step 1: Create the workflow**

```yaml
name: update-geoip-db
on:
  schedule:
    - cron: '0 4 * * *'   # 04:00 UTC daily
  workflow_dispatch:

permissions:
  contents: read
  issues: write

concurrency:
  group: update-geoip-db
  cancel-in-progress: false

jobs:
  guard:
    runs-on: ubuntu-latest
    steps:
      - name: assert ref is main
        env:
          REF: ${{ github.ref }}
        run: |
          if [ "$REF" != "refs/heads/main" ]; then
            echo "::error::update-geoip-db.yml may only run from main (got: $REF)"
            exit 1
          fi

  update:
    needs: guard
    runs-on: [self-hosted, oe5xrx-prod-01]
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4

      - name: invoke update_geoip_db inside web container
        run: |
          cd /opt/oe5xrx-services/station_manager
          docker compose exec -T web python manage.py update_geoip_db 2>&1 | tee /tmp/geoip-update.log

      - name: Open or update failure issue
        if: failure()
        uses: ./.github/actions/open-failure-issue
        with:
          label: geoip-update-failure
          title-prefix: "geoip-update failed"
          log-file: /tmp/geoip-update.log
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/update-geoip-db.yml
git commit -m "feat(ci): daily update-geoip-db cron with failure-issue"
```

---

### Task 8.4: `prune-token-sessions.yml` cron workflow

**Files:**
- Create: `.github/workflows/prune-token-sessions.yml`

- [ ] **Step 1: Create the workflow**

```yaml
name: prune-token-sessions
on:
  schedule:
    - cron: '20 3 * * *'   # 03:20 UTC, 20 min after backup.yml
  workflow_dispatch:

permissions:
  contents: read
  issues: write

concurrency:
  group: prune-token-sessions
  cancel-in-progress: false

jobs:
  guard:
    runs-on: ubuntu-latest
    steps:
      - name: assert ref is main
        env:
          REF: ${{ github.ref }}
        run: |
          if [ "$REF" != "refs/heads/main" ]; then
            echo "::error::prune-token-sessions.yml may only run from main (got: $REF)"
            exit 1
          fi

  prune:
    needs: guard
    runs-on: [self-hosted, oe5xrx-prod-01]
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@v4

      - name: invoke prune_token_sessions inside web container
        run: |
          cd /opt/oe5xrx-services/station_manager
          docker compose exec -T web python manage.py prune_token_sessions 2>&1 | tee /tmp/prune-sessions.log

      - name: Open or update failure issue
        if: failure()
        uses: ./.github/actions/open-failure-issue
        with:
          label: prune-token-sessions-failure
          title-prefix: "prune-token-sessions failed"
          log-file: /tmp/prune-sessions.log
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/prune-token-sessions.yml
git commit -m "feat(ci): daily prune-token-sessions cron with failure-issue"
```

---

### Task 8.5: Refactor `backup.yml` to use the composite action

**Files:**
- Modify: `.github/workflows/backup.yml`

- [ ] **Step 1: Replace the inline failure-issue block**

Find the existing `Open or update failure issue` step in `backup.yml` (~lines 68–130). Replace the entire `uses: actions/github-script@v9` block + its `script:` body with:

```yaml
      - name: Open or update failure issue
        if: failure()
        uses: ./.github/actions/open-failure-issue
        with:
          label: backup-failure
          title-prefix: "backup failed"
          log-file: /tmp/journal.txt
```

Keep the preceding `Collect docker journal (on failure)` step untouched — it writes the journal that the composite action reads.

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/backup.yml
git commit -m "refactor(ci): backup.yml uses open-failure-issue composite action"
```

---

### Task 8.6: `services/station_manager/README.md`

**Files:**
- Modify (or create): `services/station_manager/README.md`

- [ ] **Step 1: Document the new mount + cron**

Append (or create with) a section like:

```markdown
## Persistent state

The `web` service has two bind-mounts on the host:

| Path on host | Path in container | Purpose |
|---|---|---|
| `/opt/oe5xrx-data/station_manager/oidc_keys` | `/app/oidc_keys` | RSA keypair for OIDC token signing |
| `/opt/oe5xrx-data/station_manager/geoip_db`  | `/app/geoip_db` | db-ip.com City Lite database for session geolocation |

Both directories are created with appuser ownership (UID 1000) by the
`prepare-volumes` init container at every `docker compose up`.

## Scheduled jobs

| Workflow | Schedule | Purpose |
|---|---|---|
| `update-geoip-db.yml` | daily 04:00 UTC | Refresh the GeoIP DB; falls back to previous-month file when current-month is not yet published by db-ip.com |
| `prune-token-sessions.yml` | daily 03:20 UTC | Delete TokenSession rows older than 30 days |

Both workflows file a GitHub issue on failure via the `open-failure-issue`
composite action (labels `geoip-update-failure` / `prune-token-sessions-failure`).
```

- [ ] **Step 2: Commit**

```bash
git add services/station_manager/README.md
git commit -m "docs(station_manager): document GeoIP mount + scheduled jobs"
```

---

### Task 8.7: Manual verification (PR review checklist)

- [ ] **Step 1: Trigger `update-geoip-db` manually**

```bash
gh workflow run update-geoip-db.yml -R OE5XRX/servers --ref <feature-branch>
```

Expected: workflow run completes green; the DB file in `/opt/oe5xrx-data/station_manager/geoip_db/` is updated.

- [ ] **Step 2: Trigger with an intentionally broken command to verify the issue is filed**

Temporarily change the workflow to `python manage.py update_geoip_db --does-not-exist` on the feature branch, push, manually trigger. Verify:

- Workflow run is RED.
- A new issue with label `geoip-update-failure` exists.

Revert the temporary change before merging.

- [ ] **Step 3: Open the PR**

```bash
gh pr create -R OE5XRX/servers \
  --title "feat: SSO sessions/policies/groups deployment" \
  --body "$(cat <<'EOF'
## Summary

Deployment changes for the SSO sessions + policies + groups feature in
station-manager. Includes:

- New bind-mount `/opt/oe5xrx-data/station_manager/geoip_db` for the
  db-ip.com City Lite database.
- New `GEOIP_DB_PATH` env var on the `web` service.
- Daily cron `update-geoip-db.yml` (current-month + previous-month
  fallback for db-ip release-lag).
- Daily cron `prune-token-sessions.yml` (30-day retention).
- New composite action `open-failure-issue` (extracted from `backup.yml`).
- `backup.yml` refactored to use the composite action.

## Companion PR

- station-manager: <link to station-manager PR>

## Test plan

- [ ] Manually trigger `update-geoip-db.yml`; verify DB file is replaced.
- [ ] Manually trigger with broken command; verify issue is opened
  with label `geoip-update-failure`.
- [ ] Trigger `backup.yml` once on `main` after merge to verify the
  refactored composite-action call works in the hot path.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review Notes (already applied)

- [x] Every spec section maps to a task: §3 (Policy) → 1.1, 2.1, 5.2; §4 (TokenSession) → 1.2, 4.1–4.3, 5.1; §5 (Groups) → 2.2, 5.3, 6.3; §6 (GeoIP) → 3.1–3.2; §7 (UI) → 5.4, 6.*; §8 (Security) covered by `AdminOnlyMixin` reuse and try/except in validator; §9 (Testing) → distributed; §10 (Migrations) → 1.1–1.3; §14 (servers) → Phase 8.
- [x] No "TBD"/"TODO"/"implement later" placeholders — every code step shows the actual code.
- [x] Type/method names consistent: `_record_token_session` (4.1) matches the call from `save_bearer_token` (4.1); `_active_sessions_for` defined in 5.1 reused in 6.3; `_build_groups` defined in 2.2.
- [x] One gotcha noted inline: `Station.slug` may or may not exist; Task 2.2 Step 4 instructs to verify and adapt.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-06-08-sso-sessions-policies-and-groups.md`.**

Per project convention (CLAUDE.md: "Bei `docs/superpowers/plans/*.md`: **subagent-driven-development** als Default"), this plan is intended to be executed via `superpowers:subagent-driven-development` — fresh subagent per task + two-stage review.

The Phase 8 (`servers` repo) tasks have to happen in a separate worktree / repo checkout, since they live in a different repo. The subagent-driven flow can dispatch Phase 8 tasks against `~/OE5XRX/servers` on a parallel branch.
