# User/Automation API — Phase 2 (Read-Fläche) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose every Phase-2 allowlist resource as a scope-filtered, read-only REST endpoint (list + retrieve) under `/api/v1/`, with a permission model that exactly mirrors membership + topology access.

**Architecture:** DRF `DefaultRouter` under `/api/v1/` with one `ReadOnlyModelViewSet` per resource, each using an **explicit** serializer (never `fields="__all__"`). A single `TopologyScopedPermission` gates authentication/membership; per-view `get_queryset()` does the scope filtering so out-of-scope objects are invisible in both list and detail (detail → 404, no existence leak). Cross-app scope querysets live in `apps/api/scoping.py`, built on the existing `apps/stations/scoping.py` helpers (single source of truth). Station-bound OneToOne / child resources (telemetry, inventory, log-entries, photos, modules) are nested under `/api/v1/stations/{id}/…` as DRF detail `@action`s.

**Tech Stack:** Django 6.0, DRF 3.18, `django-filter` (new dep), `drf-spectacular` (already wired), PostgreSQL, pytest + pytest-django.

**Spec:** `docs/superpowers/specs/2026-10-01-user-automation-api-design.md` (§Scope read-only table + §Autorisierung + §Ressourcen-Layout). This plan additionally pulls **StationTelemetry** into the read allowlist per project memory `feature/user-automation-api/phase2-followups` (merged in #150).

## Global Constraints

- **Dependency floors (newest stable):** `django-filter>=26.0,<27.0` (26.1 is current). Pin via `requirements/base.in`; recompile all three lockfiles with the exact CI commands (Task 1). CI fails if `requirements/*.txt` drift from `*.in`.
- **No `fields="__all__"`** in any serializer — every field listed explicitly; sensitive fields structurally absent.
- **Never expose** (per model map): `User.password`, `User.last_login`, `User.is_staff`, `User.is_superuser`, `User.is_active`, `User.deleted_at`, `User.deleted_by`, any `*token_hash*`/`secret_hash`/`*_s3_key`/`cosign_bundle_s3_key`, `StationTelemetry.power_alerted_boot_id`, `StationTelemetry.alerted_io_error_count`.
- **Scope filtering is in `get_queryset()`** for every viewset; out-of-scope detail access returns **404**, not 403 (no existence leak). `TopologyScopedPermission.has_object_permission` may trust the queryset.
- **Applicants and anonymous users get nothing** (mirrors `_ApplicantForbiddenMixin`): 401 for missing/invalid token, 403 for authenticated applicant.
- **Auth classes per viewset:** `[PersonalAccessTokenAuthentication, SessionAuthentication]` — explicitly NOT `DeviceKeyAuthentication` (device auth resolves `request.user` to a `DeviceKey`, not a real user). Throttle scope `api-token`.
- **Tests:** top-level `tests/test_*.py` (project convention), run `python -m pytest -q`. Multi-line Django template comments forbidden (N/A here — no templates).
- **Squash-merge; one PR for the whole phase.** Commit per task on `feature/user-automation-api-phase2`.
- **Audit logs out of scope for Phase 2:** The spec's read-only table lists `StationAuditLog`/`AccountAuditLog`, but the coordinator's Phase-2 resource list (brief) deliberately omits them. Follow the brief — audit-log read endpoints are deferred to a later phase.

## Review Focus

Inputs/failure modes the spec implies but that a naive happy-path test suite would miss (each is pinned to a task below):

- **Out-of-scope detail retrieve must 404, not 403** — a station-assigned user requesting `/stations/{other_id}/` must not learn the object exists. (Task 2 tests; repeated for every scoped resource.)
- **Nested sub-resource leaks parent scope** — `/stations/{out_of_scope_id}/telemetry/` must 404 before touching telemetry; `get_object()` on the parent must be the scoped one. (Task 3 tests.)
- **Sensitive fields silently present** — assert specific forbidden keys are absent from each serializer's output (User secrets, S3 keys, token hashes, telemetry alerting bookkeeping). (Tasks 3, 8, 9 tests.)
- **Deployment with `target_type=all`/`tag` visibility** — a region-manager must see a fleet-wide/tag deployment **iff** it touched one of their stations (via `results`), not merely because it exists. (Task 6 tests.)
- **DeviceKey bearer / session cross-contamination** — a station agent's `DeviceKey` auth (or an unauthenticated request) must never satisfy `TopologyScopedPermission`; only a real user with ≥member does. (Task 1 tests.)

---

## File Structure

**New files (`apps/api/`):**
- `apps/api/scoping.py` — cross-app scope querysets (`accessible_deployments`, `accessible_deployment_results`, `accessible_alerts`, `accessible_provisioning_jobs`), built on `apps.stations.scoping`.
- `apps/api/pagination.py` — `StandardResultsSetPagination`.
- `apps/api/read_serializers.py` — all Phase-2 read serializers (explicit fields).
- `apps/api/read_views.py` — all `ReadOnlyModelViewSet`s + base `ScopedReadOnlyViewSet` + the `StationViewSet` nested `@action`s.
- `apps/api/read_filters.py` — `django-filter` `FilterSet`s.
- `apps/api/router.py` — `DefaultRouter` registration (imported by `urls.py`).

**Modified files:**
- `requirements/base.in` (+`django-filter`), `requirements/{base,prod,dev}.txt` (recompiled).
- `config/settings/base.py` — add `django_filters` to `INSTALLED_APPS`; add `DEFAULT_FILTER_BACKENDS`, `DEFAULT_PAGINATION_CLASS`, `PAGE_SIZE` to `REST_FRAMEWORK`.
- `apps/api/permissions.py` — add `TopologyScopedPermission`.
- `apps/api/urls.py` — include the read router (ordering: device/legacy explicit paths first, router last); remove the legacy inventory path (consolidated in Task 3).
- `apps/api/views.py` — remove `StationInventoryView` (migrated to `StationViewSet.inventory` action in Task 3).
- `tests/test_heartbeat_inventory.py` — update inventory-read assertions to the consolidated endpoint (Task 3).

**New test files:**
- `tests/test_api_read_fixtures.py` — shared role/user + in/out-of-scope object factory helpers reused by every resource test.
- `tests/test_api_read_<domain>.py` per task (stations, nested, assignments, rollouts, deployments, monitoring, provisioning_images, users, schema).

---

### Task 1: Dependency, settings, base permission & infrastructure

Establishes `django-filter`, pagination, the scope-permission, and the shared base viewset. Everything downstream consumes these.

**Files:**
- Modify: `requirements/base.in`
- Modify (regenerate): `requirements/base.txt`, `requirements/prod.txt`, `requirements/dev.txt`
- Modify: `config/settings/base.py` (INSTALLED_APPS + REST_FRAMEWORK)
- Create: `apps/api/pagination.py`
- Create: `apps/api/scoping.py`
- Modify: `apps/api/permissions.py`
- Test: `tests/test_api_read_fixtures.py` (fixtures), `tests/test_api_permission_scope.py` (permission unit tests)

**Interfaces:**
- Produces: `apps.api.pagination.StandardResultsSetPagination`.
- Produces: `apps.api.permissions.TopologyScopedPermission` (DRF `BasePermission`).
- Produces: `apps.api.scoping.accessible_deployments(user)`, `accessible_deployment_results(user)`, `accessible_alerts(user)`, `accessible_provisioning_jobs(user)` → each a `QuerySet`.
- Produces test helpers in `tests/test_api_read_fixtures.py`: `make_role_users()`, `make_scoped_fixture()`, `auth(client, token_or_user)` — see Step 1.

- [ ] **Step 1: Write shared test fixtures (`tests/test_api_read_fixtures.py`)**

```python
"""Reusable role users + in/out-of-scope topology for API read tests."""
import pytest
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.api.models import PersonalAccessToken
from apps.stations.models import Region, Station, StationAssignment, RegionAssignment


def _user(username, level):
    return User.objects.create_user(
        username=username, password="x", membership_level=level
    )


@pytest.fixture
def topology(db):
    """Two regions/stations: 'in' (what the scoped user may see) and 'out'."""
    region_in = Region.objects.create(name="In", slug="in")
    region_out = Region.objects.create(name="Out", slug="out")
    station_in = Station.objects.create(name="S-in", callsign="OE1AAA", region=region_in)
    station_out = Station.objects.create(name="S-out", callsign="OE1BBB", region=region_out)

    admin = _user("admin", User.MembershipLevel.ADMIN)
    staff = _user("staff", User.MembershipLevel.STAFF)
    region_mgr = _user("rmgr", User.MembershipLevel.MEMBER)
    RegionAssignment.objects.create(user=region_mgr, region=region_in, role="manager")
    station_user = _user("suser", User.MembershipLevel.MEMBER)
    StationAssignment.objects.create(user=station_user, station=station_in, role="maintainer")
    applicant = _user("appl", User.MembershipLevel.APPLICANT)

    return {
        "region_in": region_in, "region_out": region_out,
        "station_in": station_in, "station_out": station_out,
        "admin": admin, "staff": staff, "region_mgr": region_mgr,
        "station_user": station_user, "applicant": applicant,
    }


def bearer(user):
    """Return an APIClient authenticated as `user` via a personal access token."""
    token, raw = PersonalAccessToken.issue(user=user, name="test")
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
    return client


def anon_client():
    return APIClient()
```

- [ ] **Step 2: Run fixtures import to verify it collects**

Run: `python -m pytest tests/test_api_read_fixtures.py -q`
Expected: PASS (0 tests, no import/collection error). If `PersonalAccessToken.issue` signature differs, read `apps/api/models.py` and adjust the helper.

- [ ] **Step 3: Write failing permission tests (`tests/test_api_permission_scope.py`)**

```python
import pytest
from rest_framework.test import APIRequestFactory

from apps.api.permissions import TopologyScopedPermission
from tests.test_api_read_fixtures import topology  # noqa: F401


@pytest.fixture
def perm():
    return TopologyScopedPermission()


class _View:
    pass


@pytest.mark.django_db
@pytest.mark.parametrize("role,allowed", [
    ("admin", True), ("staff", True), ("region_mgr", True),
    ("station_user", True), ("applicant", False),
])
def test_has_permission_by_role(perm, topology, role, allowed):
    req = APIRequestFactory().get("/")
    req.user = topology[role]
    assert perm.has_permission(req, _View()) is allowed


@pytest.mark.django_db
def test_anonymous_denied(perm):
    from django.contrib.auth.models import AnonymousUser
    req = APIRequestFactory().get("/")
    req.user = AnonymousUser()
    assert perm.has_permission(req, _View()) is False


@pytest.mark.django_db
def test_devicekey_auth_denied(perm, topology):
    """A DeviceKey principal (no membership_level) must not pass."""
    from apps.api.models import DeviceKey
    req = APIRequestFactory().get("/")
    req.user = DeviceKey(station=topology["station_in"])  # not a real user
    assert perm.has_permission(req, _View()) is False
```

- [ ] **Step 4: Run to verify failure**

Run: `python -m pytest tests/test_api_permission_scope.py -q`
Expected: FAIL — `ImportError: cannot import name 'TopologyScopedPermission'`.

- [ ] **Step 5: Implement `TopologyScopedPermission` (`apps/api/permissions.py`)**

Append:

```python
from apps.accounts.models import User as _User


class TopologyScopedPermission(BasePermission):
    """Gate the user/automation API: authenticated, real user, not applicant.

    Object-level scope is enforced by each view's ``get_queryset()`` (out-of-
    scope objects are absent → detail 404), so ``has_object_permission``
    trusts the queryset. Applicants mirror ``_ApplicantForbiddenMixin`` and
    get nothing; a DeviceKey principal (no ``membership_level``) is rejected.
    """

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        if not (user and getattr(user, "is_authenticated", False)):
            return False
        level = getattr(user, "membership_level", None)
        if level is None:  # e.g. DeviceKey principal
            return False
        return level != _User.MembershipLevel.APPLICANT

    def has_object_permission(self, request, view, obj):
        return True
```

- [ ] **Step 6: Run permission tests to verify pass**

Run: `python -m pytest tests/test_api_permission_scope.py -q`
Expected: PASS (all parametrized + anon + devicekey).

- [ ] **Step 7: Implement pagination (`apps/api/pagination.py`)**

```python
from rest_framework.pagination import PageNumberPagination


class StandardResultsSetPagination(PageNumberPagination):
    """Default paginator for the user/automation read API."""

    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 200
```

- [ ] **Step 8: Implement cross-app scope helpers (`apps/api/scoping.py`)**

```python
"""Cross-app scope querysets for the user/automation API.

Built on apps.stations.scoping (single source of truth). Imports models at
call time to avoid import cycles (stations must not depend on these apps).
"""

from apps.stations.scoping import accessible_stations


def accessible_deployments(user):
    from apps.deployments.models import Deployment
    from django.db.models import Q

    if getattr(user, "is_internal", False):
        return Deployment.objects.all()
    stations = accessible_stations(user)
    return Deployment.objects.filter(
        Q(target_station__in=stations) | Q(results__station__in=stations)
    ).distinct()


def accessible_deployment_results(user):
    from apps.deployments.models import DeploymentResult

    if getattr(user, "is_internal", False):
        return DeploymentResult.objects.all()
    return DeploymentResult.objects.filter(station__in=accessible_stations(user))


def accessible_alerts(user):
    from apps.monitoring.models import Alert

    if getattr(user, "is_internal", False):
        return Alert.objects.all()
    return Alert.objects.filter(station__in=accessible_stations(user))


def accessible_provisioning_jobs(user):
    from apps.provisioning.models import ProvisioningJob

    if getattr(user, "is_internal", False):
        return ProvisioningJob.objects.all()
    return ProvisioningJob.objects.filter(station__in=accessible_stations(user))
```

- [ ] **Step 9: Write failing scope-helper tests (append to `tests/test_api_permission_scope.py`)**

```python
@pytest.mark.django_db
def test_accessible_deployments_scopes_by_results(topology):
    from apps.api.scoping import accessible_deployments
    from apps.deployments.models import Deployment, DeploymentResult
    from apps.images.models import ImageRelease

    rel = ImageRelease.objects.create(tag="v1", machine="qemux86-64", sha256="a" * 64)
    dep_all = Deployment.objects.create(image_release=rel, target_type="all")
    # touches only the out-of-scope station
    DeploymentResult.objects.create(deployment=dep_all, station=topology["station_out"])

    mgr = topology["region_mgr"]
    assert dep_all not in accessible_deployments(mgr)

    DeploymentResult.objects.create(deployment=dep_all, station=topology["station_in"])
    assert dep_all in accessible_deployments(mgr)
    assert dep_all in accessible_deployments(topology["admin"])
```

- [ ] **Step 10: Run to verify pass**

Run: `python -m pytest tests/test_api_permission_scope.py -q`
Expected: PASS. (If `ImageRelease`/`Deployment` required fields differ, consult the model map / `apps/*/models.py` and adjust.)

- [ ] **Step 11: Add `django-filter` dependency + settings**

Edit `requirements/base.in` — add after the `drf-spectacular-sidecar` block:

```
# django-filter backs DjangoFilterBackend for the user/automation read API.
django-filter>=26.0,<27.0
```

Recompile (exact CI commands):

```bash
uv pip compile requirements/base.in -o requirements/base.txt --python-version 3.14 --no-strip-extras
uv pip compile requirements/prod.in -c requirements/base.txt -o requirements/prod.txt --python-version 3.14 --no-strip-extras
uv pip compile requirements/dev.in -c requirements/base.txt -o requirements/dev.txt --python-version 3.14 --no-strip-extras
```

Edit `config/settings/base.py`:
- In `INSTALLED_APPS`, add `"django_filters",` (near the other third-party apps, e.g. after `"drf_spectacular_sidecar"`).
- In `REST_FRAMEWORK`, add:

```python
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_PAGINATION_CLASS": "apps.api.pagination.StandardResultsSetPagination",
    "PAGE_SIZE": 50,
```

- [ ] **Step 12: Verify settings load and lockfiles are in sync**

Run:
```bash
python -c "import django_filters" && python -m pytest tests/test_api_permission_scope.py -q
git diff --exit-code requirements/base.txt >/dev/null && echo "base.txt committed-clean after your edit"
```
Expected: import OK; permission tests still PASS. (The `git diff` line is just a reminder to stage the regenerated lockfiles.)

- [ ] **Step 13: Commit**

```bash
git add requirements/ config/settings/base.py apps/api/pagination.py apps/api/scoping.py apps/api/permissions.py tests/test_api_read_fixtures.py tests/test_api_permission_scope.py
git commit -m "feat(api): read-API foundation — django-filter, pagination, TopologyScopedPermission, scope helpers"
```

---

### Task 2: Stations core (Station, Region, StationTag) + router + base viewset

First resources on the router; establishes the `ScopedReadOnlyViewSet` base and the router wiring. Proves list/retrieve + the out-of-scope-404 property end to end.

**Files:**
- Create: `apps/api/read_serializers.py`
- Create: `apps/api/read_filters.py`
- Create: `apps/api/read_views.py`
- Create: `apps/api/router.py`
- Modify: `apps/api/urls.py`
- Test: `tests/test_api_read_stations.py`

**Interfaces:**
- Consumes: `StandardResultsSetPagination`, `TopologyScopedPermission` (Task 1); `accessible_stations`, `accessible_regions` (`apps.stations.scoping`).
- Produces: `apps.api.read_views.ScopedReadOnlyViewSet` (base: sets `authentication_classes`, `permission_classes`, `throttle_scope`).
- Produces: `StationSerializer`, `RegionSerializer`, `StationTagSerializer`.
- Produces: `apps.api.router.router` (a `DefaultRouter`) with `stations`, `regions`, `station-tags` registered (basenames `station`, `region`, `station-tag`).

- [ ] **Step 1: Write failing tests (`tests/test_api_read_stations.py`)**

```python
import pytest
from django.urls import reverse

from tests.test_api_read_fixtures import topology, bearer, anon_client  # noqa: F401


@pytest.mark.django_db
def test_anon_list_401(topology):
    resp = anon_client().get("/api/v1/stations/")
    assert resp.status_code == 401


@pytest.mark.django_db
def test_applicant_list_403(topology):
    resp = bearer(topology["applicant"]).get("/api/v1/stations/")
    assert resp.status_code == 403


@pytest.mark.django_db
@pytest.mark.parametrize("role,expected_names", [
    ("admin", {"S-in", "S-out"}),
    ("staff", {"S-in", "S-out"}),
    ("region_mgr", {"S-in"}),
    ("station_user", {"S-in"}),
])
def test_station_list_scope(topology, role, expected_names):
    resp = bearer(topology[role]).get("/api/v1/stations/")
    assert resp.status_code == 200
    names = {row["name"] for row in resp.data["results"]}
    assert names == expected_names


@pytest.mark.django_db
def test_out_of_scope_retrieve_is_404_not_403(topology):
    client = bearer(topology["station_user"])
    resp = client.get(f"/api/v1/stations/{topology['station_out'].pk}/")
    assert resp.status_code == 404  # no existence leak


@pytest.mark.django_db
def test_station_serializer_has_no_unexpected_secret_fields(topology):
    resp = bearer(topology["admin"]).get(f"/api/v1/stations/{topology['station_in'].pk}/")
    assert resp.status_code == 200
    assert "is_online" in resp.data  # computed field present
    assert set(resp.data) >= {"id", "name", "callsign", "region", "status"}


@pytest.mark.django_db
def test_region_and_tag_lists(topology):
    c = bearer(topology["region_mgr"])
    assert c.get("/api/v1/regions/").status_code == 200
    # region_mgr sees only region_in
    assert {r["slug"] for r in c.get("/api/v1/regions/").data["results"]} == {"in"}
    assert c.get("/api/v1/station-tags/").status_code == 200
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_api_read_stations.py -q`
Expected: FAIL — 404 for all (`/api/v1/stations/` not routed yet).

- [ ] **Step 3: Implement serializers (`apps/api/read_serializers.py`)**

```python
"""Explicit read-only serializers for the user/automation API (v1).

No ``fields="__all__"`` anywhere — sensitive fields are structurally absent.
"""

from rest_framework import serializers

from apps.stations.models import Region, Station, StationTag


class RegionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Region
        fields = ["id", "name", "slug", "description", "created_at"]


class StationTagSerializer(serializers.ModelSerializer):
    class Meta:
        model = StationTag
        fields = ["id", "name", "slug", "color", "description", "created_at"]


class StationSerializer(serializers.ModelSerializer):
    is_online = serializers.BooleanField(read_only=True)

    class Meta:
        model = Station
        fields = [
            "id", "name", "callsign", "description", "location_name",
            "latitude", "longitude", "altitude", "hardware_revision",
            "region", "tags", "notes", "current_os_version",
            "current_agent_version", "current_image_variant",
            "last_ip_address", "last_seen", "status",
            "current_image_release", "is_online", "created_at", "updated_at",
        ]
```

- [ ] **Step 4: Implement filters (`apps/api/read_filters.py`)**

```python
"""django-filter FilterSets for the user/automation read API."""

import django_filters

from apps.stations.models import Station


class StationFilter(django_filters.FilterSet):
    class Meta:
        model = Station
        fields = {"status": ["exact"], "region": ["exact"], "tags": ["exact"]}
```

- [ ] **Step 5: Implement base viewset + station/region/tag viewsets (`apps/api/read_views.py`)**

```python
"""Read-only viewsets for the user/automation API (v1)."""

from rest_framework.authentication import SessionAuthentication
from rest_framework.viewsets import ReadOnlyModelViewSet

from apps.api.authentication import PersonalAccessTokenAuthentication
from apps.api.permissions import TopologyScopedPermission
from apps.api.read_filters import StationFilter
from apps.api.read_serializers import (
    RegionSerializer,
    StationSerializer,
    StationTagSerializer,
)
from apps.stations.models import StationTag
from apps.stations.scoping import accessible_regions, accessible_stations


class ScopedReadOnlyViewSet(ReadOnlyModelViewSet):
    """Base: bearer/session auth, membership gate, token throttle."""

    authentication_classes = [PersonalAccessTokenAuthentication, SessionAuthentication]
    permission_classes = [TopologyScopedPermission]
    throttle_scope = "api-token"


class StationViewSet(ScopedReadOnlyViewSet):
    serializer_class = StationSerializer
    filterset_class = StationFilter
    search_fields = ["name", "callsign", "location_name"]
    ordering_fields = ["name", "last_seen", "created_at"]

    def get_queryset(self):
        return accessible_stations(self.request.user).order_by("name")


class RegionViewSet(ScopedReadOnlyViewSet):
    serializer_class = RegionSerializer
    search_fields = ["name", "slug"]
    ordering_fields = ["name"]

    def get_queryset(self):
        return accessible_regions(self.request.user).order_by("name")


class StationTagViewSet(ScopedReadOnlyViewSet):
    serializer_class = StationTagSerializer
    search_fields = ["name", "slug"]
    ordering_fields = ["name"]

    def get_queryset(self):
        # Global taxonomy; any ≥member may read (applicants blocked by perm).
        return StationTag.objects.all().order_by("name")
```

- [ ] **Step 6: Implement router (`apps/api/router.py`)**

```python
"""DefaultRouter for the user/automation read API (v1)."""

from rest_framework.routers import DefaultRouter

from apps.api import read_views

router = DefaultRouter()
router.register(r"stations", read_views.StationViewSet, basename="station")
router.register(r"regions", read_views.RegionViewSet, basename="region")
router.register(r"station-tags", read_views.StationTagViewSet, basename="station-tag")
```

- [ ] **Step 7: Wire router into `apps/api/urls.py`**

Add `from apps.api.router import router` and append the router **after** all existing explicit paths (device/legacy paths must resolve first):

```python
urlpatterns = [
    # ... existing explicit paths (health, heartbeat, inventory, deployments include) ...
    path("v1/", include((router.urls, "api"), namespace=None)),
    path("v1/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("v1/docs/", SpectacularSwaggerSplitView.as_view(url_name="api:schema"), name="docs"),
]
```

NOTE: keep `app_name = "api"` at module top. Router URLs then reverse as `api:station-list`, `api:station-detail`, etc. Keep schema/docs last so the router doesn't shadow them (it won't — distinct prefixes — but order is clearest).

