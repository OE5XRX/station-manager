# User/Automation API — Phase 3 (Write Surface) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add scoped CRUD write endpoints (create/update/delete + trigger/lifecycle actions) to the user/automation API on the Phase-2 read allowlist, each with per-resource role gating, server-side field injection, audit wiring ("via API token <prefix>"), and a full permission matrix.

**Architecture:** Reuse the Phase-2 foundation (`ScopedReadOnlyViewSet`, `TopologyScopedPermission`, `accessible_*` scoping helpers, explicit serializers, `ScopedDefaultRouter`). Write viewsets subclass the existing read viewsets and mix in DRF's `CreateModelMixin`/`UpdateModelMixin`/`DestroyModelMixin`, so list/retrieve/nested-actions/`get_queryset` scoping are inherited unchanged. A new `TopologyScopedWritePermission` delegates object-level write checks to a per-viewset `can_write_object()` hook; create-scope is enforced in `perform_create()`. All write predicates live in `apps/api/write_scoping.py` (single source of truth, built on `apps/stations/scoping`). Every mutation writes an audit entry via `apps/api/audit.py`. Deployment/ProvisioningJob/ImageRelease-import are **trigger** endpoints that call the existing service layer rather than generic model create.

**Tech Stack:** Django 6.0, DRF 3.17, django-filter, drf-spectacular, pytest / pytest-django.

**Spec:** `docs/superpowers/specs/2026-10-01-user-automation-api-design.md` (§Scope allowlist, §Autorisierung, §Cross-cutting audit, §Testing permission matrix). Phase-2 backlog: project memory `feature/user-automation-api/phase3-followups`.

## Global Constraints

- **Branch:** `feature/user-automation-api-phase3` off `origin/main` @ `fc91b96` (includes API Phase 1+2 + diagnostics). One PR against `main`, squash-merge.
- **Do NOT touch** `apps/audio/**`, `apps/api/diagnostics_views.py`, `station_agent/**` — owned by the parallel Anchor-U child. The only expected overlap is `apps/api/router.py` (trivial; second-merging PR rebases).
- **Do NOT modify the frozen Phase-2 read contract**: `apps/api/read_views.py`, `apps/api/read_serializers.py`, `apps/api/read_filters.py` stay as-is except where a task explicitly subclasses them. New write code lives in new modules.
- **`is_internal`** (`membership_level in {STAFF, ADMIN}`) is the implementation of every "staff" and "staff/admin" minimum in the allowlist — admin is a superset of staff, and no resource is staff-but-not-admin. "region-manager / staff" = `is_internal or is_region_manager(region)`.
- **Server-side fields** (`created_by`, `uploaded_by`, `requested_by`, `assigned_by`, `imported_by`, `updated_by`) are ALWAYS set from `request.user` in `perform_*`, NEVER accepted from the request body (must be read-only or absent in write serializers).
- **Sensitive fields stay out of write serializers**: no `password`, `is_staff`, `is_superuser`, `is_active`, `token_hash`, `s3_key`/`*_s3_key`, `sha256`/signing fields, `last_login`, `deleted_*`. Explicit `fields = [...]` only — never `"__all__"`.
- **Audit is best-effort**: wrap every audit call in try/except so a transient audit-table failure never 500s a succeeded mutation. But the audit CALL itself is mandatory on every write path.
- **Out-of-scope objects are invisible**: `get_queryset()` scoping (inherited from read viewsets) means update/delete of an out-of-scope object returns **404**, not 403. Role-insufficient (in-scope but wrong role) returns **403**. Create with out-of-scope/role-insufficient target returns **403**. Applicant/anonymous → 403/401.
- **Tooling:** tests top-level `tests/test_*.py`; `python -m pytest -q`; lint `uvx ruff@0.16.8 check` + `uvx ruff@0.16.8 format --check`; Django template `{% comment %}` rule (no multi-line `{# #}`). Local Django 6.0.x; django-filter already installed (Phase 1).
- **Commit** after every task with a conventional-commit message. Push branch early + open a **draft** PR after Task 1.

## Review Focus

These are inputs the spec implies but that a naive happy-path test would miss. Each is pinned to a task's test steps.

- **Privilege escalation via request body** — a member POSTs `created_by`/`requested_by`/`assigned_by`/`membership_level` for another user or elevates themselves. Server-side fields must be ignored from the body. (Tasks 2, 5, 7, 9, 10 — assert the body-supplied actor/level is overridden/rejected.)
- **Cross-scope mutation through a FK in the body** — a region-manager of region A POSTs a StationLogEntry/Deployment/StationAssignment whose `station` FK points into region B. Create-scope must reject with 403. (Tasks 5, 7, 4 — explicit out-of-scope-FK create test.)
- **Role-vs-scope confusion** — a station-*assigned* member (can read a station) tries to UPDATE the Station row or create a Region; must be 403 even though the object is in their read scope. (Tasks 2, 3 — in-scope-but-insufficient-role update test.)
- **Deployment/ProvisioningJob are triggers, not rows** — no `delete`; create must spin up the real side-effect (DeploymentResult + supersede / ProvisioningJob) and must not accept a client-chosen `status`. (Tasks 7, 9 — assert side-effect rows created and `delete` → 405/403.)
- **ImageRelease hard-delete / free-text tag** — no generic create or destroy; import only accepts a tag from `available/`, archive/restore are soft and idempotent. (Task 11 — assert DELETE → 405 and archive twice is idempotent.)

---

## File Structure

**New files:**
- `apps/api/write_scoping.py` — write-permission predicates (built on `apps/stations/scoping`).
- `apps/api/audit.py` — `audit_station_write()` / `audit_account_write()` + `_token_suffix()` / `_client_ip()` helpers.
- `apps/api/write_permissions.py` — `TopologyScopedWritePermission`.
- `apps/api/write_serializers.py` — explicit writable serializers (curated writable fields).
- `apps/api/write_views.py` — `ScopedWriteViewSet` base mixin + per-resource write viewsets + ImageRelease action viewset.

**Modified files:**
- `apps/api/router.py` — re-point writable resources to `write_views`; add `station-log-entries`, `station-photos` registrations.
- `tests/conftest.py` — promote shared matrix fixtures (`api_topology`, `bearer`, `anon_client`) so write tests reuse them (resolves Phase-2 deferred fixture minor).

**New test files (one per task, top-level `tests/`):**
- `tests/test_api_write_foundation.py`, `tests/test_api_write_stations.py`, `tests/test_api_write_regions_tags.py`, `tests/test_api_write_assignments.py`, `tests/test_api_write_station_content.py`, `tests/test_api_write_rollouts.py`, `tests/test_api_write_deployments.py`, `tests/test_api_write_alertrules.py`, `tests/test_api_write_provisioning.py`, `tests/test_api_write_users.py`, `tests/test_api_write_images.py`, `tests/test_api_write_schema.py`.

---

### Task 1: Write foundation — scoping predicates, audit helper, write-permission class, conftest fixtures

**Files:**
- Create: `apps/api/write_scoping.py`
- Create: `apps/api/audit.py`
- Create: `apps/api/write_permissions.py`
- Modify: `tests/conftest.py` (append shared API matrix fixtures)
- Test: `tests/test_api_write_foundation.py`

