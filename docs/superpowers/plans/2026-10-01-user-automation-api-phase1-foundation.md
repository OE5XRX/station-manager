# User/Automation API — Phase 1 (Foundation) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lay the token-auth + scoping + OpenAPI foundation the User/Automation API is built on — no resource endpoints yet, but every piece Phase 2 (read) and Phase 3 (write) will consume.

**Architecture:** A new `PersonalAccessToken` model (SHA-256 hash only) with a DRF `Bearer` authentication class, registered ahead of SessionAuthentication. Central topology scoping helpers that mirror the existing membership + assignment access model. drf-spectacular wired for a versioned OpenAPI contract. A self-scoped token-management UI in the accounts app.

**Tech Stack:** Django 6.1, DRF 3.18, drf-spectacular 0.30, PostgreSQL, pytest (versions per the committed `requirements/*.txt` locks). Existing device API (`apps/api`, Ed25519 `DeviceKey`) is untouched.

**Spec:** `docs/superpowers/specs/2026-10-01-user-automation-api-design.md`

## Global Constraints

- **Secrets:** `PersonalAccessToken` stores **only** the SHA-256 hash; the raw token is returned exactly once at creation and never retrievable again (pattern mirrors `AccountToken` / `DeviceKey`).
- **Auth ordering:** `PersonalAccessTokenAuthentication` registers in `REST_FRAMEWORK.DEFAULT_AUTHENTICATION_CLASSES` **before** `SessionAuthentication`; `DeviceKeyAuthentication` stays first for device endpoints.
- **Versioning:** all new API URLs live under `/api/v1/`.
- **Versions-Regel:** use the newest stable release of any new dependency — verify on PyPI before pinning; do not fall back to an older default. Floors given below are minimums, not targets.
- **Django template comments:** multi-line `{# … #}` is forbidden (leaks into HTTP body); always `{% comment %} … {% endcomment %}`. The CI template-guard enforces this.
- **Membership role cache:** `User.is_admin` / `is_internal` are `@cached_property`; after mutating `membership_level` on an instance in the same request, call `User._invalidate_role_cache(user)`. Scoping helpers must read a fresh user.
- **Merge:** one PR for this phase (squash-merge convention).
- **Test runner:** `python -m pytest -q` (settings `config.settings.test`); test files live in top-level `tests/test_*.py`.

## Review Focus

- **Malformed `Authorization` header** (`Bearer` with no token, extra spaces, non-Bearer keyword): must return a clean 401, never a 500. → Task 3.
- **Soft-deleted / inactive token owner:** a token whose user is deactivated must not authenticate. → Task 3.
- **Expiry boundary & revoked tokens:** `expires_at` exactly now and a `revoked_at`-set token must both be rejected. → Task 2 (`is_active`) + Task 3 (auth rejects).
- **`last_used_at` write failure must not break the request:** a DB error updating the timestamp is swallowed; the request still authenticates. → Task 3.
- **Token IDOR:** a user must never list, view, or revoke another user's tokens. → Task 5.

---

### Task 1: Add drf-spectacular + versioned schema/docs endpoints

**Files:**
- Modify: `requirements/base.in` (add dependency), then recompile `requirements/base.txt`
- Modify: `config/settings/base.py` (INSTALLED_APPS, REST_FRAMEWORK, SPECTACULAR_SETTINGS)
- Modify: `apps/api/urls.py` (schema + docs routes)
- Test: `tests/test_api_schema.py`

**Interfaces:**
- Produces: URL names `api:schema`, `api:docs` under `/api/v1/schema/` and `/api/v1/docs/`; `DEFAULT_SCHEMA_CLASS` set so Phase 2/3 viewsets appear in the schema automatically.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_schema.py
import pytest
from django.urls import reverse


@pytest.mark.django_db
def test_openapi_schema_is_served(client):
    resp = client.get(reverse("api:schema"))
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("application/vnd.oai.openapi")