- [ ] **Step 8: Run tests to verify pass**

Run: `python -m pytest tests/test_api_read_stations.py -q`
Expected: PASS. If `station-tags` 500s on filtering, confirm `DjangoFilterBackend` is importable (Task 1 Step 11).

- [ ] **Step 9: Commit**

```bash
git add apps/api/read_serializers.py apps/api/read_filters.py apps/api/read_views.py apps/api/router.py apps/api/urls.py tests/test_api_read_stations.py
git commit -m "feat(api): read endpoints for stations, regions, station-tags"
```

---

### Task 3: Station nested sub-resources (telemetry, inventory, log-entries, photos, modules)

Nests the station-bound OneToOne / child resources under `/stations/{id}/…` as detail `@action`s. **Consolidates** the legacy `StationInventoryView` into the `StationViewSet.inventory` action (single source of truth; the legacy view is referenced only by tests, no UI).

**Files:**
- Modify: `apps/api/read_serializers.py` (+5 serializers)
- Modify: `apps/api/read_views.py` (+5 actions on `StationViewSet`)
- Modify: `apps/api/views.py` (remove `StationInventoryView`)
- Modify: `apps/api/urls.py` (remove the legacy `station_inventory` path + its import)
- Modify: `tests/test_heartbeat_inventory.py` (point inventory-read assertions at the consolidated endpoint)
- Test: `tests/test_api_read_station_nested.py`