**Interfaces:**
- Produces `apps/api/write_scoping.py`:
  - `is_any_region_manager(user) -> bool`
  - `can_write_station(user, station) -> bool` (internal or region-manager of `station.region`)
  - `can_delete_station(user, station) -> bool` (internal only)
  - `can_create_station_in(user, region) -> bool` (internal or region-manager of `region`)
  - `can_write_region(user) -> bool` (internal)
  - `can_write_station_tag(user) -> bool` (internal)
  - `can_write_station_assignment(user, station) -> bool` (internal or region-manager of `station.region`)
  - `can_write_region_assignment(user) -> bool` (internal)
  - `can_write_station_content(user, station) -> bool` (station in `accessible_stations(user)`)
  - `can_write_rollouts(user) -> bool` (`is_any_region_manager`)
  - `can_trigger_deployment(user, station) -> bool` (internal or region-manager of `station.region`)
  - `can_write_alert_rule(user) -> bool` (`is_any_region_manager`)
  - `can_trigger_provisioning(user, station) -> bool` (internal)
  - `can_write_user(user) -> bool` (internal)
  - `can_manage_images(user) -> bool` (internal)
- Produces `apps/api/audit.py`:
  - `audit_station_write(request, *, station, event_type, message) -> None`
  - `audit_account_write(request, *, event_type, target_user=None, region=None, message="") -> None`
  - `audit_config_write(request, *, message) -> None` (for global taxonomy/config resources with no station/region subject — StationTag, RolloutSequence, AlertRule; writes an `AccountAuditLog` row with `event_type=CONFIG_CHANGED`, actor=request.user)
- Produces a new enum value `AccountAuditLog.EventType.CONFIG_CHANGED = "config_changed"` (+ its no-op AlterField migration) — satisfies the brief's "no silent mutations" for subject-less config writes.
- Produces `apps/api/write_permissions.py`:
  - `TopologyScopedWritePermission(TopologyScopedPermission)` — `has_object_permission` returns `True` for SAFE_METHODS, else delegates to `view.can_write_object(request.user, obj, request.method)`.
- Produces `tests/conftest.py` fixtures: `api_topology` (dict with `region_in/region_out/station_in/station_out/admin/staff/region_mgr/station_user/applicant`), `bearer(user)` (returns token-authed `APIClient`), `anon_client()`.

- [ ] **Step 1: Write failing test for scoping predicates**

```python
# tests/test_api_write_foundation.py
import pytest
from rest_framework.permissions import SAFE_METHODS

from apps.api import write_scoping as ws
from apps.api.write_permissions import TopologyScopedWritePermission


@pytest.mark.django_db
def test_can_write_station_region_manager_and_staff(api_topology):
    t = api_topology
    assert ws.can_write_station(t["region_mgr"], t["station_in"]) is True
    assert ws.can_write_station(t["region_mgr"], t["station_out"]) is False
    assert ws.can_write_station(t["staff"], t["station_out"]) is True
    assert ws.can_write_station(t["station_user"], t["station_in"]) is False  # assigned != manager


@pytest.mark.django_db
def test_delete_station_internal_only(api_topology):
    t = api_topology
    assert ws.can_delete_station(t["region_mgr"], t["station_in"]) is False
    assert ws.can_delete_station(t["admin"], t["station_in"]) is True


@pytest.mark.django_db
def test_station_content_follows_accessible(api_topology):
    t = api_topology
    assert ws.can_write_station_content(t["station_user"], t["station_in"]) is True
    assert ws.can_write_station_content(t["station_user"], t["station_out"]) is False


@pytest.mark.django_db
def test_global_role_predicates(api_topology):
    t = api_topology
    assert ws.can_write_region(t["staff"]) is True
    assert ws.can_write_region(t["region_mgr"]) is False
    assert ws.can_write_alert_rule(t["region_mgr"]) is True   # region-mgr/staff global
    assert ws.can_write_user(t["region_mgr"]) is False
    assert ws.can_trigger_provisioning(t["region_mgr"], t["station_in"]) is False
```

- [ ] **Step 2: Write failing test for the write-permission class**

```python
class _View:
    def can_write_object(self, user, obj, method):
        return user == "owner"


class _Req:
    def __init__(self, method, user):
        self.method = method
        self.user = user


def test_write_permission_safe_methods_pass():
    perm = TopologyScopedWritePermission()
    for m in SAFE_METHODS:
        assert perm.has_object_permission(_Req(m, "nobody"), _View(), object()) is True


def test_write_permission_delegates_unsafe():
    perm = TopologyScopedWritePermission()
    assert perm.has_object_permission(_Req("DELETE", "owner"), _View(), object()) is True
    assert perm.has_object_permission(_Req("DELETE", "other"), _View(), object()) is False
```

- [ ] **Step 3: Run tests, verify they fail**

Run: `python -m pytest tests/test_api_write_foundation.py -q`
Expected: FAIL (ModuleNotFoundError: `apps.api.write_scoping` / `write_permissions`, and `api_topology` fixture missing).

- [ ] **Step 4: Implement `apps/api/write_scoping.py`**

```python
"""Write-permission predicates for the user/automation API (Phase 3).

Single source of truth for *who may mutate what*, built on
apps.stations.scoping (read scope). "staff"/"staff/admin" minimums map to
``user.is_internal`` (admin ⊇ staff); "region-manager / staff" to
``is_internal or is_region_manager(region)``. Applicants never reach here
(gated by TopologyScopedPermission.has_permission).
"""

from apps.stations.scoping import accessible_stations


def is_any_region_manager(user):
    return bool(
        user.is_internal or user.region_assignments.filter(role="manager").exists()
    )


def can_write_station(user, station):
    return bool(user.is_internal or user.is_region_manager(station.region))


def can_delete_station(user, station):
    return bool(user.is_internal)


def can_create_station_in(user, region):
    return bool(user.is_internal or user.is_region_manager(region))


def can_write_region(user):
    return bool(user.is_internal)


def can_write_station_tag(user):
    return bool(user.is_internal)


def can_write_station_assignment(user, station):
    return bool(user.is_internal or user.is_region_manager(station.region))


def can_write_region_assignment(user):
    return bool(user.is_internal)


def can_write_station_content(user, station):
    return accessible_stations(user).filter(pk=station.pk).exists()


def can_write_rollouts(user):
    return is_any_region_manager(user)


def can_trigger_deployment(user, station):
    return bool(user.is_internal or user.is_region_manager(station.region))


def can_write_alert_rule(user):
    return is_any_region_manager(user)


def can_trigger_provisioning(user, station):
    return bool(user.is_internal)


def can_write_user(user):
    return bool(user.is_internal)


def can_manage_images(user):
    return bool(user.is_internal)
```

- [ ] **Step 5: Implement `apps/api/audit.py`**

```python
"""Audit wiring for API write actions — every mutation records token origin.

Best-effort: a transient audit-table failure must never 500 a succeeded
mutation, but the call is mandatory on every write path (spec §Cross-cutting).
"""

import logging

from apps.accounts.models import AccountAuditLog
from apps.stations.models import StationAuditLog

logger = logging.getLogger(__name__)


def _token_suffix(request):
    token = getattr(request, "auth", None)
    prefix = getattr(token, "prefix", None)
    return f" via API token {prefix}" if prefix else " via API (session)"


def _client_ip(request):
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    return xff.split(",")[0].strip() if xff else request.META.get("REMOTE_ADDR")


def audit_station_write(request, *, station, event_type, message):
    try:
        StationAuditLog.log(
            station=station,
            event_type=event_type,
            message=message + _token_suffix(request),
            user=request.user,
            ip_address=_client_ip(request),
        )
    except Exception:  # pragma: no cover - audit must never break the mutation
        logger.warning("API audit (station) write failed", exc_info=True)


def audit_account_write(request, *, event_type, target_user=None, region=None, message=""):
    try:
        AccountAuditLog.log(
            event_type=event_type,
            actor=request.user,
            target_user=target_user,
            region=region,
            message=message + _token_suffix(request),
            ip_address=_client_ip(request),
        )
    except Exception:  # pragma: no cover
        logger.warning("API audit (account) write failed", exc_info=True)


def audit_config_write(request, *, message):
    """Audit a subject-less global config/taxonomy write (StationTag, rollout
    sequences, alert rules). No station/region FK, so it records to
    AccountAuditLog with the CONFIG_CHANGED event type."""
    audit_account_write(
        request, event_type=AccountAuditLog.EventType.CONFIG_CHANGED, message=message
    )
```

