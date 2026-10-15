# Unified Permissions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace every scattered permission check (`is_admin`, `is_internal`, Django `is_staff`, five view mixins, API `write_scoping`/topology scoping, control `ROLE_RANK`) with one registry `apps/accounts/permissions.py` (area × view/use/manage) that UI views, templates, REST API and WebSockets all read — enforced by guard tests.

**Architecture:** A pure-Python registry maps each area to a minimum `Level` per verb. Thin adapters bind it to each channel: `AreaPermissionMixin` + decorators (Django views), a lazy `can` context processor (templates), `AreaPermission` (DRF), `AreaConsumerMixin` (Channels). Every denial goes through one `require()` → `AreaDenied` → channel-specific 403 (HTML page / JSON / WS frame) + deduplicated `AccountAuditLog` entry. Old helpers are deleted outright (no alias layer); guard tests (URL walk, forbidden names, matrix, generated docs) keep it that way.

**Tech Stack:** Django 6.0, DRF 3.17, Django Channels, pytest + pytest-django, Bootstrap 5 / HTMX templates.

**Spec:** `docs/superpowers/specs/2026-10-14-unified-permissions-design.md` (approved — read it before every task; this plan argues from it).

## Global Constraints

- Levels: `APPLICANT=1 < MEMBER=2 < STAFF=3 < ADMIN=4`; anonymous/inactive/soft-deleted → `None`, passes nothing.
- Exactly three verbs `view`, `use`, `manage`; monotonic `view ≤ use ≤ manage`; undefined lower verb inherits next higher defined verb; undefined higher verb = nobody.
- Area matrix is exactly spec §1.3 (copied verbatim into Task 1 code). No new verbs, no per-object rights except `can_override_station`.
- No alias / compatibility layer for removed helpers (spec §2.3) — user requirement.
- `is_staff = is_superuser = (membership_level == ADMIN)`, derived only, used only by Django `/admin/`.
- Denials: `AreaDenied(PermissionDenied)`; API body `{"detail","required","required_level","your_level"}`; WS frame `{"type":"error","code":"forbidden","message":…}` + close 4403 on connect; audit `PERMISSION_DENIED` with `details` JSON, 10-minute dedupe per (user, area, verb, path) via Django cache, anonymous never logged.
- Tests live top-level in `tests/test_*.py` (existing subfolder `tests/module_firmware/` stays). Run `python -m pytest -q`. Lint `uvx ruff@0.16.8 check . && uvx ruff@0.16.8 format --check .`.
- Templates: never multi-line `{# … #}` — use `{% comment %}…{% endcomment %}` (CI guard).
- UI work (403 page, read-only badge, dashboard tile, sidebar): implementer MUST invoke `Skill("frontend-design")` first and reuse existing design tokens/classes (`pill`, `btn-ghost`, etc.).
- German UI strings via `gettext`/`{% trans %}` as in surrounding code; DE locale number formatting irrelevant here.
- Commits/pushes only from 16:30 local time (labour-law rule); before that commit locally only.
- One feature branch `feature/unified-permissions`, one PR (spec + plan + code). Do not merge.

## Review Focus

1. **A view, viewset action or consumer that silently keeps its old check or has none.** The URL-walk (Task 14) catches undeclared routes; reviewers must additionally confirm each declaration matches spec §1.3 (e.g. `GitHubReleasesPartialView` is `images.manage`, not `images.view`, because it calls GitHub on behalf of an importer). Pinned by Task 14 + Task 15 matrix.
2. **Demotion during a live WebSocket.** A user demoted from Member to Applicant while holding the control lock must be refused on the next PTT/command/lock frame, not on reconnect. Pinned by `test_demoted_user_loses_ptt_without_reconnect` (Task 9) and `test_demoted_user_mic_open_denied` (Task 9).
3. **Template typo `can.image.manage`** renders as falsy and silently hides a button. Pinned by the template-lint test in Task 14 (`test_template_can_references_are_valid`).
4. **API `region` change on a station by a Staff user** (stations.manage yes, topology.manage no) must be 403, including setting `region` on create. Pinned by `test_staff_cannot_set_station_region_via_api` (Task 11).
5. **Denial log flooding** — a polling client (HTMX `alert_count`, WS reconnect loop) hammering a forbidden endpoint must produce one audit row with a growing `count`, not thousands. Pinned by `test_denial_dedupe_increments_count` (Task 2) and `test_ws_connect_denials_are_deduplicated` (Task 10).

---

## File Structure

| File | Responsibility |
|---|---|
| `apps/accounts/permissions.py` (new) | Registry + core API + `AreaDenied` + `require` + denial logging + Django/DRF/Channels adapters + exempt-namespace table. Only place that compares levels. |
| `apps/accounts/context_processors.py` (new) | `permissions(request)` → `{"can": CanProxy}` |
| `apps/accounts/views_errors.py` (new) | `permission_denied(request, exception)` = `handler403` |
| `apps/api/exceptions.py` (new) | DRF exception handler rendering `AreaDenied` JSON |
| `apps/accounts/management/commands/permissions_table.py` (new) | Markdown generator for `docs/permissions.md` |
| `templates/403.html`, `templates/includes/readonly_badge.html` (new) | Denial page, read-only hint |
| `apps/accounts/migrations/0022_accountauditlog_details_permission_denied.py`, `0023_derive_is_staff.py` (new) | Schema + data migration |
| `docs/permissions.md` (new, generated) | Human-readable matrix |
| `tests/test_permissions_registry.py`, `tests/test_permission_denials.py`, `tests/test_permission_web_binding.py`, `tests/test_permission_api_ws_binding.py`, `tests/test_permission_guards.py`, `tests/test_permission_matrix.py`, `tests/test_permissions_doc.py` (new) | Tests per task |
| Deleted: `apps/api/write_scoping.py`, `apps/api/write_permissions.py`, `apps/api/scoping.py`, `apps/stations/scoping.py` | |

Everything else is modified in place (listed per task).

---

### Task 1: Registry core

**Files:**
- Create: `apps/accounts/permissions.py`
- Modify: `apps/accounts/models.py` (add `User.can`)
- Test: `tests/test_permissions_registry.py`

**Interfaces:**
- Produces (exact names used by every later task):
  - `Level(IntEnum)`: `APPLICANT=1, MEMBER=2, STAFF=3, ADMIN=4`; `Level.slug` property (`"applicant"` …); `Level.from_slug(str) -> Level | None`.
  - `VERBS: tuple[str, ...] = ("view", "use", "manage")`
  - `Area` frozen dataclass `(label, view=None, use=None, manage=None, verb_labels={})`
  - `AREAS: dict[str, Area]`
  - `level_of(user) -> Level | None`
  - `level_label(level: Level | None) -> str`
  - `required_level(area: str, verb: str) -> Level | None` (raises `KeyError` on unknown area/verb)
  - `can(user, area: str, verb: str) -> bool`
  - `verb_label(area: str, verb: str) -> str`
  - `users_with(area: str, verb: str) -> QuerySet[User]`
  - `can_override_station(user, station) -> bool`
  - `matrix() -> list[MatrixRow]` with `MatrixRow(area: str, label: str, view: Level|None, use: Level|None, manage: Level|None)` — values are *resolved* levels.
  - `User.can(area, verb) -> bool`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_permissions_registry.py
import pytest
from django.core.exceptions import ImproperlyConfigured

from apps.accounts import permissions as P
from apps.accounts.models import User
from apps.accounts.permissions import AREAS, Area, Level, can, level_of, required_level


SPEC_MATRIX = {
    # area: (view, use, manage) as declared in spec §1.3 (None = "–")
    "dashboard": ("A", None, None),
    "profile": (None, "A", None),
    "stations": ("A", None, "S"),
    "control": ("A", "M", "S"),
    "terminal": (None, "Ad", None),
    "topology": ("A", None, "Ad"),
    "deployments": ("A", None, "S"),
    "rollouts": ("A", None, "Ad"),
    "images": ("A", None, "Ad"),
    "provisioning": (None, None, "Ad"),
    "alerts": ("A", None, "S"),
    "alert_rules": ("A", None, "Ad"),
    "modules": ("A", None, "S"),
    "users": ("M", None, "Ad"),
    "api_tokens": (None, "M", None),
    "audit": ("Ad", None, None),
    "sso": (None, None, "Ad"),
}
_L = {"A": Level.APPLICANT, "M": Level.MEMBER, "S": Level.STAFF, "Ad": Level.ADMIN, None: None}


def test_registry_matches_spec_matrix_exactly():
    assert set(AREAS) == set(SPEC_MATRIX)
    for name, (v, u, m) in SPEC_MATRIX.items():
        area = AREAS[name]
        assert (area.view, area.use, area.manage) == (_L[v], _L[u], _L[m]), name


def test_lower_verb_inherits_next_higher_defined():
    assert required_level("terminal", "view") == Level.ADMIN
    assert required_level("provisioning", "view") == Level.ADMIN
    assert required_level("provisioning", "use") == Level.ADMIN
    assert required_level("profile", "view") == Level.APPLICANT
    assert required_level("api_tokens", "view") == Level.MEMBER
    assert required_level("stations", "use") == Level.STAFF


def test_higher_undefined_verb_is_nobody():
    assert required_level("audit", "manage") is None
    assert required_level("audit", "use") is None
    assert required_level("profile", "manage") is None
    assert required_level("dashboard", "manage") is None


def test_unknown_area_or_verb_raises_keyerror():
    with pytest.raises(KeyError):
        required_level("image", "view")
    with pytest.raises(KeyError):
        required_level("images", "edit")


def test_monotonic_violation_rejected():
    with pytest.raises(ImproperlyConfigured):
        P._validate_areas({"x": Area("x", view=Level.ADMIN, manage=Level.MEMBER)})


def test_area_without_any_verb_rejected():
    with pytest.raises(ImproperlyConfigured):
        P._validate_areas({"x": Area("x")})


def test_verb_label_for_undefined_verb_rejected():
    with pytest.raises(ImproperlyConfigured):
        P._validate_areas({"x": Area("x", view=Level.MEMBER, verb_labels={"manage": "boom"})})


@pytest.mark.parametrize(
    "slug,expected",
    [("applicant", Level.APPLICANT), ("member", Level.MEMBER), ("staff", Level.STAFF), ("admin", Level.ADMIN)],
)
def test_level_of_active_user(db, slug, expected):
    u = User.objects.create_user(username=f"u-{slug}", password="x", membership_level=slug)
    assert level_of(u) == expected


def test_level_of_anonymous_inactive_deleted(db):
    from django.contrib.auth.models import AnonymousUser
    from django.utils import timezone

    assert level_of(None) is None
    assert level_of(AnonymousUser()) is None
    u = User.objects.create_user(username="ina", password="x", membership_level="admin", is_active=False)
    assert level_of(u) is None
    d = User.objects.create_user(username="del", password="x", membership_level="admin")
    d.deleted_at = timezone.now()
    assert level_of(d) is None


def test_can_and_user_can(db):
    m = User.objects.create_user(username="m", password="x", membership_level="member")
    assert can(m, "control", "use") is True
    assert can(m, "control", "manage") is False
    assert m.can("users", "view") is True
    assert m.can("audit", "view") is False


def test_can_anonymous_is_false():
    from django.contrib.auth.models import AnonymousUser

    assert can(AnonymousUser(), "dashboard", "view") is False


def test_can_unknown_area_raises(db):
    m = User.objects.create_user(username="m2", password="x", membership_level="admin")
    with pytest.raises(KeyError):
        can(m, "imagez", "view")


def test_users_with_excludes_inactive_and_deleted(db):
    from django.utils import timezone

    a = User.objects.create_user(username="a", password="x", membership_level="admin")
    s = User.objects.create_user(username="s", password="x", membership_level="staff")
    User.objects.create_user(username="m", password="x", membership_level="member")
    User.objects.create_user(username="i", password="x", membership_level="admin", is_active=False)
    d = User.objects.create_user(username="d", password="x", membership_level="admin")
    d.deleted_at = timezone.now()
    d.save(update_fields=["deleted_at"])
    assert set(P.users_with("alerts", "manage")) == {a, s}
    assert set(P.users_with("audit", "manage")) == set()


def test_can_override_station(db, station):
    from apps.stations.models import Region, RegionAssignment, StationAssignment

    staff = User.objects.create_user(username="st", password="x", membership_level="staff")
    member = User.objects.create_user(username="mm", password="x", membership_level="member")
    sadm = User.objects.create_user(username="sa", password="x", membership_level="member")
    rmgr = User.objects.create_user(username="rm", password="x", membership_level="member")
    region = Region.objects.create(name="R1")
    station.region = region
    station.save(update_fields=["region"])
    StationAssignment.objects.create(user=sadm, station=station, role="admin")
    RegionAssignment.objects.create(user=rmgr, region=region, role="manager")
    assert P.can_override_station(staff, station) is True
    assert P.can_override_station(sadm, station) is True
    assert P.can_override_station(rmgr, station) is True
    assert P.can_override_station(member, station) is False


def test_matrix_rows_resolved():
    rows = {r.area: r for r in P.matrix()}
    assert rows["terminal"].view == Level.ADMIN
    assert rows["audit"].manage is None
    assert list(rows) == list(AREAS)
```

(Adjust `StationAssignment`/`RegionAssignment` import path and required fields to the real models — check `apps/stations/models.py`; role values are `"admin"`, `"maintainer"` for stations and `"manager"` for regions per `User.is_station_admin` / `is_region_manager`.)

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_permissions_registry.py -q`
Expected: FAIL — `ModuleNotFoundError: apps.accounts.permissions`.

- [ ] **Step 3: Implement `apps/accounts/permissions.py` (registry part)**