**Interfaces:**
- Consumes: `StationViewSet` (Task 2), `accessible_stations`.
- Produces: `StationTelemetrySerializer`, `StationInventorySerializer`, `StationLogEntrySerializer`, `StationPhotoSerializer`, `StationModuleSerializer`.
- Produces routes (reverse names via router action): `api:station-telemetry`, `api:station-inventory`, `api:station-log-entries`, `api:station-photos`, `api:station-modules` — all `kwargs={"pk": station_id}`.

- [ ] **Step 1: Write failing tests (`tests/test_api_read_station_nested.py`)**

```python
import pytest

from tests.test_api_read_fixtures import topology, bearer  # noqa: F401


def _mk_children(topology):
    from apps.stations.models import StationLogEntry, StationTelemetry, StationInventory
    from apps.control.models import StationModule
    s = topology["station_in"]
    StationTelemetry.objects.create(station=s, boot_count=3, active_slot="A", image_version="v1")
    StationInventory.objects.create(station=s, data={"cpu": "rp4"})
    StationLogEntry.objects.create(station=s, entry_type="note", title="t", message="m")
    StationModule.objects.create(station=s, slot="1", module_id="fm0", type="fm")


@pytest.mark.django_db
def test_telemetry_nested_in_scope(topology):
    _mk_children(topology)
    pk = topology["station_in"].pk
    resp = bearer(topology["station_user"]).get(f"/api/v1/stations/{pk}/telemetry/")
    assert resp.status_code == 200
    assert resp.data["boot_count"] == 3
    # internal alerting bookkeeping must NOT leak
    assert "power_alerted_boot_id" not in resp.data
    assert "alerted_io_error_count" not in resp.data


@pytest.mark.django_db
def test_telemetry_out_of_scope_parent_404(topology):
    pk = topology["station_out"].pk
    resp = bearer(topology["station_user"]).get(f"/api/v1/stations/{pk}/telemetry/")
    assert resp.status_code == 404


@pytest.mark.django_db
def test_telemetry_missing_is_404(topology):
    pk = topology["station_in"].pk  # no telemetry row created
    resp = bearer(topology["station_user"]).get(f"/api/v1/stations/{pk}/telemetry/")
    assert resp.status_code == 404


@pytest.mark.django_db
def test_inventory_nested(topology):
    _mk_children(topology)
    pk = topology["station_in"].pk
    resp = bearer(topology["station_user"]).get(f"/api/v1/stations/{pk}/inventory/")
    assert resp.status_code == 200
    assert resp.data["data"] == {"cpu": "rp4"}


@pytest.mark.django_db
def test_log_entries_and_modules_lists(topology):
    _mk_children(topology)
    pk = topology["station_in"].pk
    c = bearer(topology["station_user"])
    le = c.get(f"/api/v1/stations/{pk}/log-entries/")
    assert le.status_code == 200 and le.data["count"] == 1
    mods = c.get(f"/api/v1/stations/{pk}/modules/")
    assert mods.status_code == 200 and mods.data["count"] == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_api_read_station_nested.py -q`