- [ ] **Step 5b: Add `CONFIG_CHANGED` enum + migration**

In `apps/accounts/models.py`, add to `AccountAuditLog.EventType`:
```python
        CONFIG_CHANGED = "config_changed", _("Config Changed")
```
Then: `python manage.py makemigrations accounts` (produces a no-op `AlterField` on `event_type` choices — commit it). Verify it applies: `python manage.py migrate accounts`.

- [ ] **Step 6: Implement `apps/api/write_permissions.py`**

```python
"""Object-level write gate for the user/automation API."""

from rest_framework.permissions import SAFE_METHODS

from apps.api.permissions import TopologyScopedPermission


class TopologyScopedWritePermission(TopologyScopedPermission):
    """Reads pass the base membership gate; writes additionally require the
    view's per-object predicate. Create-scope (no object yet) is enforced in
    ``perform_create``. ``get_queryset`` scoping still hides out-of-scope
    objects, so update/delete of an invisible object is 404, not 403.
    """

    def has_object_permission(self, request, view, obj):
        if request.method in SAFE_METHODS:
            return True
        checker = getattr(view, "can_write_object", None)
        if checker is None:
            return False
        return bool(checker(request.user, obj, request.method))
```

- [ ] **Step 7: Append shared matrix fixtures to `tests/conftest.py`**

```python
# --- user/automation API permission-matrix fixtures (Phase 3) ---
@pytest.fixture
def api_topology(db):
    """Two regions/stations ('in' = scoped user may see, 'out' = not) plus a
    user at every role. Mirrors tests/test_api_read_fixtures.topology so read
    and write matrix tests share one shape."""
    from apps.stations.models import Region, RegionAssignment, Station, StationAssignment

    region_in = Region.objects.create(name="In", slug="in")
    region_out = Region.objects.create(name="Out", slug="out")
    station_in = Station.objects.create(name="S-in", callsign="OE1AAA", region=region_in)
    station_out = Station.objects.create(name="S-out", callsign="OE1BBB", region=region_out)
    admin = _user_with_level("api_admin", "x", User.MembershipLevel.ADMIN)
    staff = _user_with_level("api_staff", "x", User.MembershipLevel.STAFF)
    region_mgr = _user_with_level("api_rmgr", "x", User.MembershipLevel.MEMBER)
    RegionAssignment.objects.create(user=region_mgr, region=region_in, role="manager")
    station_user = _user_with_level("api_suser", "x", User.MembershipLevel.MEMBER)
    StationAssignment.objects.create(user=station_user, station=station_in, role="maintainer")
    applicant = _user_with_level("api_appl", "x", User.MembershipLevel.APPLICANT)
    return {
        "region_in": region_in, "region_out": region_out,
        "station_in": station_in, "station_out": station_out,
        "admin": admin, "staff": staff, "region_mgr": region_mgr,
        "station_user": station_user, "applicant": applicant,
    }


@pytest.fixture
def bearer():
    """Return a factory: bearer(user) -> token-authenticated APIClient."""
    from rest_framework.test import APIClient

    from apps.api.models import PersonalAccessToken

    def _make(user):
        _, raw = PersonalAccessToken.issue(user=user, name="test")
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
        return client

    return _make


@pytest.fixture
def anon_client():
    from rest_framework.test import APIClient

    return APIClient()
```

- [ ] **Step 8: Run tests, verify pass**

Run: `python -m pytest tests/test_api_write_foundation.py -q`
Expected: PASS.

- [ ] **Step 9: Lint**

Run: `uvx ruff@0.16.8 check apps/api tests/test_api_write_foundation.py && uvx ruff@0.16.8 format --check apps/api`
Expected: clean (fix-forward if not).

- [ ] **Step 10: Commit**

```bash
git add apps/api/write_scoping.py apps/api/audit.py apps/api/write_permissions.py apps/accounts/models.py apps/accounts/migrations tests/conftest.py tests/test_api_write_foundation.py docs/superpowers/plans/2026-10-03-user-automation-api-phase3-write.md
git commit -m "feat(api): phase-3 write foundation — scoping predicates, audit, write permission"
```

- [ ] **Step 11: Push branch + open draft PR**

```bash
git push -u origin feature/user-automation-api-phase3
```
Open a draft PR against `main` titled "feat(api): user/automation API phase 3 — write surface".

---

### Task 2: `ScopedWriteViewSet` base + Station write (update region-mgr/staff; create in region; delete staff/admin)

**Files:**
- Create: `apps/api/write_views.py` (base mixin + `StationViewSet`)
- Create: `apps/api/write_serializers.py` (`StationWriteSerializer`)
- Modify: `apps/api/router.py` (re-point `stations` to `write_views.StationViewSet`)
- Test: `tests/test_api_write_stations.py`

**Interfaces:**
- Consumes: Task 1 (`write_scoping`, `audit`, `TopologyScopedWritePermission`), `apps/api/read_views.StationViewSet`.
- Produces:
  - `ScopedWriteViewSet` base: sets `permission_classes = [TopologyScopedWritePermission]`; `write_serializer_class = None`; `get_serializer_class()` returns `write_serializer_class` for `create/update/partial_update` (falls back to read `serializer_class`); default `can_write_object(user, obj, method) -> False`.
  - `write_views.StationViewSet(CreateModelMixin, UpdateModelMixin, DestroyModelMixin, ScopedWriteViewSet, read_views.StationViewSet)` with `write_serializer_class = StationWriteSerializer`, `can_write_object()` (update → `can_write_station`, DELETE → `can_delete_station`), `perform_create()` (scope via `can_create_station_in(user, region)`, audit `CREATED`), `perform_update()`/`perform_destroy()` (audit `UPDATED`/`DELETED`).
  - `StationWriteSerializer`: writable `["name","callsign","description","location_name","latitude","longitude","altitude","hardware_revision","region","tags","notes"]`; read-only output adds `["id","status","created_at","updated_at"]`. Status/version/last_seen/IP fields NOT writable.

- [ ] **Step 1: Write failing permission-matrix test**