```python
"""The one place that defines who may do what.

Model (spec 2026-10-14-unified-permissions-design.md): every capability is an
*area* × one of three fixed verbs (view ⊆ use ⊆ manage), each mapped to a
minimum membership level. UI views, templates, the REST API and WebSockets all
read from here; nothing else compares membership levels for authorization.

The single assignment-based exception is :func:`can_override_station`.
"""

from __future__ import annotations

import dataclasses
from enum import IntEnum

from django.core.exceptions import ImproperlyConfigured
from django.utils.translation import gettext_lazy as _

VERBS: tuple[str, ...] = ("view", "use", "manage")


class Level(IntEnum):
    APPLICANT = 1
    MEMBER = 2
    STAFF = 3
    ADMIN = 4

    @property
    def slug(self) -> str:
        return self.name.lower()

    @classmethod
    def from_slug(cls, slug) -> Level | None:
        return _BY_SLUG.get(slug)


_BY_SLUG = {lvl.slug: lvl for lvl in Level}


@dataclasses.dataclass(frozen=True)
class Area:
    label: str
    view: Level | None = None
    use: Level | None = None
    manage: Level | None = None
    verb_labels: dict[str, str] = dataclasses.field(default_factory=dict)


A, M, S, AD = Level.APPLICANT, Level.MEMBER, Level.STAFF, Level.ADMIN

AREAS: dict[str, Area] = {
    "dashboard": Area(_("Dashboard"), view=A),
    "profile": Area(_("Eigenes Profil"), use=A),
    "stations": Area(
        _("Stationen"),
        view=A,
        manage=S,
        verb_labels={"manage": _("Stationen anlegen/bearbeiten, Logbuch, Fotos, Station-Audit")},
    ),
    "control": Area(
        _("Stationssteuerung"),
        view=A,
        use=M,
        manage=S,
        verb_labels={
            "use": _("Station bedienen (Lock, PTT, Mikrofon)"),
            "manage": _("Station kalibrieren (Filter, Lautstärke, Audio-Diagnose)"),
        },
    ),
    "terminal": Area(_("Terminal"), use=AD, verb_labels={"use": _("Root-Shell öffnen")}),
    "topology": Area(
        _("Regionen & Zuweisungen"),
        view=A,
        manage=AD,
        verb_labels={"manage": _("Regionen und Zuweisungen verwalten")},
    ),
    "deployments": Area(
        _("Deployments"),
        view=A,
        manage=S,
        verb_labels={"manage": _("Deployments auslösen/abbrechen")},
    ),
    "rollouts": Area(
        _("Rollouts"),
        view=A,
        manage=AD,
        verb_labels={"manage": _("Rollout-Sequenz bearbeiten, Gruppen-Upgrade")},
    ),
    "images": Area(
        _("Images"),
        view=A,
        manage=AD,
        verb_labels={"manage": _("Images importieren/archivieren/als Latest markieren")},
    ),
    "provisioning": Area(_("Provisioning"), manage=AD),
    "alerts": Area(
        _("Alarme"), view=A, manage=S, verb_labels={"manage": _("Alarme quittieren/auflösen")}
    ),
    "alert_rules": Area(
        _("Alarm-Regeln"),
        view=A,
        manage=AD,
        verb_labels={"manage": _("Alarm-Regeln und Benachrichtigungen konfigurieren")},
    ),
    "modules": Area(
        _("Module & Firmware"),
        view=A,
        manage=S,
        verb_labels={"manage": _("Module bestätigen, Firmware importieren/archivieren")},
    ),
    "users": Area(
        _("Benutzer"), view=M, manage=AD, verb_labels={"manage": _("Benutzer verwalten")}
    ),
    "api_tokens": Area(_("API-Tokens"), use=M, verb_labels={"use": _("API-Tokens erstellen")}),
    "audit": Area(_("Audit-Log"), view=AD),
    "sso": Area(_("SSO-Verwaltung"), manage=AD),
}

_DEFAULT_VERB_LABELS = {"view": _("ansehen"), "use": _("bedienen"), "manage": _("verwalten")}


def _validate_areas(areas: dict[str, Area]) -> None:
    for name, area in areas.items():
        defined = [getattr(area, v) for v in VERBS if getattr(area, v) is not None]
        if not defined:
            raise ImproperlyConfigured(f"permissions: area {name!r} defines no verb")
        if defined != sorted(defined):
            raise ImproperlyConfigured(f"permissions: area {name!r} is not monotonic")
        for verb in area.verb_labels:
            if verb not in VERBS or getattr(area, verb) is None:
                raise ImproperlyConfigured(
                    f"permissions: area {name!r} labels undefined verb {verb!r}"
                )


_validate_areas(AREAS)


def required_level(area: str, verb: str) -> Level | None:
    """Resolved minimum level; ``None`` = nobody. Unknown area/verb → KeyError."""
    spec = AREAS[area]
    if verb not in VERBS:
        raise KeyError(verb)
    for candidate in VERBS[VERBS.index(verb) :]:
        level = getattr(spec, candidate)
        if level is not None:
            return level
    return None


def level_of(user) -> Level | None:
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    if not getattr(user, "is_active", False) or getattr(user, "deleted_at", None) is not None:
        return None
    return Level.from_slug(getattr(user, "membership_level", None))


def level_label(level: Level | None) -> str:
    if level is None:
        return str(_("niemand"))
    from apps.accounts.models import User

    return str(User.MembershipLevel(level.slug).label)


def verb_label(area: str, verb: str) -> str:
    spec = AREAS[area]
    if verb in spec.verb_labels:
        return str(spec.verb_labels[verb])
    return f"{spec.label} – {_DEFAULT_VERB_LABELS[verb]}"


def can(user, area: str, verb: str) -> bool:
    needed = required_level(area, verb)  # KeyError on typos, even for anonymous
    have = level_of(user)
    return needed is not None and have is not None and have >= needed


def users_with(area: str, verb: str):
    from apps.accounts.models import User

    needed = required_level(area, verb)
    if needed is None:
        return User.objects.none()
    slugs = [lvl.slug for lvl in Level if lvl >= needed]
    return User.objects.filter(is_active=True, deleted_at__isnull=True, membership_level__in=slugs)


def can_override_station(user, station) -> bool:
    """The only assignment-based rule: preempt/override the control lock.

    ``control.manage`` OR Station-Admin of ``station`` OR Region-Manager of its
    region. Assignment holders additionally need ``control.use`` (preempting
    hands them the lock, which they must be allowed to operate).
    """
    if can(user, "control", "manage"):
        return True
    if not can(user, "control", "use"):
        return False
    if user.station_assignments.filter(station=station, role="admin").exists():
        return True
    region_id = getattr(station, "region_id", None)
    return region_id is not None and user.region_assignments.filter(
        region_id=region_id, role="manager"
    ).exists()


@dataclasses.dataclass(frozen=True)
class MatrixRow:
    area: str
    label: str
    view: Level | None
    use: Level | None
    manage: Level | None


def matrix() -> list[MatrixRow]:
    return [
        MatrixRow(
            area=name,
            label=str(spec.label),
            view=required_level(name, "view"),
            use=required_level(name, "use"),
            manage=required_level(name, "manage"),
        )
        for name, spec in AREAS.items()
    ]
```

Note: `can_override_station`'s `control.use` floor for assignment holders tightens spec §3.4 by one condition (an Applicant holding a stale assignment could otherwise preempt and get a lock they cannot operate). Keep it; it is called out in the PR description.

- [ ] **Step 4: Add `User.can`** in `apps/accounts/models.py` (next to the other User methods):

```python
    def can(self, area, verb):
        from apps.accounts.permissions import can

        return can(self, area, verb)
```

- [ ] **Step 5: Run tests** — `python -m pytest tests/test_permissions_registry.py -q` → PASS; full suite `python -m pytest -q` → PASS (nothing else changed).

- [ ] **Step 6: Commit**

```bash
git add apps/accounts/permissions.py apps/accounts/models.py tests/test_permissions_registry.py
git commit -m "feat(permissions): area × verb registry with can()/required_level()"
```

---

### Task 2: Denial pipeline — `AreaDenied`, `require()`, audit with dedupe

**Files:**
- Modify: `apps/accounts/models.py` (`AccountAuditLog.EventType.PERMISSION_DENIED`, `details` field, `log(details=…)`)
- Create: `apps/accounts/migrations/0022_accountauditlog_details_permission_denied.py` (via `makemigrations accounts -n accountauditlog_details_permission_denied`)
- Modify: `apps/accounts/permissions.py`
- Test: `tests/test_permission_denials.py`

**Interfaces:**
- Consumes: Task 1 API.
- Produces:
  - `AreaDenied(PermissionDenied)` with attrs `area, verb, required_level: Level|None, actual_level: Level|None`, `str(exc)` = user-facing German message; `exc.as_api_body() -> dict`.
  - `require(user, area, verb, *, request=None, channel="web", path=None, method=None, ip=None) -> None` — raises `AreaDenied` and logs (unless anonymous).
  - `log_denial(user, exc: AreaDenied, *, channel: str, path: str, method: str, ip: str | None) -> None`
  - `DENIAL_DEDUPE_SECONDS = 600`
  - `AccountAuditLog.EventType.PERMISSION_DENIED = "permission_denied"`, `AccountAuditLog.details` JSONField.

- [ ] **Step 1: Failing tests**

```python
# tests/test_permission_denials.py
import pytest
from django.core.cache import cache
from django.test import RequestFactory

from apps.accounts.models import AccountAuditLog, User
from apps.accounts.permissions import AreaDenied, Level, require

DENIED = AccountAuditLog.EventType.PERMISSION_DENIED


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def member(db):
    return User.objects.create_user(username="mem", password="x", membership_level="member")


def test_require_passes_silently(member):
    require(member, "control", "use")
    assert not AccountAuditLog.objects.filter(event_type=DENIED).exists()


def test_require_raises_with_levels_and_logs(member):
    req = RequestFactory().post("/de/images/1/archive/", REMOTE_ADDR="10.0.0.9")
    req.user = member
    with pytest.raises(AreaDenied) as ei:
        require(member, "images", "manage", request=req)
    exc = ei.value
    assert (exc.area, exc.verb) == ("images", "manage")
    assert exc.required_level == Level.ADMIN and exc.actual_level == Level.MEMBER
    assert "Vereins-Admin" in str(exc) and "Vereins-Mitglied" in str(exc)
    row = AccountAuditLog.objects.get(event_type=DENIED)
    assert row.actor == member
    assert row.ip_address == "10.0.0.9"
    assert row.details == {
        "area": "images",
        "verb": "manage",
        "path": "/de/images/1/archive/",
        "method": "POST",
        "channel": "web",
        "required_level": "admin",
        "actual_level": "member",
        "count": 1,
    }


def test_denial_dedupe_increments_count(member):
    for _ in range(3):
        with pytest.raises(AreaDenied):
            require(member, "audit", "view", path="/de/audit/", method="GET")
    row = AccountAuditLog.objects.get(event_type=DENIED)
    assert row.details["count"] == 3


def test_dedupe_is_per_path_and_verb(member):
    for path in ("/a/", "/b/"):
        with pytest.raises(AreaDenied):
            require(member, "audit", "view", path=path, method="GET")
    assert AccountAuditLog.objects.filter(event_type=DENIED).count() == 2


def test_dedupe_survives_deleted_row(member):
    with pytest.raises(AreaDenied):
        require(member, "audit", "view", path="/x/", method="GET")
    AccountAuditLog.objects.filter(event_type=DENIED).delete()
    with pytest.raises(AreaDenied):
        require(member, "audit", "view", path="/x/", method="GET")
    assert AccountAuditLog.objects.get(event_type=DENIED).details["count"] == 1


def test_anonymous_raises_but_is_not_logged(db):
    from django.contrib.auth.models import AnonymousUser

    with pytest.raises(AreaDenied):
        require(AnonymousUser(), "dashboard", "view", path="/", method="GET")
    assert not AccountAuditLog.objects.filter(event_type=DENIED).exists()


def test_api_body_shape(member):
    with pytest.raises(AreaDenied) as ei:
        require(member, "images", "manage", path="/api/v1/images/1/archive/", method="POST", channel="api")
    body = ei.value.as_api_body()
    assert body["required"] == "images.manage"
    assert body["required_level"] == "admin"
    assert body["your_level"] == "member"
    assert body["detail"]


def test_nobody_verb_message(member):
    with pytest.raises(AreaDenied) as ei:
        require(member, "audit", "manage", path="/x/", method="POST")
    assert ei.value.required_level is None
    assert ei.value.as_api_body()["required_level"] is None
```

- [ ] **Step 2: Run** `python -m pytest tests/test_permission_denials.py -q` → FAIL (ImportError `AreaDenied`).

- [ ] **Step 3: Model changes** in `apps/accounts/models.py`:

```python
        # === Added in Unified Permissions (2026-10-14) ===
        PERMISSION_DENIED = "permission_denied", _("Permission Denied")
```
appended to `EventType`, and a new field after `message`:
```python
    details = models.JSONField(_("details"), default=dict, blank=True)
```
and extend `log(...)` with `details=None` → `details=details or {}`. Then `python manage.py makemigrations accounts -n accountauditlog_details_permission_denied` and commit the generated migration.

- [ ] **Step 4: Append to `apps/accounts/permissions.py`**

```python
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db import transaction

DENIAL_DEDUPE_SECONDS = 600


class AreaDenied(PermissionDenied):
    def __init__(self, area: str, verb: str, actual_level: Level | None):
        self.area = area
        self.verb = verb
        self.required_level = required_level(area, verb)
        self.actual_level = actual_level
        super().__init__(self._message())

    def _message(self) -> str:
        what = verb_label(self.area, self.verb)
        if self.required_level is None:
            return str(_("Keine Berechtigung — %(what)s ist für niemanden freigegeben.") % {"what": what})
        return str(
            _(
                "Keine Berechtigung — %(what)s erfordert %(need)s, du bist %(have)s. "
                "Wenn du glaubst, dass das falsch ist, wende dich an einen Admin."
            )
            % {
                "what": what,
                "need": level_label(self.required_level),
                "have": level_label(self.actual_level),
            }
        )

    def as_api_body(self) -> dict:
        return {
            "detail": str(self),
            "required": f"{self.area}.{self.verb}",
            "required_level": self.required_level.slug if self.required_level else None,
            "your_level": self.actual_level.slug if self.actual_level else None,
        }


def _client_ip(request):
    return request.META.get("REMOTE_ADDR") if request is not None else None


def log_denial(user, exc: AreaDenied, *, channel: str, path: str, method: str, ip=None) -> None:
    from apps.accounts.models import AccountAuditLog

    if not getattr(user, "is_authenticated", False):
        return  # anonymous: never logged
    key = f"perm-denied:{user.pk}:{exc.area}:{exc.verb}:{path}"
    pk = cache.get(key)
    if pk is not None:
        with transaction.atomic():
            row = AccountAuditLog.objects.select_for_update().filter(pk=pk).first()
            if row is not None:
                row.details["count"] = int(row.details.get("count", 1)) + 1
                row.save(update_fields=["details"])
                return
    row = AccountAuditLog.log(
        event_type=AccountAuditLog.EventType.PERMISSION_DENIED,
        actor=user,
        message=f"{exc.area}.{exc.verb} denied ({method} {path})",
        ip_address=ip,
        details={
            "area": exc.area,
            "verb": exc.verb,
            "path": path,
            "method": method,
            "channel": channel,
            "required_level": exc.required_level.slug if exc.required_level else None,
            "actual_level": exc.actual_level.slug if exc.actual_level else None,
            "count": 1,
        },
    )
    cache.set(key, row.pk, DENIAL_DEDUPE_SECONDS)


def require(user, area, verb, *, request=None, channel="web", path=None, method=None, ip=None):
    if can(user, area, verb):
        return
    exc = AreaDenied(area, verb, level_of(user))
    if getattr(user, "is_authenticated", False):
        log_denial(
            user,
            exc,
            channel=channel,
            path=path if path is not None else (request.path if request is not None else ""),
            method=method if method is not None else (request.method if request is not None else ""),
            ip=ip if ip is not None else _client_ip(request),
        )
    raise exc
```