Expected: FAIL — nested routes 404.

- [ ] **Step 3: Add the five serializers (`apps/api/read_serializers.py`)**

```python
from apps.stations.models import (  # add to existing stations import
    StationInventory, StationLogEntry, StationPhoto, StationTelemetry,
)
from apps.control.models import StationModule


class StationTelemetrySerializer(serializers.ModelSerializer):
    class Meta:
        model = StationTelemetry
        fields = [
            "id", "station", "data", "boot_id", "boot_count",
            "last_reboot_reason", "last_reboot_at", "uptime_seconds",
            "undervoltage_now", "undervoltage_occurred",
            "throttled_now", "throttled_occurred", "active_slot",
            "image_version", "last_ota_result", "worst_life_time_pct",
            "worst_pre_eol", "io_error_count", "updated_at",
        ]  # excludes power_alerted_boot_id, alerted_io_error_count (internal)


class StationInventorySerializer(serializers.ModelSerializer):
    class Meta:
        model = StationInventory
        fields = ["id", "station", "data", "updated_at"]


class StationLogEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model = StationLogEntry
        fields = ["id", "station", "entry_type", "title", "message",
                  "created_by", "created_at"]


class StationPhotoSerializer(serializers.ModelSerializer):
    class Meta:
        model = StationPhoto
        fields = ["id", "station", "image", "caption", "uploaded_by", "uploaded_at"]


class StationModuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = StationModule
        fields = ["id", "station", "slot", "module_id", "type", "model",
                  "version", "tracked_module", "capability_descriptor",
                  "last_state", "online", "last_seen", "created_at", "updated_at"]
```

- [ ] **Step 4: Add nested actions to `StationViewSet` (`apps/api/read_views.py`)**

Add imports and the five actions:

```python
from rest_framework.decorators import action
from rest_framework.response import Response
from django.shortcuts import get_object_or_404

from apps.api.read_serializers import (
    StationInventorySerializer, StationLogEntrySerializer,
    StationModuleSerializer, StationPhotoSerializer, StationTelemetrySerializer,
)
```

```python
    # --- inside StationViewSet ---
    def _one_to_one(self, request, pk, related_name, serializer_cls):
        station = self.get_object()  # scoped get_queryset → 404 if out of scope
        obj = getattr(station, related_name, None)
        if obj is None:
            from rest_framework.exceptions import NotFound
            raise NotFound()
        return Response(serializer_cls(obj, context={"request": request}).data)

    def _child_list(self, request, manager, serializer_cls):
        qs = manager.all()
        page = self.paginate_queryset(qs)
        ser = serializer_cls(page, many=True, context={"request": request})
        return self.get_paginated_response(ser.data)

    @action(detail=True, url_path="telemetry")
    def telemetry(self, request, pk=None):
        station = self.get_object()
        from apps.stations.models import StationTelemetry
        obj = StationTelemetry.objects.filter(station=station).first()
        if obj is None:
            from rest_framework.exceptions import NotFound
            raise NotFound()
        return Response(StationTelemetrySerializer(obj, context={"request": request}).data)

    @action(detail=True, url_path="inventory")
    def inventory(self, request, pk=None):
        station = self.get_object()
        from apps.stations.models import StationInventory
        obj = StationInventory.objects.filter(station=station).first()
        if obj is None:
            from rest_framework.exceptions import NotFound
            raise NotFound()
        return Response(StationInventorySerializer(obj, context={"request": request}).data)

    @action(detail=True, url_path="log-entries")
    def log_entries(self, request, pk=None):
        station = self.get_object()
        return self._child_list(request, station.log_entries.order_by("-created_at"),
                                StationLogEntrySerializer)

    @action(detail=True, url_path="photos")
    def photos(self, request, pk=None):
        station = self.get_object()
        return self._child_list(request, station.photos.order_by("-uploaded_at"),
                                StationPhotoSerializer)

    @action(detail=True, url_path="modules")
    def modules(self, request, pk=None):
        station = self.get_object()
        return self._child_list(request, station.modules.order_by("slot"),
                                StationModuleSerializer)
```

(The `_one_to_one` helper is optional — the explicit `telemetry`/`inventory` actions above are self-contained; drop `_one_to_one` if unused to avoid dead code.)

- [ ] **Step 5: Consolidate the legacy inventory endpoint**

In `apps/api/views.py`: delete the `StationInventoryView` class (and now-unused imports `get_object_or_404`, `StationInventory` if no longer referenced — verify before removing).

In `apps/api/urls.py`: remove the `StationInventoryView` import and the `path("v1/stations/<int:station_id>/inventory/", …, name="station_inventory")` entry.

