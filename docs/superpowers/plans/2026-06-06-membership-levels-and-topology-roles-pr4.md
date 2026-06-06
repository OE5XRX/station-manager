# Membership-Levels + Topology-Roles — PR-4: Station-Detail Topology UI + Region-CRUD

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax for tracking. UI tasks use the existing PR-3 patterns (HTMX cards, JS confirm for admin-takeover, CSP nonces via `{{ csp_nonce }}`).

**Goal:** Topology management from two more angles: (1) operators editing a Station can set its Region + Admin + Maintainers from the station's perspective; (2) Vereins-Admins manage Regions via a proper CRUD UI instead of Django Admin.

**Architecture:** New `StationSetRegionView` POST endpoint emits Station.region change via the existing signal in `apps/stations/signals.py` (STATION_REGION_CHANGED). Station-detail page gets a new HTMX card that reuses the `StationAssignmentCreateView` and `StationAssignmentRevokeView` endpoints from PR-3 — they already handle the admin-takeover atomic-409 contract. Region-CRUD = 4 standard generic-CBV views (List/Create/Update/Delete) gated on `is_admin`, with the existing signals firing audit-log entries.

**Tech Stack:** Django 6.0 CBV (ListView/CreateView/UpdateView/DeleteView), HTMX 2.0.4, existing PR-3 endpoint pattern, `_get_client_ip` helper for IP capture, AccountAuditLog/StationAuditLog signals already wired.

**Reference spec:** `docs/superpowers/specs/2026-06-05-membership-levels-and-topology-roles-design.md` §5.2, §5.3
**Reference PR-1 plan:** Tasks 19-26 (deferred)

**Out of scope (deferred to future):**
- Region-Manager edit-permissions on stations of own region (currently all topology edits gated on Vereins-Admin)
- Notification preferences per user
- Renaming `AdminOrOperatorMixin` / `AdminOrOperatorRequiredMixin` mixins
- Telegram routing

---

## In-Tree State (verified 2026-06-06 post PR-3 merge)

| Item | Reality |
|---|---|
| Station-Detail URL | `/stations/<pk>/` → `StationDetailView` → `station_detail.html` |
| `StationDetailView` mixin | `LoginRequiredMixin` (everyone logged-in can read) |
| `StationDetailView.get_context_data` | conditionally adds admin-only sections via `if request.user.is_admin` |
| `apps/stations/signals.py` | already emits STATION_REGION_CHANGED on pre_save+post_save with select_for_update lock pattern (PR-2 + PR-3 fixes) |
| `StationAssignmentCreateView` / `RevokeView` | PR-3, AdminRequiredMixin from `apps.accounts.views`, supports `takeover=1` atomic |
| `Region` model | exists, name+slug+description+created_at; `Station.region` FK SET_NULL |
| `Region` signals | `_on_region_save` (CREATE/UPDATE), `_on_region_delete` (DELETE) — wired in PR-2 |
| `AccountAuditLog` EventType | includes REGION_CREATED/UPDATED/DELETED |
| `AdminRequiredMixin` in stations | NOT defined yet — `apps.accounts.views.AdminRequiredMixin` is the canonical one |

---

## File Structure

### New

| Path | Responsibility |
|---|---|
| `apps/stations/views_region_set.py` | `StationSetRegionView` POST endpoint |
| `apps/stations/views_region_crud.py` | Region `ListView`/`CreateView`/`UpdateView`/`DeleteView` |
| `apps/stations/forms.py` (append) | `RegionForm` (ModelForm for Region CRUD) |
| `apps/stations/templates/stations/_station_topology_card.html` | HTMX card on station-detail (region + admin + maintainers) |
| `apps/stations/templates/stations/region_list.html` | Region list page |
| `apps/stations/templates/stations/region_form.html` | Region create/update form |
| `apps/stations/templates/stations/region_confirm_delete.html` | Delete with N-stations warning |
| `tests/test_views_station_set_region.py` | StationSetRegionView tests |
| `tests/test_views_region_crud.py` | Region CRUD tests |
| `tests/test_station_detail_topology_card.py` | Topology-card rendering tests |

### Modified

| Path | Reason |
|---|---|
| `apps/stations/urls.py` | Add 5 routes (region/set + region_list/create/update/delete) |
| `apps/stations/views.py` | `StationDetailView.get_context_data` adds region + station-assignments context |
| `apps/stations/templates/stations/station_detail.html` | Include topology card for admin |

---

# Phase 7: Station-Detail Topology UI

## Task 1: `StationSetRegionView` POST endpoint

**Files:**
- Create: `apps/stations/views_region_set.py`
- Modify: `apps/stations/urls.py`
- Create: `tests/test_views_station_set_region.py`

POST `/stations/<int:pk>/region/` with `{"region": "<pk>" or ""}` to set or clear `Station.region`. Vereins-Admin only. The existing signal in `apps/stations/signals.py` emits STATION_REGION_CHANGED automatically when the FK changes.