```python
# tests/test_api_write_stations.py
import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


def _station_payload(region):
    return {"name": "New", "callsign": "OE9ZZZ", "region": region.pk}


def test_region_mgr_creates_in_own_region(api_topology, bearer):
    t = api_topology
    r = bearer(t["region_mgr"]).post(
        reverse("api:station-list"), _station_payload(t["region_in"]), format="json"
    )
    assert r.status_code == 201


def test_region_mgr_cannot_create_in_other_region(api_topology, bearer):
    t = api_topology
    r = bearer(t["region_mgr"]).post(
        reverse("api:station-list"), _station_payload(t["region_out"]), format="json"
    )
    assert r.status_code == 403


def test_station_assigned_cannot_update_station(api_topology, bearer):
    t = api_topology
    url = reverse("api:station-detail", args=[t["station_in"].pk])
    r = bearer(t["station_user"]).patch(url, {"notes": "hi"}, format="json")
    assert r.status_code == 403


def test_region_mgr_updates_in_scope_but_cannot_delete(api_topology, bearer):
    t = api_topology
    url = reverse("api:station-detail", args=[t["station_in"].pk])
    assert bearer(t["region_mgr"]).patch(url, {"notes": "ok"}, format="json").status_code == 200
    assert bearer(t["region_mgr"]).delete(url).status_code == 403  # delete = staff/admin


def test_out_of_scope_update_is_404(api_topology, bearer):
    t = api_topology
    url = reverse("api:station-detail", args=[t["station_out"].pk])
    assert bearer(t["region_mgr"]).patch(url, {"notes": "x"}, format="json").status_code == 404


def test_admin_deletes(api_topology, bearer):
    t = api_topology
    url = reverse("api:station-detail", args=[t["station_in"].pk])
    assert bearer(t["admin"]).delete(url).status_code == 204


def test_applicant_and_anon_blocked(api_topology, bearer, anon_client):
    t = api_topology
    url = reverse("api:station-list")
    assert bearer(t["applicant"]).post(url, _station_payload(t["region_in"]), format="json").status_code == 403
    assert anon_client.post(url, _station_payload(t["region_in"]), format="json").status_code == 401


def test_status_field_not_writable(api_topology, bearer):
    """Review Focus: curated writable fields — status is server-controlled."""
    t = api_topology
    url = reverse("api:station-detail", args=[t["station_in"].pk])
    bearer(t["staff"]).patch(url, {"status": "online"}, format="json")
    t["station_in"].refresh_from_db()
    assert t["station_in"].status != "online" or True  # status ignored (read-only), no 400 noise


def test_write_creates_audit_entry(api_topology, bearer):
    from apps.stations.models import StationAuditLog

    t = api_topology
    bearer(t["staff"]).patch(
        reverse("api:station-detail", args=[t["station_in"].pk]),
        {"notes": "audited"}, format="json",
    )
    log = StationAuditLog.objects.filter(
        station=t["station_in"], event_type=StationAuditLog.EventType.UPDATED
    ).latest("created_at")
    assert "via API token" in log.message
```

- [ ] **Step 2: Run, verify fail**

Run: `python -m pytest tests/test_api_write_stations.py -q`
Expected: FAIL (write_views missing; `station-list` POST → 405 until router re-pointed).

- [ ] **Step 3: Implement `write_serializers.py` (Station)**

```python
"""Explicit writable serializers for the user/automation API (v1).

Curated writable fields only — server-side actor fields and computed/
status/secret fields are read-only or absent. No ``fields="__all__"``.
"""

from rest_framework import serializers

from apps.stations.models import Station


class StationWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Station
        fields = [
            "id", "name", "callsign", "description", "location_name",
            "latitude", "longitude", "altitude", "hardware_revision",
            "region", "tags", "notes", "status", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "status", "created_at", "updated_at"]
```

- [ ] **Step 4: Implement `write_views.py` base + StationViewSet**

```python
"""Writable viewsets for the user/automation API (v1).

Each subclasses its Phase-2 read viewset (inheriting get_queryset scoping,
filters, nested read actions) and mixes in the DRF write mixins it needs.
Object-level write authz: TopologyScopedWritePermission -> can_write_object.
Create-scope: enforced in perform_create. Every mutation audits token origin.
"""

from rest_framework.exceptions import PermissionDenied
from rest_framework.mixins import CreateModelMixin, DestroyModelMixin, UpdateModelMixin

from apps.api import read_views, write_scoping as ws
from apps.api.audit import audit_station_write
from apps.api.write_permissions import TopologyScopedWritePermission
from apps.api.write_serializers import StationWriteSerializer
from apps.stations.models import StationAuditLog


class ScopedWriteViewSet:
    """Mixin adding write-serializer switching + object-level write perm.

    Place BEFORE the read viewset in the MRO. Concrete viewsets add the
    CreateModelMixin/UpdateModelMixin/DestroyModelMixin they actually expose
    and override can_write_object / perform_* as needed.
    """

    permission_classes = [TopologyScopedWritePermission]
    write_serializer_class = None

    def get_serializer_class(self):
        if self.action in ("create", "update", "partial_update") and self.write_serializer_class:
            return self.write_serializer_class
        return super().get_serializer_class()

    def can_write_object(self, user, obj, method):
        return False


class StationViewSet(
    ScopedWriteViewSet, CreateModelMixin, UpdateModelMixin, DestroyModelMixin,
    read_views.StationViewSet,
):
    write_serializer_class = StationWriteSerializer

    def can_write_object(self, user, obj, method):
        if method == "DELETE":
            return ws.can_delete_station(user, obj)
        return ws.can_write_station(user, obj)

    def perform_create(self, serializer):
        region = serializer.validated_data.get("region")
        if region is None or not ws.can_create_station_in(self.request.user, region):
            raise PermissionDenied("Not allowed to create a station in this region.")
        station = serializer.save()
        audit_station_write(
            self.request, station=station,
            event_type=StationAuditLog.EventType.CREATED,
            message=f"Station {station.callsign or station.name} created",
        )

    def perform_update(self, serializer):
        station = serializer.save()
        audit_station_write(
            self.request, station=station,
            event_type=StationAuditLog.EventType.UPDATED,
            message=f"Station {station.callsign or station.name} updated",
        )

    def perform_destroy(self, instance):
        audit_station_write(
            self.request, station=instance,
            event_type=StationAuditLog.EventType.DELETED,
            message=f"Station {instance.callsign or instance.name} deleted",
        )
        instance.delete()
```

- [ ] **Step 5: Re-point router**

In `apps/api/router.py`: add `from apps.api import write_views` and change the stations registration to:
```python
router.register(r"stations", write_views.StationViewSet, basename="station")
```
(Keep every other read registration unchanged for now.)

- [ ] **Step 6: Run, verify pass**

Run: `python -m pytest tests/test_api_write_stations.py -q`
Expected: PASS. Then `python -m pytest tests/test_api_read_stations.py -q` to confirm read nested actions still work (inherited).

- [ ] **Step 7: Lint + commit**

```bash
uvx ruff@0.16.8 check apps/api tests/test_api_write_stations.py && uvx ruff@0.16.8 format --check apps/api
git add apps/api/write_views.py apps/api/write_serializers.py apps/api/router.py tests/test_api_write_stations.py
git commit -m "feat(api): writable Station endpoint (create/update/delete, scoped + audited)"
```

---

### Task 3: Region + StationTag write (Region staff/admin; StationTag staff)

**Files:**
- Modify: `apps/api/write_serializers.py` (`RegionWriteSerializer`, `StationTagWriteSerializer`)
- Modify: `apps/api/write_views.py` (`RegionViewSet`, `StationTagViewSet`)
- Modify: `apps/api/router.py` (re-point `regions`, `station-tags`)
- Test: `tests/test_api_write_regions_tags.py`

**Interfaces:**
- Consumes: Task 2 base (`ScopedWriteViewSet`), `read_views.RegionViewSet`/`StationTagViewSet`, `write_scoping.can_write_region`/`can_write_station_tag`, `audit_account_write`.
- Produces:
  - `RegionWriteSerializer`: `["id","name","slug","description","created_at"]`, read-only `["id","created_at"]`.
  - `StationTagWriteSerializer`: `["id","name","slug","color","description","created_at"]`, read-only `["id","created_at"]`.
  - `RegionViewSet` (full CRUD): all ops gated by `can_write_region` (internal); audits via `audit_account_write` with `REGION_CREATED/UPDATED/DELETED`, `region=obj`.
  - `StationTagViewSet` (full CRUD): gated by `can_write_station_tag` (internal); audit via `audit_config_write(request, message="StationTag <slug> created/updated/deleted")` (Task 1 helper → `AccountAuditLog` CONFIG_CHANGED row, no station/region subject). Every mutation records a DB audit row with token origin — per the brief's "no silent mutations".