In `tests/test_heartbeat_inventory.py`: the 5 `reverse("api:station_inventory", kwargs={"station_id": station.pk})` calls now target the consolidated action. Replace with:
```python
reverse("api:station-inventory", kwargs={"pk": station.pk})
```
and update the auth/response expectations: the consolidated endpoint uses `TopologyScopedPermission` (internal users still allowed; now also scoped station users) and returns `{"id", "station", "data", "updated_at"}`. Adjust assertions that checked `station_id`/internal-only-403 accordingly — an internal user still gets 200 with `data`; a missing inventory still 404. If a test specifically asserted a non-internal 403, change it to assert a scoped member gets 200 for their own station (the improved behavior) or remove if redundant. Keep the test file green.

- [ ] **Step 6: Run both test files**

Run: `python -m pytest tests/test_api_read_station_nested.py tests/test_heartbeat_inventory.py -q`
Expected: PASS. If the heartbeat test writes inventory via a device endpoint and then reads it back, ensure the read path now uses the new reverse name.

- [ ] **Step 7: Commit**

```bash
git add apps/api/read_serializers.py apps/api/read_views.py apps/api/views.py apps/api/urls.py tests/test_api_read_station_nested.py tests/test_heartbeat_inventory.py
git commit -m "feat(api): nested station sub-resources (telemetry/inventory/log-entries/photos/modules); consolidate legacy inventory view"
```

---

### Task 4: Assignments (StationAssignment, RegionAssignment)

Scope: a user sees assignments on stations/regions in their scope **plus their own** assignments; internal sees all.

**Files:**
- Modify: `apps/api/read_serializers.py`, `apps/api/read_views.py`, `apps/api/router.py`
- Test: `tests/test_api_read_assignments.py`

**Interfaces:**
- Produces: `StationAssignmentSerializer`, `RegionAssignmentSerializer`; viewsets `StationAssignmentViewSet`, `RegionAssignmentViewSet` (basenames `station-assignment`, `region-assignment`).

- [ ] **Step 1: Write failing tests (`tests/test_api_read_assignments.py`)**

```python
import pytest
from tests.test_api_read_fixtures import topology, bearer  # noqa: F401


@pytest.mark.django_db
def test_station_assignment_scope(topology):
    # station_user has an assignment on station_in; must see it, not others'
    from apps.stations.models import StationAssignment
    StationAssignment.objects.create(user=topology["admin"], station=topology["station_out"],
                                     role="maintainer")
    c = bearer(topology["station_user"])
    resp = c.get("/api/v1/station-assignments/")
    assert resp.status_code == 200
    stations = {row["station"] for row in resp.data["results"]}
    assert topology["station_out"].pk not in stations
    assert topology["station_in"].pk in stations


@pytest.mark.django_db
def test_region_assignment_scope_admin_sees_all(topology):
    resp = bearer(topology["admin"]).get("/api/v1/region-assignments/")
    assert resp.status_code == 200
    assert resp.data["count"] >= 1


@pytest.mark.django_db
def test_assignment_applicant_403(topology):
    assert bearer(topology["applicant"]).get("/api/v1/station-assignments/").status_code == 403
```

- [ ] **Step 2: Run to verify failure** — Run: `python -m pytest tests/test_api_read_assignments.py -q` → FAIL (404).

- [ ] **Step 3: Add serializers (`apps/api/read_serializers.py`)**

```python
from apps.stations.models import RegionAssignment, StationAssignment  # add to import


class StationAssignmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = StationAssignment
        fields = ["id", "user", "station", "role", "assigned_at", "assigned_by"]


class RegionAssignmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = RegionAssignment
        fields = ["id", "user", "region", "role", "assigned_at", "assigned_by"]
```

- [ ] **Step 4: Add viewsets (`apps/api/read_views.py`)**

```python
from django.db.models import Q
from apps.stations.models import RegionAssignment, StationAssignment
from apps.api.read_serializers import RegionAssignmentSerializer, StationAssignmentSerializer


class StationAssignmentViewSet(ScopedReadOnlyViewSet):
    serializer_class = StationAssignmentSerializer
    filterset_fields = ["station", "user", "role"]
    ordering_fields = ["assigned_at"]

    def get_queryset(self):
        user = self.request.user
        if user.is_internal:
            return StationAssignment.objects.all().order_by("-assigned_at")
        return StationAssignment.objects.filter(
            Q(station__in=accessible_stations(user)) | Q(user=user)
        ).distinct().order_by("-assigned_at")


class RegionAssignmentViewSet(ScopedReadOnlyViewSet):
    serializer_class = RegionAssignmentSerializer
    filterset_fields = ["region", "user", "role"]
    ordering_fields = ["assigned_at"]

    def get_queryset(self):
        user = self.request.user
        if user.is_internal:
            return RegionAssignment.objects.all().order_by("-assigned_at")
        return RegionAssignment.objects.filter(
            Q(region__in=accessible_regions(user)) | Q(user=user)
        ).distinct().order_by("-assigned_at")
```

- [ ] **Step 5: Register (`apps/api/router.py`)**

```python
router.register(r"station-assignments", read_views.StationAssignmentViewSet, basename="station-assignment")
router.register(r"region-assignments", read_views.RegionAssignmentViewSet, basename="region-assignment")
```

- [ ] **Step 6: Run to verify pass** — Run: `python -m pytest tests/test_api_read_assignments.py -q` → PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/api/read_serializers.py apps/api/read_views.py apps/api/router.py tests/test_api_read_assignments.py
git commit -m "feat(api): read endpoints for station/region assignments (scoped + own)"
```

---

### Task 5: Rollouts (RolloutSequence + entries)

Global fleet config (singleton sequence). Readable by any ≥member; entries exposed nested and top-level.

**Files:**
- Modify: `apps/api/read_serializers.py`, `apps/api/read_views.py`, `apps/api/router.py`
- Test: `tests/test_api_read_rollouts.py`

**Interfaces:**
- Produces: `RolloutSequenceSerializer`, `RolloutSequenceEntrySerializer`; viewsets `RolloutSequenceViewSet` (basename `rollout-sequence`, with `entries` action) and `RolloutSequenceEntryViewSet` (basename `rollout-sequence-entry`).

- [ ] **Step 1: Write failing tests (`tests/test_api_read_rollouts.py`)**

```python
import pytest
from tests.test_api_read_fixtures import topology, bearer  # noqa: F401


def _seq(topology):
    from apps.rollouts.models import RolloutSequence, RolloutSequenceEntry
    from apps.stations.models import StationTag
    seq = RolloutSequence.objects.create()
    tag = StationTag.objects.create(name="early", slug="early")
    RolloutSequenceEntry.objects.create(sequence=seq, tag=tag, position=0)
    return seq


@pytest.mark.django_db
def test_rollout_sequence_list_member(topology):
    _seq(topology)
    resp = bearer(topology["station_user"]).get("/api/v1/rollout-sequences/")
    assert resp.status_code == 200 and resp.data["count"] == 1


@pytest.mark.django_db
def test_rollout_sequence_entries_nested(topology):
    seq = _seq(topology)
    resp = bearer(topology["station_user"]).get(f"/api/v1/rollout-sequences/{seq.pk}/entries/")
    assert resp.status_code == 200 and resp.data["count"] == 1


@pytest.mark.django_db
def test_rollout_applicant_403(topology):
    assert bearer(topology["applicant"]).get("/api/v1/rollout-sequences/").status_code == 403
```

- [ ] **Step 2: Run to verify failure** — FAIL (404).

- [ ] **Step 3: Serializers**

```python
from apps.rollouts.models import RolloutSequence, RolloutSequenceEntry


class RolloutSequenceEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model = RolloutSequenceEntry
        fields = ["id", "sequence", "tag", "position"]


class RolloutSequenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = RolloutSequence
        fields = ["id", "singleton_key", "created_at", "updated_at", "updated_by"]
```

- [ ] **Step 4: Viewsets**

```python
from apps.rollouts.models import RolloutSequence, RolloutSequenceEntry
from apps.api.read_serializers import RolloutSequenceEntrySerializer, RolloutSequenceSerializer


class RolloutSequenceViewSet(ScopedReadOnlyViewSet):
    serializer_class = RolloutSequenceSerializer

    def get_queryset(self):
        return RolloutSequence.objects.all().order_by("id")

    @action(detail=True, url_path="entries")
    def entries(self, request, pk=None):
        seq = self.get_object()
        return self._child_list(request, seq.entries.order_by("position"),
                                RolloutSequenceEntrySerializer)


class RolloutSequenceEntryViewSet(ScopedReadOnlyViewSet):
    serializer_class = RolloutSequenceEntrySerializer
    filterset_fields = ["sequence", "tag"]
    ordering_fields = ["position"]

    def get_queryset(self):
        return RolloutSequenceEntry.objects.all().order_by("position")