- [ ] **Step 1: Write failing tests**

Create `tests/test_views_station_set_region.py`:

```python
"""Tests for StationSetRegionView (admin-only region setter)."""

import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.stations.models import Region, Station, StationAuditLog


def _user(level, username):
    u = User.objects.create_user(
        username=username, password="x", email=f"{username}@x"
    )
    u.membership_level = level
    u.save(update_fields=["membership_level"])
    return u


@pytest.mark.django_db
class TestStationSetRegionView:
    def test_admin_can_set_region(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
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
        # Signal should have emitted the audit entry
        assert StationAuditLog.objects.filter(
            event_type=StationAuditLog.EventType.STATION_REGION_CHANGED,
            station=s,
        ).exists()

    def test_admin_can_clear_region(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
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

    def test_non_admin_forbidden(self, client):
        staff = _user(User.MembershipLevel.STAFF, "staff")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        r = Region.objects.create(name="Tirol", slug="tirol")
        client.force_login(staff)
        response = client.post(
            reverse("stations:station_set_region", args=[s.pk]),
            {"region": r.pk},
        )
        assert response.status_code in (302, 403)
        s.refresh_from_db()
        assert s.region is None

    def test_invalid_region_returns_404(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        client.force_login(admin)
        response = client.post(
            reverse("stations:station_set_region", args=[s.pk]),
            {"region": "99999"},
        )
        assert response.status_code == 404

    def test_no_change_does_not_emit(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        r = Region.objects.create(name="Tirol", slug="tirol")
        s = Station.objects.create(name="OE5A", callsign="OE5A", region=r)
        client.force_login(admin)
        response = client.post(
            reverse("stations:station_set_region", args=[s.pk]),
            {"region": r.pk},
        )
        assert response.status_code == 200
        # No signal fired since region didn't change
        assert not StationAuditLog.objects.filter(
            event_type=StationAuditLog.EventType.STATION_REGION_CHANGED,
            station=s,
        ).exists()
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/python -m pytest tests/test_views_station_set_region.py -v
```

Expected: NoReverseMatch on `stations:station_set_region`.

- [ ] **Step 3: Create the view**

Create `apps/stations/views_region_set.py`:

```python
"""StationSetRegionView: admin-only POST endpoint to set Station.region.

The Station.region change is detected by apps/stations/signals.py
(_on_station_pre_save + _on_station_save), which emits
STATION_REGION_CHANGED on StationAuditLog. No view-level emission
needed.
"""

from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views import View

from apps.accounts.views import AdminRequiredMixin
from apps.stations.models import Region, Station


class StationSetRegionView(AdminRequiredMixin, View):
    def post(self, request, pk):
        station = get_object_or_404(Station, pk=pk)
        region_pk = request.POST.get("region", "").strip()
        if region_pk:
            station.region = get_object_or_404(Region, pk=region_pk)
        else:
            station.region = None
        station.save(update_fields=["region"])
        return JsonResponse({"success": True})
```

- [ ] **Step 4: Wire URL**

In `apps/stations/urls.py`, add inside `urlpatterns`:

```python
    path(
        "<int:pk>/region/",
        __import__(
            "apps.stations.views_region_set",
            fromlist=["StationSetRegionView"],
        ).StationSetRegionView.as_view(),
        name="station_set_region",
    ),
```

Or use a normal top-level import. Simpler: add to the top of `urls.py`:

```python
from .views_region_set import StationSetRegionView
```

and use:

```python
    path("<int:pk>/region/", StationSetRegionView.as_view(), name="station_set_region"),
```

- [ ] **Step 5: Ruff format + test**

```bash
.venv/bin/ruff format apps/stations/views_region_set.py apps/stations/urls.py tests/test_views_station_set_region.py
.venv/bin/ruff format --check . && .venv/bin/ruff check .
.venv/bin/python -m pytest tests/test_views_station_set_region.py -v
```

Expected: 5 PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/stations/views_region_set.py apps/stations/urls.py tests/test_views_station_set_region.py
git commit -m "feat(stations): StationSetRegionView (admin POST to change Station.region)

Vereins-Admin POST endpoint for setting/clearing Station.region.
The existing signal handler in apps/stations/signals.py emits
STATION_REGION_CHANGED on the StationAuditLog when the FK changes
(detected via pre_save/post_save diff). No view-level emission
needed; idempotent same-region POSTs do not emit."
```

---

## Task 2: Station-Detail topology card

**Files:**
- Create: `apps/stations/templates/stations/_station_topology_card.html`
- Modify: `apps/stations/views.py` (`StationDetailView.get_context_data`)
- Modify: `apps/stations/templates/stations/station_detail.html`
- Create: `tests/test_station_detail_topology_card.py`

Reuses the PR-3 endpoints (`StationAssignmentCreateView`, `StationAssignmentRevokeView`) — they already handle the admin-takeover atomic-409 contract and signal-based audit emission.

- [ ] **Step 1: Write failing tests**

Create `tests/test_station_detail_topology_card.py`:

```python
"""Tests for the Station-Detail topology card rendering."""