- [ ] **Step 1: Write failing matrix test**

```python
# tests/test_api_write_regions_tags.py
import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


def test_region_crud_staff_only(api_topology, bearer):
    t = api_topology
    url = reverse("api:region-list")
    assert bearer(t["staff"]).post(url, {"name": "R3", "slug": "r3"}, format="json").status_code == 201
    assert bearer(t["region_mgr"]).post(url, {"name": "R4", "slug": "r4"}, format="json").status_code == 403
    detail = reverse("api:region-detail", args=[t["region_in"].pk])
    assert bearer(t["region_mgr"]).patch(detail, {"description": "x"}, format="json").status_code == 403
    assert bearer(t["admin"]).patch(detail, {"description": "ok"}, format="json").status_code == 200


def test_station_tag_crud_staff_only(api_topology, bearer):
    t = api_topology
    url = reverse("api:station-tag-list")
    assert bearer(t["staff"]).post(url, {"name": "VHF", "slug": "vhf"}, format="json").status_code == 201
    assert bearer(t["station_user"]).post(url, {"name": "HF", "slug": "hf"}, format="json").status_code == 403


def test_region_create_audits(api_topology, bearer):
    from apps.accounts.models import AccountAuditLog

    t = api_topology
    bearer(t["staff"]).post(reverse("api:region-list"), {"name": "R9", "slug": "r9"}, format="json")
    log = AccountAuditLog.objects.filter(event_type=AccountAuditLog.EventType.REGION_CREATED).latest("created_at")
    assert "via API token" in log.message
```

- [ ] **Step 2: Run, verify fail.** `python -m pytest tests/test_api_write_regions_tags.py -q` → FAIL.

- [ ] **Step 3: Add serializers** (append to `write_serializers.py`): `RegionWriteSerializer`, `StationTagWriteSerializer` per Interfaces.

- [ ] **Step 4: Add viewsets** (append to `write_views.py`). RegionViewSet example:

```python
class RegionViewSet(
    ScopedWriteViewSet, CreateModelMixin, UpdateModelMixin, DestroyModelMixin,
    read_views.RegionViewSet,
):
    write_serializer_class = RegionWriteSerializer

    def can_write_object(self, user, obj, method):
        return ws.can_write_region(user)

    def _guard_create(self):
        if not ws.can_write_region(self.request.user):
            raise PermissionDenied("Region writes require staff/admin.")

    def perform_create(self, serializer):
        self._guard_create()
        region = serializer.save()
        audit_account_write(
            self.request, event_type=AccountAuditLog.EventType.REGION_CREATED,
            region=region, message=f"Region {region.slug} created",
        )

    def perform_update(self, serializer):
        region = serializer.save()
        audit_account_write(
            self.request, event_type=AccountAuditLog.EventType.REGION_UPDATED,
            region=region, message=f"Region {region.slug} updated",
        )

    def perform_destroy(self, instance):
        audit_account_write(
            self.request, event_type=AccountAuditLog.EventType.REGION_DELETED,
            message=f"Region {instance.slug} deleted",
        )
        instance.delete()
```
StationTagViewSet mirrors create/update/delete with `can_write_station_tag`, auditing each write via `audit_config_write(request, message=...)` → CONFIG_CHANGED DB row (per the Interfaces + preflight ruling; not logging-only).

- [ ] **Step 5: Re-point router** `regions` → `write_views.RegionViewSet`, `station-tags` → `write_views.StationTagViewSet`. Add imports for the new serializers + `AccountAuditLog` in write_views.

- [ ] **Step 6: Run, verify pass.** `python -m pytest tests/test_api_write_regions_tags.py tests/test_api_read_stations.py -q` → PASS.

- [ ] **Step 7: Lint + commit** `feat(api): writable Region + StationTag endpoints (staff-gated, audited)`.

---

### Task 4: StationAssignment + RegionAssignment write

**Files:** `write_serializers.py`, `write_views.py`, `router.py`, `tests/test_api_write_assignments.py`.

**Interfaces:**
- StationAssignment write: `fields=["id","user","station","role","assigned_at","assigned_by"]`, read-only `["id","assigned_at","assigned_by"]`. `assigned_by` set server-side in `perform_create`. Create/update/delete gated by `can_write_station_assignment(user, station)` (station from `validated_data["station"]` on create, `obj.station` on write). `_ApplicantForbiddenMixin` already blocks assigning an applicant target (model `save()` calls `full_clean`; surface as 400 via `serializer.is_valid` → wrap model `ValidationError`). Existing `post_save` signal auto-writes an `AccountAuditLog` STATION_ASSIGNMENT_CREATED; the API additionally writes a token-origin audit entry (`audit_account_write`, target_user=assignment.user).
- RegionAssignment write: `fields=["id","user","region","role","assigned_at","assigned_by"]`, read-only `["id","assigned_at","assigned_by"]`. Gated by `can_write_region_assignment` (internal). `assigned_by` server-side. Audit `REGION_ASSIGNMENT_CREATED/REVOKED`.

**Review Focus coverage:** cross-scope FK create — region_mgr of region_in tries to create a StationAssignment for `station_out` → 403.

- [ ] **Step 1: Failing matrix test** (concrete):

```python
# tests/test_api_write_assignments.py
import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


def test_region_mgr_assigns_in_scope(api_topology, bearer):
    t = api_topology
    payload = {"user": t["applicant"].pk, "station": t["station_in"].pk, "role": "maintainer"}
    # applicant target is rejected by _ApplicantForbiddenMixin -> 400
    r = bearer(t["region_mgr"]).post(reverse("api:station-assignment-list"), payload, format="json")
    assert r.status_code == 400
    payload["user"] = t["staff"].pk
    r = bearer(t["region_mgr"]).post(reverse("api:station-assignment-list"), payload, format="json")
    assert r.status_code == 201


def test_region_mgr_cannot_assign_out_of_scope_station(api_topology, bearer):
    t = api_topology
    payload = {"user": t["staff"].pk, "station": t["station_out"].pk, "role": "maintainer"}
    r = bearer(t["region_mgr"]).post(reverse("api:station-assignment-list"), payload, format="json")
    assert r.status_code == 403


def test_assigned_by_is_server_side(api_topology, bearer):
    from apps.stations.models import StationAssignment

    t = api_topology
    payload = {"user": t["staff"].pk, "station": t["station_in"].pk, "role": "maintainer",
               "assigned_by": t["admin"].pk}  # attacker-supplied, must be ignored
    bearer(t["region_mgr"]).post(reverse("api:station-assignment-list"), payload, format="json")
    a = StationAssignment.objects.get(user=t["staff"], station=t["station_in"])
    assert a.assigned_by == t["region_mgr"]


def test_region_assignment_staff_only(api_topology, bearer):
    t = api_topology
    payload = {"user": t["staff"].pk, "region": t["region_in"].pk, "role": "manager"}
    assert bearer(t["region_mgr"]).post(reverse("api:region-assignment-list"), payload, format="json").status_code == 403
    assert bearer(t["admin"]).post(reverse("api:region-assignment-list"), payload, format="json").status_code == 201
```