@pytest.mark.django_db
def test_swagger_docs_served(client):
    resp = client.get(reverse("api:docs"))
    assert resp.status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_api_schema.py -q`
Expected: FAIL — `NoReverseMatch` for `api:schema`.

- [ ] **Step 3: Add the dependency**

Add to `requirements/base.in` (verify newest stable on PyPI first):

```
drf-spectacular>=0.28,<0.29
```

Recompile the pinned file with the repo's existing workflow (the `# via` comments in `requirements/base.txt` indicate pip-compile/uv):

```bash
uv pip compile requirements/base.in -o requirements/base.txt
uv pip install -r requirements/base.txt
```

- [ ] **Step 4: Wire settings**

In `config/settings/base.py`, add `"drf_spectacular"` to `INSTALLED_APPS`, add the schema class to `REST_FRAMEWORK`, and add a settings block:

```python
REST_FRAMEWORK = {
    # ... existing keys ...
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "OE5XRX station-manager API",
    "DESCRIPTION": "User/automation REST API. Rights mirror membership + topology.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}
```

- [ ] **Step 5: Add URL routes**

In `apps/api/urls.py`, add imports and routes:

```python
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

urlpatterns += [
    path("v1/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "v1/docs/",
        SpectacularSwaggerView.as_view(url_name="api:schema"),
        name="docs",
    ),
]
```

- [ ] **Step 6: Run test to verify it passes**

Run: `python -m pytest tests/test_api_schema.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add requirements/base.in requirements/base.txt config/settings/base.py apps/api/urls.py tests/test_api_schema.py
git commit -m "feat(api): add drf-spectacular OpenAPI schema + docs under /api/v1/"
```

---

### Task 2: `PersonalAccessToken` model

**Files:**
- Modify: `apps/api/models.py` (append model)
- Create: `apps/api/migrations/XXXX_personalaccesstoken.py` (via makemigrations)
- Test: `tests/test_personal_access_token.py`

**Interfaces:**
- Produces:
  - `PersonalAccessToken.issue(user, name, expires_at=None) -> tuple[PersonalAccessToken, str]` — returns `(instance, raw_token)`; raw only here.
  - `PersonalAccessToken.hash_token(raw: str) -> str` — SHA-256 hex digest (staticmethod).
  - `instance.is_active() -> bool` — not revoked and (no expiry or expiry in the future).
  - Fields: `user` (FK accounts.User, `related_name="api_tokens"`), `name`, `prefix` (first 8 chars of raw, non-secret, indexed), `token_hash` (indexed), `created_at`, `last_used_at` (nullable), `expires_at` (nullable), `revoked_at` (nullable).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_personal_access_token.py
import hashlib

import pytest
from django.utils import timezone
from datetime import timedelta

from apps.api.models import PersonalAccessToken


@pytest.mark.django_db
def test_issue_returns_raw_once_and_stores_only_hash(member_user):
    token, raw = PersonalAccessToken.issue(member_user, name="ci-script")
    assert raw and len(raw) >= 32
    assert token.token_hash == hashlib.sha256(raw.encode()).hexdigest()
    assert raw not in (token.token_hash, token.prefix)
    assert token.prefix == raw[:8]


@pytest.mark.django_db
def test_is_active_true_for_fresh_token(member_user):
    token, _ = PersonalAccessToken.issue(member_user, name="x")
    assert token.is_active() is True


@pytest.mark.django_db
def test_is_active_false_when_expired(member_user):
    token, _ = PersonalAccessToken.issue(
        member_user, name="x", expires_at=timezone.now() - timedelta(seconds=1)
    )
    assert token.is_active() is False


@pytest.mark.django_db
def test_is_active_false_when_revoked(member_user):
    token, _ = PersonalAccessToken.issue(member_user, name="x")
    token.revoked_at = timezone.now()
    assert token.is_active() is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_personal_access_token.py -q`
Expected: FAIL — `ImportError: cannot import name 'PersonalAccessToken'`.

- [ ] **Step 3: Write the model**

Append to `apps/api/models.py`:

```python
import hashlib
import secrets