```

- [ ] **Step 5: Register**

```python
router.register(r"rollout-sequences", read_views.RolloutSequenceViewSet, basename="rollout-sequence")
router.register(r"rollout-sequence-entries", read_views.RolloutSequenceEntryViewSet, basename="rollout-sequence-entry")
```

- [ ] **Step 6: Run to verify pass** — PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/api/read_serializers.py apps/api/read_views.py apps/api/router.py tests/test_api_read_rollouts.py
git commit -m "feat(api): read endpoints for rollout sequences + entries"
```

---

### Task 6: Deployments (Deployment + DeploymentResult)

Uses the cross-app scope helpers from Task 1. **Pins the Review-Focus case:** a region-manager sees a `target_type=all`/`tag` deployment only if it touched one of their stations.

**Files:**
- Modify: `apps/api/read_serializers.py`, `apps/api/read_views.py`, `apps/api/read_filters.py`, `apps/api/router.py`
- Test: `tests/test_api_read_deployments.py`

**Interfaces:**
- Consumes: `accessible_deployments`, `accessible_deployment_results` (`apps.api.scoping`).
- Produces: `DeploymentSerializer`, `DeploymentResultSerializer`; viewsets `DeploymentViewSet` (basename `deployment`, with `results` action), `DeploymentResultViewSet` (basename `deployment-result`).

- [ ] **Step 1: Write failing tests (`tests/test_api_read_deployments.py`)**

```python
import pytest
from tests.test_api_read_fixtures import topology, bearer  # noqa: F401


def _dep(topology, station):
    from apps.deployments.models import Deployment, DeploymentResult
    from apps.images.models import ImageRelease
    rel = ImageRelease.objects.create(tag="v1", machine="qemux86-64", sha256="a" * 64)
    dep = Deployment.objects.create(image_release=rel, target_type="all")
    DeploymentResult.objects.create(deployment=dep, station=station)
    return dep


@pytest.mark.django_db
def test_region_mgr_sees_deployment_only_if_touches_their_station(topology):
    dep_out = _dep(topology, topology["station_out"])
    c = bearer(topology["region_mgr"])
    assert dep_out.pk not in {d["id"] for d in c.get("/api/v1/deployments/").data["results"]}
    dep_in = _dep(topology, topology["station_in"])
    assert dep_in.pk in {d["id"] for d in c.get("/api/v1/deployments/").data["results"]}


@pytest.mark.django_db
def test_deployment_results_scoped(topology):
    _dep(topology, topology["station_in"])
    _dep(topology, topology["station_out"])
    c = bearer(topology["region_mgr"])
    resp = c.get("/api/v1/deployment-results/")
    assert resp.status_code == 200
    stations = {r["station"] for r in resp.data["results"]}
    assert topology["station_out"].pk not in stations


@pytest.mark.django_db
def test_deployment_progress_field_present(topology):
    dep = _dep(topology, topology["station_in"])
    resp = bearer(topology["admin"]).get(f"/api/v1/deployments/{dep.pk}/")
    assert resp.status_code == 200 and "progress" in resp.data
```

- [ ] **Step 2: Run to verify failure** — FAIL (404).

- [ ] **Step 3: Serializers**

```python
from apps.deployments.models import Deployment, DeploymentResult


class DeploymentResultSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeploymentResult
        fields = ["id", "deployment", "station", "status", "started_at",
                  "completed_at", "error_message", "previous_version", "new_version"]


class DeploymentSerializer(serializers.ModelSerializer):
    progress = serializers.DictField(read_only=True)

    class Meta:
        model = Deployment
        fields = ["id", "image_release", "target_type", "target_tag",
                  "target_station", "strategy", "phase_config", "status",
                  "created_by", "created_at", "updated_at", "progress"]
```

- [ ] **Step 4: Filters**

```python
# apps/api/read_filters.py — add
from apps.deployments.models import Deployment, DeploymentResult


class DeploymentFilter(django_filters.FilterSet):
    class Meta:
        model = Deployment
        fields = {"status": ["exact"], "target_type": ["exact"]}


class DeploymentResultFilter(django_filters.FilterSet):
    class Meta:
        model = DeploymentResult
        fields = {"deployment": ["exact"], "station": ["exact"], "status": ["exact"]}
```

- [ ] **Step 5: Viewsets**

```python
from apps.api.scoping import accessible_deployments, accessible_deployment_results
from apps.api.read_filters import DeploymentFilter, DeploymentResultFilter
from apps.api.read_serializers import DeploymentResultSerializer, DeploymentSerializer


class DeploymentViewSet(ScopedReadOnlyViewSet):
    serializer_class = DeploymentSerializer
    filterset_class = DeploymentFilter
    ordering_fields = ["created_at", "updated_at"]

    def get_queryset(self):
        return accessible_deployments(self.request.user).order_by("-created_at")

    @action(detail=True, url_path="results")
    def results(self, request, pk=None):
        dep = self.get_object()
        return self._child_list(request, dep.results.order_by("station_id"),
                                DeploymentResultSerializer)


class DeploymentResultViewSet(ScopedReadOnlyViewSet):
    serializer_class = DeploymentResultSerializer
    filterset_class = DeploymentResultFilter
    ordering_fields = ["started_at", "completed_at"]

    def get_queryset(self):
        return accessible_deployment_results(self.request.user).order_by("-id")
```

- [ ] **Step 6: Register**

```python
router.register(r"deployments", read_views.DeploymentViewSet, basename="deployment")
router.register(r"deployment-results", read_views.DeploymentResultViewSet, basename="deployment-result")
```

**IMPORTANT (URL ordering):** The device deployment endpoints (`apps/deployments/api_urls.py`, included at `v1/deployments/`) must resolve **before** the router. Confirm `apps/api/urls.py` lists `path("v1/deployments/", include("apps.deployments.api_urls"))` ahead of the router include (it does, per Task 2 Step 7). Add a test (below) that `GET /api/v1/deployments/check/` is NOT captured by the router as `pk="check"`.

- [ ] **Step 7: Add URL-ordering regression test (append to `tests/test_api_read_deployments.py`)**

```python
@pytest.mark.django_db
def test_device_deployment_check_not_shadowed_by_router(topology):
    # The automation router must not swallow the device /deployments/check/ path.
    resp = bearer(topology["admin"]).get("/api/v1/deployments/check/")
    # Device endpoint rejects bearer/user auth (device-signature only) → 401/403,
    # NOT a 404 "no Deployment matches pk=check" from the router.
    assert resp.status_code in (401, 403, 405)
```

- [ ] **Step 8: Run to verify pass** — Run: `python -m pytest tests/test_api_read_deployments.py -q` → PASS. If the ordering test returns 404, move the device include above the router include in `apps/api/urls.py`.

- [ ] **Step 9: Commit**

```bash
git add apps/api/read_serializers.py apps/api/read_views.py apps/api/read_filters.py apps/api/router.py tests/test_api_read_deployments.py
git commit -m "feat(api): read endpoints for deployments + results (scope via touched stations)"
```

---

### Task 7: Monitoring (AlertRule, Alert)

AlertRule is global config (readable by any ≥member). Alert is station-scoped.

**Files:**
- Modify: `apps/api/read_serializers.py`, `apps/api/read_views.py`, `apps/api/read_filters.py`, `apps/api/router.py`
- Test: `tests/test_api_read_monitoring.py`

**Interfaces:**
- Consumes: `accessible_alerts`.
- Produces: `AlertRuleSerializer`, `AlertSerializer`; viewsets `AlertRuleViewSet` (basename `alert-rule`), `AlertViewSet` (basename `alert`).

- [ ] **Step 1: Write failing tests (`tests/test_api_read_monitoring.py`)**

```python
import pytest
from tests.test_api_read_fixtures import topology, bearer  # noqa: F401


@pytest.mark.django_db
def test_alert_rule_list_member_and_new_types(topology):
    from apps.monitoring.models import AlertRule
    AlertRule.objects.create(alert_type="unexpected_reboot", threshold=1, severity="warning")
    resp = bearer(topology["station_user"]).get("/api/v1/alert-rules/")
    assert resp.status_code == 200
    assert "unexpected_reboot" in {r["alert_type"] for r in resp.data["results"]}


@pytest.mark.django_db
def test_alert_scope(topology):
    from apps.monitoring.models import Alert
    Alert.objects.create(station=topology["station_out"], severity="warning",
                         title="x", message="y")
    a_in = Alert.objects.create(station=topology["station_in"], severity="critical",
                                title="z", message="w")
    resp = bearer(topology["station_user"]).get("/api/v1/alerts/")
    assert resp.status_code == 200
    ids = {a["id"] for a in resp.data["results"]}
    assert ids == {a_in.id}
```