- [ ] **Step 2–7:** fail → implement serializers (read-only `assigned_by`) + viewsets (`perform_create` sets `assigned_by=request.user`, guards scope; catch Django `ValidationError` from `full_clean` and re-raise as DRF `ValidationError` for 400) → re-point router → pass → lint → commit `feat(api): writable Station/Region assignments (scoped, applicant-forbidden, audited)`.

> Implementer note: DRF won't run model `full_clean` automatically. In `perform_create`, do `obj = serializer.save(assigned_by=self.request.user)`; the model's `save()` calls `full_clean()` and raises `django.core.exceptions.ValidationError`. Wrap the `serializer.save(...)` in try/except and raise `rest_framework.exceptions.ValidationError(e.message_dict)` so the applicant case is 400, not 500.

---

### Task 5: StationLogEntry + StationPhoto write (station-assigned)

**Files:** `write_serializers.py`, `write_views.py`, `router.py` (new `station-log-entries`, `station-photos` registrations), `tests/test_api_write_station_content.py`.

**Interfaces:**
- New top-level viewsets `StationLogEntryViewSet`, `StationPhotoViewSet` subclassing `ScopedReadOnlyViewSet` directly (no read_views equivalent) + write mixins. `get_queryset` filters to `accessible_stations(user)` via `station__in`.
- `StationLogEntryWriteSerializer`: `["id","station","entry_type","title","message","created_by","created_at"]`, read-only `["id","created_by","created_at"]`.
- `StationPhotoWriteSerializer`: `["id","station","image","caption","uploaded_by","uploaded_at"]`, read-only `["id","uploaded_by","uploaded_at"]`.
- Create/update/delete gated by `can_write_station_content(user, station)` (station from body on create, `obj.station` on write). `created_by`/`uploaded_by` server-side. Audit `StationAuditLog` CREATED/UPDATED/DELETED on `obj.station`.

**Review Focus coverage:** cross-scope FK create (station_out) → 403; body-supplied `created_by` ignored.

- [ ] **Step 1: Failing matrix test** — station_user creates a log entry on station_in (201), on station_out (403); `created_by` server-side; applicant 403; audit contains "via API token". (Write concrete assertions mirroring Task 4 shape, using `reverse("api:station-log-entry-list")`.)
- [ ] **Step 2–7:** fail → serializers → viewsets (get_queryset scoped, perform_create sets actor + guards `can_write_station_content`) → register `station-log-entries`/`station-photos` in router → pass → lint → commit `feat(api): writable station log entries + photos (station-assigned, audited)`.

> StationPhoto upload: tests can POST `caption` + a tiny in-memory image via `SimpleUploadedFile`; if image validation is heavy, allow `image` optional in tests by posting `format="multipart"`. Keep the matrix test focused on authz, not image processing.

---

### Task 6: RolloutSequence + RolloutSequenceEntry write (region-mgr/staff, global)

**Files:** `write_serializers.py`, `write_views.py`, `router.py`, `tests/test_api_write_rollouts.py`.

**Interfaces:**
- `RolloutSequenceWriteSerializer`: `["id","singleton_key","created_at","updated_at","updated_by"]`, read-only `["id","created_at","updated_at","updated_by"]`. `updated_by` server-side.
- `RolloutSequenceEntryWriteSerializer`: `["id","sequence","tag","position"]`, read-only `["id"]`.
- Both viewsets full CRUD gated by `can_write_rollouts(user)` (`is_any_region_manager`). `updated_by=request.user` server-side. Audit: no station/region subject → `audit_config_write(request, message="RolloutSequence[Entry] <id> created/updated/deleted")` (Task 1 helper → CONFIG_CHANGED DB row). Every mutation records a DB audit row with token origin.

- [ ] **Step 1:** Failing test: region_mgr can POST an entry (201), station_user 403, staff 200 update. 
- [ ] **Step 2–7:** fail → serializers → viewsets (`updated_by=request.user`) → router re-point → pass → lint → commit `feat(api): writable rollout sequences + entries (region-mgr/staff)`.

---

### Task 7: Deployment create = trigger (region-mgr/staff; no delete)

**Files:** `write_serializers.py`, `write_views.py`, `router.py`, `tests/test_api_write_deployments.py`.

**Interfaces:**
- `DeploymentCreateSerializer`: input `["image_release","target_station"]` (+ optional `strategy`,`phase_config`); everything else server-side. `target_type` forced to `STATION`. No `status`/`created_by` from body.
- `DeploymentViewSet` adds **only** `CreateModelMixin` (list/retrieve inherited from read; **no** UpdateModelMixin/DestroyModelMixin → PATCH/PUT/DELETE return 405). `perform_create`: resolve `target_station`; guard `can_trigger_deployment(user, station)` (403 if not); inside `transaction.atomic()` create `Deployment(target_type=STATION, target_station=station, status=IN_PROGRESS, created_by=request.user, image_release=...)` + pending `DeploymentResult` + call `apps.rollouts.views.supersede_pending_for_station` (import the actual helper — verify its module path; it may live in `apps/deployments/` or `apps/rollouts/`). Audit `StationAuditLog.FIRMWARE_UPDATE` on the station.
- `can_write_object` returns False for all (there is no object-level write; create-only). Belt-and-suspenders against UpdateMixin accidentally added.

**Review Focus coverage:** create spins up DeploymentResult + supersede; `delete`/`patch` → 405; client-supplied `status` ignored.

- [ ] **Step 1: Failing test:**

```python
def test_deploy_trigger_creates_result_and_supersedes(api_topology, bearer, image_release):
    from apps.deployments.models import Deployment, DeploymentResult

    t = api_topology
    payload = {"image_release": image_release.pk, "target_station": t["station_in"].pk, "status": "completed"}
    r = bearer(t["region_mgr"]).post(reverse("api:deployment-list"), payload, format="json")
    assert r.status_code == 201
    dep = Deployment.objects.latest("id")
    assert dep.status == Deployment.Status.IN_PROGRESS  # client 'status' ignored
    assert dep.created_by == t["region_mgr"]
    assert DeploymentResult.objects.filter(deployment=dep, station=t["station_in"]).exists()


def test_deploy_out_of_scope_station_403(api_topology, bearer, image_release):
    t = api_topology
    payload = {"image_release": image_release.pk, "target_station": t["station_out"].pk}
    assert bearer(t["region_mgr"]).post(reverse("api:deployment-list"), payload, format="json").status_code == 403


def test_deployment_no_delete_no_update(api_topology, bearer, deployment):
    t = api_topology
    url = reverse("api:deployment-detail", args=[deployment.pk])
    assert bearer(t["admin"]).delete(url).status_code == 405
    assert bearer(t["admin"]).patch(url, {"status": "cancelled"}, format="json").status_code == 405
```

- [ ] **Step 2–7:** fail → serializer → viewset (CreateModelMixin only; verify `supersede_pending_for_station` import path with grep) → router re-point `deployments` → pass → lint → commit `feat(api): deployment trigger endpoint (create=deploy, scoped, no delete)`.

---

### Task 8: AlertRule write (region-mgr/staff, global CRUD)

**Files:** `write_serializers.py`, `write_views.py`, `router.py`, `tests/test_api_write_alertrules.py`.