from django.conf import settings
from django.utils import timezone


class PersonalAccessToken(models.Model):
    """User-owned bearer token for the automation/CLI API.

    Only the SHA-256 hash is stored; the raw token is shown once at
    creation (pattern mirrors AccountToken / DeviceKey private keys).
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="api_tokens",
    )
    name = models.CharField(max_length=100)
    prefix = models.CharField(max_length=8, db_index=True)
    token_hash = models.CharField(max_length=64, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "personal access token"
        verbose_name_plural = "personal access tokens"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.prefix}…) — {self.user}"

    @staticmethod
    def hash_token(raw):
        return hashlib.sha256(raw.encode()).hexdigest()

    @classmethod
    def issue(cls, user, name, expires_at=None):
        raw = secrets.token_urlsafe(32)
        token = cls.objects.create(
            user=user,
            name=name,
            prefix=raw[:8],
            token_hash=cls.hash_token(raw),
            expires_at=expires_at,
        )
        return token, raw

    def is_active(self):
        if self.revoked_at is not None:
            return False
        if self.expires_at is not None and self.expires_at <= timezone.now():
            return False
        return True
```

- [ ] **Step 4: Make + apply the migration**

```bash
python manage.py makemigrations api
python manage.py migrate
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_personal_access_token.py -q`
Expected: PASS (all four).

- [ ] **Step 6: Commit**

```bash
git add apps/api/models.py apps/api/migrations/ tests/test_personal_access_token.py
git commit -m "feat(api): PersonalAccessToken model (hash-only, issue/is_active)"
```

---

### Task 3: `PersonalAccessTokenAuthentication` + throttle scope

**Files:**
- Modify: `apps/api/authentication.py` (append auth class)
- Modify: `config/settings/base.py` (register auth class first-after-device; add throttle rate)
- Test: `tests/test_personal_access_token_auth.py`

**Interfaces:**
- Consumes: `PersonalAccessToken.hash_token`, `.is_active()` (Task 2).
- Produces: `PersonalAccessTokenAuthentication` (DRF `BaseAuthentication`); on success sets `request.user` = token owner, `request.auth` = the `PersonalAccessToken`. Throttle scope name `api-token`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_personal_access_token_auth.py
import pytest
from datetime import timedelta
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.test import APIRequestFactory

from apps.api.authentication import PersonalAccessTokenAuthentication
from apps.api.models import PersonalAccessToken


def _auth(raw):
    req = APIRequestFactory().get("/api/v1/ping/", HTTP_AUTHORIZATION=f"Bearer {raw}")
    return PersonalAccessTokenAuthentication().authenticate(req)


@pytest.mark.django_db
def test_valid_token_authenticates_owner(member_user):
    token, raw = PersonalAccessToken.issue(member_user, name="x")
    user, auth = _auth(raw)
    assert user == member_user
    assert auth == token
    token.refresh_from_db()
    assert token.last_used_at is not None


@pytest.mark.django_db
def test_no_header_returns_none():
    req = APIRequestFactory().get("/api/v1/ping/")
    assert PersonalAccessTokenAuthentication().authenticate(req) is None


@pytest.mark.django_db
def test_malformed_header_is_clean_401_or_none():
    # Non-Bearer scheme -> None (let other authenticators try).
    for other_scheme in ["Token abc", "DeviceKey 1"]:
        req = APIRequestFactory().get("/api/v1/ping/", HTTP_AUTHORIZATION=other_scheme)
        assert PersonalAccessTokenAuthentication().authenticate(req) is None
    # Bearer but malformed -> clean AuthenticationFailed (401), never a 500.
    for bad in ["Bearer", "Bearer   ", "Bearer a b"]:
        req = APIRequestFactory().get("/api/v1/ping/", HTTP_AUTHORIZATION=bad)
        with pytest.raises(AuthenticationFailed):
            PersonalAccessTokenAuthentication().authenticate(req)


@pytest.mark.django_db
def test_last_used_write_failure_does_not_break_auth(member_user, monkeypatch):
    token, raw = PersonalAccessToken.issue(member_user, name="x")

    # The auth lookup uses .select_related(...).get(...); only the
    # last_used_at stamp uses .filter(...).update(...). Make .filter blow
    # up so the stamp path raises — auth must still succeed.
    def _boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(type(PersonalAccessToken.objects), "filter", _boom)
    user, auth = _auth(raw)
    assert user == member_user


@pytest.mark.django_db
def test_unknown_token_rejected():
    with pytest.raises(AuthenticationFailed):
        _auth("totally-bogus-token-value-123456")


@pytest.mark.django_db
def test_expired_and_revoked_rejected(member_user):
    expired, raw_e = PersonalAccessToken.issue(
        member_user, name="e", expires_at=timezone.now() - timedelta(seconds=1)
    )
    with pytest.raises(AuthenticationFailed):
        _auth(raw_e)
    revoked, raw_r = PersonalAccessToken.issue(member_user, name="r")
    revoked.revoked_at = timezone.now()
    revoked.save(update_fields=["revoked_at"])
    with pytest.raises(AuthenticationFailed):
        _auth(raw_r)


@pytest.mark.django_db
def test_inactive_owner_rejected(member_user):
    token, raw = PersonalAccessToken.issue(member_user, name="x")
    member_user.is_active = False
    member_user.save(update_fields=["is_active"])
    with pytest.raises(AuthenticationFailed):
        _auth(raw)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_personal_access_token_auth.py -q`
Expected: FAIL — cannot import `PersonalAccessTokenAuthentication`.

- [ ] **Step 3: Write the auth class**

Append to `apps/api/authentication.py`:

```python
from rest_framework.authentication import BaseAuthentication, get_authorization_header

from apps.api.models import PersonalAccessToken


class PersonalAccessTokenAuthentication(BaseAuthentication):
    """Bearer-token auth for the user/automation API.

    Header: ``Authorization: Bearer <raw_token>``. Resolves the owning
    user and sets request.auth to the PersonalAccessToken. Returns None
    for a non-Bearer header so other authenticators can try.
    """

    keyword = "Bearer"

    def authenticate(self, request):
        auth = get_authorization_header(request).split()
        if not auth or auth[0].decode().lower() != self.keyword.lower():
            return None
        if len(auth) != 2:
            raise AuthenticationFailed("Invalid bearer header.")

        raw = auth[1].decode()
        token_hash = PersonalAccessToken.hash_token(raw)
        try:
            token = PersonalAccessToken.objects.select_related("user").get(
                token_hash=token_hash
            )
        except PersonalAccessToken.DoesNotExist:
            raise AuthenticationFailed("Invalid token.")

        if not token.is_active():
            raise AuthenticationFailed("Token expired or revoked.")
        if not token.user.is_active:
            raise AuthenticationFailed("Token owner is inactive.")

        # best-effort last-used stamp; never break the request on failure
        try:
            PersonalAccessToken.objects.filter(pk=token.pk).update(
                last_used_at=timezone.now()
            )
        except Exception:  # noqa: BLE001 - telemetry only, must not 500
            pass

        return (token.user, token)

    def authenticate_header(self, request):
        return self.keyword
```

Add the imports at the top of the file if missing: `from django.utils import timezone` and ensure `AuthenticationFailed` is imported (it already is for `DeviceKeyAuthentication`).

- [ ] **Step 4: Register in settings**

In `config/settings/base.py`, update `REST_FRAMEWORK`:

```python
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.api.authentication.DeviceKeyAuthentication",
        "apps.api.authentication.PersonalAccessTokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "heartbeat": "10/min",
        "register": "10/hour",
        "api-token": "120/min",
    },
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_personal_access_token_auth.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/api/authentication.py config/settings/base.py tests/test_personal_access_token_auth.py
git commit -m "feat(api): Bearer PersonalAccessToken authentication + api-token throttle"
```

---

### Task 4: Topology scoping helpers

**Files:**
- Create: `apps/stations/scoping.py`
- Test: `tests/test_api_scoping.py`

**Interfaces:**
- Consumes: `User.is_internal`, `RegionAssignment` (role `manager`), `StationAssignment` (roles `admin`/`maintainer`), `Region`, `Station`.
- Produces:
  - `accessible_stations(user) -> QuerySet[Station]`
  - `accessible_regions(user) -> QuerySet[Region]`
  - Semantics: admin/staff → all; member with RegionAssignment(manager) → stations in those regions + those regions; member with StationAssignment → those stations (and their regions); applicant/anonymous → empty.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_scoping.py
import pytest

from apps.stations.models import Region, RegionAssignment, StationAssignment
from apps.stations.scoping import accessible_regions, accessible_stations


@pytest.fixture
def region(db):
    return Region.objects.create(name="Alpen")


@pytest.mark.django_db
def test_admin_sees_all_stations(admin_user, station_factory):
    station_factory()
    station_factory()
    assert accessible_stations(admin_user).count() == 2


@pytest.mark.django_db
def test_staff_sees_all_stations(operator_user, station_factory):
    station_factory()
    assert accessible_stations(operator_user).count() == 1


@pytest.mark.django_db
def test_region_manager_sees_region_stations_only(member_user, region, station_factory):
    in_region = station_factory(region=region)
    station_factory()  # outside
    RegionAssignment.objects.create(
        user=member_user, region=region, role="manager", assigned_by=member_user
    )
    result = list(accessible_stations(member_user))
    assert result == [in_region]
    assert list(accessible_regions(member_user)) == [region]


@pytest.mark.django_db
def test_station_assigned_member_sees_assigned_only(member_user, station_factory):
    assigned = station_factory()
    station_factory()  # not assigned
    StationAssignment.objects.create(
        user=member_user, station=assigned, role="maintainer", assigned_by=member_user
    )
    assert list(accessible_stations(member_user)) == [assigned]


@pytest.mark.django_db
def test_applicant_sees_nothing(applicant_user, station_factory):
    station_factory()
    assert accessible_stations(applicant_user).count() == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_api_scoping.py -q`
Expected: FAIL — `ModuleNotFoundError: apps.stations.scoping`.

- [ ] **Step 3: Write the helpers**

Create `apps/stations/scoping.py`:

```python
"""Central "what may user X touch" querysets.