- [ ] **Step 2: Run to verify failure** — FAIL (404).

- [ ] **Step 3: Serializers**

```python
from apps.monitoring.models import Alert, AlertRule


class AlertRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = AlertRule
        fields = ["id", "alert_type", "threshold", "severity", "is_active",
                  "description", "created_at"]


class AlertSerializer(serializers.ModelSerializer):
    class Meta:
        model = Alert
        fields = ["id", "station", "alert_rule", "severity", "title", "message",
                  "is_acknowledged", "acknowledged_by", "acknowledged_at",
                  "is_resolved", "resolved_at", "created_at"]
```

- [ ] **Step 4: Filters**

```python
from apps.monitoring.models import Alert


class AlertFilter(django_filters.FilterSet):
    class Meta:
        model = Alert
        fields = {"station": ["exact"], "severity": ["exact"],
                  "is_resolved": ["exact"], "is_acknowledged": ["exact"],
                  "alert_rule": ["exact"]}
```

- [ ] **Step 5: Viewsets**

```python
from apps.monitoring.models import AlertRule
from apps.api.scoping import accessible_alerts
from apps.api.read_filters import AlertFilter
from apps.api.read_serializers import AlertRuleSerializer, AlertSerializer


class AlertRuleViewSet(ScopedReadOnlyViewSet):
    serializer_class = AlertRuleSerializer
    filterset_fields = ["alert_type", "severity", "is_active"]
    ordering_fields = ["created_at"]

    def get_queryset(self):
        return AlertRule.objects.all().order_by("alert_type")


class AlertViewSet(ScopedReadOnlyViewSet):
    serializer_class = AlertSerializer
    filterset_class = AlertFilter
    ordering_fields = ["created_at"]

    def get_queryset(self):
        return accessible_alerts(self.request.user).order_by("-created_at")
```

- [ ] **Step 6: Register**

```python
router.register(r"alert-rules", read_views.AlertRuleViewSet, basename="alert-rule")
router.register(r"alerts", read_views.AlertViewSet, basename="alert")
```

- [ ] **Step 7: Run to verify pass** — PASS.

- [ ] **Step 8: Commit**

```bash
git add apps/api/read_serializers.py apps/api/read_views.py apps/api/read_filters.py apps/api/router.py tests/test_api_read_monitoring.py
git commit -m "feat(api): read endpoints for monitoring alert-rules + alerts"
```

---

### Task 8: Provisioning + Images (ProvisioningJob, ImageRelease, ImageImportJob)

ProvisioningJob is station-scoped. ImageRelease is global (non-archived via default manager). ImageImportJob is internal-only (staff concern). **Pins Review-Focus:** S3 keys must be absent from serializer output.

**Files:**
- Modify: `apps/api/read_serializers.py`, `apps/api/read_views.py`, `apps/api/read_filters.py`, `apps/api/router.py`
- Test: `tests/test_api_read_provisioning_images.py`

**Interfaces:**
- Consumes: `accessible_provisioning_jobs`.
- Produces: `ProvisioningJobSerializer`, `ImageReleaseSerializer`, `ImageImportJobSerializer`; viewsets `ProvisioningJobViewSet` (basename `provisioning-job`), `ImageReleaseViewSet` (basename `image`), `ImageImportJobViewSet` (basename `image-import-job`).

- [ ] **Step 1: Write failing tests (`tests/test_api_read_provisioning_images.py`)**

```python
import pytest
from tests.test_api_read_fixtures import topology, bearer  # noqa: F401


@pytest.mark.django_db
def test_image_release_no_s3_keys_leak(topology):
    from apps.images.models import ImageRelease
    ImageRelease.objects.create(tag="v1", machine="qemux86-64", sha256="a" * 64,
                                s3_key="secret/path", cosign_bundle_s3_key="c/k")
    resp = bearer(topology["station_user"]).get("/api/v1/images/")
    assert resp.status_code == 200
    row = resp.data["results"][0]
    for forbidden in ("s3_key", "cosign_bundle_s3_key", "rootfs_s3_key"):
        assert forbidden not in row
    assert "is_ota_ready" in row


@pytest.mark.django_db
def test_provisioning_job_scope_and_no_s3(topology):
    from apps.provisioning.models import ProvisioningJob
    from apps.images.models import ImageRelease
    rel = ImageRelease.objects.create(tag="v1", machine="qemux86-64", sha256="a" * 64)
    ProvisioningJob.objects.create(station=topology["station_out"], image_release=rel)
    j_in = ProvisioningJob.objects.create(station=topology["station_in"], image_release=rel)
    resp = bearer(topology["station_user"]).get("/api/v1/provisioning-jobs/")
    assert resp.status_code == 200
    assert {str(r["id"]) for r in resp.data["results"]} == {str(j_in.id)}
    assert "output_s3_key" not in resp.data["results"][0]


@pytest.mark.django_db
def test_image_import_jobs_internal_only(topology):
    from apps.images.models import ImageImportJob
    ImageImportJob.objects.create(tag="v1", machine="qemux86-64")
    assert bearer(topology["station_user"]).get("/api/v1/image-import-jobs/").data["count"] == 0
    assert bearer(topology["admin"]).get("/api/v1/image-import-jobs/").data["count"] == 1
```

- [ ] **Step 2: Run to verify failure** — FAIL (404).

- [ ] **Step 3: Serializers**

```python
from apps.provisioning.models import ProvisioningJob
from apps.images.models import ImageImportJob, ImageRelease


class ProvisioningJobSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProvisioningJob
        fields = ["id", "station", "image_release", "status", "error_message",
                  "output_size_bytes", "created_at", "ready_at", "downloaded_at",
                  "expires_at", "requested_by"]  # excludes output_s3_key


class ImageReleaseSerializer(serializers.ModelSerializer):
    is_ota_ready = serializers.BooleanField(read_only=True)

    class Meta:
        model = ImageRelease
        fields = ["id", "tag", "machine", "channel", "sha256", "size_bytes",
                  "rootfs_sha256", "rootfs_size_bytes", "is_latest",
                  "is_ota_ready", "imported_at", "imported_by", "archived_at"]
        # excludes s3_key, cosign_bundle_s3_key, rootfs_s3_key (infra storage)


class ImageImportJobSerializer(serializers.ModelSerializer):
    class Meta:
        model = ImageImportJob
        fields = ["id", "tag", "machine", "channel", "mark_as_latest", "status",
                  "error_message", "image_release", "requested_by",
                  "created_at", "completed_at"]
```

- [ ] **Step 4: Filters**

```python
from apps.images.models import ImageRelease
from apps.provisioning.models import ProvisioningJob


class ImageReleaseFilter(django_filters.FilterSet):
    class Meta:
        model = ImageRelease
        fields = {"machine": ["exact"], "channel": ["exact"], "is_latest": ["exact"]}


class ProvisioningJobFilter(django_filters.FilterSet):
    class Meta:
        model = ProvisioningJob
        fields = {"station": ["exact"], "status": ["exact"]}
```

- [ ] **Step 5: Viewsets**

```python
from apps.images.models import ImageImportJob, ImageRelease
from apps.provisioning.models import ProvisioningJob
from apps.api.scoping import accessible_provisioning_jobs
from apps.api.read_filters import ImageReleaseFilter, ProvisioningJobFilter
from apps.api.read_serializers import (
    ImageImportJobSerializer, ImageReleaseSerializer, ProvisioningJobSerializer,
)


class ProvisioningJobViewSet(ScopedReadOnlyViewSet):
    serializer_class = ProvisioningJobSerializer
    filterset_class = ProvisioningJobFilter
    ordering_fields = ["created_at"]

    def get_queryset(self):
        return accessible_provisioning_jobs(self.request.user).order_by("-created_at")


class ImageReleaseViewSet(ScopedReadOnlyViewSet):
    serializer_class = ImageReleaseSerializer
    filterset_class = ImageReleaseFilter
    search_fields = ["tag"]
    ordering_fields = ["imported_at", "tag"]

    def get_queryset(self):
        return ImageRelease.objects.all().order_by("-imported_at")


class ImageImportJobViewSet(ScopedReadOnlyViewSet):
    serializer_class = ImageImportJobSerializer
    filterset_fields = ["machine", "status", "channel"]
    ordering_fields = ["created_at"]

    def get_queryset(self):
        user = self.request.user
        if user.is_internal:
            return ImageImportJob.objects.all().order_by("-created_at")
        return ImageImportJob.objects.none()
```