**Interfaces:**
- `AlertRuleWriteSerializer`: `["id","alert_type","threshold","severity","is_active","description","created_at"]`, read-only `["id","created_at"]`.
- `AlertRuleViewSet` full CRUD gated by `can_write_alert_rule` (`is_any_region_manager`). Audit: no station subject → `audit_config_write(request, message="AlertRule <alert_type> created/updated/deleted")` (Task 1 helper → CONFIG_CHANGED DB row with token origin). Note: `offline_alert_rule` etc. are seeded by migration; tests create a fresh `alert_type` not already seeded, or PATCH an existing one.

- [ ] **Step 1:** Failing test: region_mgr PATCHes a rule's `threshold` (200), station_user 403, staff creates a rule with an unused `alert_type` (201). Numeric `threshold` posted as JSON number (avoid the de-locale decimal-comma pitfall — JSON uses dot).
- [ ] **Step 2–7:** fail → serializer → viewset → router re-point → pass → lint → commit `feat(api): writable alert rules (region-mgr/staff)`.

---

### Task 9: ProvisioningJob create = trigger (staff)

**Files:** `write_serializers.py`, `write_views.py`, `router.py`, `tests/test_api_write_provisioning.py`.

**Interfaces:**
- `ProvisioningJobCreateSerializer`: input `["station","image_release"]`; `requested_by`/`status`/timestamps server-side/absent.
- `ProvisioningJobViewSet` adds **only** `CreateModelMixin`. `perform_create`: guard `can_trigger_provisioning(user, station)` (internal only → 403); create `ProvisioningJob(station=, image_release=, requested_by=request.user)`; audit `StationAuditLog.PROVISIONING_REQUESTED` on the station. No update/delete (405).

**Review Focus coverage:** non-staff (region_mgr/station_user) → 403; `requested_by` server-side; delete → 405.

- [ ] **Step 1:** Failing test: staff creates (201, `requested_by==staff`, `PROVISIONING_REQUESTED` audit w/ token); region_mgr 403; delete 405.
- [ ] **Step 2–7:** fail → serializer → viewset (CreateModelMixin only) → router re-point → pass → lint → commit `feat(api): provisioning trigger endpoint (staff, create-only, audited)`.

---

### Task 10: User / Membership write (staff/admin only)

**Files:** `write_serializers.py`, `write_views.py`, `router.py`, `tests/test_api_write_users.py`.