(An inactive-but-authenticated user — e.g. deactivated mid-session — *is* logged with `actual_level: null`; only anonymous is skipped.)

- [ ] **Step 5: Run** the new test file and the existing `tests/test_account_audit_log.py` → PASS. Full suite → PASS.

- [ ] **Step 6: Commit** `feat(permissions): AreaDenied + require() with deduplicated PERMISSION_DENIED audit`

---

### Task 3: Web binding — mixin, decorators, 403 page, `can` in templates

**Files:**
- Modify: `apps/accounts/permissions.py` (adapters)
- Create: `apps/accounts/context_processors.py`, `apps/accounts/views_errors.py`, `templates/403.html`, `templates/includes/readonly_badge.html`
- Modify: `config/settings/base.py` (context processor), `config/urls.py` (`handler403`)
- Test: `tests/test_permission_web_binding.py`

**Interfaces:**
- Produces:
  - `SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})`
  - `resolve_verb(view, method: str) -> str` — `view.verbs[method]` > `view.verb` > safe→`view` / else `manage`.
  - `class AreaPermissionMixin` — attrs `area: str|None`, `verb: str|None`, `verbs: dict|None`; overridable `get_area_verb(self) -> tuple[str, str]`; anonymous → `redirect_to_login`; otherwise `require(..., channel="web")`. Must be the **leftmost** base. Replaces `LoginRequiredMixin` on the view (drop it).
  - `area_permission(area, verb=None)` decorator for function views; sets `func.area = area`, `func.verb = verb`.
  - `permission_exempt(reason: str)` decorator for function views; sets `func.permission_exempt = reason`. CBVs declare the class attr `permission_exempt = "…"`.
  - Template: `{{ can.<area>.<verb> }}` → bool. `{% include "includes/readonly_badge.html" %}`.

- [ ] **Step 1: Failing tests** (use a test-only urlconf)

```python
# tests/test_permission_web_binding.py
import pytest
from django.http import HttpResponse
from django.template import Context, Template
from django.test import RequestFactory, override_settings
from django.urls import path
from django.views import View

from apps.accounts.models import AccountAuditLog, User
from apps.accounts.permissions import AreaPermissionMixin, area_permission, permission_exempt


class ImagesView(AreaPermissionMixin, View):
    area = "images"

    def get(self, request):
        return HttpResponse("list")

    def post(self, request):
        return HttpResponse("changed")


class TokenView(AreaPermissionMixin, View):
    area = "api_tokens"
    verb = "use"

    def get(self, request):
        return HttpResponse("ok")

    def post(self, request):
        return HttpResponse("ok")


@area_permission("audit", "view")
def audit_fn(request):
    return HttpResponse("audit")


@permission_exempt("test: public")
def public_fn(request):
    return HttpResponse("pub")


urlpatterns = [
    path("t/images/", ImagesView.as_view()),
    path("t/tokens/", TokenView.as_view()),
    path("t/audit/", audit_fn),
    path("t/pub/", public_fn),
]


def _u(level):
    return User.objects.create_user(username=f"w-{level}", password="x", membership_level=level)


pytestmark = [pytest.mark.django_db, pytest.mark.urls("tests.test_permission_web_binding")]


def test_safe_method_needs_view(client):
    client.force_login(_u("applicant"))
    assert client.get("/t/images/").status_code == 200


def test_unsafe_method_needs_manage_and_renders_403_page(client):
    client.force_login(_u("staff"))
    r = client.post("/t/images/")
    assert r.status_code == 403
    body = r.content.decode()
    assert "Keine Berechtigung" in body
    assert "Vereins-Admin" in body and "Vereins-Staff" in body
    assert AccountAuditLog.objects.filter(event_type="permission_denied").count() == 1


def test_explicit_verb_applies_to_all_methods(client):
    client.force_login(_u("member"))
    assert client.post("/t/tokens/").status_code == 200
    client.force_login(_u("applicant"))
    assert client.get("/t/tokens/").status_code == 403


def test_function_decorator(client):
    client.force_login(_u("staff"))
    assert client.get("/t/audit/").status_code == 403
    client.force_login(_u("admin"))
    assert client.get("/t/audit/").status_code == 200


def test_anonymous_redirects_to_login_not_logged(client):
    r = client.get("/t/images/")
    assert r.status_code == 302 and "/login" in r["Location"]
    assert not AccountAuditLog.objects.filter(event_type="permission_denied").exists()


def test_exempt_marker():
    assert public_fn.permission_exempt == "test: public"


def test_generic_permission_denied_uses_same_page(client, rf):
    from django.core.exceptions import PermissionDenied

    from apps.accounts.views_errors import permission_denied

    req = rf.get("/x/")
    req.user = _u("member")
    resp = permission_denied(req, PermissionDenied("Du kannst dich nicht selbst herabstufen."))
    assert resp.status_code == 403
    assert "Du kannst dich nicht selbst herabstufen." in resp.content.decode()


def test_can_context_processor(rf):
    from apps.accounts.context_processors import permissions

    req = rf.get("/")
    req.user = _u("staff")
    ctx = permissions(req)
    tpl = Template("{% if can.alerts.manage %}A{% endif %}{% if can.images.manage %}B{% endif %}")
    assert tpl.render(Context(ctx)) == "A"
```

- [ ] **Step 2: Run** → FAIL (ImportError).

- [ ] **Step 3: Implement adapters** (append to `permissions.py`):

```python
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def resolve_verb(view, method: str) -> str:
    method = method.upper()
    verbs = getattr(view, "verbs", None) or {}
    if method in verbs:
        return verbs[method]
    explicit = getattr(view, "verb", None)
    if explicit:
        return explicit
    return "view" if method in SAFE_METHODS else "manage"


class AreaPermissionMixin:
    """Leftmost base of every Django CBV. Declare ``area`` (+ optional ``verb``/``verbs``)."""

    area: str | None = None
    verb: str | None = None
    verbs: dict[str, str] | None = None

    def get_area_verb(self) -> tuple[str, str]:
        return self.area, resolve_verb(self, self.request.method)

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            from django.contrib.auth.views import redirect_to_login

            return redirect_to_login(request.get_full_path())
        area, verb = self.get_area_verb()
        require(request.user, area, verb, request=request, channel="web")
        return super().dispatch(request, *args, **kwargs)


def area_permission(area: str, verb: str | None = None):
    import functools

    def decorator(func):
        @functools.wraps(func)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                from django.contrib.auth.views import redirect_to_login

                return redirect_to_login(request.get_full_path())
            used = verb or ("view" if request.method.upper() in SAFE_METHODS else "manage")
            require(request.user, area, used, request=request, channel="web")
            return func(request, *args, **kwargs)

        wrapper.area = area
        wrapper.verb = verb
        wrapper.area_enforced = True
        return wrapper

    return decorator


def permission_exempt(reason: str):
    if not reason or not reason.strip():
        raise ImproperlyConfigured("permission_exempt needs a non-empty reason")

    def decorator(func):
        func.permission_exempt = reason
        return func

    return decorator
```

`apps/accounts/context_processors.py`:

```python
"""Expose the permission registry to templates as ``can.<area>.<verb>``."""

from apps.accounts.permissions import AREAS, VERBS, can


class _AreaProxy:
    __slots__ = ("_user", "_area", "_cache")

    def __init__(self, user, area):
        self._user, self._area, self._cache = user, area, {}

    def __getattr__(self, verb):
        if verb not in VERBS:
            raise AttributeError(verb)
        if verb not in self._cache:
            self._cache[verb] = can(self._user, self._area, verb)
        return self._cache[verb]


class CanProxy:
    __slots__ = ("_user", "_areas")

    def __init__(self, user):
        self._user, self._areas = user, {}

    def __getitem__(self, area):
        if area not in AREAS:
            raise KeyError(area)
        if area not in self._areas:
            self._areas[area] = _AreaProxy(self._user, area)
        return self._areas[area]


def permissions(request):
    return {"can": CanProxy(getattr(request, "user", None))}
```

Register `"apps.accounts.context_processors.permissions"` after `django.contrib.auth.context_processors.auth` in `config/settings/base.py`.

`apps/accounts/views_errors.py`:

```python
from django.shortcuts import render

from apps.accounts.permissions import AreaDenied, level_label, verb_label


def permission_denied(request, exception=None):
    ctx = {"message": str(exception) if exception and str(exception) else ""}
    if isinstance(exception, AreaDenied):
        ctx.update(
            area_denied=True,
            what=verb_label(exception.area, exception.verb),
            need=level_label(exception.required_level),
            have=level_label(exception.actual_level),
        )
    return render(request, "403.html", ctx, status=403)
```

`config/urls.py`: add `handler403 = "apps.accounts.views_errors.permission_denied"`.

`templates/403.html` — **invoke `Skill("frontend-design")` first.** Extends `base.html`, normal layout. Heading `{% trans "Keine Berechtigung" %}`; if `area_denied`: `{% blocktrans %}<strong>{{ what }}</strong> erfordert <strong>{{ need }}</strong>, du bist <strong>{{ have }}</strong>.{% endblocktrans %}` plus `{% trans "Wenn du glaubst, dass das falsch ist, wende dich an einen Admin." %}`; else `message` or a generic `{% trans "Für diese Aktion fehlt dir die Berechtigung." %}`. Two buttons: "Zurück" (`javascript:history.back()` — CSP: use a plain `<a href="{{ request.META.HTTP_REFERER|default:'/' }}">` instead of inline JS) and "Zur Übersicht" (`{% url 'dashboard:index' %}`). Anonymous never reaches this page.

`templates/includes/readonly_badge.html`: a small muted pill `{% trans "nur lesend" %}` with `title="{% trans 'Du kannst diesen Bereich ansehen, aber nicht ändern.' %}"`. Used in later tasks on pages rendered for `view`-only users.

- [ ] **Step 4: Run** new tests → PASS; full suite → PASS.

- [ ] **Step 5: Commit** `feat(permissions): Django view mixin/decorators, 403 page, can context processor`

---

### Task 4: API + WebSocket binding primitives

**Files:**
- Modify: `apps/accounts/permissions.py`
- Create: `apps/api/exceptions.py`
- Modify: `config/settings/base.py` (`REST_FRAMEWORK["EXCEPTION_HANDLER"]`)
- Test: `tests/test_permission_api_ws_binding.py`

**Interfaces:**
- Produces:
  - `class AreaPermission(BasePermission)` — non-user principals (DeviceKey, anonymous) → `False` (DRF 401/403, not logged); otherwise `require(user, view.area, resolve_verb(view, request.method), request=request, channel="api")`; `has_object_permission` → `True` (no object rules).
  - DRF view convention: class attrs `area`, `verb = None`, `verbs = None` on every user viewset; `@action(..., verb="manage")` sets the action-specific verb (DRF passes action kwargs as initkwargs, so the class **must** declare `verb = None`).
  - `area_exception_handler(exc, context)` returns `Response(exc.as_api_body(), status=403)` for `AreaDenied`, else DRF default.
  - `ws_denied_frame(exc: AreaDenied, request_id=None) -> dict` = `{"type":"error","code":"forbidden","message":str(exc),"request_id":request_id}` (omit `request_id` key when `None`).
  - `class AreaConsumerMixin` with attrs `area: str|None`, `connect_verb = "view"`, and:
    - `async def area_connect_gate(self) -> bool` — call right **after** `accept()`; on deny sends `ws_denied_frame` and `close(code=4403)`; returns allowed.
    - `async def area_allows(self, verb, *, area=None, request_id=None, notify=True) -> bool` — re-loads the user from DB (`_fresh_user`), checks, logs via `require` (channel `"ws"`, path = `scope["path"]`, method = `f"WS {verb}"`, ip = `scope["client"][0]`), sends frame on deny when `notify`.
    - `async def fresh_user(self)` → fresh `User` or `None`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_permission_api_ws_binding.py
import json

import pytest
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.testing import WebsocketCommunicator
from django.urls import path
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.test import APIClient
from rest_framework.views import APIView
from rest_framework.viewsets import ViewSet

from apps.accounts.models import AccountAuditLog, User
from apps.accounts.permissions import AreaConsumerMixin, AreaPermission


class ImgViewSet(ViewSet):
    permission_classes = [AreaPermission]
    area = "images"
    verb = None
    verbs = None

    def list(self, request):
        return Response([])

    @action(detail=False, methods=["get"], verb="manage")
    def available(self, request):
        return Response([])


urlpatterns = [
    path("t/img/", ImgViewSet.as_view({"get": "list"})),
    path("t/img/available/", ImgViewSet.as_view({"get": "available"}, verb="manage")),
]

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.urls("tests.test_permission_api_ws_binding")]


def _u(level):
    return User.objects.create_user(username=f"a-{level}", password="x", membership_level=level)


def test_api_view_allowed_for_applicant():
    c = APIClient()
    c.force_authenticate(_u("applicant"))
    assert c.get("/t/img/").status_code == 200


def test_api_action_verb_denied_with_body():
    c = APIClient()
    c.force_authenticate(_u("staff"))
    r = c.get("/t/img/available/")
    assert r.status_code == 403
    assert r.json()["required"] == "images.manage"
    assert r.json()["your_level"] == "staff"
    assert AccountAuditLog.objects.get(event_type="permission_denied").details["channel"] == "api"


def test_api_anonymous_not_logged():
    r = APIClient().get("/t/img/")
    assert r.status_code in (401, 403)
    assert not AccountAuditLog.objects.filter(event_type="permission_denied").exists()


class DummyConsumer(AreaConsumerMixin, AsyncWebsocketConsumer):
    area = "control"

    async def connect(self):
        await self.accept()
        await self.area_connect_gate()

    async def receive(self, text_data=None, bytes_data=None):
        if await self.area_allows("use", request_id="r1"):
            await self.send(text_data=json.dumps({"type": "ok"}))


async def _connect(user):
    comm = WebsocketCommunicator(DummyConsumer.as_asgi(), "/ws/t/")
    comm.scope["user"] = user
    comm.scope["client"] = ("10.1.1.1", 1234)
    ok, _ = await comm.connect()
    return comm


@pytest.mark.asyncio
async def test_ws_connect_denied_frame_and_4403():
    from asgiref.sync import sync_to_async

    user = await sync_to_async(_u)("applicant")
    await sync_to_async(User.objects.filter(pk=user.pk).update)(is_active=False)
    comm = await _connect(user)
    frame = json.loads(await comm.receive_from())
    assert frame["type"] == "error" and frame["code"] == "forbidden"
    assert (await comm.receive_output())["code"] == 4403


@pytest.mark.asyncio
async def test_ws_action_uses_fresh_user():
    from asgiref.sync import sync_to_async

    user = await sync_to_async(_u)("member")
    comm = await _connect(user)
    await comm.send_to(text_data="{}")
    assert json.loads(await comm.receive_from())["type"] == "ok"
    await sync_to_async(User.objects.filter(pk=user.pk).update)(membership_level="applicant")
    await comm.send_to(text_data="{}")
    frame = json.loads(await comm.receive_from())
    assert frame == {
        "type": "error",
        "code": "forbidden",
        "message": frame["message"],
        "request_id": "r1",
    }
    await comm.disconnect()