import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.stations.models import Region, Station, StationAssignment


def _user(level, username):
    u = User.objects.create_user(
        username=username, password="x", email=f"{username}@x"
    )
    u.membership_level = level
    u.save(update_fields=["membership_level"])
    return u


@pytest.mark.django_db
class TestStationDetailTopologyCard:
    def test_admin_sees_topology_card(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        Region.objects.create(name="Tirol", slug="tirol")
        client.force_login(admin)
        response = client.get(
            reverse("stations:station_detail", args=[s.pk])
        )
        body = response.content.decode()
        assert response.status_code == 200
        assert "Region & Topology" in body or "Region" in body
        # Region picker rendered
        assert "Tirol" in body
        # Assignment form rendered
        assert reverse(
            "accounts:station_assignment_create", args=[0]
        ).replace("/0/", "/") in body or "station_assignments" in body

    def test_member_does_not_see_topology_card(self, client):
        member = _user(User.MembershipLevel.MEMBER, "member")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        client.force_login(member)
        response = client.get(
            reverse("stations:station_detail", args=[s.pk])
        )
        body = response.content.decode()
        # Topology editing surface is admin-only — Member sees no
        # region picker form, no maintainer add button.
        # We test for an admin-specific marker: the set-region form URL.
        assert reverse(
            "stations:station_set_region", args=[s.pk]
        ) not in body

    def test_card_lists_existing_admin_and_maintainers(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        franz = _user(User.MembershipLevel.MEMBER, "franz")
        hans = _user(User.MembershipLevel.MEMBER, "hans")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        StationAssignment.objects.create(
            user=franz, station=s, role=StationAssignment.Role.ADMIN
        )
        StationAssignment.objects.create(
            user=hans, station=s, role=StationAssignment.Role.MAINTAINER
        )
        client.force_login(admin)
        response = client.get(
            reverse("stations:station_detail", args=[s.pk])
        )
        body = response.content.decode()
        assert "franz" in body
        assert "hans" in body
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/python -m pytest tests/test_station_detail_topology_card.py -v
```

Expected: 3 failures (no card yet).

- [ ] **Step 3: Add context to StationDetailView**

In `apps/stations/views.py`, find the existing `StationDetailView.get_context_data` and add topology context inside the `if self.request.user.is_admin:` block:

```python
        if self.request.user.is_admin:
            # ... existing provisioning-section context ...

            # Topology card.
            context["all_regions"] = Region.objects.order_by("name")
            context["all_users"] = (
                User.objects.exclude(
                    membership_level=User.MembershipLevel.APPLICANT
                )
                .order_by("username")
            )
            assignments = list(
                self.object.assignments.select_related("user").order_by(
                    "role", "user__username"
                )
            )
            context["station_admin"] = next(
                (
                    a
                    for a in assignments
                    if a.role == StationAssignment.Role.ADMIN
                ),
                None,
            )
            context["station_maintainers"] = [
                a
                for a in assignments
                if a.role == StationAssignment.Role.MAINTAINER
            ]
```

Add the imports at the top of `apps/stations/views.py`:

```python
from apps.accounts.models import User as _User_for_typing  # noqa: F401
from apps.stations.models import Region, StationAssignment
```

(Use `get_user_model()` if not already imported. The Region + StationAssignment imports should be added near the existing Station imports — confirm by reading the import block.)

- [ ] **Step 4: Create the card template**

Create `apps/stations/templates/stations/_station_topology_card.html`:

```html
{% load i18n %}
<section class="panel" id="station-topology-card" style="margin-top:16px;">
  <div class="panel-head">
    <div class="panel-title"><span class="dot"></span>{% trans "Region & Topology" %}</div>
  </div>
  <div class="panel-body">

    {# --- Region picker --- #}
    <div class="stack-gap-2" style="margin-bottom:16px;">
      <label class="form-label">{% trans "Region" %}:</label>
      <form hx-post="{% url 'stations:station_set_region' station.pk %}"
            hx-on::after-request="if (event.detail.successful) window.location.reload()">
        {% csrf_token %}
        <div class="row-gap-8" style="align-items:flex-end;">
          <select name="region" class="form-select" style="max-width:280px;">
            <option value="" {% if not station.region %}selected{% endif %}>
              {% trans "— None —" %}
            </option>
            {% for r in all_regions %}
              <option value="{{ r.pk }}" {% if station.region_id == r.pk %}selected{% endif %}>
                {{ r.name }}
              </option>
            {% endfor %}
          </select>
          <button type="submit" class="btn btn-primary btn-sm">{% trans "Save" %}</button>
        </div>
      </form>
    </div>

    {# --- Station-Admin slot --- #}
    <div class="stack-gap-2" style="margin-bottom:16px;">
      <label class="form-label">{% trans "Station-Admin (max 1)" %}:</label>
      {% if station_admin %}
        <div class="row-gap-8" style="align-items:center;">
          <span class="pill pill-muted">{{ station_admin.user.username }}</span>
          <form hx-post="{% url 'accounts:station_assignment_revoke' station_admin.pk %}"
                hx-on::after-request="if (event.detail.successful) window.location.reload()"
                style="display:inline;">
            {% csrf_token %}
            <button type="submit" class="btn btn-ghost btn-sm"
                    title="{% trans 'Revoke' %}">✕</button>
          </form>
        </div>
      {% else %}
        <p class="t-muted t-mono-sm" style="margin:0;">{% trans "No Station-Admin assigned." %}</p>
      {% endif %}
    </div>

    {# --- Maintainers list --- #}
    <div class="stack-gap-2" style="margin-bottom:16px;">
      <label class="form-label">{% trans "Station-Maintainers" %}:</label>
      {% if station_maintainers %}
        <ul class="stack-gap-2" style="margin:0;list-style:none;padding-left:0;">
          {% for a in station_maintainers %}
            <li class="row-gap-8" style="align-items:center;">
              <span class="pill pill-muted">{{ a.user.username }}</span>
              <form hx-post="{% url 'accounts:station_assignment_revoke' a.pk %}"
                    hx-on::after-request="if (event.detail.successful) window.location.reload()"
                    style="display:inline;">
                {% csrf_token %}
                <button type="submit" class="btn btn-ghost btn-sm" title="{% trans 'Revoke' %}">✕</button>
              </form>
            </li>
          {% endfor %}
        </ul>
      {% else %}
        <p class="t-muted t-mono-sm" style="margin:0;">{% trans "No maintainers assigned." %}</p>
      {% endif %}
    </div>

    {# --- Add-assignment form. Pick user + role, single submit. --- #}
    {% if all_users %}
      <form id="station-topology-add-form"
            hx-vals='js:{"user_pk_target": event.detail.elt.querySelector("select[name=user_pk]").value}'
            hx-on::after-request="handleStationTopologyResponse(event)">
        {% csrf_token %}
        <label class="form-label">{% trans "Add user as" %}:</label>
        <div class="row-gap-8" style="align-items:flex-end;flex-wrap:wrap;">
          <select name="user_pk" class="form-select" style="max-width:240px;">
            {% for u in all_users %}
              <option value="{{ u.pk }}">{{ u.username }}</option>
            {% endfor %}
          </select>
          <select name="role" class="form-select" style="max-width:200px;">
            <option value="admin">{% trans "Station-Admin" %}</option>
            <option value="maintainer" selected>{% trans "Station-Maintainer" %}</option>
          </select>
          <input type="hidden" name="station" value="{{ station.pk }}">
          <input type="hidden" name="takeover" value="0">
          <button type="submit" class="btn btn-primary btn-sm"
                  hx-post="{% url 'accounts:station_assignment_create' 0 %}">
            {% trans "Add" %}
          </button>
        </div>
      </form>

      <script nonce="{{ csp_nonce }}">
        function handleStationTopologyResponse(event) {
          const xhr = event.detail.xhr;
          if (event.detail.successful) {
            window.location.reload();
            return;
          }
          const form = document.getElementById('station-topology-add-form');
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

        // The hx-post URL is the StationAssignmentCreateView for the
        // selected user, not a static URL. We rewrite the form's
        // hx-post target on the fly from the selected user_pk.
        (function() {
          const form = document.getElementById('station-topology-add-form');
          const userSelect = form.querySelector('select[name=user_pk]');
          const button = form.querySelector('button[type=submit]');
          function updateUrl() {
            const userPk = userSelect.value;
            const baseUrl = "{% url 'accounts:station_assignment_create' 0 %}";
            // baseUrl ends with "/0/" — replace with "/<userPk>/"
            const newUrl = baseUrl.replace(/\/0\//, '/' + userPk + '/');
            form.setAttribute('hx-post', newUrl);
            // Re-process htmx attrs after dynamic change.
            htmx.process(form);
          }
          userSelect.addEventListener('change', updateUrl);
          updateUrl();
        })();
      </script>
    {% endif %}
  </div>
</section>
```

(Note: this script rewrites the form's `hx-post` URL on user-selection-change because `StationAssignmentCreateView` takes the target user as a URL parameter. The `{% url '...' 0 %}` produces a baseline URL like `/accounts/users/0/station_assignments/`, and the script swaps `/0/` for the selected `user_pk`.)

- [ ] **Step 5: Embed the card in station_detail.html**

In `apps/stations/templates/stations/station_detail.html`, find an appropriate place for admin-only sections (likely after the existing provisioning section, before the audit-log section). Add:

```html
{% if user.is_admin %}
  {% include "stations/_station_topology_card.html" %}
{% endif %}
```

Read the existing template to find the right insertion point.

- [ ] **Step 6: Run tests**

```bash
.venv/bin/python -m pytest tests/test_station_detail_topology_card.py -v
```

Expected: 3 PASS.

- [ ] **Step 7: Ruff + full suite**

```bash
.venv/bin/ruff format apps/stations/views.py tests/test_station_detail_topology_card.py
.venv/bin/ruff format --check . && .venv/bin/ruff check .
.venv/bin/python -m pytest -q 2>&1 | tail -3
```

Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add apps/stations/templates/stations/_station_topology_card.html apps/stations/templates/stations/station_detail.html apps/stations/views.py tests/test_station_detail_topology_card.py
git commit -m "feat(stations): station-detail topology card (admin)

HTMX card on station-detail page (admin-only) covering:
- Region picker (POSTs to StationSetRegionView)
- Station-Admin slot (max 1)
- Maintainer list

Add-assignment form uses the PR-3 StationAssignmentCreateView so
the admin-takeover 409 + JS confirm flow is reused unchanged. The
form's hx-post URL is rewritten on user-selection-change because
the create endpoint takes the target user as a URL param."
```

---

# Phase 8: Region CRUD

## Task 3: `RegionForm` + Region List/Create/Update

**Files:**
- Modify: `apps/stations/forms.py` (append `RegionForm`)
- Create: `apps/stations/views_region_crud.py`
- Modify: `apps/stations/urls.py`
- Create: `apps/stations/templates/stations/region_list.html`
- Create: `apps/stations/templates/stations/region_form.html`
- Create: `tests/test_views_region_crud.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_views_region_crud.py`:

```python
"""Tests for Region CRUD views (admin-only)."""

import pytest
from django.urls import reverse

from apps.accounts.models import AccountAuditLog, User
from apps.stations.models import Region, Station


def _user(level, username):
    u = User.objects.create_user(
        username=username, password="x", email=f"{username}@x"
    )
    u.membership_level = level
    u.save(update_fields=["membership_level"])
    return u


@pytest.mark.django_db
class TestRegionListView:
    def test_admin_can_view(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        Region.objects.create(name="Tirol", slug="tirol")
        Region.objects.create(name="OOe", slug="ooe")
        client.force_login(admin)
        response = client.get(reverse("stations:region_list"))
        assert response.status_code == 200
        body = response.content.decode()
        assert "Tirol" in body
        assert "OOe" in body

    def test_non_admin_forbidden(self, client):
        staff = _user(User.MembershipLevel.STAFF, "staff")
        client.force_login(staff)
        response = client.get(reverse("stations:region_list"))
        assert response.status_code in (302, 403)


@pytest.mark.django_db
class TestRegionCreateView:
    def test_admin_can_create(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        client.force_login(admin)
        response = client.post(
            reverse("stations:region_create"),
            {
                "name": "Tirol",
                "slug": "tirol",
                "description": "Bezirk West",
            },
        )
        assert response.status_code in (200, 302)
        assert Region.objects.filter(name="Tirol", slug="tirol").exists()
        # Signal should have emitted REGION_CREATED audit
        assert AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.REGION_CREATED
        ).exists()

    def test_duplicate_slug_returns_form_error(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        Region.objects.create(name="Old", slug="tirol")
        client.force_login(admin)
        response = client.post(
            reverse("stations:region_create"),
            {"name": "New", "slug": "tirol", "description": ""},
        )
        # Form re-renders with errors (200) — Region not created twice
        assert Region.objects.filter(slug="tirol").count() == 1


@pytest.mark.django_db
class TestRegionUpdateView:
    def test_admin_can_update(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        r = Region.objects.create(name="Tirol", slug="tirol")
        client.force_login(admin)
        response = client.post(
            reverse("stations:region_update", args=[r.pk]),
            {
                "name": "Tirol-West",
                "slug": "tirol-west",
                "description": "",
            },
        )
        assert response.status_code in (200, 302)
        r.refresh_from_db()
        assert r.name == "Tirol-West"
        assert r.slug == "tirol-west"
        # REGION_UPDATED signal
        assert AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.REGION_UPDATED
        ).exists()
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/python -m pytest tests/test_views_region_crud.py -v
```

Expected: NoReverseMatch on `stations:region_list`/`region_create`/`region_update`.

- [ ] **Step 3: Append `RegionForm` to forms.py**

In `apps/stations/forms.py`, append:

```python
from apps.stations.models import Region


class RegionForm(forms.ModelForm):
    class Meta:
        model = Region
        fields = ("name", "slug", "description")
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "slug": forms.TextInput(attrs={"class": "form-control"}),
            "description": forms.Textarea(
                attrs={"class": "form-control", "rows": 3}
            ),
        }
```

(Confirm `from django import forms` is already imported at top.)

- [ ] **Step 4: Create the CRUD view module**

Create `apps/stations/views_region_crud.py`:

```python
"""Region CRUD views (admin-only).

ListView shows all regions with station counts.
CreateView + UpdateView use RegionForm.
DeleteView shows a confirmation page with the count of stations
that will lose their region (SET_NULL via FK).

Audit-log emission is wired via signals in apps/stations/signals.py:
REGION_CREATED on post_save, REGION_UPDATED on post_save with
created=False, REGION_DELETED on post_delete.
"""

from django.urls import reverse_lazy
from django.views.generic import (
    CreateView,
    DeleteView,
    ListView,
    UpdateView,
)

from apps.accounts.views import AdminRequiredMixin
from apps.stations.forms import RegionForm
from apps.stations.models import Region


class RegionListView(AdminRequiredMixin, ListView):
    model = Region
    template_name = "stations/region_list.html"
    context_object_name = "regions"

    def get_queryset(self):
        return super().get_queryset().order_by("name")


class RegionCreateView(AdminRequiredMixin, CreateView):
    model = Region
    form_class = RegionForm
    template_name = "stations/region_form.html"
    success_url = reverse_lazy("stations:region_list")


class RegionUpdateView(AdminRequiredMixin, UpdateView):
    model = Region
    form_class = RegionForm
    template_name = "stations/region_form.html"
    success_url = reverse_lazy("stations:region_list")


class RegionDeleteView(AdminRequiredMixin, DeleteView):
    model = Region
    template_name = "stations/region_confirm_delete.html"
    success_url = reverse_lazy("stations:region_list")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["affected_stations_count"] = self.object.stations.count()
        return context
```

- [ ] **Step 5: Wire URLs**

In `apps/stations/urls.py`, add to imports:

```python
from .views_region_crud import (
    RegionCreateView,
    RegionDeleteView,
    RegionListView,
    RegionUpdateView,
)
```

Append to `urlpatterns`:

```python
    # Region CRUD
    path("regions/", RegionListView.as_view(), name="region_list"),
    path(
        "regions/create/",
        RegionCreateView.as_view(),
        name="region_create",
    ),
    path(
        "regions/<int:pk>/edit/",
        RegionUpdateView.as_view(),
        name="region_update",
    ),
    path(
        "regions/<int:pk>/delete/",
        RegionDeleteView.as_view(),
        name="region_delete",
    ),
```

- [ ] **Step 6: Create the list template**

Create `apps/stations/templates/stations/region_list.html`:

```html
{% extends "base.html" %}
{% load i18n %}

{% block title %}{% trans "Regions" %} · OE5XRX{% endblock %}

{% block breadcrumbs %}
  <a href="{% url 'dashboard:index' %}">OE5XRX</a>
  <span class="sep">/</span>
  <span class="cur">{% trans "Regions" %}</span>
{% endblock %}

{% block content %}
<div class="page-head">
  <div class="page-head-main">
    <div class="page-eyebrow">{% trans "Administration" %} · {% trans "Topology" %}</div>
    <h1 class="page-title">{% trans "Regions" %}</h1>
    <p class="page-sub">{% trans "Geographic / organizational groupings of stations." %}</p>
  </div>
  <div class="page-head-actions">
    <a href="{% url 'stations:region_create' %}" class="btn btn-primary">
      {% trans "New region" %}
    </a>
  </div>
</div>

<div class="table-wrap" data-mobile-cards>
  <table class="t-table">
    <thead>
      <tr>
        <th>{% trans "Name" %}</th>
        <th>{% trans "Slug" %}</th>
        <th>{% trans "Stations" %}</th>
        <th>{% trans "Description" %}</th>
        <th></th>
      </tr>
    </thead>
    <tbody>
      {% for r in regions %}
      <tr>
        <td data-primary>
          <span style="font-weight:600;color:var(--ink-0);">{{ r.name }}</span>
        </td>
        <td class="t-mono-sm" data-label="{% trans 'Slug' %}">{{ r.slug }}</td>
        <td data-label="{% trans 'Stations' %}">{{ r.stations.count }}</td>
        <td data-label="{% trans 'Description' %}" class="t-muted">{{ r.description|default:"—"|truncatewords:8 }}</td>
        <td class="actions">
          <a href="{% url 'stations:region_update' r.pk %}" class="btn btn-sm btn-ghost">{% trans "Edit" %}</a>
          <a href="{% url 'stations:region_delete' r.pk %}" class="btn btn-sm btn-danger">{% trans "Delete" %}</a>
        </td>
      </tr>
      {% empty %}
      <tr>
        <td colspan="5">
          <div class="empty">
            <div class="empty-title">{% trans "No regions yet." %}</div>
          </div>
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>
{% endblock %}
```

- [ ] **Step 7: Create the form template**

Create `apps/stations/templates/stations/region_form.html`:

```html
{% extends "base.html" %}
{% load i18n %}

{% block title %}{% if object %}{% trans "Edit Region" %}{% else %}{% trans "New Region" %}{% endif %} · OE5XRX{% endblock %}

{% block breadcrumbs %}
  <a href="{% url 'dashboard:index' %}">OE5XRX</a>
  <span class="sep">/</span>
  <a href="{% url 'stations:region_list' %}">{% trans "Regions" %}</a>
  <span class="sep">/</span>
  <span class="cur">{% if object %}{{ object.name }}{% else %}{% trans "New" %}{% endif %}</span>
{% endblock %}

{% block content %}
<div class="page-head"><div class="page-head-main">
  <div class="page-eyebrow">{% trans "Administration" %}</div>
  <h1 class="page-title">{% if object %}{% trans "Edit Region" %}{% else %}{% trans "New Region" %}{% endif %}</h1>
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
      <button type="submit" class="btn btn-primary">{% trans "Save region" %}</button>
      <a href="{% url 'stations:region_list' %}" class="btn btn-ghost">{% trans "Cancel" %}</a>
    </div>
  </section>
</form>
{% endblock %}
```

- [ ] **Step 8: Ruff + tests**

```bash
.venv/bin/ruff format apps/stations/forms.py apps/stations/views_region_crud.py apps/stations/urls.py tests/test_views_region_crud.py
.venv/bin/ruff format --check . && .venv/bin/ruff check .
.venv/bin/python -m pytest tests/test_views_region_crud.py -v
```

Expected: 6 PASS (TestRegionListView 2 + TestRegionCreateView 2 + TestRegionUpdateView 1 — but delete is in Task 4).

Wait — let me re-count. ListView has 2 tests. CreateView has 2 tests. UpdateView has 1 test. = 5 tests. Delete tests go in Task 4.

- [ ] **Step 9: Commit**

```bash
git add apps/stations/forms.py apps/stations/views_region_crud.py apps/stations/urls.py apps/stations/templates/stations/region_list.html apps/stations/templates/stations/region_form.html tests/test_views_region_crud.py
git commit -m "feat(stations): Region CRUD views — List/Create/Update (admin)

Admin-only Region management UI. Replaces the Django Admin for the
day-to-day case of adding/renaming geographic regions.

List shows name+slug+station-count+description with edit/delete
buttons. Create+Update use a shared RegionForm. Audit-log emission
(REGION_CREATED / REGION_UPDATED) is wired via signals in
apps/stations/signals.py — no view-level emission needed."
```

---

## Task 4: Region Delete with confirm

**Files:**
- Create: `apps/stations/templates/stations/region_confirm_delete.html`
- Append to: `tests/test_views_region_crud.py`

- [ ] **Step 1: Append failing tests**

```python
@pytest.mark.django_db
class TestRegionDeleteView:
    def test_admin_sees_confirmation_page(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        r = Region.objects.create(name="Tirol", slug="tirol")
        Station.objects.create(name="OE5A", callsign="OE5A", region=r)
        Station.objects.create(name="OE5B", callsign="OE5B", region=r)
        client.force_login(admin)
        response = client.get(reverse("stations:region_delete", args=[r.pk]))
        assert response.status_code == 200
        body = response.content.decode()
        # Confirmation shows the station count that will lose region
        assert "2" in body

    def test_admin_can_delete(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        r = Region.objects.create(name="Tirol", slug="tirol")
        s = Station.objects.create(
            name="OE5A", callsign="OE5A", region=r
        )
        client.force_login(admin)
        response = client.post(
            reverse("stations:region_delete", args=[r.pk])
        )
        assert response.status_code in (200, 302)
        assert not Region.objects.filter(pk=r.pk).exists()
        # Station's region FK is now NULL (SET_NULL)
        s.refresh_from_db()
        assert s.region is None
        # REGION_DELETED signal fired
        assert AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.REGION_DELETED
        ).exists()
```

- [ ] **Step 2: Create confirm template**

Create `apps/stations/templates/stations/region_confirm_delete.html`:

```html
{% extends "base.html" %}
{% load i18n %}

{% block title %}{% trans "Delete Region" %} · OE5XRX{% endblock %}

{% block breadcrumbs %}
  <a href="{% url 'dashboard:index' %}">OE5XRX</a>
  <span class="sep">/</span>
  <a href="{% url 'stations:region_list' %}">{% trans "Regions" %}</a>
  <span class="sep">/</span>
  <span class="cur">{{ object.name }}</span>
{% endblock %}

{% block content %}
<div class="page-head"><div class="page-head-main">
  <div class="page-eyebrow">{% trans "Administration" %}</div>
  <h1 class="page-title">{% trans "Delete Region" %}: {{ object.name }}</h1>
</div></div>

<form method="post" style="max-width:640px;">
  {% csrf_token %}
  <section class="panel">
    <div class="panel-body">
      <p>{% blocktrans %}Are you sure you want to delete this region?{% endblocktrans %}</p>
      {% if affected_stations_count %}
        <p class="t-mono-sm" style="color:var(--warn);">
          {% blocktrans count counter=affected_stations_count %}
            {{ counter }} station will have its region cleared (FK set to NULL).
          {% plural %}
            {{ counter }} stations will have their region cleared (FK set to NULL).
          {% endblocktrans %}
        </p>
      {% else %}
        <p class="t-muted t-mono-sm">{% trans "No stations are currently assigned to this region." %}</p>
      {% endif %}
    </div>
    <div class="panel-foot row-gap-8">
      <button type="submit" class="btn btn-danger">{% trans "Delete region" %}</button>
      <a href="{% url 'stations:region_list' %}" class="btn btn-ghost">{% trans "Cancel" %}</a>
    </div>
  </section>
</form>
{% endblock %}
```

- [ ] **Step 3: Ruff + tests**

```bash
.venv/bin/python -m pytest tests/test_views_region_crud.py -v
.venv/bin/ruff format --check . && .venv/bin/ruff check .
```

Expected: 7 PASS (5 + 2 new).

- [ ] **Step 4: Commit**

```bash
git add apps/stations/templates/stations/region_confirm_delete.html tests/test_views_region_crud.py
git commit -m "feat(stations): Region delete confirmation page (admin)

Standard Django DeleteView with a custom confirmation that shows
how many stations will have their region cleared (FK SET_NULL).
REGION_DELETED audit emission via existing signal in
apps/stations/signals.py."
```

---

# Wrap-Up

## Task 5: Full-suite regression + push + PR

- [ ] **Step 1: Full suite + lint**

```bash
.venv/bin/python -m pytest -q 2>&1 | tail -3
.venv/bin/ruff format --check . && .venv/bin/ruff check .
```

Expected: all PASS, ruff clean.

- [ ] **Step 2: Push**

```bash
git push -u origin feat/membership-levels-pr4-station-region-ui
```

- [ ] **Step 3: Open PR**

```bash
gh pr create --title "feat(stations): station-detail topology UI + Region CRUD (PR-4)" --body "$(cat <<'EOF'
## Summary

**PR-4 of 4** — Final UI piece. Topology management from the station's perspective + dedicated Region-CRUD pages. Operators no longer need Django Admin for any common topology workflow.

## What this PR does

**Phase 7 — Station-Detail topology:**
- New `StationSetRegionView` POST `/stations/<pk>/region/` — admin-only, sets/clears `Station.region`. Audit emission via existing `_on_station_pre_save`/`_on_station_save` signals.
- Station-Detail page gets a topology card (admin-only): Region picker + Station-Admin slot + Maintainer list + add-assignment form.
- Add-assignment form reuses the PR-3 `StationAssignmentCreateView` so the admin-takeover 409 + JS confirm flow is identical.

**Phase 8 — Region CRUD:**
- `/stations/regions/` list page with station counts per region.
- Create + Update via standard CBV + `RegionForm`.
- Delete page shows the count of stations that will have their region cleared (`SET_NULL`).
- All audit-log emission (REGION_CREATED/UPDATED/DELETED) via existing signals.

## What this PR does NOT do (out of scope)

- Region-Manager edit-permissions on stations of own region — all topology edits remain Vereins-Admin-only. Future enhancement.
- Renaming `AdminOrOperatorMixin` / `AdminOrOperatorRequiredMixin`.
- Notification preferences / Telegram routing.

## Migrations

None. View + template + form only.

## Spec + Plan

- Spec: `docs/superpowers/specs/2026-06-05-membership-levels-and-topology-roles-design.md` §5.2, §5.3
- Plan: `docs/superpowers/plans/2026-06-06-membership-levels-and-topology-roles-pr4.md`

## Test plan

- [x] Full suite green
- [x] `ruff format --check . && ruff check .` clean
- [ ] Copilot review
- [ ] Post-merge: deploy via `gh workflow run main.yml --repo OE5XRX/servers`
- [ ] Post-merge: as Vereins-Admin, visit `/de/stations/regions/` → create + edit + delete a region
- [ ] Post-merge: as Vereins-Admin, visit a Station detail page → set region, assign Station-Admin, add maintainer
- [ ] Post-merge: trigger Station-Admin takeover from station-detail (existing admin + assign new one) — confirm dialog appears

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review

**Spec coverage:**
- §5.2 Station-Detail Region picker → Task 1+2
- §5.2 Station-Admin picker (single, takeover) → Task 2 (reuses PR-3 endpoint)
- §5.2 Maintainer list → Task 2
- §5.3 Region-CRUD list/create/update/delete → Tasks 3+4

**Type/signature consistency:**
- `StationSetRegionView` URL name: `station_set_region` — used in Task 1 view, Task 2 template, Task 1 tests, Task 2 tests
- Region-CRUD URL names: `region_list`, `region_create`, `region_update`, `region_delete` — all used consistently
- `RegionForm`: defined in Task 3 forms.py, consumed in Task 3 view
- `affected_stations_count` context: Task 3 view, Task 4 template, Task 4 test

**No placeholders.** All commands and code blocks complete.