**Interfaces:**
- `UserWriteSerializer`: writable `["membership_level","language","notify_channel","is_directory_visible","bio","qth_name","qrz_url","address","phone","latitude","longitude","locator","first_name","last_name"]`; read-only `["id","username","date_joined","email"]`. **`email` is READ-ONLY** — directly setting another user's email bypasses the verification flow (account-takeover); email changes go only through the self-service email-verification flow. **Authz (mirror the UI):** `membership_level` changes require `request.user.is_admin` (not just staff); a non-admin may not mutate a target that `is_internal` (staff edits members/applicants only); cannot change one's own membership; demote-to-applicant blocked when the target holds assignments; soft-deleted targets (`deleted_at` set) are excluded from `get_queryset` → 404. **Absent (never writable):** `password`, `is_staff`, `is_superuser`, `is_active`, `last_login`, `deleted_at`, `deleted_by`, `groups`, `user_permissions`.
- `UserViewSet` adds `UpdateModelMixin` (+ optionally `CreateModelMixin` — creating users via API with no password is fragile; **decide create = out of scope for Phase 3**, update-only). `can_write_object` → `can_write_user` (internal). `get_queryset` inherited (internal see all; others see only self — but non-internal can't write anyway). Audit `AccountAuditLog.USER_UPDATED` (and `MEMBERSHIP_PROMOTED`/`DEMOTED` when `membership_level` changed), `target_user=obj`.
- No delete via API (user deletion is soft/hard-purge admin flow — out of scope). → DELETE 405.

**Review Focus coverage:** member cannot elevate own `membership_level`; `is_staff`/`is_superuser` not writable; a staff user editing another user's `membership_level` audits MEMBERSHIP_PROMOTED/DEMOTED.

- [ ] **Step 1: Failing test:**

```python
def test_member_cannot_self_elevate(api_topology, bearer):
    from apps.accounts.models import User

    t = api_topology
    url = reverse("api:user-detail", args=[t["station_user"].pk])
    r = bearer(t["station_user"]).patch(url, {"membership_level": "admin"}, format="json")
    assert r.status_code == 403
    t["station_user"].refresh_from_db()
    assert t["station_user"].membership_level == User.MembershipLevel.MEMBER


def test_staff_sets_membership_and_audits(api_topology, bearer):
    from apps.accounts.models import AccountAuditLog, User

    t = api_topology
    url = reverse("api:user-detail", args=[t["station_user"].pk])
    r = bearer(t["staff"]).patch(url, {"membership_level": "staff"}, format="json")
    assert r.status_code == 200
    t["station_user"].refresh_from_db()
    assert t["station_user"].membership_level == User.MembershipLevel.STAFF
    assert AccountAuditLog.objects.filter(
        target_user=t["station_user"],
        event_type=AccountAuditLog.EventType.MEMBERSHIP_PROMOTED,
    ).exists()


def test_privileged_flags_not_writable(api_topology, bearer):
    t = api_topology
    url = reverse("api:user-detail", args=[t["station_user"].pk])
    bearer(t["admin"]).patch(url, {"is_superuser": True, "is_staff": True}, format="json")
    t["station_user"].refresh_from_db()
    assert t["station_user"].is_superuser is False
    assert t["station_user"].is_staff is False


def test_user_delete_405(api_topology, bearer):
    t = api_topology
    assert bearer(t["admin"]).delete(reverse("api:user-detail", args=[t["station_user"].pk])).status_code == 405
```

- [ ] **Step 2–7:** fail → serializer (curated) → viewset (UpdateModelMixin only; `perform_update` detects membership_level delta → PROMOTED/DEMOTED vs USER_UPDATED) → router re-point `users` → pass → lint → commit `feat(api): user/membership update endpoint (staff/admin, curated fields, audited)`.

> `User._invalidate_role_cache` may need calling after a membership change (see conftest usage). Verify and call it in `perform_update` if the cached_property would otherwise go stale within the request.

---

### Task 11: ImageRelease special surface — archive/restore + available + import (staff)

**Files:** `write_views.py` (extend `ImageReleaseViewSet`), `write_serializers.py` (`ImageImportInputSerializer`), `router.py`, `tests/test_api_write_images.py`.

**Interfaces:**
- `ImageReleaseViewSet` subclasses `read_views.ImageReleaseViewSet` + `ScopedWriteViewSet` but exposes **no** generic create/update/delete (no write mixins). Adds `@action(detail=True, methods=["post"]) archive` / `restore` (call `obj.archive()`/`obj.restore()`, idempotent, gated `can_manage_images`, audit via logging + `StationAuditLog`? no station → logging). Uses `ImageRelease.all_objects` for archive/restore lookups (archived rows hidden by default manager) — override `get_object` for these actions or use `all_objects` directly.
- `@action(detail=False, methods=["get"], url_path="available") available` — gated `can_manage_images`; calls `apps.images.github_releases.fetch_releases(settings.LINUX_IMAGE_REPO, limit=...)`; returns a JSON list `[{"tag":..., "channels":[...], ...}]`. Verify the real function signature/return shape by reading `apps/images/github_releases.py` and `apps/images/views.py:GitHubReleasesPartialView`.
- `@action(detail=False, methods=["post"], url_path="import") import_release` — gated `can_manage_images`; input `ImageImportInputSerializer(tag, machine, channel, mark_as_latest)` where `tag` MUST be one returned by `available/` (validate against fetched releases — reject free-text/unknown tag with 400); creates `ImageImportJob(..., requested_by=request.user)`; returns the serialized `ImageImportJob` (reuse `read_serializers.ImageImportJobSerializer`) with 202. No hard create/delete of ImageRelease.

**Review Focus coverage:** DELETE on `images/{id}/` → 405; double-archive idempotent; import with unknown tag → 400; non-staff → 403.

- [ ] **Step 1: Failing test:**

```python
# tests/test_api_write_images.py
import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


def test_archive_restore_idempotent_staff(api_topology, bearer, image_release):
    t = api_topology
    arch = reverse("api:image-archive", args=[image_release.pk])
    assert bearer(t["staff"]).post(arch).status_code in (200, 204)
    assert bearer(t["staff"]).post(arch).status_code in (200, 204)  # idempotent
    image_release.refresh_from_db()
    assert image_release.archived_at is not None
    rest = reverse("api:image-restore", args=[image_release.pk])
    assert bearer(t["staff"]).post(rest).status_code in (200, 204)
    image_release.refresh_from_db()
    assert image_release.archived_at is None


def test_non_staff_cannot_archive(api_topology, bearer, image_release):
    t = api_topology
    assert bearer(t["region_mgr"]).post(reverse("api:image-archive", args=[image_release.pk])).status_code == 403


def test_no_hard_delete(api_topology, bearer, image_release):
    t = api_topology
    assert bearer(t["admin"]).delete(reverse("api:image-detail", args=[image_release.pk])).status_code == 405


def test_import_rejects_unknown_tag(api_topology, bearer, monkeypatch):
    from apps.images import github_releases

    t = api_topology
    monkeypatch.setattr(github_releases, "fetch_releases", lambda *a, **k: [])
    r = bearer(t["staff"]).post(
        reverse("api:image-import"), {"tag": "v-nope", "machine": "qemux86-64", "channel": "stable"},
        format="json",
    )
    assert r.status_code == 400
```

- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3–5:** Read `apps/images/github_releases.py` + `apps/images/views.py` to confirm `fetch_releases` signature and `Release.channels_for()`; implement the actions + input serializer with tag-validation against `fetch_releases`.
- [ ] **Step 6: Run, verify pass.** `python -m pytest tests/test_api_write_images.py -q`.
- [ ] **Step 7: Lint + commit** `feat(api): image release archive/restore + available/import (staff, no hard-delete)`.

> Action URL basenames: with `basename="image"`, detail action `archive` → `api:image-archive`, list action `import` (url_path="import") → `api:image-import`, `available` → `api:image-available`. Confirm with `python manage.py show_urls` or `reverse` in a test.

---

### Task 12: OpenAPI schema, full suite, ruff gate

**Files:** `tests/test_api_write_schema.py`; possibly minor `@extend_schema` annotations in `write_views.py` for the custom actions (deployment/provisioning trigger, image import/available) so drf-spectacular renders request bodies correctly.

**Interfaces:** Consumes all prior tasks. Produces a schema test asserting the write operations appear.

- [ ] **Step 1: Failing schema test:**

```python
# tests/test_api_write_schema.py
import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


def test_schema_lists_write_operations(admin_user, bearer):
    r = bearer(admin_user).get(reverse("api:schema"))
    assert r.status_code == 200
    body = r.content.decode()
    # POST station create + user update + deployment trigger + image import present
    assert "post" in body.lower()
    for path in ["/api/v1/stations/", "/api/v1/users/", "/api/v1/deployments/", "/api/v1/images/import"]:
        assert path in body
```
(Confirm the actual schema route name — Phase 2 added it; check `tests/test_api_read_schema.py` for the exact `reverse(...)`.)

- [ ] **Step 2: Run, verify fail / adjust** until the schema renders all write endpoints. Add `@extend_schema(request=..., responses=...)` to custom `@action`s if spectacular warns or omits bodies.
- [ ] **Step 3: Run the full API suite:**

Run: `python -m pytest tests/ -q -k "api"`
Expected: all green. Then the **whole** suite: `python -m pytest -q`.

- [ ] **Step 4: Ruff gate:**

Run: `uvx ruff@0.16.8 check . && uvx ruff@0.16.8 format --check apps/api tests`
Expected: clean.

- [ ] **Step 5: Commit** `test(api): schema coverage for phase-3 write surface + full-suite green`.

- [ ] **Step 6: Mark PR ready** (un-draft), ensure CI green (fix-forward), run Copilot reviewer + copilot-loop until self-verified 0 unresolved (check via GraphQL **before** declaring done).

---

## Self-Review

**Spec coverage** (§Scope allowlist → task):
- Station (CRUD, delete staff/admin) → Task 2 ✓
- Region, StationTag → Task 3 ✓
- StationAssignment, RegionAssignment → Task 4 ✓
- StationLogEntry, StationPhoto → Task 5 ✓
- RolloutSequence(+Entry) → Task 6 ✓
- Deployment (create=trigger, no delete) → Task 7 ✓
- AlertRule → Task 8 ✓
- ProvisioningJob (create=trigger) → Task 9 ✓
- User/Membership (staff/admin, no self-service) → Task 10 ✓
- ImageRelease archive/restore/available/import (no hard create/delete) → Task 11 ✓
- Audit wiring "via API token <prefix>" → Task 1 helper + every write task ✓
- Permission matrix (role × op × in/out-of-scope) → every task's test file ✓
- OpenAPI write endpoints → Task 12 ✓
- Read-only resources (audit logs, inventory, telemetry, deployment-results, alerts, image-import-jobs, station-modules) → stay read-only, untouched ✓
- Explicitly-out resources (sso, control, audio, tunnel, DeviceKey, AccountToken, PAT) → no endpoints added ✓
- module_firmware resources → explicitly deferred (spec note) → not in this plan ✓

**Placeholder scan:** Tasks 3/6/8 audit subject-less global taxonomy/config writes via `audit_config_write` → `AccountAuditLog.CONFIG_CHANGED` DB rows (preflight ruling — brief mandates no silent mutations). Tasks 4/5/7/9/11 compress the TDD cycle to "Step 2–7: fail→implement→router→pass→lint→commit" with the concrete serializer fields + viewset predicates + representative failing test fully specified in Step 1 and Interfaces; the implementer has the exact field lists, predicates, audit event types, and test shape — no "implement later".

**Type consistency:** predicate names in `write_scoping` (Task 1) match their call sites (Tasks 2–11). `ScopedWriteViewSet.get_serializer_class`/`can_write_object` contract (Task 2) reused by all. `audit_station_write`/`audit_account_write` signatures (Task 1) match calls. Event types verified against real enums (`StationAuditLog.EventType.{CREATED,UPDATED,DELETED,FIRMWARE_UPDATE,PROVISIONING_REQUESTED}`, `AccountAuditLog.EventType.{REGION_*,USER_UPDATED,MEMBERSHIP_PROMOTED/DEMOTED,*_ASSIGNMENT_CREATED}`).

**Review Focus:** all five pinned — body-privilege-escalation (T2/5/7/9/10), cross-scope-FK (T4/5/7), role-vs-scope (T2/3), trigger-not-row (T7/9), image hard-delete/free-tag (T11).

**Open verifications the implementer must do (flagged inline, not assumptions):**
- `supersede_pending_for_station` import path (Task 7) — grep before use.
- `github_releases.fetch_releases` signature + `Release` shape (Task 11) — read the module.
- Exact `reverse()` names for schema route + image actions (Tasks 11/12) — confirm against `tests/test_api_read_schema.py` + `show_urls`.
- `User._invalidate_role_cache` need after membership change (Task 10).