```

(If `pytest-asyncio` markers differ in this repo, copy the async-test pattern from `tests/test_control_consumer_lock.py`.)

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement** (append to `permissions.py`; import DRF/channels lazily inside functions or at module top — both are installed; module-level import is fine):

```python
from asgiref.sync import sync_to_async
from rest_framework.permissions import BasePermission


class AreaPermission(BasePermission):
    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        if not getattr(user, "is_authenticated", False) or not hasattr(user, "membership_level"):
            return False
        require(user, view.area, resolve_verb(view, request.method), request=request, channel="api")
        return True

    def has_object_permission(self, request, view, obj):
        return True


def ws_denied_frame(exc: AreaDenied, request_id=None) -> dict:
    frame = {"type": "error", "code": "forbidden", "message": str(exc)}
    if request_id is not None:
        frame["request_id"] = request_id
    return frame


def _load_fresh_user(pk):
    from apps.accounts.models import User

    if pk is None:
        return None
    return User.objects.filter(pk=pk).first()


class AreaConsumerMixin:
    area: str | None = None
    connect_verb: str = "view"

    async def fresh_user(self):
        user = self.scope.get("user")
        return await sync_to_async(_load_fresh_user)(getattr(user, "pk", None))

    def _check(self, user, area, verb):
        client = self.scope.get("client") or (None, None)
        require(
            user,
            area,
            verb,
            channel="ws",
            path=self.scope.get("path", ""),
            method=f"WS {verb}",
            ip=client[0],
        )

    async def area_allows(self, verb, *, area=None, request_id=None, notify=True) -> bool:
        from django.contrib.auth.models import AnonymousUser

        user = await self.fresh_user() or AnonymousUser()
        try:
            await sync_to_async(self._check)(user, area or self.area, verb)
        except AreaDenied as exc:
            if notify:
                await self.send(text_data=json.dumps(ws_denied_frame(exc, request_id)))
            return False
        return True

    async def area_connect_gate(self) -> bool:
        if await self.area_allows(self.connect_verb, notify=True):
            return True
        await self.close(code=4403)
        return False
```

(`import json` at module top.) `apps/api/exceptions.py`:

```python
from rest_framework.response import Response
from rest_framework.views import exception_handler

from apps.accounts.permissions import AreaDenied


def area_exception_handler(exc, context):
    if isinstance(exc, AreaDenied):
        return Response(exc.as_api_body(), status=403)
    return exception_handler(exc, context)
```

Settings: `REST_FRAMEWORK["EXCEPTION_HANDLER"] = "apps.api.exceptions.area_exception_handler"`.

- [ ] **Step 4: Run** new tests + `python -m pytest -q` → PASS.

- [ ] **Step 5: Commit** `feat(permissions): DRF AreaPermission, JSON 403 body, AreaConsumerMixin`

---

## Migration tasks (5–12) — shared rules

Every migration task applies these rules; the tables in each task list the concrete declarations.

1. Replace the old mixin with `AreaPermissionMixin` as **leftmost** base and drop `LoginRequiredMixin`/`UserPassesTestMixin`/`test_func` from that class.
2. Form/confirm views whose GET only exists to perform a change (Create/Update/Delete views, "new" forms, editors) declare `verb = "manage"` so a view-only user gets 403 on the form rather than a form that fails on submit.
3. Pages rendered for `view` users hide action controls with `{% if can.<area>.<verb> %}` and show `{% include "includes/readonly_badge.html" %}` in the page header when `not can.<area>.manage` (and the area has a `manage` level).
4. In templates replace every `user.is_admin`, `user.is_internal`, `request.user.is_staff`, `can_use_station` with the `can.<area>.<verb>` the gated control needs (tables below).
5. View context flags like `can_edit` computed from old helpers become `can(request.user, area, verb)` or are removed in favour of `can.` in the template.
6. Business rules stay (e.g. "cannot demote yourself", "only own tokens", lifecycle guards) — they may still raise plain `PermissionDenied`/return 400; the 403 page renders their message.
7. Existing tests asserting the *old* semantics are updated to the spec's matrix in the same task (e.g. Applicant now gets 200 on deployments list). Never delete a test without replacing its intent; note each changed expectation in the commit body.
8. Old helpers (`is_admin`, `AdminRequiredMixin`, …) still exist until Task 13 — just stop *using* them in the touched files.

---

### Task 5: Stations + topology (UI)

**Files:**
- Modify: `apps/stations/views.py`, `apps/stations/views_region_crud.py`, `apps/stations/views_region_set.py`, `apps/stations/forms.py`, `apps/stations/models.py:536`, `apps/accounts/views_station_assignments.py`, `apps/accounts/views_region_assignments.py`
- Modify templates: `apps/stations/templates/stations/station_list.html`, `station_detail.html`, `_device_token.html`, region list template, tag list template
- Modify: `templates/includes/sidebar.html` only for the Stations/Regions/Tags entries
- Test: `tests/test_permission_stations_ui.py` (new) + update `tests/test_ui_permission_gating.py`, `tests/test_topology_permissions.py`, station view tests as needed

**Interfaces:** Consumes Tasks 1–3. Produces nothing new.

| View | Declaration |
|---|---|
| `StationListView`, `StationDetailView` | `area = "stations"` |
| `StationCreateView`, `StationUpdateView`, `StationDeleteView`, `StationPhotoUploadView`, `StationLogEntryCreateView`, `StationGenerateKeyView`, `StationRevokeKeyView`, `StationTagCreateView`, `StationTagUpdateView`, `StationTagDeleteView` | `area = "stations"; verb = "manage"` |
| `StationTagListView` | `area = "stations"` (view; tags list becomes readable by everyone, edit links gated) |
| `RegionListView` | `area = "topology"` |
| `RegionCreateView`, `RegionUpdateView`, `RegionDeleteView`, `StationSetRegionView` | `area = "topology"; verb = "manage"` |
| `StationAssignmentCreateView`, `StationAssignmentRevokeView`, `RegionAssignmentCreateView`, `RegionAssignmentRevokeView` | `area = "topology"; verb = "manage"` |

Template replacements:

| Where | Old | New |
|---|---|---|
| `station_list.html:22` | `user.is_internal` (new-station button) | `can.stations.manage` |
| `station_detail.html:42` | `can_use_station` (control button) | `can.control.view` (button label "Steuerung"; read-only for Applicants is handled on the control page) |
| `station_detail.html:45,184,226` | `user.is_internal` | `can.stations.manage` |
| `station_detail.html:64` | `user.is_admin` (image link) | `can.images.view` |
| `station_detail.html:98` | `user.is_admin` | the gated control's area (inspect: region set → `can.topology.manage`, upgrade card → `can.deployments.manage`) |
| `station_detail.html:496,516` | `user.is_admin` | same rule — inspect the block; topology card → `can.topology.manage`, audit tab → `can.stations.manage` |
| `_device_token.html:12,39` | `request.user.is_internal` | `can.stations.manage` |

`StationDetailView.get_context_data`: delete `context["can_use_station"]`; the `if self.request.user.is_admin:` block that builds the topology card becomes `if can(self.request.user, "topology", "manage"):`. The station Audit tab (IPs) must only render and only be *queried* when `can(user, "stations", "manage")`.

`apps/stations/forms.py:46`: `if user is None or not getattr(user, "is_internal", False)` → `if not can(user, "stations", "manage")` (import `can`).
`apps/stations/models.py:536`: `if self.user.membership_level == User.MembershipLevel.APPLICANT:` → `if level_of(self.user) == Level.APPLICANT:` (import inside `clean()` to avoid cycles). Note `level_of` returns `None` for inactive users — keep semantics by comparing `Level.from_slug(self.user.membership_level) == Level.APPLICANT` instead (validation must not depend on `is_active`).

Sidebar: Stations entry → `{% if can.stations.view %}`, Regions → `{% if can.topology.view %}`, Tags → `{% if can.stations.view %}` (adds the missing link per spec §3.2).

- [ ] **Step 1: Failing tests** `tests/test_permission_stations_ui.py`:

```python
import pytest
from django.urls import reverse

from apps.accounts.models import User

pytestmark = pytest.mark.django_db


def _login(client, level):
    u = User.objects.create_user(username=f"s-{level}", password="x", membership_level=level)
    client.force_login(u)
    return u


def test_applicant_sees_station_detail_without_edit_or_audit(client, station):
    _login(client, "applicant")
    r = client.get(reverse("stations:station_detail", args=[station.pk]))
    assert r.status_code == 200
    html = r.content.decode()
    assert reverse("stations:station_edit", args=[station.pk]) not in html
    assert 'id="tab-audit"' not in html  # adjust selector to the real audit-tab id


def test_staff_can_edit_station_but_not_set_region(client, station):
    _login(client, "staff")
    assert client.get(reverse("stations:station_edit", args=[station.pk])).status_code == 200
    assert client.post(reverse("stations:station_set_region", args=[station.pk]), {}).status_code == 403


def test_member_cannot_open_create_form(client):
    _login(client, "member")
    assert client.get(reverse("stations:station_create")).status_code == 403


def test_applicant_sees_region_list_read_only(client):
    _login(client, "applicant")
    r = client.get(reverse("stations:region_list"))
    assert r.status_code == 200
    assert reverse("stations:region_create") not in r.content.decode()
    assert "nur lesend" in r.content.decode()


def test_staff_cannot_create_assignment(client, station):
    u = _login(client, "staff")
    r = client.post(reverse("accounts:station_assignment_create", args=[u.pk]), {})
    assert r.status_code == 403


def test_tag_list_readable_by_member(client):
    _login(client, "member")
    assert client.get(reverse("stations:tag_list")).status_code == 200
```

(Fix URL args to each route's real signature — read `apps/stations/urls.py` / `apps/accounts/urls.py`.)

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Apply the declaration table, template table, context and form changes.**
- [ ] **Step 4: Run** `python -m pytest -q`; update old-semantics tests per shared rule 7 until green.
- [ ] **Step 5: Commit** `refactor(stations): stations/topology views on area permissions`

---

### Task 6: Users, profile, API tokens, notifications, web-push (UI) + visibility inputs

**Files:**
- Modify: `apps/accounts/views.py`, `apps/accounts/views_membership.py`, `apps/accounts/views_api_tokens.py`, `apps/accounts/views_notifications.py`, `apps/webpush/views.py`, `apps/accounts/visibility.py`
- Templates: `apps/accounts/templates/accounts/user_detail.html` (line 102), `user_list.html` (keeps `is_admin_view` context name — it is an audience flag, not a permission), `templates/oauth2_provider/authorize.html:127`, sidebar Users entry
- Test: `tests/test_permission_users_ui.py` (new) + update `tests/test_user_visibility.py`, `tests/test_user_detail_view.py`, `tests/test_accounts.py`, `tests/test_api_token_ui.py`, `tests/test_profile_view.py` as needed

| View | Declaration |
|---|---|
| `LoginView`, `LogoutView`, `SetPasswordView`, `VerifyEmailView`, `PasswordResetRequestView` | `permission_exempt = "auth-flow"` (keep `login_not_required` where present) |
| `ProfileView`, `ProfilePasswordChangeView`, `NotificationSettingsView` | `area = "profile"; verb = "use"` |
| `ApiTokenListView`, `ApiTokenRevokeView` | `area = "api_tokens"; verb = "use"` |
| `UserListView` | `area = "users"` |
| `UserDetailView` | `area = "users"`; override `get_area_verb()` → `("profile", "use")` when `self.kwargs["pk"] == self.request.user.pk`, else `("users", "view")` |
| `UserCreateView`, `UserUpdateView`, `UserSoftDeleteView`, `UserRestoreView`, `UserHardPurgeView`, `ResendWelcomeView`, `MembershipSetView` | `area = "users"; verb = "manage"` |
| `webpush.views.service_worker`, `manifest` | `@permission_exempt("static: PWA asset")` (keep `login_not_required`) |
| `webpush.views.subscribe`, `unsubscribe` | replace `@login_required` with `@area_permission("profile", "use")` |

Code changes:
- `UserListView` (`views.py:668-746`): remove the `if not self.request.user.is_admin` 404 branch — access is now `users.view`. Admin-only behaviour (trash bucket `show=`, applicant rows) keys on `can(self.request.user, "users", "manage")`. `ctx["is_admin_view"] = can(..., "users", "manage")`, `ctx["is_member_view"] = not ctx["is_admin_view"]`. The non-admin queryset excluding applicants (line 694) moves into `visibility.directory_queryset(viewer)` (new function, see below).
- `UserDetailView` line 1277: `self.request.user.membership_level != APPLICANT` (api-token section) → `can(self.request.user, "api_tokens", "use")`.
- `views_api_tokens.py:29-31`: the applicant check becomes redundant (area `api_tokens.use` = Member) — delete it and its comment.
- `views_membership.py:88`: keep `User._invalidate_role_cache(target)` until Task 13 removes it.
- `user_detail.html:102`: `request.user.is_admin` → `can.users.manage`.
- `authorize.html:127`: `user.is_admin` → `user.membership_level == "admin"` is presentation of the level badge; use `{{ user.get_membership_level_display }}` instead of the admin check.
- `visibility.py`:
  - `audience_for`: `if viewer.is_admin:` → `if can(viewer, "users", "manage"):`; applicant checks → `level_of(viewer) == Level.APPLICANT` and `Level.from_slug(target.membership_level) == Level.APPLICANT`.
  - `user_can_view_directory(viewer)` → `return can(viewer, "users", "view")`.
  - New `directory_queryset(viewer)`: `User.objects.active()`; if not `can(viewer, "users", "manage")` → `.exclude(membership_level="applicant")`. (Filter kwargs are data-shaping, allowed.)
- Sidebar Users entry: `{% if can.users.view %}` (Members gain the link).

- [ ] **Step 1: Failing tests** `tests/test_permission_users_ui.py`:

```python
import pytest
from django.urls import reverse

from apps.accounts.models import User

pytestmark = pytest.mark.django_db


def _mk(level, name=None):
    return User.objects.create_user(username=name or f"u-{level}", password="x", membership_level=level)


def test_member_sees_directory_and_sidebar_link(client):
    client.force_login(_mk("member"))
    r = client.get(reverse("accounts:user_list"))
    assert r.status_code == 200
    assert reverse("accounts:user_list") in client.get(reverse("dashboard:index")).content.decode()


def test_applicant_directory_403_but_own_detail_ok(client):
    me = _mk("applicant")
    client.force_login(me)
    assert client.get(reverse("accounts:user_list")).status_code == 403
    assert client.get(reverse("accounts:user_detail", args=[me.pk])).status_code == 200


def test_staff_cannot_edit_users(client):
    client.force_login(_mk("staff"))
    target = _mk("member", "tgt")
    assert client.get(reverse("accounts:user_edit", args=[target.pk])).status_code == 403