Single source of truth for API permission filtering (and, over time, UI
scoping). Mirrors the membership + topology access model: staff/admin see
everything; members see only what their Region/Station assignments grant;
applicants and anonymous users see nothing.
"""

from apps.stations.models import Region, Station


def _is_authenticated_member(user):
    return bool(
        user
        and user.is_authenticated
        and user.membership_level != user.MembershipLevel.APPLICANT
    )


def accessible_regions(user):
    if not _is_authenticated_member(user):
        return Region.objects.none()
    if user.is_internal:
        return Region.objects.all()
    managed = user.region_assignments.filter(role="manager").values_list(
        "region_id", flat=True
    )
    via_station = user.station_assignments.values_list("station__region_id", flat=True)
    region_ids = set(managed) | {r for r in via_station if r is not None}
    return Region.objects.filter(id__in=region_ids)


def accessible_stations(user):
    if not _is_authenticated_member(user):
        return Station.objects.none()
    if user.is_internal:
        return Station.objects.all()
    managed_regions = user.region_assignments.filter(role="manager").values_list(
        "region_id", flat=True
    )
    assigned = user.station_assignments.values_list("station_id", flat=True)
    return Station.objects.filter(region_id__in=managed_regions) | Station.objects.filter(
        id__in=assigned
    )
```

Note: the union uses `|` on two querysets over the same model; add `.distinct()` at call sites that paginate if duplicates are possible. For Phase 1 the tests assert exact membership, so keep as written and confirm no duplicates arise.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_api_scoping.py -q`
Expected: PASS.

If the region-manager test returns duplicates, append `.distinct()` to the `accessible_stations` return and re-run.

- [ ] **Step 5: Commit**

```bash
git add apps/stations/scoping.py tests/test_api_scoping.py
git commit -m "feat(stations): topology scoping helpers (accessible_stations/regions)"
```

---

### Task 5: Token-management UI (list / create / revoke, self-scoped)

**Files:**
- Create: `apps/accounts/views_api_tokens.py`
- Create: `apps/accounts/templates/accounts/api_tokens.html`
- Create: `apps/accounts/templates/accounts/api_token_created.html`
- Modify: `apps/accounts/urls.py` (routes)
- Test: `tests/test_api_token_ui.py`

**Interfaces:**
- Consumes: `PersonalAccessToken.issue` (Task 2).
- Produces: URL names `accounts:api_tokens` (GET list + POST create), `accounts:api_token_revoke` (POST). Create shows the raw token once via `api_token_created.html`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_token_ui.py
import pytest
from django.urls import reverse

from apps.api.models import PersonalAccessToken


@pytest.mark.django_db
def test_list_requires_login(client):
    resp = client.get(reverse("accounts:api_tokens"))
    assert resp.status_code in (302, 403)


@pytest.mark.django_db
def test_user_sees_only_own_tokens(client, member_user, admin_user):
    mine, _ = PersonalAccessToken.issue(member_user, name="mine")
    PersonalAccessToken.issue(admin_user, name="theirs")
    client.force_login(member_user)
    resp = client.get(reverse("accounts:api_tokens"))
    assert resp.status_code == 200
    assert b"mine" in resp.content
    assert b"theirs" not in resp.content


@pytest.mark.django_db
def test_create_shows_raw_once(client, member_user):
    client.force_login(member_user)
    resp = client.post(reverse("accounts:api_tokens"), {"name": "ci"})
    assert resp.status_code == 200
    token = PersonalAccessToken.objects.get(user=member_user, name="ci")
    # raw token rendered, and it is not the stored hash
    assert token.prefix.encode() in resp.content
    assert token.token_hash.encode() not in resp.content


@pytest.mark.django_db
def test_cannot_revoke_other_users_token(client, member_user, admin_user):
    theirs, _ = PersonalAccessToken.issue(admin_user, name="theirs")
    client.force_login(member_user)
    resp = client.post(reverse("accounts:api_token_revoke", kwargs={"pk": theirs.pk}))
    assert resp.status_code == 404
    theirs.refresh_from_db()
    assert theirs.revoked_at is None


@pytest.mark.django_db
def test_revoke_own_token(client, member_user):
    mine, _ = PersonalAccessToken.issue(member_user, name="mine")
    client.force_login(member_user)
    resp = client.post(reverse("accounts:api_token_revoke", kwargs={"pk": mine.pk}))
    assert resp.status_code in (302, 200)
    mine.refresh_from_db()
    assert mine.revoked_at is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_api_token_ui.py -q`
Expected: FAIL — `NoReverseMatch` for `accounts:api_tokens`.

- [ ] **Step 3: Write the views**

Create `apps/accounts/views_api_tokens.py`:

```python
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View

from apps.api.models import PersonalAccessToken


class ApiTokenListView(LoginRequiredMixin, View):
    template_name = "accounts/api_tokens.html"

    def get(self, request):
        tokens = PersonalAccessToken.objects.filter(user=request.user)
        return render(request, self.template_name, {"tokens": tokens})

    def post(self, request):
        name = (request.POST.get("name") or "").strip()
        if not name:
            tokens = PersonalAccessToken.objects.filter(user=request.user)
            return render(
                request,
                self.template_name,
                {"tokens": tokens, "error": "Name is required."},
            )
        token, raw = PersonalAccessToken.issue(request.user, name=name)
        return render(
            request,
            "accounts/api_token_created.html",
            {"token": token, "raw_token": raw},
        )


class ApiTokenRevokeView(LoginRequiredMixin, View):
    def post(self, request, pk):
        token = get_object_or_404(PersonalAccessToken, pk=pk, user=request.user)
        if token.revoked_at is None:
            token.revoked_at = timezone.now()
            token.save(update_fields=["revoked_at"])
        return redirect(reverse("accounts:api_tokens"))
```

- [ ] **Step 4: Write the templates**

Create `apps/accounts/templates/accounts/api_tokens.html`:

```django
{% extends "base.html" %}
{% comment %} Self-scoped personal access token management. {% endcomment %}
{% block content %}
<h1>API-Tokens</h1>
{% if error %}<p class="error">{{ error }}</p>{% endif %}
<form method="post" action="{% url 'accounts:api_tokens' %}">
  {% csrf_token %}
  <input type="text" name="name" placeholder="Token-Name (z.B. ci-script)" required>
  <button type="submit">Token erstellen</button>
</form>
<table>
  <thead><tr><th>Name</th><th>Präfix</th><th>Erstellt</th><th>Zuletzt genutzt</th><th></th></tr></thead>
  <tbody>
  {% for t in tokens %}
    <tr>
      <td>{{ t.name }}</td>
      <td>{{ t.prefix }}…</td>
      <td>{{ t.created_at }}</td>
      <td>{{ t.last_used_at|default:"—" }}</td>
      <td>
        {% if t.revoked_at %}widerrufen{% else %}
        <form method="post" action="{% url 'accounts:api_token_revoke' t.pk %}">
          {% csrf_token %}<button type="submit">Widerrufen</button>
        </form>
        {% endif %}
      </td>
    </tr>
  {% empty %}
    <tr><td colspan="5">Noch keine Tokens.</td></tr>
  {% endfor %}
  </tbody>
</table>
{% endblock %}
```

Create `apps/accounts/templates/accounts/api_token_created.html`:

```django
{% extends "base.html" %}
{% comment %} Shown exactly once — raw token is never retrievable again. {% endcomment %}
{% block content %}
<h1>Token „{{ token.name }}" erstellt</h1>
<p><strong>Kopiere den Token jetzt — er wird nie wieder angezeigt:</strong></p>
<pre>{{ raw_token }}</pre>
<p>Präfix zur Wiedererkennung: {{ token.prefix }}…</p>
<a href="{% url 'accounts:api_tokens' %}">Zurück zur Token-Liste</a>
{% endblock %}
```

- [ ] **Step 5: Wire URLs**

In `apps/accounts/urls.py`, import and add routes:

```python
from .views_api_tokens import ApiTokenListView, ApiTokenRevokeView

urlpatterns += [
    path("api-tokens/", ApiTokenListView.as_view(), name="api_tokens"),
    path(
        "api-tokens/<int:pk>/revoke/",
        ApiTokenRevokeView.as_view(),
        name="api_token_revoke",
    ),
]
```

- [ ] **Step 6: Run test to verify it passes**

Run: `python -m pytest tests/test_api_token_ui.py -q`
Expected: PASS (all five).

- [ ] **Step 7: Run the full phase test set + template guard**

Run: `python -m pytest tests/test_api_schema.py tests/test_personal_access_token.py tests/test_personal_access_token_auth.py tests/test_api_scoping.py tests/test_api_token_ui.py -q`
Expected: all PASS. Confirm no multi-line `{# #}` comments were introduced (CI template-guard).

- [ ] **Step 8: Commit**

```bash
git add apps/accounts/views_api_tokens.py apps/accounts/templates/accounts/api_tokens.html apps/accounts/templates/accounts/api_token_created.html apps/accounts/urls.py tests/test_api_token_ui.py
git commit -m "feat(accounts): self-scoped API token management UI"
```

---

## Phase boundary

Phase 1 ends here: tokens can be issued/revoked via UI, authenticate via `Bearer`, and scoping helpers exist and are tested — but no resource endpoints are exposed yet. **Phase 2 (read-only resource surface)** and **Phase 3 (write surface)** get their own plans, authored once this foundation is merged so the ViewSet/serializer/permission pattern is concrete in code (avoids copy-paste drift across ~12 resources). Each is its own PR per the squash-merge-one-PR-per-phase convention.