- [ ] **Step 6: Register**

```python
router.register(r"provisioning-jobs", read_views.ProvisioningJobViewSet, basename="provisioning-job")
router.register(r"images", read_views.ImageReleaseViewSet, basename="image")
router.register(r"image-import-jobs", read_views.ImageImportJobViewSet, basename="image-import-job")
```

- [ ] **Step 7: Run to verify pass** — PASS. (`ImageRelease.objects` excludes archived by default — confirm from model map; that's the intended read set.)

- [ ] **Step 8: Commit**

```bash
git add apps/api/read_serializers.py apps/api/read_views.py apps/api/read_filters.py apps/api/router.py tests/test_api_read_provisioning_images.py
git commit -m "feat(api): read endpoints for provisioning jobs, image releases, import jobs"
```

---

### Task 9: Users (self + staff/admin)

Non-internal users see **only themselves**; internal see all. **Pins Review-Focus:** no password/secret/admin-flag leakage, and members cannot read peers' PII.

**Files:**
- Modify: `apps/api/read_serializers.py`, `apps/api/read_views.py`, `apps/api/router.py`
- Test: `tests/test_api_read_users.py`

**Interfaces:**
- Produces: `UserSerializer`; viewset `UserViewSet` (basename `user`).

- [ ] **Step 1: Write failing tests (`tests/test_api_read_users.py`)**

```python
import pytest
from tests.test_api_read_fixtures import topology, bearer  # noqa: F401

FORBIDDEN = {"password", "last_login", "is_staff", "is_superuser",
             "is_active", "deleted_at", "deleted_by"}


@pytest.mark.django_db
def test_member_sees_only_self(topology):
    resp = bearer(topology["station_user"]).get("/api/v1/users/")
    assert resp.status_code == 200
    assert {u["username"] for u in resp.data["results"]} == {"suser"}


@pytest.mark.django_db
def test_admin_sees_all(topology):
    resp = bearer(topology["admin"]).get("/api/v1/users/")
    assert resp.status_code == 200
    assert resp.data["count"] >= 5


@pytest.mark.django_db
def test_member_cannot_retrieve_peer_404(topology):
    other = topology["admin"].pk
    resp = bearer(topology["station_user"]).get(f"/api/v1/users/{other}/")
    assert resp.status_code == 404


@pytest.mark.django_db
def test_no_sensitive_fields(topology):
    resp = bearer(topology["admin"]).get(f"/api/v1/users/{topology['admin'].pk}/")
    assert resp.status_code == 200
    assert FORBIDDEN.isdisjoint(resp.data.keys())
    assert {"id", "username", "email", "membership_level", "is_admin"} <= set(resp.data)
```

- [ ] **Step 2: Run to verify failure** — FAIL (404).

- [ ] **Step 3: Serializer**

```python
from apps.accounts.models import User


class UserSerializer(serializers.ModelSerializer):
    is_admin = serializers.BooleanField(read_only=True)
    is_internal = serializers.BooleanField(read_only=True)

    class Meta:
        model = User
        fields = ["id", "username", "email", "first_name", "last_name",
                  "language", "notify_channel", "membership_level", "bio",
                  "avatar", "qth_name", "qrz_url", "address", "phone",
                  "latitude", "longitude", "locator", "is_directory_visible",
                  "date_joined", "is_admin", "is_internal"]
```

- [ ] **Step 4: Viewset**

```python
from apps.accounts.models import User
from apps.api.read_serializers import UserSerializer


class UserViewSet(ScopedReadOnlyViewSet):
    serializer_class = UserSerializer
    search_fields = ["username", "email", "first_name", "last_name"]
    ordering_fields = ["username", "date_joined"]

    def get_queryset(self):
        user = self.request.user
        if user.is_internal:
            return User.objects.all().order_by("username")
        return User.objects.filter(pk=user.pk)
```

- [ ] **Step 5: Register**

```python
router.register(r"users", read_views.UserViewSet, basename="user")
```

- [ ] **Step 6: Run to verify pass** — PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/api/read_serializers.py apps/api/read_views.py apps/api/router.py tests/test_api_read_users.py
git commit -m "feat(api): read endpoint for users (self + staff/admin), sensitive fields excluded"
```

---

### Task 10: OpenAPI schema integration + full-suite gate

Verifies the whole read surface appears cleanly in `/api/v1/schema/`, no sensitive fields leak into the schema, and the full test suite is green.

**Files:**
- Test: `tests/test_api_read_schema.py`

**Interfaces:**
- Consumes: every viewset registered on the router.

- [ ] **Step 1: Write schema tests (`tests/test_api_read_schema.py`)**

```python
import pytest
from tests.test_api_read_fixtures import topology, bearer  # noqa: F401

EXPECTED_PATHS = [
    "/api/v1/stations/", "/api/v1/regions/", "/api/v1/station-tags/",
    "/api/v1/station-assignments/", "/api/v1/region-assignments/",
    "/api/v1/rollout-sequences/", "/api/v1/rollout-sequence-entries/",
    "/api/v1/deployments/", "/api/v1/deployment-results/",
    "/api/v1/alert-rules/", "/api/v1/alerts/",
    "/api/v1/provisioning-jobs/", "/api/v1/images/", "/api/v1/image-import-jobs/",
    "/api/v1/users/",
    "/api/v1/stations/{id}/telemetry/", "/api/v1/stations/{id}/inventory/",
]


@pytest.mark.django_db
def test_schema_lists_all_read_resources(topology):
    resp = bearer(topology["admin"]).get("/api/v1/schema/?format=json")
    assert resp.status_code == 200
    paths = set(resp.data["paths"].keys())
    missing = [p for p in EXPECTED_PATHS if p not in paths]
    assert not missing, f"missing from schema: {missing}"


@pytest.mark.django_db
def test_schema_has_no_sensitive_component_fields(topology):
    resp = bearer(topology["admin"]).get("/api/v1/schema/?format=json")
    schemas = resp.data["components"]["schemas"]
    # User schema must not expose password/admin flags
    user_props = set(schemas["User"]["properties"].keys())
    assert {"password", "is_staff", "is_superuser"}.isdisjoint(user_props)
    # ImageRelease must not expose S3 keys
    img_props = set(schemas["ImageRelease"]["properties"].keys())
    assert {"s3_key", "cosign_bundle_s3_key", "rootfs_s3_key"}.isdisjoint(img_props)
```

- [ ] **Step 2: Run schema tests**

Run: `python -m pytest tests/test_api_read_schema.py -q`
Expected: PASS. If a nested path renders differently (e.g. `{id}` vs `{pk}`), adjust `EXPECTED_PATHS` to match drf-spectacular's output (inspect `resp.data["paths"].keys()`); the component-field assertions are the hard gate.

- [ ] **Step 3: Run the FULL suite**

Run: `python -m pytest -q`
Expected: PASS (no regressions, esp. `tests/test_heartbeat_inventory.py` after Task 3). Fix any fallout before committing.

- [ ] **Step 4: Run ruff (lint parity with CI)**

Run: `ruff check apps/api tests config` and `ruff format --check apps/api tests config`
Expected: clean. Fix lint/format issues.

- [ ] **Step 5: Commit**

```bash
git add tests/test_api_read_schema.py
git commit -m "test(api): OpenAPI schema coverage + sensitive-field exclusion gate"
```

---

## Notes for the executor

- **Serializer FK representation:** PK-by-default (`PrimaryKeyRelatedField`) is intentional — it keeps the v1 contract stable and avoids N+1 nested serialization. Clients resolve related objects via their own endpoints.
- **`is_internal`/`is_admin`/`is_online`/`is_ota_ready`/`progress`** are model properties; expose them as explicit read-only serializer fields (shown above) so drf-spectacular types them.
- **If a model field name differs** from the map (e.g. a `related_name`), trust the actual model file and adjust — the map was generated, not authoritative. Re-run the task's tests after any such fix.
- **Do not** add write methods, `@action` mutations, or audit wiring — that is Phase 3.
- **Keep `apps/api/read_views.py` imports tidy** (group stdlib/django/drf/local); run `ruff --fix` before each commit to satisfy the `I` (isort) rule.