def test_applicant_cannot_open_api_tokens(client):
    client.force_login(_mk("applicant"))
    assert client.get(reverse("accounts:api_tokens")).status_code == 403


def test_applicant_can_change_own_profile(client):
    client.force_login(_mk("applicant"))
    assert client.get(reverse("accounts:profile")).status_code == 200
    assert client.get(reverse("accounts:notification_settings")).status_code == 200


def test_staff_does_not_get_admin_audience(db):
    from apps.accounts.visibility import Audience, audience_for

    assert audience_for(_mk("staff"), _mk("member", "x")) == Audience.MEMBER
```

- [ ] **Step 2–4:** run → FAIL, implement table + code changes, run full suite and fix old-semantics tests.
- [ ] **Step 5: Commit** `refactor(accounts): users/profile/tokens/webpush on area permissions`

---

### Task 7: Deployments, rollouts, images, provisioning (UI)

**Files:**
- Modify: `apps/deployments/views.py`, `apps/rollouts/views.py`, `apps/images/views.py`, `apps/provisioning/views.py`
- Templates: `apps/rollouts/templates/rollouts/_station_upgrade_card.html:2`, `apps/provisioning/templates/provisioning/_provisioning_section.html:2`, images list template (action buttons), deployments list/detail templates (new/cancel buttons), `upgrade_dashboard.html` (group-upgrade + sequence-edit links), sidebar entries for Deployments/Rollouts/Images
- Test: `tests/test_permission_ops_ui.py` (new) + update `tests/test_deployments.py`, `tests/test_rollouts.py`, `tests/test_images*.py`, `tests/test_provisioning.py`

| View | Declaration |
|---|---|
| `DeploymentListView`, `DeploymentDetailView` | `area = "deployments"` |
| `DeploymentCreateView`, `DeploymentCancelView` | `area = "deployments"; verb = "manage"` |
| `UpgradeStationView` | `area = "deployments"; verb = "manage"` (spec: Upgrade-Button = deploy, Staff) |
| `UpgradeDashboardView` | `area = "rollouts"` |
| `UpgradeGroupView`, `SequenceEditView`, `SequenceAddView`, `SequenceRemoveView`, `SequenceReorderView` | `area = "rollouts"; verb = "manage"` |
| `ImageListView` | `area = "images"` |
| `ImageMarkLatestView`, `ImageDeleteView`, `ImageArchiveView`, `ImageRestoreView`, `GitHubReleasesPartialView`, `QuickQueueView` | `area = "images"; verb = "manage"` |
| `CreateProvisioningJobView`, `ProvisioningJobStatusView`, `ProvisioningJobDownloadView` | `area = "provisioning"` (view/use inherit Admin) |

Templates:
- `_station_upgrade_card.html:2` `user.is_admin` → `can.deployments.manage` (Staff now sees and uses the upgrade card — spec decision 4).
- `_provisioning_section.html:2` `user.is_admin` → `can.provisioning.manage`.
- Images list: archive/restore/latest/delete/import buttons and the GitHub browser panel → `{% if can.images.manage %}`; add readonly badge.
- Deployments list/detail: "New deployment"/"Cancel" → `can.deployments.manage`; readonly badge.
- Upgrade dashboard: group-upgrade + "Sequenz bearbeiten" → `can.rollouts.manage`; per-station upgrade buttons → `can.deployments.manage`; readonly badge when `not can.rollouts.manage`.
- Sidebar: Deployments `can.deployments.view`, Rollouts `can.rollouts.view`, Images `can.images.view`.

- [ ] **Step 1: Failing tests** `tests/test_permission_ops_ui.py`:

```python
import pytest
from django.urls import reverse

from apps.accounts.models import User

pytestmark = pytest.mark.django_db


def _login(client, level):
    client.force_login(User.objects.create_user(username=f"o-{level}", password="x", membership_level=level))


@pytest.mark.parametrize("name", ["deployments:deployment_list", "rollouts:upgrade_dashboard", "images:list"])
def test_applicant_can_view_ops_pages(client, name):
    _login(client, "applicant")
    r = client.get(reverse(name))
    assert r.status_code == 200
    assert "nur lesend" in r.content.decode()


def test_staff_can_trigger_station_upgrade_but_not_group(client, station, image_release):
    _login(client, "staff")
    r = client.post(reverse("rollouts:upgrade_station", args=[station.pk]), {"image_release": image_release.pk})
    assert r.status_code != 403
    assert client.post(reverse("rollouts:upgrade_group"), {}).status_code == 403


def test_staff_cannot_archive_image(client, image_release):
    _login(client, "staff")
    assert client.post(reverse("images:archive", args=[image_release.pk])).status_code == 403


def test_staff_cannot_browse_github_releases(client):
    _login(client, "staff")
    assert client.get(reverse("images:gh_partial")).status_code == 403


def test_staff_cannot_see_provisioning_status(client):
    _login(client, "staff")
    assert client.get(reverse("provisioning:status", args=[1])).status_code == 403
```

(Fix arg signatures from the urls files.)

- [ ] **Steps 2–4** as before. **Step 5: Commit** `refactor(ops): deployments/rollouts/images/provisioning on area permissions`

---

### Task 8: Monitoring, modules, audit, dashboard, SSO, terminal status (UI) + sidebar + denial tile

**Files:**
- Modify: `apps/monitoring/views.py` (delete its local `AdminRequiredMixin`/`AdminOrOperatorRequiredMixin` copies), `apps/module_firmware/views.py` (delete `_StaffModuleMixin`, `_StaffFirmwareMixin`), `apps/audit/views.py`, `apps/dashboard/views.py`, `apps/sso/views.py` (delete `AdminOnlyMixin`), `apps/sso/permissions.py`, `apps/tunnel/views.py`, `apps/monitoring/recipients.py`, `apps/monitoring/notifications.py`, `config/urls.py` (`AppGrantAuthorizationView` exempt)
- Templates: `apps/monitoring/templates/monitoring/alert_list.html:20`, `_alert_cards.html:20`, `alert_settings.html:85`, `apps/module_firmware/templates/module_firmware/release_list.html:24,111,163` (+ module list/detail action buttons), `apps/dashboard/templates/dashboard/index.html:22,58,71,172,181`, audit list template (quick filter), `templates/includes/sidebar.html` (all remaining `is_admin`/`is_internal`)
- Test: `tests/test_permission_misc_ui.py` (new) + update `tests/test_monitoring*.py`, `tests/module_firmware/*`, `tests/test_sso_*.py`, `tests/test_audit*.py`, `tests/test_notification_*`, `tests/test_alert_recipients.py`

| View | Declaration |
|---|---|
| `AlertListView`, `AlertCountView` | `area = "alerts"` |
| `AlertAcknowledgeView`, `AlertResolveView` | `area = "alerts"; verb = "manage"` |
| `AlertSettingsView` | `area = "alert_rules"` (read-only render for non-admins) |
| `AlertRuleUpdateView`, `TestNotificationView` (all three test_* routes) | `area = "alert_rules"; verb = "manage"` |
| `ModuleListView`, `ModuleDetailView`, `FirmwareReleaseListView` | `area = "modules"` |
| `ModuleConfirmView`, `ModuleLifecycleView`, `ModuleNotesView`, `GitHubFirmwareReleasesPartialView`, `FirmwareImportView`, `FirmwareArchiveView`, `FirmwareRestoreView` | `area = "modules"; verb = "manage"` |
| `AuditLogListView`, `AuditLogExportView` | `area = "audit"` |
| `IndexView` (dashboard) | `area = "dashboard"` |
| all SSO admin views (`SsoDashboardView`, `ApplicationDetailView`, `GrantToggleView`, `SessionRevokeView`, `ApplicationPolicyUpdateView`, `TagListView`, `TagCreateView`, `TagDetailView`, `TagMembershipToggleView`) | `area = "sso"` |
| `AppGrantAuthorizationView` | `permission_exempt = "oidc: per-app policy"` |
| `TerminalStatusView` | `area = "terminal"; verb = "use"`; `can_connect = is_online and active_sessions < 2` (permission now enforced by the mixin) |

Code changes:
- `module_firmware/views.py:132` `ctx["can_edit"] = self.request.user.is_staff` → delete; templates use `can.modules.manage`.
- `sso/permissions.py:73-77`: `OPEN_TO_MEMBERS` → `(level_of(user) or 0) >= Level.MEMBER`; `OPEN_TO_INTERNAL` → `(level_of(user) or 0) >= Level.STAFF`; `OPEN_TO_ADMINS` → `level_of(user) == Level.ADMIN`. (App-access policy, compared via `level_of` per spec §4.)
- `monitoring/recipients.py:35`: `Q(membership_level=ADMIN)` → `Q(pk__in=users_with("alert_rules", "manage").values("pk"))`. Line 55 (topology recipients exclude applicants) stays — receiver logic, out of scope.
- `monitoring/notifications.py:200`: admin recipient list → `users_with("alert_rules", "manage").exclude(email="")`.
- `audit/views.py:206` (user filter dropdown) stays (data-shaping).
- Audit feed quick filter: add a "Verweigerungen" chip linking `?event_type=permission_denied`; add `AccountAuditLog.EventType.PERMISSION_DENIED` to the event-type choices offered by the filter. Verify `AuditLogFilterMixin` applies `event_type` to `AccountAuditLog` rows too (fix if it only filters `StationAuditLog`). Render `details` (area.verb, channel, count) in the row's detail cell.
- Dashboard `IndexView.get_context_data`: if `can(user, "audit", "view")`: `context["denials_24h"] = AccountAuditLog.objects.filter(event_type=PERMISSION_DENIED, created_at__gte=now - timedelta(hours=24)).count()`. Template: tile "Zugriffsverweigerungen (24 h): N" linking `{% url 'audit:audit_list' %}?event_type=permission_denied`, inside `{% if can.audit.view %}`. **Invoke `Skill("frontend-design")`** and match the existing tile markup.
- Dashboard template lines 22/71/172/181 (`user.is_internal`) and 58 (`user.is_admin`): replace by the area of the gated widget (deployments widgets → `can.deployments.view`; alerts → `can.alerts.view`; device-key/user counts → `can.stations.manage` / `can.users.manage`; inspect each block).
- Alerts templates: `alert_list.html:20` (`is_admin` settings link) → `can.alert_rules.view`; `_alert_cards.html:20` (`is_internal` ack buttons) → `can.alerts.manage`; `alert_settings.html:85` (`is_admin`) → `can.alert_rules.manage`; add readonly badge to settings page.
- Sidebar: every remaining entry → `{% if can.<area>.view %}` (Alerts, Modules, Audit `can.audit.view`, SSO `can.sso.manage`, Provisioning none — provisioning has no page of its own).

- [ ] **Step 1: Failing tests** `tests/test_permission_misc_ui.py`:

```python
import pytest
from django.urls import reverse

from apps.accounts.models import AccountAuditLog, User

pytestmark = pytest.mark.django_db


def _login(client, level, name=None):
    u = User.objects.create_user(username=name or f"x-{level}", password="x", membership_level=level)
    client.force_login(u)
    return u


def test_staff_sees_alert_list_and_can_ack(client, alert):
    _login(client, "staff")
    assert client.get(reverse("monitoring:alert_list")).status_code == 200
    assert client.post(reverse("monitoring:alert_acknowledge", args=[alert.pk])).status_code != 403


def test_applicant_sees_alert_settings_read_only(client):
    _login(client, "applicant")
    r = client.get(reverse("monitoring:alert_settings"))
    assert r.status_code == 200 and "nur lesend" in r.content.decode()


def test_staff_without_django_is_staff_gets_module_rights(client):
    u = _login(client, "staff")
    User.objects.filter(pk=u.pk).update(is_staff=False)
    assert client.get(reverse("module_firmware:gh_partial")).status_code != 403


def test_member_cannot_import_firmware(client):
    _login(client, "member")
    assert client.post(reverse("module_firmware:import"), {}).status_code == 403


def test_staff_terminal_status_denied(client, station):
    _login(client, "staff")
    assert client.get(reverse("tunnel:status", args=[station.pk])).status_code == 403


def test_dashboard_denial_tile_admin_only(client):
    m = _login(client, "member", "den")
    client.get(reverse("audit:audit_list"))  # creates a denial
    assert AccountAuditLog.objects.filter(event_type="permission_denied", actor=m).exists()
    _login(client, "admin", "adm")
    html = client.get(reverse("dashboard:index")).content.decode()
    assert "Zugriffsverweigerungen" in html
    assert "event_type=permission_denied" in html
    r = client.get(reverse("audit:audit_list") + "?event_type=permission_denied")
    assert "audit.view" in r.content.decode()
```

The SSO app-policy behaviour (`user_can_access`) is already pinned level-by-level by `tests/test_sso_policy.py::test_user_can_access_matrix` — it must stay green unchanged after the `level_of` rewrite.

- [ ] **Steps 2–4** as before. **Step 5: Commit** `refactor(ui): monitoring/modules/audit/dashboard/sso/terminal on area permissions + denial tile`

---

### Task 9: Control — capability policy on verbs, control page, Control/Audio consumers, lock

**Files:**
- Modify: `apps/control/capability_policy.py`, `apps/control/templatetags/control_caps.py`, `apps/control/views.py`, `apps/control/consumers.py` (`ControlConsumer` only), `apps/control/lock.py:73`, `apps/audio/consumers.py` (`AudioConsumer` only), control templates using `write_role_label`/`viewer_role`/`can_admin`, `static/js/control-panel.js`, `static/js/audio-panel.js`, `apps/api/diagnostics_views.py` is **not** here (Task 11)
- Test: rewrite `tests/test_control_capability_policy.py`, `tests/test_control_role_aware_render.py`, `tests/test_control_write_role_enforcement.py`; update `tests/test_control_views.py`, `tests/test_control_consumer_lock.py`, `tests/test_control_panel_ws.py`, `tests/test_audio_consumer.py`, `tests/test_control_page_integrity.py`, `tests/test_audio_server_relay_e2e.py`, JS tests in `tests/js/` if they assert frame shapes; new `tests/test_permission_control_ws.py`

**Interfaces:**
- Produces:
  - `CapabilityPolicy(verb: Literal["use","manage"] = "use", persist: bool = False)`; `policy_for(capability, module_type) -> CapabilityPolicy` (unchanged signature); `can_write(user, capability, module_type) -> bool` (**station parameter removed**) = `can(user, "control", policy.verb)`.
  - `cap_access` tag returns `{"writable": bool, "verb": str, "required_label": str}`.

Capability policy (replace the file body below the docstring):

```python
from __future__ import annotations

import dataclasses

from apps.accounts.permissions import can

_VERB_ORDER = ("use", "manage")


@dataclasses.dataclass(frozen=True)
class CapabilityPolicy:
    verb: str = "use"
    persist: bool = False

    def __post_init__(self):
        if self.verb not in _VERB_ORDER:
            raise ValueError(f"capability verb must be use|manage, got {self.verb!r}")


_DEFAULT = CapabilityPolicy()
_MANAGE_PERSISTED = CapabilityPolicy(verb="manage", persist=True)

POLICY: dict[tuple[str, str | None], CapabilityPolicy] = {
    ("filter_pre_emphasis", None): _MANAGE_PERSISTED,
    ("filter_hpf", None): _MANAGE_PERSISTED,
    ("filter_lpf", None): _MANAGE_PERSISTED,
    ("volume", "fm_transceiver"): _MANAGE_PERSISTED,
}


def policy_for(capability, module_type):
    if not isinstance(capability, str):
        return _DEFAULT
    if not isinstance(module_type, str):
        return _strictest_for(capability)
    return POLICY.get((capability, module_type)) or POLICY.get((capability, None)) or _DEFAULT


def _strictest_for(capability):
    entries = [p for (cap, _type), p in POLICY.items() if cap == capability]
    if not entries:
        return _DEFAULT
    verb = max((p.verb for p in entries), key=_VERB_ORDER.index)
    return CapabilityPolicy(verb=verb, persist=any(p.persist for p in entries))


def can_write(user, capability, module_type) -> bool:
    return can(user, "control", policy_for(capability, module_type).verb)
```

Keep the existing module docstring/comments for the POLICY entries (SA818 notes).

`control_caps.py`:

```python
@register.simple_tag(takes_context=True)
def cap_access(context, cap, module):
    name = cap.get("name") if isinstance(cap, dict) else None
    policy = capability_policy.policy_for(name, getattr(module, "type", None))
    request = context.get("request")
    user = getattr(request, "user", None)
    return {
        "writable": can(user, "control", policy.verb),
        "verb": policy.verb,
        "required_label": level_label(required_level("control", policy.verb)),
    }
```
Update every template reading `write_role_label`/`write_role` to `required_label`/`verb`.

`StationControlView`: `area = "control"` (view) as leftmost `AreaPermissionMixin`; delete `test_func`; context: `ctx["can_admin"] = can_override_station(u, station)`; delete `viewer_role`. Template: Lock/PTT/mic controls only `{% if can.control.use %}`, otherwise readonly badge + RX audio still available.

`ControlConsumer` (`apps/control/consumers.py`):
- Add `AreaConsumerMixin` as first base; `area = "control"`.
- `connect`: after `accept()` and the anonymous 4401 / station-404 checks, replace `_can_use` with `if not await self.area_connect_gate(): return`. Delete `_can_use`.
- `receive`: before `lock_acquire`, `lock_request`, `lock_transfer`, `ptt_keepalive`, and `command` (non-`get`), call `if not await self.area_allows("use", request_id=msg.get("request_id")): return`. `subscribe/unsubscribe` and `lock_release` stay open to every connected viewer (release only affects own lock).
- `_authorize_command`: `allowed = await self._may_write(capability, module_type)` where `_may_write` loads `_fresh_user` and returns `capability_policy.can_write(user, capability, module_type)`. On denial use `area_allows(policy.verb, request_id=…)` to send the standard forbidden frame and log (replace the custom `"Requires role"` error + the `denied (requires …)` station-audit line with the denial audit — keep the station audit line too, text `denied (requires control.<verb>)`).
- `_persist`: `capability_policy.can_write(user, capability, None)` (station arg dropped).
- `_lock_preempt`: `_can_administer` → `@database_sync_to_async def _can_override(self, station): user = _fresh_user(self.user); return user is not None and can_override_station(user, station)`. Denial: send `ws_denied_frame(AreaDenied("control", "manage", level_of(user)), None)` — and do **not** log via `require` (override is not an area right; plain station audit only).
- `lock.py:73`: `if not to_user.can_use_station(station)` → `if not can(to_user, "control", "use")`.

`AudioConsumer` (`apps/audio/consumers.py`):
- `AreaConsumerMixin`, `area = "control"`; connect → `area_connect_gate()` (RX for view); delete `_can_use`.
- `_handle_mic_open`: first `if not await self.area_allows("use"): self._mic_open = False; return`. Mic frames keep the cached gate (`_cache_mic_allowed`) — re-check happens on every `mic_open` and on every lock change (gate broadcast), which already revokes the lock of a demoted user once Control denies them.

JS:
- `control-panel.js` error case: normalise `var err = msg.error || (msg.code ? {code: msg.code, msg: msg.message} : null);` before the existing toast logic; the connect-reject branch (`msg.reason`) also accepts `msg.message` (`this.connError = msg.message || msg.reason;`).
- `audio-panel.js` `case "error"`: same normalisation for `message`.

- [ ] **Step 1: Failing tests** `tests/test_permission_control_ws.py` (copy communicator/agent fixtures from `tests/test_control_consumer_lock.py`):

```python
# Pseudostructure — concrete helpers come from tests/test_control_consumer_lock.py
@pytest.mark.asyncio
async def test_applicant_connects_read_only_and_cannot_acquire(...):
    comm = await connect_control(applicant, station)
    inventory = await recv_json(comm)            # connect succeeds (control.view)
    assert inventory["type"] == "inventory"
    await comm.send_json_to({"type": "lock_acquire"})
    frame = await recv_until(comm, "error")
    assert frame["code"] == "forbidden"

@pytest.mark.asyncio
async def test_demoted_user_loses_ptt_without_reconnect(...):
    comm = await connect_control(member, station)
    await acquire_lock(comm)
    await set_level(member, "applicant")
    await comm.send_json_to({"type": "ptt_keepalive", "request_id": "k1"})
    frame = await recv_until(comm, "error")
    assert frame["code"] == "forbidden" and frame["request_id"] == "k1"

@pytest.mark.asyncio
async def test_member_cannot_set_filter_staff_can(...):
    # command {"type":"command","op":"set","capability":"filter_hpf",...}
    # member → forbidden frame + PERMISSION_DENIED row (area control, verb manage)
    # staff → relayed to agent

@pytest.mark.asyncio
async def test_region_manager_member_can_preempt_plain_member_cannot(...): ...

@pytest.mark.asyncio
async def test_applicant_hears_rx_but_mic_open_denied(...):
    comm = await connect_audio(applicant, station)  # accepted
    await comm.send_json_to({"type": "mic_open"})
    frame = await recv_until(comm, "error")
    assert frame["code"] == "forbidden"

@pytest.mark.asyncio
async def test_demoted_user_mic_open_denied(...): ...
```

Write these out fully using the helper functions that exist in `tests/test_control_consumer_lock.py` / `tests/test_audio_consumer.py` (do not invent new helpers when an equivalent exists). Assertions must be exactly as shown.

Plus unit tests in rewritten `tests/test_control_capability_policy.py`:

```python
from apps.control import capability_policy as cp


def test_filter_is_manage_persisted():
    p = cp.policy_for("filter_hpf", "fm_transceiver")
    assert (p.verb, p.persist) == ("manage", True)


def test_unknown_module_type_takes_strictest():
    assert cp.policy_for("volume", None).verb == "manage"


def test_default_is_use():
    assert cp.policy_for("ptt", "fm_transceiver").verb == "use"


def test_invalid_verb_rejected():
    import pytest

    with pytest.raises(ValueError):
        cp.CapabilityPolicy(verb="admin")


def test_can_write(db, member_user, operator_user):
    assert cp.can_write(member_user, "ptt", "fm_transceiver") is True
    assert cp.can_write(member_user, "filter_hpf", None) is False
    assert cp.can_write(operator_user, "filter_hpf", None) is True
```

- [ ] **Steps 2–4**: run → FAIL; implement; run `python -m pytest -q` (control/audio suites are large — fix old-semantics expectations: Applicant connect now accepted; station-manager assignment no longer grants writes). Run JS tests if present: `python -m pytest tests/test_control_logic_js.py tests/test_audio_logic_js.py -q`.
- [ ] **Step 5: Commit** `refactor(control): capability policy on control verbs; WS per-action checks with fresh user`

---

### Task 10: Remaining consumers — station status, deployment status, terminal, agent exemptions

**Files:**
- Modify: `apps/stations/consumers.py`, `apps/deployments/consumers.py`, `apps/tunnel/consumers.py`, agent consumers in `apps/control/consumers.py` (`AgentControlConsumer`), `apps/audio/consumers.py` (`AgentAudioConsumer`), `apps/tunnel/consumers.py` (agent terminal consumer), `static/js/app.js` (terminal error frame reads `message || reason`)
- Test: `tests/test_permission_ws_feeds.py` (new) + update existing deployment/terminal consumer tests

| Consumer | Declaration |
|---|---|
| `StationStatusConsumer` | `AreaConsumerMixin`, `area = "stations"`; connect: `accept()` then `area_connect_gate()` (switch from close-before-accept to accept-then-gate so the frame reaches the browser) |
| `DeploymentStatusConsumer` | `area = "deployments"` (Applicant may now subscribe) — same accept-then-gate pattern; group_add only after the gate passes |
| `TerminalConsumer` | `area = "terminal"; connect_verb = "use"`; replace `if not user.is_admin:` block with `if not await self.area_connect_gate(): return` |
| all `Agent*Consumer` classes | `permission_exempt = "agent: ed25519"` |

- [ ] **Step 1: Failing tests** (communicator pattern from existing consumer tests):

```python
@pytest.mark.asyncio
async def test_applicant_can_subscribe_deployment_feed(applicant): ...   # connected, no close
@pytest.mark.asyncio
async def test_staff_terminal_connect_denied_with_frame(staff, online_station):
    # frame {"type":"error","code":"forbidden",...} then close 4403
@pytest.mark.asyncio
async def test_ws_connect_denials_are_deduplicated(staff, online_station):
    # connect 5× → one PERMISSION_DENIED row with details.count == 5, channel "ws"
@pytest.mark.asyncio
async def test_anonymous_station_feed_4401_not_logged(): ...
```
Write fully with the repo's existing communicator helpers; assertions as stated.

- [ ] **Steps 2–4**, **Step 5: Commit** `refactor(ws): status feeds and terminal on area permissions`

---

### Task 11: REST API — all viewsets except users on `AreaPermission`, no topology scoping

**Files:**
- Modify: `apps/api/read_views.py`, `apps/api/write_views.py`, `apps/api/router.py`, `apps/api/diagnostics_views.py`, `apps/api/read_serializers.py` (`accessible_deployment_results` import), `apps/api/views.py`, `apps/deployments/api_views.py`, `apps/module_firmware/api_views.py`, `apps/module_firmware/reconcile_api_views.py`
- Delete: nothing yet (`write_scoping.py` still used by `UserViewSet` until Task 12)
- Test: new `tests/test_permission_api.py`; rewrite `tests/test_api_permission_scope.py` and `tests/test_api_scoping.py` to the new model (or delete them **only** if every case is covered by `tests/test_permission_api.py` + Task 15 matrix — say so in the commit body); update `tests/test_api_read_*.py`, `tests/test_api_write_*.py` (except users)

Base classes:
- `read_views.ScopedReadOnlyViewSet` → rename `AreaReadOnlyViewSet`: `permission_classes = [AreaPermission]`, `area: str | None = None`, `verb = None`, `verbs = None`; `get_queryset` returns the model's full queryset (ordering preserved).
- `write_views.ScopedWriteViewSet` → rename `AreaWriteViewSet`: keep the write-serializer switching; `permission_classes = [AreaPermission]`; delete `can_write_object`, `create_requires` machinery.
- `router.ScopedAPIRootView.permission_classes = [AreaPermission]` with `area = "dashboard"`.

| ViewSet / view | `area` | Extra |
|---|---|---|
| `StationViewSet` | `stations` | region rule (below); delete = manage (Staff) per matrix |
| `RegionViewSet` | `topology` | |
| `StationTagViewSet` | `stations` | |
| `StationAssignmentViewSet`, `RegionAssignmentViewSet` | `topology` | keep demote/applicant guards (`_ApplicantForbiddenMixin` validation) |
| `StationLogEntryViewSet`, `StationPhotoViewSet` | `stations` | |
| `RolloutSequenceViewSet`, `RolloutSequenceEntryViewSet` | `rollouts` | |
| `DeploymentViewSet` | `deployments` | `http_method_names` without `delete` (no delete per spec) |
| `DeploymentResultViewSet` | `deployments` | |
| `AlertRuleViewSet` | `alert_rules` | |
| `AlertViewSet` | `alerts` | |
| `ProvisioningJobViewSet` | `provisioning` | |
| `ImageReleaseViewSet` | `images` | `@action(..., verb="manage")` on `archive`, `restore`, `available`, `import`; delete inline `ws.can_manage_images` checks |
| `ImageImportJobViewSet` | `images` | |
| `StationAudioDiagnosticView` (APIView) | `control`, `verb = "manage"` | `permission_classes = [AreaPermission]`; station lookup `Station.objects.filter(pk=pk).first()` → 404 if missing |
| `HealthCheckView` | — | `permission_exempt = "health"` |
| `HeartbeatView`, all `deployments/api_views.py` and `module_firmware/*api_views.py` device views | — | `permission_exempt = "device-key"` |
| `SpectacularAPIView`, `SpectacularSwaggerSplitView` | — | exempt via `EXEMPT_URL_NAMESPACES`/route table in Task 14 (`"api-docs: schema only, IsAuthenticated"`) |

Delete every `ws.*` call in the touched viewsets; delete `perform_create` scope checks that only re-implemented area rules. Keep pure validation (field 400s, lifecycle locks, image-variant checks, audit "via API token").

Station region rule (in `StationViewSet`):

```python
    def _require_topology_if_region_changes(self, serializer, instance=None):
        if "region" not in serializer.validated_data:
            return
        new = serializer.validated_data["region"]
        old = getattr(instance, "region", None) if instance is not None else None
        if instance is None and new is None:
            return
        if instance is not None and new == old:
            return
        require(self.request.user, "topology", "manage", request=self.request, channel="api")

    def perform_create(self, serializer):
        self._require_topology_if_region_changes(serializer)
        ...existing create + audit...

    def perform_update(self, serializer):
        self._require_topology_if_region_changes(serializer, serializer.instance)
        ...existing update + audit...
```

- [ ] **Step 1: Failing tests** `tests/test_permission_api.py`:

```python
import pytest
from rest_framework.test import APIClient

from apps.accounts.models import User

pytestmark = pytest.mark.django_db


def _c(level):
    c = APIClient()
    c.force_authenticate(User.objects.create_user(username=f"api-{level}", password="x", membership_level=level))
    return c


def test_member_without_assignment_sees_all_stations(station_factory):
    station_factory(); station_factory()
    r = _c("member").get("/api/v1/stations/")
    assert r.status_code == 200 and r.json()["count"] == 2


def test_applicant_can_read_api(station):
    assert _c("applicant").get("/api/v1/stations/").status_code == 200


def test_staff_cannot_create_region():
    r = _c("staff").post("/api/v1/regions/", {"name": "X"}, format="json")
    assert r.status_code == 403 and r.json()["required"] == "topology.manage"


def test_staff_can_edit_station_name(station):
    r = _c("staff").patch(f"/api/v1/stations/{station.pk}/", {"name": "new"}, format="json")
    assert r.status_code == 200


def test_staff_cannot_set_station_region_via_api(station):
    from apps.stations.models import Region

    region = Region.objects.create(name="R")
    c = _c("staff")
    r = c.patch(f"/api/v1/stations/{station.pk}/", {"region": region.pk}, format="json")
    assert r.status_code == 403 and r.json()["required"] == "topology.manage"
    r = c.post("/api/v1/stations/", {"name": "n2", "region": region.pk}, format="json")
    assert r.status_code == 403


def test_region_manager_has_no_api_extra_rights(station):
    from apps.stations.models import Region, RegionAssignment

    region = Region.objects.create(name="R")
    station.region = region; station.save(update_fields=["region"])
    c = APIClient()
    u = User.objects.create_user(username="rm", password="x", membership_level="member")
    RegionAssignment.objects.create(user=u, region=region, role="manager")
    c.force_authenticate(u)
    assert c.patch(f"/api/v1/stations/{station.pk}/", {"name": "z"}, format="json").status_code == 403


def test_staff_cannot_archive_image_api(image_release):
    r = _c("staff").post(f"/api/v1/images/{image_release.pk}/archive/")
    assert r.status_code == 403 and r.json()["required"] == "images.manage"


def test_staff_cannot_touch_provisioning_or_alert_rules():
    c = _c("staff")
    assert c.get("/api/v1/provisioning-jobs/").status_code == 403
    assert c.post("/api/v1/alert-rules/", {}, format="json").status_code == 403


def test_deployment_delete_not_allowed(deployment):
    assert _c("admin").delete(f"/api/v1/deployments/{deployment.pk}/").status_code == 405


def test_audio_diagnostics_requires_control_manage(station):
    r = _c("member").post(f"/api/v1/stations/{station.pk}/audio-diagnostics/", {}, format="json")
    assert r.status_code == 403 and r.json()["required"] == "control.manage"


def test_device_key_principal_rejected_on_user_api(client, station_with_key):
    from apps.accounts.models import AccountAuditLog
    from tests.conftest import device_auth_headers

    station, private_key = station_with_key
    r = client.get("/api/v1/stations/", **device_auth_headers(private_key, station.pk, b""))
    assert r.status_code in (401, 403)
    assert not AccountAuditLog.objects.filter(event_type="permission_denied").exists()
```

- [ ] **Steps 2–4**, full suite green. **Step 5: Commit** `refactor(api): viewsets on AreaPermission; drop topology scoping; region needs topology.manage`

---

### Task 12: Users API (read + write) — visibility-filtered fields, `users.manage`

**Files:**
- Modify: `apps/api/read_views.py` (`UserViewSet`), `apps/api/write_views.py` (`UserViewSet`), `apps/api/read_serializers.py` (`UserSerializer`), `apps/api/write_serializers.py` (`UserWriteSerializer` — remove `is_admin` guard; membership change = `users.manage` already required)
- Delete: `apps/api/write_scoping.py`, `apps/api/write_permissions.py` (no users left after this task)
- Test: rewrite `tests/test_api_read_users.py`, `tests/test_api_write_users.py`; new cases in `tests/test_permission_api.py`

Rules:
- `area = "users"`; read requires `users.view` (Member+), writes `users.manage` (Admin).
- `get_queryset` = `visibility.directory_queryset(self.request.user)` (from Task 6) — Members do not see applicants or soft-deleted users; Admins see all non-deleted.
- `retrieve` of a user the viewer has no audience for → 404 (`audience_for(...) is None`).
- `UserSerializer`: remove `is_admin`, `is_internal` fields. `to_representation(self, obj)`: compute `visible = directory_visible_fields(viewer, obj)`; keep `id` always; map serializer keys to visibility concept keys `{"date_joined": "date_joined_year"}`, treat `notify_channel` as private (key `"language"` group: include only when `"language" in visible`); drop every key whose concept key is not in `visible`. Viewer = `self.context["request"].user`.
- Write `UserViewSet`: delete `can_write_object`, `ws.can_write_user`, the "staff may not edit internal users" rule and `_invalidate_role_cache` call. Keep self-demotion guard and membership audit.

- [ ] **Step 1: Failing tests** (append to `tests/test_permission_api.py`):

```python
def test_staff_api_does_not_expose_private_fields():
    target = User.objects.create_user(username="tgt", password="x", membership_level="member", phone="+43 1", address="Weg 1")
    r = _c("staff").get(f"/api/v1/users/{target.pk}/")
    assert r.status_code == 200
    body = r.json()
    assert "phone" not in body and "address" not in body
    assert "is_admin" not in body and "is_internal" not in body


def test_admin_api_sees_private_fields():
    target = User.objects.create_user(username="tgt2", password="x", membership_level="member", phone="+43 1")
    assert _c("admin").get(f"/api/v1/users/{target.pk}/").json()["phone"] == "+43 1"


def test_member_does_not_see_applicants():
    User.objects.create_user(username="app1", password="x", membership_level="applicant")
    names = [u["username"] for u in _c("member").get("/api/v1/users/").json()["results"]]
    assert "app1" not in names


def test_applicant_users_api_forbidden():
    assert _c("applicant").get("/api/v1/users/").status_code == 403


def test_staff_cannot_patch_users():
    target = User.objects.create_user(username="t3", password="x", membership_level="member")
    r = _c("staff").patch(f"/api/v1/users/{target.pk}/", {"first_name": "X"}, format="json")
    assert r.status_code == 403 and r.json()["required"] == "users.manage"
```

(Check `phone`/`address` field names on `User`; adapt `create_user` kwargs accordingly.)

- [ ] **Steps 2–4**; `grep -rn "write_scoping\|write_permissions" apps tests` must be empty except removed tests. **Step 5: Commit** `refactor(api): users endpoint on users.view/manage with directory visibility`

---

### Task 13: Remove old helpers, derive `is_staff`, forbidden-names guard

**Files:**
- Modify: `apps/accounts/models.py` (delete `is_admin`, `is_internal`, `can_use_station`, `can_maintain_station`, `can_administer_station`, `is_station_admin`, `is_region_manager`, `is_station_maintainer` *if no remaining caller*, `_invalidate_role_cache`; add `save()` derivation), `apps/accounts/managers.py`, `apps/accounts/views.py` (delete `AdminRequiredMixin`), `apps/stations/views.py` (delete `AdminOrOperatorRequiredMixin`), `apps/api/permissions.py` (delete `TopologyScopedPermission`, keep `IsDevice`), `apps/control/capability_policy.py` leftovers, `apps/accounts/views_membership.py:88`, `tests/conftest.py`
- Delete: `apps/stations/scoping.py`, `apps/api/scoping.py`
- Create: `apps/accounts/migrations/0023_derive_is_staff.py`
- Rename fixture: `operator_user` → `staff_user` in `tests/conftest.py` and all 13 test files (`grep -rl operator_user tests | xargs sed -i 's/\boperator_user\b/staff_user/g'`), plus fixture username `"operator"` → `"staff"` (check no test asserts the literal username first).
- Modify: every test still using `is_staff=True`/`is_superuser=True` to grant rights (module_firmware tests) → create users with `membership_level="staff"`/`"admin"`.
- Test: `tests/test_permission_guards.py` (forbidden names + is_staff derivation)

`User.save()`:

```python
    def save(self, *args, **kwargs):
        # Django's /admin/ is the only consumer of is_staff/is_superuser; both are
        # derived from the membership level and never set independently.
        derived = self.membership_level == self.MembershipLevel.ADMIN
        self.is_staff = derived
        self.is_superuser = derived
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "membership_level" in update_fields:
            kwargs["update_fields"] = {*update_fields, "is_staff", "is_superuser"}
        super().save(*args, **kwargs)
```

`managers.create_superuser`: pass `membership_level=ADMIN` via `extra_fields.setdefault("membership_level", User.MembershipLevel.ADMIN)` before `super().create_superuser(...)`; delete the post-save mutation and `_invalidate_role_cache` call; rewrite the docstring (no references to removed names).

`conftest._user_with_level`: drop `_invalidate_role_cache`.

Migration `0023_derive_is_staff.py`:

```python
from django.db import migrations


def derive(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    User.objects.update(is_staff=False, is_superuser=False)
    User.objects.filter(membership_level="admin").update(is_staff=True, is_superuser=True)


class Migration(migrations.Migration):
    dependencies = [("accounts", "0022_accountauditlog_details_permission_denied")]
    operations = [migrations.RunPython(derive, migrations.RunPython.noop)]
```

Remaining callers of `is_station_admin`/`is_region_manager`/`can_administer_station` (grep): replace with `can_override_station` or direct assignment queries where the code is *receiver/display* logic (e.g. user-detail pills). If none remain, delete the methods.

- [ ] **Step 1: Failing tests** `tests/test_permission_guards.py`:

```python
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCAN_DIRS = ["apps", "config", "templates"]
SCAN_GLOBS = ["station_manager*"]
FORBIDDEN = [
    r"\bis_admin\b",
    r"\bis_internal\b",
    r"\bis_staff\b",
    r"\bis_superuser\b",
    r"\bcan_use_station\b",
    r"\bcan_maintain_station\b",
    r"\b_invalidate_role_cache\b",
    r"\bAdminRequiredMixin\b",
    r"\bAdminOrOperatorRequiredMixin\b",
    r"\bAdminOnlyMixin\b",
    r"\b_StaffModuleMixin\b",
    r"\b_StaffFirmwareMixin\b",
    r"\bwrite_scoping\b",
    r"\bwrite_permissions\b",
    r"\baccessible_stations\b",
    r"\bTopologyScopedPermission\b",
    r"\bROLE_RANK\b",
    r"\bROLE_LABELS\b",
    r"\bviewer_role\b",
    r"\brole_allows\b",
]
ALLOW = {
    "apps/accounts/permissions.py",
    "apps/accounts/models.py",
    "apps/accounts/managers.py",
    "apps/accounts/admin.py",
}
LEVEL_COMPARISON = re.compile(
    r"membership_level\s*[!=]=|MembershipLevel\.\w+\s*[!=]=|[!=]=\s*[\w.]*MembershipLevel\."
)
LEVEL_ALLOW = {
    "apps/accounts/permissions.py",
    "apps/accounts/models.py",
    "apps/accounts/views_membership.py",
    "apps/api/write_serializers.py",
}


def _files():
    for d in SCAN_DIRS:
        for p in (ROOT / d).rglob("*"):
            if p.suffix in {".py", ".html", ".txt", ".js"} and "migrations" not in p.parts:
                yield p
    for g in SCAN_GLOBS:
        for base in ROOT.glob(g):
            for p in base.rglob("*.py") if base.is_dir() else [base]:
                yield p


def test_no_forbidden_permission_names():
    hits = []
    for p in _files():
        rel = p.relative_to(ROOT).as_posix()
        if rel in ALLOW:
            continue
        for n, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            for pat in FORBIDDEN:
                if re.search(pat, line):
                    hits.append(f"{rel}:{n}: {line.strip()}")
    assert not hits, "Old permission helpers used:\n" + "\n".join(hits)


def test_no_direct_membership_level_comparisons():
    hits = []
    for p in _files():
        rel = p.relative_to(ROOT).as_posix()
        if p.suffix != ".py" or rel in LEVEL_ALLOW:
            continue
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if LEVEL_COMPARISON.search(line):
                hits.append(f"{rel}:{n}: {line.strip()}")
    assert not hits, "Compare levels via apps.accounts.permissions:\n" + "\n".join(hits)


def test_allowlisted_models_only_use_is_staff_in_save():
    text = (ROOT / "apps/accounts/models.py").read_text()
    assert "def is_admin" not in text and "def is_internal" not in text


@pytest.mark.django_db
@pytest.mark.parametrize("level,expected", [("applicant", False), ("member", False), ("staff", False), ("admin", True)])
def test_is_staff_derived_from_level(level, expected):
    from apps.accounts.models import User

    u = User.objects.create_user(username=f"d-{level}", password="x", membership_level=level, is_staff=True)
    u.refresh_from_db()
    assert (u.is_staff, u.is_superuser) == (expected, expected)


@pytest.mark.django_db
def test_demotion_with_update_fields_clears_is_staff():
    from apps.accounts.models import User

    u = User.objects.create_user(username="dd", password="x", membership_level="admin")
    u.membership_level = "member"
    u.save(update_fields=["membership_level"])
    u.refresh_from_db()
    assert u.is_staff is False


@pytest.mark.django_db
def test_createsuperuser_is_admin_level():
    from apps.accounts.models import User

    u = User.objects.create_superuser("root", "r@example.org", "pw")
    assert u.membership_level == "admin" and u.is_staff and u.is_superuser
```

Note `.js` is scanned too — `static/` is not in `SCAN_DIRS`; app-level static JS under `apps/` is. Comments/docstrings count (rewrite them). The template context name `is_admin_view` does not match `\bis_admin\b`.

Also add `tests/test_derive_is_staff_migration.py` using the repo's existing migration-test pattern (see `tests/test_membership_level_migration.py`): a `member` row with `is_staff=True` before → `False` after; `admin` row → `True`.

- [ ] **Steps 2–4**: run → FAIL with the hit list; remove everything listed; full suite green; `uvx ruff@0.16.8 check .`.
- [ ] **Step 5: Commit** `refactor(permissions)!: remove legacy role helpers; derive is_staff from membership level`

---

### Task 14: URL-walk + template-reference guard

**Files:**
- Modify: `apps/accounts/permissions.py` (add `EXEMPT_URL_NAMESPACES`, `EXEMPT_URL_NAMES`)
- Test: append to `tests/test_permission_guards.py`

```python
# apps/accounts/permissions.py
EXEMPT_URL_NAMESPACES: dict[str, str] = {
    "admin": "django-admin: gated by derived is_staff",
    "oauth2_provider": "oidc: protocol endpoints, per-app policy",
    "djdt": "debug-toolbar: DEBUG only",
}
EXEMPT_URL_NAMES: dict[str, str] = {
    "set_language": "locale switch",
    "api:schema": "api-docs: schema only, IsAuthenticated",
    "api:docs": "api-docs: schema only, IsAuthenticated",
}
```

Tests:

```python
from django.urls import URLPattern, URLResolver, get_resolver


def _walk(patterns, ns=None):
    for p in patterns:
        if isinstance(p, URLResolver):
            yield from _walk(p.url_patterns, p.namespace or ns)
        elif isinstance(p, URLPattern):
            yield ns, p


def _declaration(callback):
    from apps.accounts import permissions as P

    view = getattr(callback, "view_class", None) or getattr(callback, "cls", None)
    target = view or callback
    initkwargs = getattr(callback, "initkwargs", {}) or {}
    area = initkwargs.get("area") or getattr(target, "area", None)
    exempt = getattr(target, "permission_exempt", None)
    return view, target, area, exempt


def test_every_url_declares_area_or_exemption():
    from apps.accounts import permissions as P

    problems = []
    for ns, pat in _walk(get_resolver().url_patterns):
        full_name = f"{ns}:{pat.name}" if ns and pat.name else pat.name
        if ns in P.EXEMPT_URL_NAMESPACES or full_name in P.EXEMPT_URL_NAMES:
            continue
        view, target, area, exempt = _declaration(pat.callback)
        if exempt:
            assert str(exempt).strip(), full_name
            continue
        if not area:
            problems.append(f"{full_name or pat.pattern}: no area/permission_exempt ({target})")
            continue
        assert area in P.AREAS, f"{full_name}: unknown area {area!r}"
        enforced = (
            (view is not None and issubclass(view, P.AreaPermissionMixin))
            or (view is not None and P.AreaPermission in getattr(view, "permission_classes", []))
            or getattr(target, "area_enforced", False)
        )
        if not enforced:
            problems.append(f"{full_name}: declares area but does not enforce it")
        verb = getattr(target, "verb", None)
        assert verb in (None, *P.VERBS), f"{full_name}: bad verb {verb!r}"
    assert not problems, "\n".join(problems)


def test_every_browser_consumer_declares_area_or_exemption():
    from config.asgi import agent_ws_routes, browser_ws_routes
    from apps.accounts import permissions as P

    for route in [*browser_ws_routes, *agent_ws_routes]:
        consumer = route.callback.consumer_class
        area = getattr(consumer, "area", None)
        exempt = getattr(consumer, "permission_exempt", None)
        assert area or exempt, consumer
        if area:
            assert area in P.AREAS and issubclass(consumer, P.AreaConsumerMixin), consumer
    for route in browser_ws_routes:
        assert getattr(route.callback.consumer_class, "area", None), "browser consumers need an area"


TEMPLATE_CAN = re.compile(r"\bcan\.(\w+)\.(\w+)")


def test_template_can_references_are_valid():
    from apps.accounts.permissions import AREAS, VERBS

    bad = []
    for d in ["apps", "templates"]:
        for p in (ROOT / d).rglob("*.html"):
            for m in TEMPLATE_CAN.finditer(p.read_text(encoding="utf-8")):
                if m.group(1) not in AREAS or m.group(2) not in VERBS:
                    bad.append(f"{p.relative_to(ROOT)}: {m.group(0)}")
    assert not bad, "\n".join(bad)


def test_every_drf_action_verb_is_valid():
    from apps.accounts.permissions import VERBS
    from apps.api.router import router

    for prefix, viewset, basename in router.registry:
        for name in dir(viewset):
            fn = getattr(viewset, name, None)
            kwargs = getattr(fn, "kwargs", None)
            if getattr(fn, "mapping", None) is not None and kwargs and "verb" in kwargs:
                assert kwargs["verb"] in VERBS, (basename, name)
```

(`browser_ws_routes`/`agent_ws_routes` are module-level in `config/asgi.py` — verified. If `debug_toolbar`'s namespace differs, read it from the resolver output.)

- [ ] **Step 1**: add tests; **Step 2**: run — expect a list of undeclared routes (e.g. `i18n` set_language, anything missed in Tasks 5–12). **Step 3**: declare each per spec §1.3 / §3.1 (or add a reasoned exemption here — only for third-party code). **Step 4**: green. **Step 5: Commit** `test(permissions): URL-walk, consumer and template-reference guards`

---

### Task 15: Permission matrix test (UI + API + WS)

**Files:**
- Test: `tests/test_permission_matrix.py`

Design: a table of one representative endpoint per (area, verb) per channel; the expected outcome is computed from `required_level()` — never hard-coded. A coverage test asserts every resolvable (area, verb) has a UI representative, and an API representative where the area has API endpoints.

```python
import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.accounts.permissions import AREAS, VERBS, Level, required_level

LEVELS = [None, Level.APPLICANT, Level.MEMBER, Level.STAFF, Level.ADMIN]

# (area, verb) -> (method, url_name, kwargs_factory_name, data)
# kwargs factory names refer to the `objs` fixture keys below.
UI = {
    ("dashboard", "view"): ("GET", "dashboard:index", None, None),
    ("profile", "view"): ("GET", "accounts:profile", None, None),
    ("profile", "use"): ("POST", "accounts:notification_settings", None, {}),
    ("stations", "view"): ("GET", "stations:station_detail", "station", None),
    ("stations", "manage"): ("GET", "stations:station_edit", "station", None),
    ("control", "view"): ("GET", "control:station_control", "station", None),
    ("terminal", "use"): ("GET", "tunnel:status", "station", None),
    ("topology", "view"): ("GET", "stations:region_list", None, None),
    ("topology", "manage"): ("GET", "stations:region_create", None, None),
    ("deployments", "view"): ("GET", "deployments:deployment_list", None, None),
    ("deployments", "manage"): ("GET", "deployments:deployment_create", None, None),
    ("rollouts", "view"): ("GET", "rollouts:upgrade_dashboard", None, None),
    ("rollouts", "manage"): ("GET", "rollouts:sequence_edit", None, None),
    ("images", "view"): ("GET", "images:list", None, None),
    ("images", "manage"): ("POST", "images:archive", "image", {}),
    ("provisioning", "manage"): ("GET", "provisioning:status", "provisioning_job", None),
    ("alerts", "view"): ("GET", "monitoring:alert_list", None, None),
    ("alerts", "manage"): ("POST", "monitoring:alert_acknowledge", "alert", {}),
    ("alert_rules", "view"): ("GET", "monitoring:alert_settings", None, None),
    ("alert_rules", "manage"): ("POST", "monitoring:test_push", None, {}),
    ("modules", "view"): ("GET", "module_firmware:module_list", None, None),
    ("modules", "manage"): ("GET", "module_firmware:gh_partial", None, None),
    ("users", "view"): ("GET", "accounts:user_list", None, None),
    ("users", "manage"): ("GET", "accounts:user_create", None, None),
    ("api_tokens", "use"): ("GET", "accounts:api_tokens", None, None),
    ("audit", "view"): ("GET", "audit:audit_list", None, None),
    ("sso", "manage"): ("GET", "sso:dashboard", None, None),
}
# control.use / control.manage live on the WS channel (WS table below).
UI_NOT_APPLICABLE = {("control", "use"), ("control", "manage")}

API = {
    ("stations", "view"): ("GET", "/api/v1/stations/", None),
    ("stations", "manage"): ("PATCH", "/api/v1/stations/{station}/", {"name": "m"}),
    ("topology", "view"): ("GET", "/api/v1/regions/", None),
    ("topology", "manage"): ("POST", "/api/v1/regions/", {"name": "mx"}),
    ("deployments", "view"): ("GET", "/api/v1/deployments/", None),
    ("rollouts", "view"): ("GET", "/api/v1/rollout-sequences/", None),
    ("images", "view"): ("GET", "/api/v1/images/", None),
    ("images", "manage"): ("GET", "/api/v1/images/available/", None),
    ("provisioning", "manage"): ("GET", "/api/v1/provisioning-jobs/", None),
    ("alerts", "view"): ("GET", "/api/v1/alerts/", None),
    ("alert_rules", "view"): ("GET", "/api/v1/alert-rules/", None),
    ("users", "view"): ("GET", "/api/v1/users/", None),
    ("users", "manage"): ("PATCH", "/api/v1/users/{other_user}/", {"first_name": "Z"}),
    ("control", "manage"): ("POST", "/api/v1/stations/{station}/audio-diagnostics/", {}),
}
API_AREAS = {"stations", "topology", "deployments", "rollouts", "images", "provisioning",
             "alerts", "alert_rules", "users", "control"}


def _resolvable():
    for area in AREAS:
        for verb in VERBS:
            if getattr(AREAS[area], verb) is not None:  # declared verbs only
                yield area, verb


def test_matrix_covers_every_declared_verb():
    missing_ui = [k for k in _resolvable() if k not in UI and k not in UI_NOT_APPLICABLE]
    assert not missing_ui, missing_ui
    assert {a for a, _ in API} == API_AREAS


def _expected_allowed(level, area, verb):
    need = required_level(area, verb)
    return level is not None and need is not None and level >= need


@pytest.fixture
def objs(db, station, image_release, alert, deployment, admin_user):
    from apps.provisioning.models import ProvisioningJob

    other = User.objects.create_user(username="other", password="x", membership_level="member")
    job = ProvisioningJob.objects.create(
        station=station, image_release=image_release, requested_by=admin_user
    )
    return {"station": station.pk, "image": image_release.pk, "alert": alert.pk,
            "provisioning_job": job.pk, "other_user": other.pk}


def _user(level):
    if level is None:
        return None
    return User.objects.create_user(username=f"mx-{level.slug}", password="x", membership_level=level.slug)


@pytest.mark.parametrize("level", LEVELS, ids=lambda l: l.slug if l else "anon")
@pytest.mark.parametrize("key", sorted(UI), ids=lambda k: f"{k[0]}.{k[1]}")
def test_ui_matrix(client, objs, level, key):
    method, name, kw, data = UI[key]
    url = reverse(name, args=[objs[kw]] if kw else [])
    user = _user(level)
    if user:
        client.force_login(user)
    resp = client.get(url) if method == "GET" else client.post(url, data or {})
    if level is None:
        assert resp.status_code == 302 and "/login" in resp["Location"]
    elif _expected_allowed(level, *key):
        assert resp.status_code != 403, (key, level, resp.status_code)
    else:
        assert resp.status_code == 403, (key, level, resp.status_code)


@pytest.mark.parametrize("level", LEVELS, ids=lambda l: l.slug if l else "anon")
@pytest.mark.parametrize("key", sorted(API), ids=lambda k: f"{k[0]}.{k[1]}")
def test_api_matrix(objs, level, key):
    method, url, data = API[key]
    url = url.format(**objs)
    c = APIClient()
    user = _user(level)
    if user:
        c.force_authenticate(user)
    resp = getattr(c, method.lower())(url, data, format="json") if data is not None else getattr(c, method.lower())(url)
    if level is None:
        assert resp.status_code in (401, 403)
    elif _expected_allowed(level, *key):
        assert resp.status_code != 403, (key, level, resp.status_code, getattr(resp, "data", None))
    else:
        assert resp.status_code == 403 and resp.json()["required"] == f"{key[0]}.{key[1]}"
```

Ensure the `GET /api/v1/images/available/` call is mocked like `tests/test_images_github_browser.py` does (no network). Same for `images:archive` side effects if any.

WS matrix (same file), using the communicator helpers from Task 9/10 tests:

```python
WS = [
    # (consumer-path-builder, area, verb, action)
    ("stations-status", "stations", "view", "connect"),
    ("deployment-status", "deployments", "view", "connect"),
    ("control", "control", "view", "connect"),
    ("control", "control", "use", "lock_acquire"),
    ("control", "control", "manage", "command:filter_hpf"),
    ("audio", "control", "view", "connect"),
    ("audio", "control", "use", "mic_open"),
    ("terminal", "terminal", "use", "connect"),
]
# For each LEVEL × entry: expected = _expected_allowed(level, area, verb).
# connect: allowed → no 4403 close; denied → forbidden frame + close 4403 (anon → 4401).
# actions: allowed → no forbidden frame for that request_id; denied → forbidden frame.
```

Implement `test_ws_matrix` fully with these semantics (terminal requires an online station with a fake agent — reuse the terminal consumer test setup).

- [ ] **Step 1**: write the file; **Step 2**: run — fix any representative whose non-403 outcome is actually a business 404/400 (allowed) vs a genuine permission bug (fix the view). **Step 3**: green. **Step 4: Commit** `test(permissions): UI/API/WS permission matrix derived from registry`

---

### Task 16: `permissions_table` command + generated docs + doc guard

**Files:**
- Create: `apps/accounts/management/commands/permissions_table.py` (check `apps/accounts/management/commands/` exists; create `__init__.py` files if not)
- Create: `docs/permissions.md` (generated)
- Test: `tests/test_permissions_doc.py`

```python
# apps/accounts/management/commands/permissions_table.py
from django.core.management.base import BaseCommand
from django.utils import translation

from apps.accounts.permissions import matrix

_ABBR = {None: "–", 1: "A", 2: "M", 3: "S", 4: "Ad"}

HEADER = """# Berechtigungen

> Generiert mit `python manage.py permissions_table > docs/permissions.md` aus
> `apps/accounts/permissions.py`. Nicht von Hand bearbeiten — CI prüft Aktualität.

A = Applicant, M = Member, S = Staff, Ad = Admin, – = niemand.
Ein nicht definiertes niedrigeres Verb erbt die Stufe des nächsthöheren.

| Bereich | Name | view | use | manage |
|---|---|---|---|---|
"""

FOOTNOTE = """
**Zusatzregel (einzige zuweisungsbasierte):** Control-Override (Lock preempten)
dürfen `control.manage` sowie — sofern sie `control.use` haben — Station-Admins
der Station und Region-Manager ihrer Region (`can_override_station`).
"""


class Command(BaseCommand):
    help = "Print the permission matrix as Markdown (source of docs/permissions.md)."

    def handle(self, *args, **options):
        with translation.override("de"):
            rows = [
                f"| `{r.area}` | {r.label} | {_ABBR[r.view and int(r.view)]} | "
                f"{_ABBR[r.use and int(r.use)]} | {_ABBR[r.manage and int(r.manage)]} |"
                for r in matrix()
            ]
        self.stdout.write(HEADER + "\n".join(rows) + "\n" + FOOTNOTE, ending="")
```

```python
# tests/test_permissions_doc.py
import pathlib
from io import StringIO

from django.core.management import call_command

DOC = pathlib.Path(__file__).resolve().parent.parent / "docs" / "permissions.md"


def test_permissions_doc_is_current():
    out = StringIO()
    call_command("permissions_table", stdout=out)
    assert DOC.read_text(encoding="utf-8") == out.getvalue(), (
        "docs/permissions.md is stale — run: python manage.py permissions_table > docs/permissions.md"
    )


def test_doc_contains_override_footnote():
    assert "can_override_station" in DOC.read_text(encoding="utf-8")
```

- [ ] **Step 1**: tests → FAIL. **Step 2**: implement command, run `python manage.py permissions_table > docs/permissions.md`. **Step 3**: tests PASS; full `python -m pytest -q`; `uvx ruff@0.16.8 check . && uvx ruff@0.16.8 format --check .`.
- [ ] **Step 4: Commit** `docs(permissions): generated permission matrix + freshness guard`

---

## After Task 16

- Whole-branch review (SDD final reviewer), then open/refresh the PR (draft early, ready after CI green) with: behaviour-change table from spec §7, the `can_override_station` `control.use` floor note, migration notes (0022 schema, 0023 data), API breaking changes (`is_admin`/`is_internal` fields removed from `/api/v1/users/`, 403 bodies).
- Run `kontor-recipes:review-loop` to zero (security focus: authz bypass, forgotten views, UI/API parity). Do not merge.
