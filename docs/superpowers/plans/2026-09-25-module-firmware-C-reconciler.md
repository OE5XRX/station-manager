# Modul-Firmware Teilbereich C — Desired-State & Reconciler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The server-side "brain" of module firmware updates — a declarative desired-state model (`ModuleFirmwareTarget`), a pure reconciler that resolves the effective release per module and tracks convergence/quarantine (`ModuleFirmwareConvergenceState`), and a DeviceKey-authenticated reconcile API (check/status/commit) that hands one instruction at a time to the agent.

**Architecture:** Extend `apps/module_firmware`. `Module` grows an immutable `variant` and a denormalized `firmware_convergence` rollup. `ModuleFirmwareTarget` holds intent per `module_type` with scope precedence station > tag > fleet plus a canary gate. Pure functions in `reconciler.py` resolve the effective target/release and compute convergence from the *reported* version (never local flags), driving `ModuleFirmwareConvergenceState` bookkeeping and the rollup. The reconciler is hooked into heartbeat ingestion (`ingest.py`) and exposed to the agent through three views mirroring the deployment check/status/commit pattern 1:1. C stops at instruction + status: no DFU/flash, no idle-gate, no drain (all Teilbereich D). The download endpoint from B is reused, not duplicated.

**Tech Stack:** Django 6.0, DRF 3.17, PostgreSQL (SQLite in tests), pytest + pytest-django (`@pytest.mark.django_db`), Ed25519 `DeviceKeyAuthentication` + `IsDevice`. Tests live under `tests/module_firmware/`.

**Spec:** `docs/superpowers/specs/2026-09-25-module-firmware-C-reconciler-design.md` (overview context: `docs/superpowers/specs/2026-09-12-module-firmware-update-design.md`).

## Global Constraints

- **Builds on A (#137) + B (#138), both merged on `origin/main`.** New migrations start at `0007` (latest is `0006_modulefirmwareimportjob`). The on-disk working tree is a stale branch — trust `origin/main`, not on-disk `apps/module_firmware`.
- **One PR, one feature branch, squash-merge** (station-manager convention). Spec + plan + code on the same branch; frequent commits within the branch; no doc-only intermediate PRs.
- **`variant` is a fixed hardware property** on `Module` — set once at discovery from `identity.variant`, immutable thereafter. A later differing non-blank report → audit anomaly (once, not per heartbeat) + ignored; exactly the `module_type`-mismatch pattern already in `ingest.py`.
- **Reconciler state is always derived from the real reported version, never local flags** (crash-safe, idempotent — matches the overview agent principle).
- **C computes intent/flag only.** It exposes `Module.firmware_convergence` and computes drift/quarantine; it wires *nothing* into the control-lock path. idle-gate and drain/lock-out are Teilbereich D.
- **Reuse B's `ModuleFirmwareDownloadView`** (`GET /api/v1/module-firmware/<pk>/download/`, url name `module_firmware_api:download`). The check response's `download_url` points at it. Do not duplicate authz.
- **Quarantine limit N = 3**, defined as a module constant/setting (`QUARANTINE_ATTEMPT_LIMIT`).
- **Audit writes are best-effort** — wrap in try/except so a transient audit-table hiccup never breaks the primary state change (pattern from `ingest.py` / deployment views).
- **Django multi-line `{# … #}` comments are forbidden** — use `{% comment %}…{% endcomment %}` in any template touched.
- **Station tag model is `apps.stations.models.StationTag`** (verified; `Deployment.target_tag` FKs it). Use this real path for `ModuleFirmwareTarget.tag` and `.canary_tag`.
- **DE-locale note:** no numeric HTML inputs added here; N is a code constant, not a form field.

## Review Focus

Five inputs the spec implies but no core happy-path test exercises, most-likely-to-bite first. Each is pinned to a test in the owning task.

1. **A module reports `variant=""` (blank) at discovery, then a non-blank variant later.** Blank at discovery must remain overwritable *once* by the first non-blank report (a module that started reporting variant later), but a *change* between two non-blank values is the anomaly. → pinned in Task 3 (`test_variant_blank_then_nonblank_is_set_not_anomaly`).
2. **A fleet target has a `canary_tag` set and the module's station is NOT in that tag.** Must yield no effective target → no drift, `firmware_convergence` stays `unknown`/`ok`, and the module is never forced onto an old version. → pinned in Task 2 (`test_canary_gate_excludes_non_canary_station`).
3. **`desired_release_for_module` finds a type match but no release for the module's `variant`** (fm-vhf firmware never lands on a uhf module). Must return `None` — no flash, drift stays visible. → pinned in Task 2 (`test_no_release_for_variant_returns_none`).
4. **`reconcile/check` while a mid-flight `updating` row exists after an agent crash.** Must prefer/return that in-progress instruction (resume), not silently start over or return 204. → pinned in Task 5 (`test_check_prefers_midflight_updating`).
5. **`reconcile/<id>/status/` or `/commit/` posted for a convergence row whose module is no longer assigned to the calling station** (module pulled between check and status). Must 404, never mutate another station's row. → pinned in Task 6 (`test_status_404_when_module_not_assigned_to_station`) and Task 7 (`test_commit_404_when_not_bound_to_station`).

---

## File Structure

**Create:**
- `apps/module_firmware/migrations/0007_module_variant_convergence.py` — adds `Module.variant`, `Module.firmware_convergence`.
- `apps/module_firmware/migrations/0008_modulefirmwaretarget.py` — `ModuleFirmwareTarget` + partial-unique constraints.
- `apps/module_firmware/migrations/0009_modulefirmwareconvergencestate.py` — `ModuleFirmwareConvergenceState` + unique `(module, target_release)`.
- `apps/module_firmware/reconciler.py` — pure resolution + convergence functions. One responsibility: given the DB, compute effective target/release and drive convergence bookkeeping + the `Module.firmware_convergence` rollup. No HTTP, no request objects.
- `apps/module_firmware/reconcile_api_views.py` — the three DeviceKey reconcile views (check/status/commit). One responsibility: HTTP boundary + TOCTOU-guarded persistence, delegating logic to `reconciler.py`.
- `apps/module_firmware/reconcile_serializers.py` — DRF serializers for the reconcile request/response shapes.
- `tests/module_firmware/test_target_model.py`, `test_reconciler_resolution.py`, `test_reconciler_convergence.py`, `test_ingest_variant.py`, `test_reconcile_check.py`, `test_reconcile_status.py`, `test_reconcile_commit.py`, `test_reconcile_audit.py`, `test_dashboard_quarantine.py`, `test_reconcile_e2e.py`.

**Modify:**
- `apps/module_firmware/models.py` — `Module` fields, `ModuleFirmwareTarget`, `ModuleFirmwareConvergenceState`, `QUARANTINE_ATTEMPT_LIMIT`.
- `apps/module_firmware/ingest.py` — capture `identity.variant` into immutable `Module.variant` (mismatch-anomaly pattern); call `reconcile_module(module)` after the version update.
- `apps/module_firmware/api_urls.py` — add `reconcile/check/`, `reconcile/<int:convergence_id>/status/`, `reconcile/commit/`.
- `apps/stations/models.py` — add EventType choices for C.
- `apps/module_firmware/admin.py` — register `ModuleFirmwareTarget` and `ModuleFirmwareConvergenceState` (operator visibility + target CRUD).
- `apps/dashboard/views.py` — extend `module_stats` with a `quarantined` count + a quarantined-module queryset for the warning indicator.

---

## Task 1: `Module` fields — `variant` + `firmware_convergence`

**Files:**
- Modify: `apps/module_firmware/models.py` (class `Module`)
- Create: `apps/module_firmware/migrations/0007_module_variant_convergence.py`
- Test: `tests/module_firmware/test_module_model.py` (append; file exists on `origin/main`)

**Interfaces:**
- Produces:
  - `Module.variant: CharField(max_length=32, blank=True, default="")`
  - `Module.Convergence` — `TextChoices` with members `OK="ok"`, `UPDATING="updating"`, `QUARANTINED="quarantined"`, `UNKNOWN="unknown"`.
  - `Module.firmware_convergence: CharField(max_length=16, choices=Module.Convergence.choices, default=Module.Convergence.UNKNOWN)`

- [ ] **Step 1: Write the failing test**

```python
# tests/module_firmware/test_module_model.py  (append)
import pytest

from apps.module_firmware.models import Module, ModuleType


@pytest.mark.django_db
def test_module_has_variant_and_convergence_defaults():
    t = ModuleType.objects.create(key="fm", display_name="FM")
    m = Module.objects.create(uid="V1", module_type=t)
    assert m.variant == ""
    assert m.firmware_convergence == Module.Convergence.UNKNOWN


@pytest.mark.django_db
def test_module_convergence_choices_are_exhaustive():
    assert set(Module.Convergence.values) == {"ok", "updating", "quarantined", "unknown"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/module_firmware/test_module_model.py::test_module_has_variant_and_convergence_defaults -v`
Expected: FAIL — `AttributeError: type object 'Module' has no attribute 'Convergence'`.

- [ ] **Step 3: Write minimal implementation**

In `apps/module_firmware/models.py`, inside `class Module`, add the choices class next to the existing `Registration` and the two fields next to `last_reported_version`:

```python
    class Convergence(models.TextChoices):
        OK = "ok", _("OK")
        UPDATING = "updating", _("Updating")
        QUARANTINED = "quarantined", _("Quarantined")
        UNKNOWN = "unknown", _("Unknown")

    variant = models.CharField(_("variant"), max_length=32, blank=True, default="")
    firmware_convergence = models.CharField(
        _("firmware convergence"),
        max_length=16,
        choices=Convergence.choices,
        default=Convergence.UNKNOWN,
    )
```

Generate the migration:

```bash
python manage.py makemigrations module_firmware -n module_variant_convergence
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/module_firmware/test_module_model.py -v`
Expected: PASS. Also run `python manage.py makemigrations --check --dry-run` → no pending changes.

- [ ] **Step 5: Commit**

```bash
git add apps/module_firmware/models.py apps/module_firmware/migrations/0007_module_variant_convergence.py tests/module_firmware/test_module_model.py
git commit -m "feat(module_firmware): add Module.variant + firmware_convergence"
```

---

## Task 2: `ModuleFirmwareTarget` model + constraints

**Files:**
- Modify: `apps/module_firmware/models.py`
- Create: `apps/module_firmware/migrations/0008_modulefirmwaretarget.py`
- Test: `tests/module_firmware/test_target_model.py`

**Interfaces:**
- Consumes: `ModuleType`, `apps.stations.models.Station`, `apps.stations.models.StationTag`.
- Produces:
  - `ModuleFirmwareTarget.Scope` — `TextChoices` `FLEET="fleet"`, `TAG="tag"`, `STATION="station"`.
  - Fields: `module_type` (FK PROTECT), `version` (CharField), `scope`, `tag` (FK StationTag null), `station` (FK Station null), `canary_tag` (FK StationTag null, related_name `+`), `created_by`, `created_at`, `updated_at`.
  - Constraints: partial-unique per `(module_type)` where `scope='fleet'`; `(module_type, tag)` where `scope='tag'`; `(module_type, station)` where `scope='station'`.

- [ ] **Step 1: Write the failing test**

```python
# tests/module_firmware/test_target_model.py
import pytest
from django.db import IntegrityError

from apps.module_firmware.models import ModuleFirmwareTarget, ModuleType
from apps.stations.models import StationTag


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


@pytest.mark.django_db
def test_scope_choices():
    assert set(ModuleFirmwareTarget.Scope.values) == {"fleet", "tag", "station"}


@pytest.mark.django_db
def test_single_fleet_target_per_module_type(fm):
    ModuleFirmwareTarget.objects.create(
        module_type=fm, version="26.09.15-01", scope=ModuleFirmwareTarget.Scope.FLEET
    )
    with pytest.raises(IntegrityError):
        ModuleFirmwareTarget.objects.create(
            module_type=fm, version="26.09.16-01", scope=ModuleFirmwareTarget.Scope.FLEET
        )


@pytest.mark.django_db
def test_single_tag_target_per_type_tag(fm):
    tag = StationTag.objects.create(name="canary", slug="canary")
    ModuleFirmwareTarget.objects.create(
        module_type=fm, version="1", scope=ModuleFirmwareTarget.Scope.TAG, tag=tag
    )
    with pytest.raises(IntegrityError):
        ModuleFirmwareTarget.objects.create(
            module_type=fm, version="2", scope=ModuleFirmwareTarget.Scope.TAG, tag=tag
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/module_firmware/test_target_model.py -v`
Expected: FAIL — `ImportError: cannot import name 'ModuleFirmwareTarget'`.

- [ ] **Step 3: Write minimal implementation**

Append to `apps/module_firmware/models.py`:

```python
class ModuleFirmwareTarget(models.Model):
    """Declarative desired firmware version per module_type, with scope
    precedence station > tag > fleet and an optional canary gate."""

    class Scope(models.TextChoices):
        FLEET = "fleet", _("Fleet default")
        TAG = "tag", _("Tag override")
        STATION = "station", _("Station override")

    module_type = models.ForeignKey(
        ModuleType,
        on_delete=models.PROTECT,
        related_name="firmware_targets",
        verbose_name=_("module type"),
    )
    version = models.CharField(_("version"), max_length=64)
    scope = models.CharField(
        _("scope"), max_length=16, choices=Scope.choices, default=Scope.FLEET
    )
    tag = models.ForeignKey(
        "stations.StationTag",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="firmware_targets",
        verbose_name=_("tag"),
    )
    station = models.ForeignKey(
        "stations.Station",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="firmware_targets",
        verbose_name=_("station"),
    )
    canary_tag = models.ForeignKey(
        "stations.StationTag",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("canary tag"),
        help_text=_("While set, a fleet target applies only to stations in this tag."),
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("created by"),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("module firmware target")
        verbose_name_plural = _("module firmware targets")
        ordering = ["module_type", "scope"]
        constraints = [
            models.UniqueConstraint(
                fields=["module_type"],
                condition=models.Q(scope="fleet"),
                name="uniq_fleet_target_per_type",
            ),
            models.UniqueConstraint(
                fields=["module_type", "tag"],
                condition=models.Q(scope="tag"),
                name="uniq_tag_target_per_type_tag",
            ),
            models.UniqueConstraint(
                fields=["module_type", "station"],
                condition=models.Q(scope="station"),
                name="uniq_station_target_per_type_station",
            ),
        ]

    def __str__(self):
        return f"{self.module_type.key} {self.scope}={self.version}"
```

Add `from django.conf import settings` is already imported at top of `models.py` (verified). Generate the migration:

```bash
python manage.py makemigrations module_firmware -n modulefirmwaretarget
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/module_firmware/test_target_model.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/module_firmware/models.py apps/module_firmware/migrations/0008_modulefirmwaretarget.py tests/module_firmware/test_target_model.py
git commit -m "feat(module_firmware): add ModuleFirmwareTarget with scope constraints"
```

---

## Task 3: `ModuleFirmwareConvergenceState` model + quarantine constant

**Files:**
- Modify: `apps/module_firmware/models.py`
- Create: `apps/module_firmware/migrations/0009_modulefirmwareconvergencestate.py`
- Test: `tests/module_firmware/test_target_model.py` (append)

**Interfaces:**
- Consumes: `Module`, `ModuleFirmwareRelease`.
- Produces:
  - Module-level constant `QUARANTINE_ATTEMPT_LIMIT = 3`.
  - `ModuleFirmwareConvergenceState.State` — `TextChoices` `OK="ok"`, `UPDATING="updating"`, `QUARANTINED="quarantined"`.
  - `ModuleFirmwareConvergenceState.ErrorMode` — `TextChoices` `REJECTED="rejected"`, `ROLLED_BACK="rolled_back"`, `TRANSIENT="transient"` (blank `""` allowed on the field).
  - Fields: `module` (FK CASCADE), `target_release` (FK PROTECT), `state`, `attempts` (PositiveIntegerField default 0), `last_error_mode` (CharField blank), `last_error_message` (TextField blank), `created_at`, `updated_at`, `last_attempt_at` (nullable). Unique `(module, target_release)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/module_firmware/test_target_model.py  (append)
from apps.module_firmware.models import (
    Module,
    ModuleFirmwareConvergenceState,
    ModuleFirmwareRelease,
    QUARANTINE_ATTEMPT_LIMIT,
)


@pytest.mark.django_db
def test_quarantine_limit_is_three():
    assert QUARANTINE_ATTEMPT_LIMIT == 3


@pytest.mark.django_db
def test_convergence_state_unique_per_module_release(fm):
    m = Module.objects.create(uid="C1", module_type=fm, variant="vhf")
    rel = ModuleFirmwareRelease.objects.create(
        module_type=fm, variant="vhf", version="1",
        storage_key="k", sha256="a" * 64, size_bytes=1, source_repo="r", source_tag="t",
    )
    ModuleFirmwareConvergenceState.objects.create(module=m, target_release=rel)
    with pytest.raises(IntegrityError):
        ModuleFirmwareConvergenceState.objects.create(module=m, target_release=rel)


@pytest.mark.django_db
def test_convergence_state_defaults(fm):
    m = Module.objects.create(uid="C2", module_type=fm, variant="vhf")
    rel = ModuleFirmwareRelease.objects.create(
        module_type=fm, variant="vhf", version="1",
        storage_key="k", sha256="b" * 64, size_bytes=1, source_repo="r", source_tag="t",
    )
    cs = ModuleFirmwareConvergenceState.objects.create(module=m, target_release=rel)
    assert cs.state == ModuleFirmwareConvergenceState.State.UPDATING
    assert cs.attempts == 0
    assert cs.last_error_mode == ""
    assert cs.last_attempt_at is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/module_firmware/test_target_model.py::test_quarantine_limit_is_three -v`
Expected: FAIL — `ImportError: cannot import name 'QUARANTINE_ATTEMPT_LIMIT'`.

- [ ] **Step 3: Write minimal implementation**

Append to `apps/module_firmware/models.py` (constant near the top after imports, class at the bottom):

```python
QUARANTINE_ATTEMPT_LIMIT = 3
```

```python
class ModuleFirmwareConvergenceState(models.Model):
    """Reconciler bookkeeping per (module, target release): attempts,
    quarantine, last error mode. Quarantine binds to the tuple, so a new
    target version is a new row and is retried automatically."""

    class State(models.TextChoices):
        OK = "ok", _("OK")
        UPDATING = "updating", _("Updating")
        QUARANTINED = "quarantined", _("Quarantined")

    class ErrorMode(models.TextChoices):
        REJECTED = "rejected", _("Rejected")
        ROLLED_BACK = "rolled_back", _("Rolled back")
        TRANSIENT = "transient", _("Transient")

    module = models.ForeignKey(
        Module, on_delete=models.CASCADE, related_name="convergence_states",
        verbose_name=_("module"),
    )
    target_release = models.ForeignKey(
        ModuleFirmwareRelease, on_delete=models.PROTECT,
        related_name="convergence_states", verbose_name=_("target release"),
    )
    state = models.CharField(
        _("state"), max_length=16, choices=State.choices, default=State.UPDATING
    )
    attempts = models.PositiveIntegerField(_("attempts"), default=0)
    last_error_mode = models.CharField(
        _("last error mode"), max_length=16, choices=ErrorMode.choices, blank=True, default=""
    )
    last_error_message = models.TextField(_("last error message"), blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)
    last_attempt_at = models.DateTimeField(_("last attempt at"), null=True, blank=True)

    class Meta:
        verbose_name = _("module firmware convergence state")
        verbose_name_plural = _("module firmware convergence states")
        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["module", "target_release"],
                name="uniq_convergence_per_module_release",
            ),
        ]

    def __str__(self):
        return f"{self.module.uid} -> {self.target_release.version} [{self.state}]"
```

Generate:

```bash
python manage.py makemigrations module_firmware -n modulefirmwareconvergencestate
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/module_firmware/test_target_model.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/module_firmware/models.py apps/module_firmware/migrations/0009_modulefirmwareconvergencestate.py tests/module_firmware/test_target_model.py
git commit -m "feat(module_firmware): add ModuleFirmwareConvergenceState + quarantine constant"
```

---

## Task 4: Reconciler resolution — `effective_target` + `desired_release_for_module`

**Files:**
- Create: `apps/module_firmware/reconciler.py`
- Test: `tests/module_firmware/test_reconciler_resolution.py`

**Interfaces:**
- Consumes: `ModuleFirmwareTarget`, `Module`, `ModuleFirmwareRelease`, `ModuleAssignmentHistory`, `Station`, `StationTag`.
- Produces:
  - `effective_target(station, module_type) -> ModuleFirmwareTarget | None`
  - `desired_release_for_module(module) -> ModuleFirmwareRelease | None`

- [ ] **Step 1: Write the failing test**

```python
# tests/module_firmware/test_reconciler_resolution.py
import pytest

from apps.module_firmware.models import (
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
    ModuleType,
)
from apps.module_firmware.reconciler import desired_release_for_module, effective_target
from apps.stations.models import StationTag


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


def _target(fm, scope, version, **kw):
    return ModuleFirmwareTarget.objects.create(
        module_type=fm, scope=scope, version=version, **kw
    )


@pytest.mark.django_db
def test_precedence_station_over_tag_over_fleet(fm, station_factory):
    st = station_factory()
    tag = StationTag.objects.create(name="t", slug="t")
    st.tags.add(tag)
    _target(fm, ModuleFirmwareTarget.Scope.FLEET, "fleet-v")
    _target(fm, ModuleFirmwareTarget.Scope.TAG, "tag-v", tag=tag)
    _target(fm, ModuleFirmwareTarget.Scope.STATION, "station-v", station=st)
    assert effective_target(st, fm).version == "station-v"


@pytest.mark.django_db
def test_tag_over_fleet_when_no_station_target(fm, station_factory):
    st = station_factory()
    tag = StationTag.objects.create(name="t", slug="t")
    st.tags.add(tag)
    _target(fm, ModuleFirmwareTarget.Scope.FLEET, "fleet-v")
    _target(fm, ModuleFirmwareTarget.Scope.TAG, "tag-v", tag=tag)
    assert effective_target(st, fm).version == "tag-v"


@pytest.mark.django_db
def test_multiple_tag_targets_newest_updated_at_wins(fm, station_factory):
    st = station_factory()
    a = StationTag.objects.create(name="a", slug="a")
    b = StationTag.objects.create(name="b", slug="b")
    st.tags.add(a, b)
    ta = _target(fm, ModuleFirmwareTarget.Scope.TAG, "a-v", tag=a)
    tb = _target(fm, ModuleFirmwareTarget.Scope.TAG, "b-v", tag=b)
    # Force tb to be the newer updated_at.
    tb.version = "b-v2"
    tb.save()
    assert effective_target(st, fm).pk == tb.pk


@pytest.mark.django_db
def test_canary_gate_excludes_non_canary_station(fm, station_factory):
    # Review Focus #2: fleet target gated on canary_tag, station not in tag.
    st = station_factory()
    canary = StationTag.objects.create(name="canary", slug="canary")
    _target(fm, ModuleFirmwareTarget.Scope.FLEET, "fleet-v", canary_tag=canary)
    assert effective_target(st, fm) is None


@pytest.mark.django_db
def test_canary_gate_includes_canary_station(fm, station_factory):
    st = station_factory()
    canary = StationTag.objects.create(name="canary", slug="canary")
    st.tags.add(canary)
    _target(fm, ModuleFirmwareTarget.Scope.FLEET, "fleet-v", canary_tag=canary)
    assert effective_target(st, fm).version == "fleet-v"


@pytest.mark.django_db
def test_desired_release_resolves_type_and_variant(fm, station_factory):
    st = station_factory()
    m = Module.objects.create(uid="U1", module_type=fm, variant="vhf")
    ModuleAssignmentHistory.objects.create(module=m, station=st, slot="slot0")
    _target(fm, ModuleFirmwareTarget.Scope.FLEET, "26.09.15-01")
    rel = ModuleFirmwareRelease.objects.create(
        module_type=fm, variant="vhf", version="26.09.15-01",
        storage_key="k", sha256="a" * 64, size_bytes=1, source_repo="r", source_tag="t",
    )
    assert desired_release_for_module(m).pk == rel.pk


@pytest.mark.django_db
def test_no_release_for_variant_returns_none(fm, station_factory):
    # Review Focus #3: type matches but no release for the module's variant.
    st = station_factory()
    m = Module.objects.create(uid="U2", module_type=fm, variant="uhf")
    ModuleAssignmentHistory.objects.create(module=m, station=st, slot="slot0")
    _target(fm, ModuleFirmwareTarget.Scope.FLEET, "26.09.15-01")
    ModuleFirmwareRelease.objects.create(
        module_type=fm, variant="vhf", version="26.09.15-01",
        storage_key="k", sha256="a" * 64, size_bytes=1, source_repo="r", source_tag="t",
    )
    assert desired_release_for_module(m) is None


@pytest.mark.django_db
def test_no_open_assignment_returns_none(fm):
    m = Module.objects.create(uid="U3", module_type=fm, variant="vhf")
    assert desired_release_for_module(m) is None


@pytest.mark.django_db
def test_archived_release_is_not_desired(fm, station_factory):
    st = station_factory()
    m = Module.objects.create(uid="U4", module_type=fm, variant="vhf")
    ModuleAssignmentHistory.objects.create(module=m, station=st, slot="slot0")
    _target(fm, ModuleFirmwareTarget.Scope.FLEET, "26.09.15-01")
    rel = ModuleFirmwareRelease.objects.create(
        module_type=fm, variant="vhf", version="26.09.15-01",
        storage_key="k", sha256="a" * 64, size_bytes=1, source_repo="r", source_tag="t",
    )
    rel.archive()
    assert desired_release_for_module(m) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/module_firmware/test_reconciler_resolution.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'apps.module_firmware.reconciler'`.

- [ ] **Step 3: Write minimal implementation**

Create `apps/module_firmware/reconciler.py`:

```python
"""Pure desired/actual resolution + convergence bookkeeping for module
firmware. No HTTP, no request objects — callable from ingestion, target
mutation, and the reconcile API. State is always derived from the real
reported version, never from local flags."""

import logging

from django.db import transaction
from django.utils import timezone

from apps.stations.models import StationAuditLog

from .models import (
    QUARANTINE_ATTEMPT_LIMIT,
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareConvergenceState,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
)

logger = logging.getLogger(__name__)


def effective_target(station, module_type):
    """Resolve the effective ModuleFirmwareTarget for (station, module_type)
    with precedence station > tag > fleet, honouring the canary gate.

    Returns None when nothing applies (incl. a fleet target whose canary_tag
    excludes this station)."""
    station_t = ModuleFirmwareTarget.objects.filter(
        module_type=module_type, scope=ModuleFirmwareTarget.Scope.STATION, station=station
    ).first()
    if station_t is not None:
        return station_t

    station_tag_ids = list(station.tags.values_list("id", flat=True))
    if station_tag_ids:
        tag_t = (
            ModuleFirmwareTarget.objects.filter(
                module_type=module_type,
                scope=ModuleFirmwareTarget.Scope.TAG,
                tag_id__in=station_tag_ids,
            )
            .order_by("-updated_at")
            .first()
        )
        if tag_t is not None:
            return tag_t

    fleet_t = ModuleFirmwareTarget.objects.filter(
        module_type=module_type, scope=ModuleFirmwareTarget.Scope.FLEET
    ).first()
    if fleet_t is None:
        return None
    # Canary gate: while canary_tag is set, a fleet target applies only to
    # stations carrying that tag. Non-canary stations get no target — no drift.
    if fleet_t.canary_tag_id is not None and fleet_t.canary_tag_id not in station_tag_ids:
        return None
    return fleet_t


def desired_release_for_module(module):
    """Resolve the concrete, non-archived ModuleFirmwareRelease for a module:
    its open-assignment station + module_type + variant -> effective target
    version -> the release matching (module_type, variant, version).

    Type AND variant gate: no release for the variant -> None (no flash,
    drift stays visible)."""
    assignment = (
        ModuleAssignmentHistory.objects.filter(module=module, to_ts__isnull=True)
        .select_related("station")
        .first()
    )
    if assignment is None or assignment.station is None:
        return None
    target = effective_target(assignment.station, module.module_type)
    if target is None:
        return None
    return ModuleFirmwareRelease.objects.filter(
        module_type=module.module_type,
        variant=module.variant,
        version=target.version,
    ).first()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/module_firmware/test_reconciler_resolution.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/module_firmware/reconciler.py tests/module_firmware/test_reconciler_resolution.py
git commit -m "feat(module_firmware): reconciler target + release resolution"
```

---

## Task 5: Reconciler convergence — `reconcile_module` + error-mode bookkeeping

**Files:**
- Modify: `apps/module_firmware/reconciler.py`
- Test: `tests/module_firmware/test_reconciler_convergence.py`

**Interfaces:**
- Consumes: `effective_target`, `desired_release_for_module`, `QUARANTINE_ATTEMPT_LIMIT`, `ModuleFirmwareConvergenceState`.
- Produces:
  - `reconcile_module(module) -> ModuleFirmwareConvergenceState | None` — compares `module.last_reported_version` vs desired.version; upserts the ConvergenceState; maintains the `Module.firmware_convergence` rollup; idempotent.
  - `record_error(convergence, error_mode) -> ModuleFirmwareConvergenceState` — applies the error-mode rules: `REJECTED` → immediate quarantine (no attempts++); `ROLLED_BACK` → attempts++, quarantine at `>= QUARANTINE_ATTEMPT_LIMIT`; `TRANSIENT` → no attempts++, stays `updating`. Stamps `last_attempt_at`, `last_error_mode`. Maintains the rollup.
  - `_set_convergence_rollup(module, value) -> None` — writes `Module.firmware_convergence` if changed (update_fields).

- [ ] **Step 1: Write the failing test**

```python
# tests/module_firmware/test_reconciler_convergence.py
import pytest

from apps.module_firmware.models import (
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareConvergenceState,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
    ModuleType,
    QUARANTINE_ATTEMPT_LIMIT,
)
from apps.module_firmware.reconciler import reconcile_module, record_error


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


def _module_with_target(fm, station, *, variant="vhf", version="", target="26.09.15-01"):
    m = Module.objects.create(
        uid="M1", module_type=fm, variant=variant, last_reported_version=version
    )
    ModuleAssignmentHistory.objects.create(module=m, station=station, slot="slot0")
    ModuleFirmwareTarget.objects.create(
        module_type=fm, scope=ModuleFirmwareTarget.Scope.FLEET, version=target
    )
    ModuleFirmwareRelease.objects.create(
        module_type=fm, variant=variant, version=target,
        storage_key="k", sha256="a" * 64, size_bytes=1, source_repo="r", source_tag="t",
    )
    return m


@pytest.mark.django_db
def test_reported_equals_desired_is_ok(fm, station_factory):
    st = station_factory()
    m = _module_with_target(fm, st, version="26.09.15-01")
    cs = reconcile_module(m)
    assert cs.state == ModuleFirmwareConvergenceState.State.OK
    m.refresh_from_db()
    assert m.firmware_convergence == Module.Convergence.OK


@pytest.mark.django_db
def test_reported_differs_is_updating(fm, station_factory):
    st = station_factory()
    m = _module_with_target(fm, st, version="26.09.10-01")
    cs = reconcile_module(m)
    assert cs.state == ModuleFirmwareConvergenceState.State.UPDATING
    m.refresh_from_db()
    assert m.firmware_convergence == Module.Convergence.UPDATING


@pytest.mark.django_db
def test_no_desired_no_drift(fm, station_factory):
    st = station_factory()
    m = Module.objects.create(uid="ND", module_type=fm, variant="vhf", last_reported_version="x")
    ModuleAssignmentHistory.objects.create(module=m, station=st, slot="slot0")
    assert reconcile_module(m) is None
    m.refresh_from_db()
    assert m.firmware_convergence == Module.Convergence.UNKNOWN


@pytest.mark.django_db
def test_reconcile_is_idempotent(fm, station_factory):
    st = station_factory()
    m = _module_with_target(fm, st, version="26.09.10-01")
    reconcile_module(m)
    reconcile_module(m)
    assert ModuleFirmwareConvergenceState.objects.filter(module=m).count() == 1


@pytest.mark.django_db
def test_rejected_immediate_quarantine(fm, station_factory):
    st = station_factory()
    m = _module_with_target(fm, st, version="26.09.10-01")
    cs = reconcile_module(m)
    cs = record_error(cs, ModuleFirmwareConvergenceState.ErrorMode.REJECTED)
    assert cs.state == ModuleFirmwareConvergenceState.State.QUARANTINED
    assert cs.attempts == 0
    m.refresh_from_db()
    assert m.firmware_convergence == Module.Convergence.QUARANTINED


@pytest.mark.django_db
def test_rolled_back_quarantines_at_limit(fm, station_factory):
    st = station_factory()
    m = _module_with_target(fm, st, version="26.09.10-01")
    cs = reconcile_module(m)
    for _ in range(QUARANTINE_ATTEMPT_LIMIT - 1):
        cs = record_error(cs, ModuleFirmwareConvergenceState.ErrorMode.ROLLED_BACK)
        assert cs.state == ModuleFirmwareConvergenceState.State.UPDATING
    cs = record_error(cs, ModuleFirmwareConvergenceState.ErrorMode.ROLLED_BACK)
    assert cs.attempts == QUARANTINE_ATTEMPT_LIMIT
    assert cs.state == ModuleFirmwareConvergenceState.State.QUARANTINED


@pytest.mark.django_db
def test_transient_does_not_count(fm, station_factory):
    st = station_factory()
    m = _module_with_target(fm, st, version="26.09.10-01")
    cs = reconcile_module(m)
    for _ in range(5):
        cs = record_error(cs, ModuleFirmwareConvergenceState.ErrorMode.TRANSIENT)
    assert cs.attempts == 0
    assert cs.state == ModuleFirmwareConvergenceState.State.UPDATING


@pytest.mark.django_db
def test_new_target_version_new_row_retried(fm, station_factory):
    st = station_factory()
    m = _module_with_target(fm, st, version="26.09.10-01")
    cs = reconcile_module(m)
    record_error(cs, ModuleFirmwareConvergenceState.ErrorMode.REJECTED)
    # Operator ships a fix: new target version + release -> new row, retried.
    ModuleFirmwareTarget.objects.filter(module_type=fm).update(version="26.09.20-01")
    ModuleFirmwareRelease.objects.create(
        module_type=fm, variant="vhf", version="26.09.20-01",
        storage_key="k2", sha256="c" * 64, size_bytes=1, source_repo="r", source_tag="t2",
    )
    cs2 = reconcile_module(m)
    assert cs2.pk != cs.pk
    assert cs2.state == ModuleFirmwareConvergenceState.State.UPDATING
    assert ModuleFirmwareConvergenceState.objects.filter(module=m).count() == 2
    m.refresh_from_db()
    assert m.firmware_convergence == Module.Convergence.UPDATING


@pytest.mark.django_db
def test_quarantined_row_not_reactivated_on_reconcile(fm, station_factory):
    st = station_factory()
    m = _module_with_target(fm, st, version="26.09.10-01")
    cs = reconcile_module(m)
    record_error(cs, ModuleFirmwareConvergenceState.ErrorMode.REJECTED)
    cs2 = reconcile_module(m)  # same target still drifted
    assert cs2.state == ModuleFirmwareConvergenceState.State.QUARANTINED
    m.refresh_from_db()
    assert m.firmware_convergence == Module.Convergence.QUARANTINED
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/module_firmware/test_reconciler_convergence.py -v`
Expected: FAIL — `ImportError: cannot import name 'record_error'`.

- [ ] **Step 3: Write minimal implementation**

Append to `apps/module_firmware/reconciler.py`:

```python
def _set_convergence_rollup(module, value):
    """Denormalized Module.firmware_convergence — cheap read for D/dashboard."""
    if module.firmware_convergence != value:
        module.firmware_convergence = value
        module.save(update_fields=["firmware_convergence", "updated_at"])


@transaction.atomic
def reconcile_module(module):
    """Compare reported vs desired, upsert the ConvergenceState for the current
    target release, and maintain the Module.firmware_convergence rollup.

    Idempotent. Returns the active ConvergenceState, or None when there is no
    desired release (no drift; rollup falls back to ``ok`` if the module is
    running something, else ``unknown``)."""
    desired = desired_release_for_module(module)
    if desired is None:
        # No target/variant match => no drift. Keep unknown unless we already
        # know it is running a version (then it is trivially ok w.r.t. intent).
        _set_convergence_rollup(
            module,
            Module.Convergence.UNKNOWN
            if module.firmware_convergence == Module.Convergence.UNKNOWN
            else Module.Convergence.OK,
        )
        return None

    cs, _created = ModuleFirmwareConvergenceState.objects.get_or_create(
        module=module, target_release=desired
    )

    # A quarantined row for the still-current target stays quarantined — never
    # auto-reactivated. A fix is a NEW target release => a different row.
    if cs.state == ModuleFirmwareConvergenceState.State.QUARANTINED:
        _set_convergence_rollup(module, Module.Convergence.QUARANTINED)
        return cs

    reported = module.last_reported_version or ""
    if reported == desired.version:
        cs.state = ModuleFirmwareConvergenceState.State.OK
        cs.save(update_fields=["state", "updated_at"])
        _set_convergence_rollup(module, Module.Convergence.OK)
    else:
        cs.state = ModuleFirmwareConvergenceState.State.UPDATING
        cs.save(update_fields=["state", "updated_at"])
        _set_convergence_rollup(module, Module.Convergence.UPDATING)
    return cs


@transaction.atomic
def record_error(convergence, error_mode, error_message=""):
    """Apply a reported failure to a ConvergenceState per the quarantine rules:

    - rejected  -> immediate quarantine, attempts unchanged (retry never helps);
    - rolled_back -> attempts++, quarantine once attempts >= N;
    - transient -> no attempts++, stays updating.

    Stamps last_attempt_at/last_error_mode and maintains the rollup."""
    convergence.last_error_mode = error_mode
    convergence.last_error_message = error_message
    convergence.last_attempt_at = timezone.now()
    fields = ["last_error_mode", "last_error_message", "last_attempt_at", "updated_at"]

    if error_mode == ModuleFirmwareConvergenceState.ErrorMode.REJECTED:
        convergence.state = ModuleFirmwareConvergenceState.State.QUARANTINED
        fields.append("state")
    elif error_mode == ModuleFirmwareConvergenceState.ErrorMode.ROLLED_BACK:
        convergence.attempts += 1
        fields.append("attempts")
        if convergence.attempts >= QUARANTINE_ATTEMPT_LIMIT:
            convergence.state = ModuleFirmwareConvergenceState.State.QUARANTINED
            fields.append("state")
    # transient: no attempts change, state stays updating.

    convergence.save(update_fields=fields)

    if convergence.state == ModuleFirmwareConvergenceState.State.QUARANTINED:
        _set_convergence_rollup(convergence.module, Module.Convergence.QUARANTINED)
    else:
        _set_convergence_rollup(convergence.module, Module.Convergence.UPDATING)
    return convergence
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/module_firmware/test_reconciler_convergence.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/module_firmware/reconciler.py tests/module_firmware/test_reconciler_convergence.py
git commit -m "feat(module_firmware): reconcile_module convergence + error-mode bookkeeping"
```

---

## Task 6: Ingest hook — variant capture (immutable) + reconcile call

**Files:**
- Modify: `apps/module_firmware/ingest.py`
- Test: `tests/module_firmware/test_ingest_variant.py`

**Interfaces:**
- Consumes: `reconcile_module` (Task 5); the existing `ingest_module(station, slot, module_id, identity, *, now, user=None)` signature (unchanged).
- Produces: on create, `Module.variant` is set from `identity.get("variant", "")`. On update: a blank stored variant is filled once from a non-blank report (not an anomaly); a *change* between two non-blank variants is audited once (EventType `UPDATED`, mirroring the type-mismatch pattern) and ignored. After the version update, `reconcile_module(module)` is called (best-effort).

- [ ] **Step 1: Write the failing test**

```python
# tests/module_firmware/test_ingest_variant.py
import pytest
from django.utils import timezone

from apps.module_firmware.ingest import ingest_module
from apps.module_firmware.models import (
    Module,
    ModuleFirmwareConvergenceState,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
    ModuleType,
)
from apps.stations.models import StationAuditLog


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


def ident(**kw):
    base = {"type": "fm", "model": "SA818", "version": "26.09.10-01", "uid": "UID1"}
    base.update(kw)
    return base


@pytest.mark.django_db
def test_variant_captured_at_discovery(fm, station_factory):
    st = station_factory()
    m = ingest_module(st, "slot0", "fm", ident(variant="vhf"), now=timezone.now())
    assert m.variant == "vhf"


@pytest.mark.django_db
def test_variant_immutable_change_is_anomaly(fm, station_factory):
    st = station_factory()
    ingest_module(st, "slot0", "fm", ident(variant="vhf"), now=timezone.now())
    for _ in range(3):
        ingest_module(st, "slot0", "fm", ident(variant="uhf"), now=timezone.now())
    m = Module.objects.get(uid="UID1")
    assert m.variant == "vhf"  # unchanged
    # Audited once, not per heartbeat.
    assert (
        StationAuditLog.objects.filter(
            module=m, event_type=StationAuditLog.EventType.UPDATED, message__icontains="variant"
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_variant_blank_then_nonblank_is_set_not_anomaly(fm, station_factory):
    # Review Focus #1: blank at discovery, non-blank later -> fill once, no anomaly.
    st = station_factory()
    ingest_module(st, "slot0", "fm", ident(variant=""), now=timezone.now())
    ingest_module(st, "slot0", "fm", ident(variant="vhf"), now=timezone.now())
    m = Module.objects.get(uid="UID1")
    assert m.variant == "vhf"
    assert (
        StationAuditLog.objects.filter(
            module=m, event_type=StationAuditLog.EventType.UPDATED, message__icontains="variant"
        ).count()
        == 0
    )


@pytest.mark.django_db
def test_ingest_triggers_reconcile(fm, station_factory):
    st = station_factory()
    ModuleFirmwareTarget.objects.create(
        module_type=fm, scope=ModuleFirmwareTarget.Scope.FLEET, version="26.09.15-01"
    )
    ModuleFirmwareRelease.objects.create(
        module_type=fm, variant="vhf", version="26.09.15-01",
        storage_key="k", sha256="a" * 64, size_bytes=1, source_repo="r", source_tag="t",
    )
    # Discovery reports an old version -> drift -> updating.
    m = ingest_module(
        st, "slot0", "fm", ident(variant="vhf", version="26.09.10-01"), now=timezone.now()
    )
    m.refresh_from_db()
    assert m.firmware_convergence == Module.Convergence.UPDATING
    assert ModuleFirmwareConvergenceState.objects.filter(module=m).count() == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/module_firmware/test_ingest_variant.py -v`
Expected: FAIL — `test_variant_captured_at_discovery` asserts `m.variant == "vhf"` but ingest never sets it (empty string).

- [ ] **Step 3: Write minimal implementation**

In `apps/module_firmware/ingest.py`:

Add the import near the top (after the existing model import):

```python
from .reconciler import reconcile_module
```

Extend the `get_or_create` defaults in `ingest_module` to capture variant at discovery:

```python
    version = identity.get("version", "")
    variant = identity.get("variant", "") or ""

    module, created = Module.objects.get_or_create(
        uid=uid,
        defaults={
            "module_type": module_type,
            "uid_source": uid_source,
            "last_reported_version": version,
            "variant": variant,
            "first_seen": now,
            "last_seen": now,
        },
    )
```

In the `else:` (existing module) branch, after the existing type-mismatch guard block and before `old_version = module.last_reported_version`, add the variant handling:

```python
        # variant is a fixed HW property: fill a blank once, but a change
        # between two non-blank variants is an anomaly (audit once, ignore).
        reported_variant = identity.get("variant", "") or ""
        if reported_variant:
            if not module.variant:
                module.variant = reported_variant
                module.save(update_fields=["variant", "updated_at"])
            elif module.variant != reported_variant:
                variant_msg = (
                    f"Variant mismatch for uid {uid}: reported "
                    f"'{reported_variant}', tracked as '{module.variant}'. Ignored."
                )
                already = StationAuditLog.objects.filter(
                    module=module,
                    event_type=StationAuditLog.EventType.UPDATED,
                    message=variant_msg,
                ).exists()
                if not already:
                    StationAuditLog.log(
                        station=station,
                        module=module,
                        event_type=StationAuditLog.EventType.UPDATED,
                        message=variant_msg,
                    )
                logger.warning(
                    "ingest: variant mismatch uid=%s reported=%s tracked=%s",
                    uid, reported_variant, module.variant,
                )
```

At the end of `ingest_module`, after `_apply_assignment(...)` and `_derive_lifecycle(...)` and before `return module`, add the reconcile call (best-effort — a reconcile hiccup must not break inventory ingestion):

```python
    try:
        reconcile_module(module)
    except Exception:
        logger.warning("ingest: reconcile_module failed for uid=%s", uid, exc_info=True)

    return module
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/module_firmware/test_ingest_variant.py -v`
Then regression: `pytest tests/module_firmware/test_ingest_core.py tests/module_firmware/test_ingest_lifecycle.py tests/module_firmware/test_ingest_swap.py -v`
Expected: PASS (variant defaults to `""` so existing ingest tests are unaffected).

- [ ] **Step 5: Commit**

```bash
git add apps/module_firmware/ingest.py tests/module_firmware/test_ingest_variant.py
git commit -m "feat(module_firmware): capture immutable variant + reconcile hook in ingest"
```

---

## Task 7: Audit EventTypes for C

**Files:**
- Modify: `apps/stations/models.py` (class `StationAuditLog.EventType`)
- Modify: `apps/module_firmware/reconciler.py` (emit `MODULE_QUARANTINED` when a state transitions to quarantined)
- Test: `tests/module_firmware/test_reconcile_audit.py`

**Interfaces:**
- Produces: new `EventType` members `FIRMWARE_TARGET_SET`, `MODULE_FLASH_STARTED`, `MODULE_FLASH_SUCCESS`, `MODULE_FLASH_ROLLED_BACK`, `MODULE_FLASH_REJECTED`, `MODULE_FLASH_FAILED`, `MODULE_QUARANTINED`. `record_error` emits `MODULE_QUARANTINED` (best-effort) exactly once on the transition into quarantine.

- [ ] **Step 1: Write the failing test**

```python
# tests/module_firmware/test_reconcile_audit.py
import pytest

from apps.module_firmware.models import (
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareConvergenceState,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
    ModuleType,
)
from apps.module_firmware.reconciler import reconcile_module, record_error
from apps.stations.models import StationAuditLog


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


def test_new_event_types_exist():
    for name in [
        "FIRMWARE_TARGET_SET",
        "MODULE_FLASH_STARTED",
        "MODULE_FLASH_SUCCESS",
        "MODULE_FLASH_ROLLED_BACK",
        "MODULE_FLASH_REJECTED",
        "MODULE_FLASH_FAILED",
        "MODULE_QUARANTINED",
    ]:
        assert hasattr(StationAuditLog.EventType, name)


@pytest.mark.django_db
def test_quarantine_emits_audit_once(fm, station_factory):
    st = station_factory()
    m = Module.objects.create(
        uid="Q1", module_type=fm, variant="vhf", last_reported_version="26.09.10-01"
    )
    ModuleAssignmentHistory.objects.create(module=m, station=st, slot="slot0")
    ModuleFirmwareTarget.objects.create(
        module_type=fm, scope=ModuleFirmwareTarget.Scope.FLEET, version="26.09.15-01"
    )
    ModuleFirmwareRelease.objects.create(
        module_type=fm, variant="vhf", version="26.09.15-01",
        storage_key="k", sha256="a" * 64, size_bytes=1, source_repo="r", source_tag="t",
    )
    cs = reconcile_module(m)
    record_error(cs, ModuleFirmwareConvergenceState.ErrorMode.REJECTED)
    assert (
        StationAuditLog.objects.filter(
            module=m, event_type=StationAuditLog.EventType.MODULE_QUARANTINED
        ).count()
        == 1
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/module_firmware/test_reconcile_audit.py::test_new_event_types_exist -v`
Expected: FAIL — `AssertionError` (attribute missing).

- [ ] **Step 3: Write minimal implementation**

In `apps/stations/models.py`, append to `StationAuditLog.EventType` after `MODULE_ASSIGNMENT_CHANGED`:

```python
        FIRMWARE_TARGET_SET = "firmware_target_set", _("Firmware Target Set")
        MODULE_FLASH_STARTED = "module_flash_started", _("Module Flash Started")
        MODULE_FLASH_SUCCESS = "module_flash_success", _("Module Flash Success")
        MODULE_FLASH_ROLLED_BACK = "module_flash_rolled_back", _("Module Flash Rolled Back")
        MODULE_FLASH_REJECTED = "module_flash_rejected", _("Module Flash Rejected")
        MODULE_FLASH_FAILED = "module_flash_failed", _("Module Flash Failed")
        MODULE_QUARANTINED = "module_quarantined", _("Module Quarantined")
```

These are `choices` additions on a `CharField` — Django does not require a schema migration for changed choices, but run `makemigrations` to capture the `choices` metadata change:

```bash
python manage.py makemigrations stations -n c_firmware_audit_event_types
```

In `apps/module_firmware/reconciler.py`, at the end of `record_error`, replace the trailing rollup block so quarantine transitions also emit an audit row once (best-effort). Change:

```python
    if convergence.state == ModuleFirmwareConvergenceState.State.QUARANTINED:
        _set_convergence_rollup(convergence.module, Module.Convergence.QUARANTINED)
    else:
        _set_convergence_rollup(convergence.module, Module.Convergence.UPDATING)
    return convergence
```

to:

```python
    if convergence.state == ModuleFirmwareConvergenceState.State.QUARANTINED:
        was_quarantined = (
            convergence.module.firmware_convergence == Module.Convergence.QUARANTINED
        )
        _set_convergence_rollup(convergence.module, Module.Convergence.QUARANTINED)
        if not was_quarantined:
            _audit(
                convergence.module,
                StationAuditLog.EventType.MODULE_QUARANTINED,
                f"Module {convergence.module.uid} quarantined for target "
                f"{convergence.target_release.version} ({error_mode}).",
            )
    else:
        _set_convergence_rollup(convergence.module, Module.Convergence.UPDATING)
    return convergence
```

Add a best-effort audit helper near the top of `reconciler.py` (after `logger = ...`):

```python
def _audit(module, event_type, message, *, station=None):
    """Best-effort dual-subject audit write — never break the state change."""
    try:
        StationAuditLog.log(
            station=station, module=module, event_type=event_type, message=message
        )
    except Exception:
        logger.warning("reconciler: audit write failed (%s)", event_type, exc_info=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/module_firmware/test_reconcile_audit.py tests/module_firmware/test_reconciler_convergence.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/stations/models.py apps/stations/migrations/ apps/module_firmware/reconciler.py tests/module_firmware/test_reconcile_audit.py
git commit -m "feat(module_firmware): C audit event types + quarantine audit"
```

---

## Task 8: Reconcile `check` endpoint

**Files:**
- Create: `apps/module_firmware/reconcile_serializers.py`
- Create: `apps/module_firmware/reconcile_api_views.py`
- Modify: `apps/module_firmware/api_urls.py`
- Test: `tests/module_firmware/test_reconcile_check.py`

**Interfaces:**
- Consumes: `DeviceKeyAuthentication`, `IsDevice`, `reconcile_module`, `desired_release_for_module`; conftest fixtures `station_with_key`, `device_auth_headers`; url name `module_firmware_api:download`.
- Produces:
  - `ReconcileCheckRequestSerializer` (empty/optional body — no required fields).
  - `ReconcileCheckResponseSerializer` with fields: `convergence_id, module_uid, slot, module_type, variant, target_version, download_url, checksum_sha256, size_bytes`.
  - `ReconcileCheckView` (POST, url name `reconcile_check`). Selects exactly one instruction: highest-priority drifted, non-quarantined module assigned to this station; prefers a mid-flight `updating` row (resume). Deterministic ordering by slot. 200 with the payload; 204 when nothing to do or no open assignment.

- [ ] **Step 1: Write the failing test**

```python
# tests/module_firmware/test_reconcile_check.py
import pytest
from django.urls import reverse

from apps.module_firmware.models import (
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareConvergenceState,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
    ModuleType,
)
from tests.conftest import device_auth_headers


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


def _release(fm, version="26.09.15-01", variant="vhf"):
    return ModuleFirmwareRelease.objects.create(
        module_type=fm, variant=variant, version=version,
        storage_key="k", sha256="a" * 64, size_bytes=123, source_repo="r", source_tag="t",
    )


def _drifted_module(fm, station, *, uid="U1", slot="slot0", variant="vhf"):
    m = Module.objects.create(
        uid=uid, module_type=fm, variant=variant, last_reported_version="26.09.10-01"
    )
    ModuleAssignmentHistory.objects.create(module=m, station=station, slot=slot)
    return m


@pytest.mark.django_db
def test_check_204_when_nothing_to_do(client, station_with_key, fm):
    station, priv = station_with_key
    resp = client.post(
        reverse("module_firmware_api:reconcile_check"),
        data="{}", content_type="application/json",
        **device_auth_headers(priv, station.pk, b"{}"),
    )
    assert resp.status_code == 204


@pytest.mark.django_db
def test_check_returns_one_instruction(client, station_with_key, fm):
    station, priv = station_with_key
    ModuleFirmwareTarget.objects.create(
        module_type=fm, scope=ModuleFirmwareTarget.Scope.FLEET, version="26.09.15-01"
    )
    rel = _release(fm)
    m = _drifted_module(fm, station)
    resp = client.post(
        reverse("module_firmware_api:reconcile_check"),
        data="{}", content_type="application/json",
        **device_auth_headers(priv, station.pk, b"{}"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["module_uid"] == m.uid
    assert body["slot"] == "slot0"
    assert body["module_type"] == "fm"
    assert body["variant"] == "vhf"
    assert body["target_version"] == "26.09.15-01"
    assert body["checksum_sha256"] == "a" * 64
    assert body["size_bytes"] == 123
    assert body["download_url"] == reverse(
        "module_firmware_api:download", args=[rel.pk]
    )
    assert body["convergence_id"] == ModuleFirmwareConvergenceState.objects.get(module=m).pk


@pytest.mark.django_db
def test_check_skips_quarantined(client, station_with_key, fm):
    station, priv = station_with_key
    ModuleFirmwareTarget.objects.create(
        module_type=fm, scope=ModuleFirmwareTarget.Scope.FLEET, version="26.09.15-01"
    )
    rel = _release(fm)
    m = _drifted_module(fm, station)
    ModuleFirmwareConvergenceState.objects.create(
        module=m, target_release=rel,
        state=ModuleFirmwareConvergenceState.State.QUARANTINED,
    )
    resp = client.post(
        reverse("module_firmware_api:reconcile_check"),
        data="{}", content_type="application/json",
        **device_auth_headers(priv, station.pk, b"{}"),
    )
    assert resp.status_code == 204


@pytest.mark.django_db
def test_check_prefers_midflight_updating(client, station_with_key, fm):
    # Review Focus #4: two drifted modules; the one already updating wins (resume).
    station, priv = station_with_key
    ModuleFirmwareTarget.objects.create(
        module_type=fm, scope=ModuleFirmwareTarget.Scope.FLEET, version="26.09.15-01"
    )
    rel = _release(fm)
    m0 = _drifted_module(fm, station, uid="U0", slot="slot0")
    m1 = _drifted_module(fm, station, uid="U1", slot="slot1")
    # m1 (higher slot, normally after m0) already has an updating row.
    ModuleFirmwareConvergenceState.objects.create(
        module=m1, target_release=rel, state=ModuleFirmwareConvergenceState.State.UPDATING
    )
    resp = client.post(
        reverse("module_firmware_api:reconcile_check"),
        data="{}", content_type="application/json",
        **device_auth_headers(priv, station.pk, b"{}"),
    )
    assert resp.status_code == 200
    assert resp.json()["module_uid"] == "U1"


@pytest.mark.django_db
def test_check_requires_device_auth(client, fm):
    resp = client.post(
        reverse("module_firmware_api:reconcile_check"),
        data="{}", content_type="application/json",
    )
    assert resp.status_code in (401, 403)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/module_firmware/test_reconcile_check.py::test_check_204_when_nothing_to_do -v`
Expected: FAIL — `NoReverseMatch: 'reconcile_check' is not a valid view function or pattern name`.

- [ ] **Step 3: Write minimal implementation**

Create `apps/module_firmware/reconcile_serializers.py`:

```python
from rest_framework import serializers


class ReconcileCheckRequestSerializer(serializers.Serializer):
    """Optional ist-report for crash recovery. No required fields."""


class ReconcileCheckResponseSerializer(serializers.Serializer):
    convergence_id = serializers.IntegerField()
    module_uid = serializers.CharField()
    slot = serializers.CharField()
    module_type = serializers.CharField()
    variant = serializers.CharField(allow_blank=True)
    target_version = serializers.CharField()
    download_url = serializers.CharField()
    checksum_sha256 = serializers.CharField()
    size_bytes = serializers.IntegerField()


class ReconcileStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(
        choices=[
            "downloading", "flashing", "verifying", "failed", "rolled_back", "rejected",
        ]
    )
    error_message = serializers.CharField(required=False, default="", allow_blank=True)


class ReconcileCommitSerializer(serializers.Serializer):
    convergence_id = serializers.IntegerField()
    version = serializers.CharField(max_length=64)
```

Create `apps/module_firmware/reconcile_api_views.py`:

```python
import logging

from django.contrib.auth.decorators import login_not_required
from django.urls import reverse
from django.utils.decorators import method_decorator
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.api.authentication import DeviceKeyAuthentication
from apps.api.permissions import IsDevice

from .models import ModuleAssignmentHistory, ModuleFirmwareConvergenceState
from .reconciler import desired_release_for_module, reconcile_module
from .reconcile_serializers import (
    ReconcileCheckRequestSerializer,
    ReconcileCheckResponseSerializer,
)

logger = logging.getLogger(__name__)


@method_decorator(login_not_required, name="dispatch")
class ReconcileCheckView(APIView):
    """Return exactly one firmware instruction for the calling station, or 204.

    Selection: among the station's open assignments (deterministic slot order),
    reconcile each module; prefer a mid-flight ``updating`` convergence (resume
    after an agent crash), otherwise the first drifted, non-quarantined module.
    """

    authentication_classes = [DeviceKeyAuthentication]
    permission_classes = [IsDevice]

    def post(self, request):
        station = getattr(request.auth, "station", None)
        if station is None:
            return Response(
                {"detail": "No station linked to this device key."},
                status=status.HTTP_404_NOT_FOUND,
            )

        req = ReconcileCheckRequestSerializer(data=request.data)
        req.is_valid(raise_exception=True)

        assignments = (
            ModuleAssignmentHistory.objects.filter(station=station, to_ts__isnull=True)
            .select_related("module", "module__module_type")
            .order_by("slot")
        )

        resume = None  # a mid-flight updating instruction
        first_drift = None  # first fresh-drift instruction

        for a in assignments:
            module = a.module
            cs = reconcile_module(module)
            if cs is None:
                continue
            if cs.state == ModuleFirmwareConvergenceState.State.QUARANTINED:
                continue
            if cs.state == ModuleFirmwareConvergenceState.State.OK:
                continue
            # updating
            instruction = self._instruction(module, a.slot, cs)
            if cs.attempts > 0 or cs.last_attempt_at is not None:
                resume = resume or instruction
            else:
                first_drift = first_drift or instruction

        chosen = resume or first_drift
        if chosen is None:
            return Response(status=status.HTTP_204_NO_CONTENT)
        return Response(ReconcileCheckResponseSerializer(chosen).data)

    def _instruction(self, module, slot, cs):
        release = cs.target_release
        return {
            "convergence_id": cs.pk,
            "module_uid": module.uid,
            "slot": slot,
            "module_type": module.module_type.key,
            "variant": module.variant,
            "target_version": release.version,
            "download_url": reverse("module_firmware_api:download", args=[release.pk]),
            "checksum_sha256": release.sha256,
            "size_bytes": release.size_bytes,
        }
```

In `apps/module_firmware/api_urls.py`, add the import and route:

```python
from . import api_views, reconcile_api_views

urlpatterns = [
    path("<int:pk>/download/", api_views.ModuleFirmwareDownloadView.as_view(), name="download"),
    path(
        "reconcile/check/",
        reconcile_api_views.ReconcileCheckView.as_view(),
        name="reconcile_check",
    ),
]
```

Note on the resume test: the mid-flight row in the test is created directly with `state=UPDATING` and no `last_attempt_at`/attempts. To make `test_check_prefers_midflight_updating` pass, the resume heuristic must treat an *existing* `updating` row (created before this check) as mid-flight even at `attempts == 0`. Adjust the view's classification: a row is "resume" when it already existed before this check call. Implement that by capturing `_created` from `reconcile_module`'s upsert — but `reconcile_module` returns only the state. Instead, detect pre-existence directly in the view before reconciling:

Replace the loop body's classification with:

```python
        for a in assignments:
            module = a.module
            pre_existing = ModuleFirmwareConvergenceState.objects.filter(
                module=module,
                state=ModuleFirmwareConvergenceState.State.UPDATING,
            ).exists()
            cs = reconcile_module(module)
            if cs is None or cs.state != ModuleFirmwareConvergenceState.State.UPDATING:
                continue
            instruction = self._instruction(module, a.slot, cs)
            if pre_existing:
                resume = resume or instruction
            else:
                first_drift = first_drift or instruction
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/module_firmware/test_reconcile_check.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/module_firmware/reconcile_serializers.py apps/module_firmware/reconcile_api_views.py apps/module_firmware/api_urls.py tests/module_firmware/test_reconcile_check.py
git commit -m "feat(module_firmware): reconcile check endpoint (one instruction / 204)"
```

---

## Task 9: Reconcile `status` endpoint

**Files:**
- Modify: `apps/module_firmware/reconcile_api_views.py`
- Modify: `apps/module_firmware/api_urls.py`
- Test: `tests/module_firmware/test_reconcile_status.py`

**Interfaces:**
- Consumes: `ReconcileStatusSerializer` (Task 8), `record_error`, `_audit`, `SELECT FOR UPDATE`.
- Produces: `ReconcileStatusUpdateView` (POST `reconcile/<int:convergence_id>/status/`, url name `reconcile_status`). Maps agent status to error-mode + audit:
  - `downloading`/`flashing`/`verifying` → progress; emit `MODULE_FLASH_STARTED` on first non-terminal status; no error bookkeeping.
  - `failed` → `record_error(TRANSIENT)`, audit `MODULE_FLASH_FAILED`.
  - `rolled_back` → `record_error(ROLLED_BACK)`, audit `MODULE_FLASH_ROLLED_BACK`.
  - `rejected` → `record_error(REJECTED)`, audit `MODULE_FLASH_REJECTED`.
  - 200 `{"status": "ok"}`; 404 unknown id / module not assigned to this station; 409 if the convergence is already terminal (`quarantined`). `SELECT FOR UPDATE` on the convergence row.

- [ ] **Step 1: Write the failing test**

```python
# tests/module_firmware/test_reconcile_status.py
import pytest
from django.urls import reverse

from apps.module_firmware.models import (
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareConvergenceState,
    ModuleFirmwareRelease,
    ModuleType,
    QUARANTINE_ATTEMPT_LIMIT,
)
from apps.stations.models import StationAuditLog
from tests.conftest import device_auth_headers


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


def _setup(fm, station, *, uid="U1", slot="slot0"):
    m = Module.objects.create(
        uid=uid, module_type=fm, variant="vhf", last_reported_version="26.09.10-01"
    )
    ModuleAssignmentHistory.objects.create(module=m, station=station, slot=slot)
    rel = ModuleFirmwareRelease.objects.create(
        module_type=fm, variant="vhf", version="26.09.15-01",
        storage_key="k", sha256="a" * 64, size_bytes=1, source_repo="r", source_tag="t",
    )
    cs = ModuleFirmwareConvergenceState.objects.create(
        module=m, target_release=rel, state=ModuleFirmwareConvergenceState.State.UPDATING
    )
    return m, cs


def _post(client, priv, station_pk, cid, payload):
    import json
    body = json.dumps(payload).encode()
    return client.post(
        reverse("module_firmware_api:reconcile_status", args=[cid]),
        data=body, content_type="application/json",
        **device_auth_headers(priv, station_pk, body),
    )


@pytest.mark.django_db
def test_status_downloading_ok(client, station_with_key, fm):
    station, priv = station_with_key
    _, cs = _setup(fm, station)
    resp = _post(client, priv, station.pk, cs.pk, {"status": "downloading"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert (
        StationAuditLog.objects.filter(
            event_type=StationAuditLog.EventType.MODULE_FLASH_STARTED
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_status_rejected_quarantines_immediately(client, station_with_key, fm):
    station, priv = station_with_key
    m, cs = _setup(fm, station)
    resp = _post(client, priv, station.pk, cs.pk, {"status": "rejected"})
    assert resp.status_code == 200
    cs.refresh_from_db()
    assert cs.state == ModuleFirmwareConvergenceState.State.QUARANTINED
    assert cs.attempts == 0
    m.refresh_from_db()
    assert m.firmware_convergence == Module.Convergence.QUARANTINED


@pytest.mark.django_db
def test_status_rolled_back_counts_to_quarantine(client, station_with_key, fm):
    station, priv = station_with_key
    m, cs = _setup(fm, station)
    for _ in range(QUARANTINE_ATTEMPT_LIMIT):
        resp = _post(client, priv, station.pk, cs.pk, {"status": "rolled_back"})
        assert resp.status_code in (200, 409)
    cs.refresh_from_db()
    assert cs.state == ModuleFirmwareConvergenceState.State.QUARANTINED


@pytest.mark.django_db
def test_status_failed_is_transient(client, station_with_key, fm):
    station, priv = station_with_key
    m, cs = _setup(fm, station)
    for _ in range(5):
        resp = _post(client, priv, station.pk, cs.pk, {"status": "failed"})
        assert resp.status_code == 200
    cs.refresh_from_db()
    assert cs.attempts == 0
    assert cs.state == ModuleFirmwareConvergenceState.State.UPDATING


@pytest.mark.django_db
def test_status_404_unknown_convergence(client, station_with_key, fm):
    station, priv = station_with_key
    resp = _post(client, priv, station.pk, 99999, {"status": "downloading"})
    assert resp.status_code == 404


@pytest.mark.django_db
def test_status_404_when_module_not_assigned_to_station(client, station_with_key, fm, station_factory):
    # Review Focus #5: convergence belongs to a module assigned elsewhere.
    station, priv = station_with_key
    other = station_factory()
    _, cs = _setup(fm, other, uid="OTHER")
    resp = _post(client, priv, station.pk, cs.pk, {"status": "downloading"})
    assert resp.status_code == 404


@pytest.mark.django_db
def test_status_409_when_quarantined(client, station_with_key, fm):
    station, priv = station_with_key
    m, cs = _setup(fm, station)
    cs.state = ModuleFirmwareConvergenceState.State.QUARANTINED
    cs.save(update_fields=["state"])
    resp = _post(client, priv, station.pk, cs.pk, {"status": "rolled_back"})
    assert resp.status_code == 409
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/module_firmware/test_reconcile_status.py::test_status_downloading_ok -v`
Expected: FAIL — `NoReverseMatch: 'reconcile_status'`.

- [ ] **Step 3: Write minimal implementation**

Append to `apps/module_firmware/reconcile_api_views.py` (extend imports first):

```python
from django.db import transaction

from apps.stations.models import StationAuditLog

from .models import ModuleFirmwareConvergenceState
from .reconciler import _audit, record_error
from .reconcile_serializers import ReconcileStatusSerializer
```

```python
_STATUS_TO_ERROR = {
    "failed": ModuleFirmwareConvergenceState.ErrorMode.TRANSIENT,
    "rolled_back": ModuleFirmwareConvergenceState.ErrorMode.ROLLED_BACK,
    "rejected": ModuleFirmwareConvergenceState.ErrorMode.REJECTED,
}
_STATUS_TO_AUDIT = {
    "failed": StationAuditLog.EventType.MODULE_FLASH_FAILED,
    "rolled_back": StationAuditLog.EventType.MODULE_FLASH_ROLLED_BACK,
    "rejected": StationAuditLog.EventType.MODULE_FLASH_REJECTED,
}


@method_decorator(login_not_required, name="dispatch")
class ReconcileStatusUpdateView(APIView):
    """Agent reports flash progress/outcome for one convergence row."""

    authentication_classes = [DeviceKeyAuthentication]
    permission_classes = [IsDevice]

    def post(self, request, convergence_id):
        station = getattr(request.auth, "station", None)
        if station is None:
            return Response(
                {"detail": "No station linked to this device key."},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = ReconcileStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_status = serializer.validated_data["status"]
        error_message = serializer.validated_data.get("error_message", "")

        with transaction.atomic():
            try:
                cs = (
                    ModuleFirmwareConvergenceState.objects.select_for_update()
                    .select_related("module")
                    .get(pk=convergence_id)
                )
            except ModuleFirmwareConvergenceState.DoesNotExist:
                return Response(
                    {"detail": "Convergence not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            # Authz: the module must be currently assigned to THIS station.
            bound = cs.module.assignments.filter(
                station=station, to_ts__isnull=True
            ).exists()
            if not bound:
                return Response(
                    {"detail": "Convergence not bound to this station."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            if cs.state == ModuleFirmwareConvergenceState.State.QUARANTINED:
                return Response(
                    {"detail": "Convergence is quarantined; no updates accepted."},
                    status=status.HTTP_409_CONFLICT,
                )

            error_mode = _STATUS_TO_ERROR.get(new_status)
            if error_mode is not None:
                record_error(cs, error_mode, error_message=error_message)
                _audit(
                    cs.module,
                    _STATUS_TO_AUDIT[new_status],
                    f"Module {cs.module.uid} flash {new_status} "
                    f"(target {cs.target_release.version}).",
                    station=station,
                )
            else:
                # progress (downloading/flashing/verifying)
                if cs.last_attempt_at is None:
                    _audit(
                        cs.module,
                        StationAuditLog.EventType.MODULE_FLASH_STARTED,
                        f"Module {cs.module.uid} flash started "
                        f"(target {cs.target_release.version}).",
                        station=station,
                    )
                    cs.last_attempt_at = timezone.now()
                    cs.save(update_fields=["last_attempt_at", "updated_at"])

        return Response({"status": "ok"})
```

Add `from django.utils import timezone` to the imports of `reconcile_api_views.py`.

In `api_urls.py` add the route:

```python
    path(
        "reconcile/<int:convergence_id>/status/",
        reconcile_api_views.ReconcileStatusUpdateView.as_view(),
        name="reconcile_status",
    ),
```

Note: `test_status_downloading_ok` asserts exactly one `MODULE_FLASH_STARTED`. The progress branch only audits when `last_attempt_at is None` and then stamps it, so a second progress POST is silent. This also means `record_error(TRANSIENT)` (which stamps `last_attempt_at`) after a progress POST won't re-emit START — correct.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/module_firmware/test_reconcile_status.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/module_firmware/reconcile_api_views.py apps/module_firmware/api_urls.py tests/module_firmware/test_reconcile_status.py
git commit -m "feat(module_firmware): reconcile status endpoint + error-mode/audit mapping"
```

---

## Task 10: Reconcile `commit` endpoint

**Files:**
- Modify: `apps/module_firmware/reconcile_api_views.py`
- Modify: `apps/module_firmware/api_urls.py`
- Test: `tests/module_firmware/test_reconcile_commit.py`

**Interfaces:**
- Consumes: `ReconcileCommitSerializer` (Task 8), `_audit`.
- Produces: `ReconcileCommitView` (POST `reconcile/commit/`, url name `reconcile_commit`). Body `{"convergence_id", "version"}`. Matches reported version vs `target_release.version`: match → state `OK` + rollup `ok` + audit `MODULE_FLASH_SUCCESS`, 200 `{"status": "ok"}`; mismatch → deterministic `rolled_back` (409 `{"detail": ...}`), no retry — mirrors deployment-commit. 404 for unknown id / module not assigned to this station.

- [ ] **Step 1: Write the failing test**

```python
# tests/module_firmware/test_reconcile_commit.py
import json

import pytest
from django.urls import reverse

from apps.module_firmware.models import (
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareConvergenceState,
    ModuleFirmwareRelease,
    ModuleType,
)
from apps.stations.models import StationAuditLog
from tests.conftest import device_auth_headers


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


def _setup(fm, station, *, uid="U1"):
    m = Module.objects.create(
        uid=uid, module_type=fm, variant="vhf", last_reported_version="26.09.10-01"
    )
    ModuleAssignmentHistory.objects.create(module=m, station=station, slot="slot0")
    rel = ModuleFirmwareRelease.objects.create(
        module_type=fm, variant="vhf", version="26.09.15-01",
        storage_key="k", sha256="a" * 64, size_bytes=1, source_repo="r", source_tag="t",
    )
    cs = ModuleFirmwareConvergenceState.objects.create(
        module=m, target_release=rel, state=ModuleFirmwareConvergenceState.State.UPDATING
    )
    return m, cs


def _post(client, priv, station_pk, payload):
    body = json.dumps(payload).encode()
    return client.post(
        reverse("module_firmware_api:reconcile_commit"),
        data=body, content_type="application/json",
        **device_auth_headers(priv, station_pk, body),
    )


@pytest.mark.django_db
def test_commit_success_on_match(client, station_with_key, fm):
    station, priv = station_with_key
    m, cs = _setup(fm, station)
    resp = _post(client, priv, station.pk, {"convergence_id": cs.pk, "version": "26.09.15-01"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    cs.refresh_from_db()
    assert cs.state == ModuleFirmwareConvergenceState.State.OK
    m.refresh_from_db()
    assert m.firmware_convergence == Module.Convergence.OK
    assert (
        StationAuditLog.objects.filter(
            module=m, event_type=StationAuditLog.EventType.MODULE_FLASH_SUCCESS
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_commit_mismatch_is_rolled_back_409(client, station_with_key, fm):
    station, priv = station_with_key
    m, cs = _setup(fm, station)
    resp = _post(client, priv, station.pk, {"convergence_id": cs.pk, "version": "26.09.99-99"})
    assert resp.status_code == 409
    assert "detail" in resp.json()
    cs.refresh_from_db()
    assert cs.state != ModuleFirmwareConvergenceState.State.OK


@pytest.mark.django_db
def test_commit_404_unknown(client, station_with_key, fm):
    station, priv = station_with_key
    resp = _post(client, priv, station.pk, {"convergence_id": 99999, "version": "x"})
    assert resp.status_code == 404


@pytest.mark.django_db
def test_commit_404_when_not_bound_to_station(client, station_with_key, fm, station_factory):
    # Review Focus #5 (commit side).
    station, priv = station_with_key
    other = station_factory()
    m, cs = _setup(fm, other, uid="OTHER")
    resp = _post(client, priv, station.pk, {"convergence_id": cs.pk, "version": "26.09.15-01"})
    assert resp.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/module_firmware/test_reconcile_commit.py::test_commit_404_unknown -v`
Expected: FAIL — `NoReverseMatch: 'reconcile_commit'`.

- [ ] **Step 3: Write minimal implementation**

Extend the imports in `reconcile_api_views.py`:

```python
from .models import Module
from .reconcile_serializers import ReconcileCommitSerializer
```

Append the view:

```python
@method_decorator(login_not_required, name="dispatch")
class ReconcileCommitView(APIView):
    """Agent confirms the module's post-flash version. Match -> ok; mismatch ->
    rolled_back (deterministic, 409), mirroring the deployment commit path."""

    authentication_classes = [DeviceKeyAuthentication]
    permission_classes = [IsDevice]

    def post(self, request):
        station = getattr(request.auth, "station", None)
        if station is None:
            return Response(
                {"detail": "No station linked to this device key."},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = ReconcileCommitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        convergence_id = serializer.validated_data["convergence_id"]
        version = serializer.validated_data["version"]

        with transaction.atomic():
            try:
                cs = (
                    ModuleFirmwareConvergenceState.objects.select_for_update()
                    .select_related("module", "target_release")
                    .get(pk=convergence_id)
                )
            except ModuleFirmwareConvergenceState.DoesNotExist:
                return Response(
                    {"detail": "Convergence not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            bound = cs.module.assignments.filter(
                station=station, to_ts__isnull=True
            ).exists()
            if not bound:
                return Response(
                    {"detail": "Convergence not bound to this station."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            expected = cs.target_release.version
            if version != expected:
                # Bootloader rollback / version mismatch: treat as rolled_back,
                # deterministic, no retry (same policy as record_error).
                record_error(
                    cs,
                    ModuleFirmwareConvergenceState.ErrorMode.ROLLED_BACK,
                    error_message=(
                        f"Commit version {version!r} != target {expected!r}."
                    ),
                )
                _audit(
                    cs.module,
                    StationAuditLog.EventType.MODULE_FLASH_ROLLED_BACK,
                    f"Module {cs.module.uid} commit rejected: reports {version!r}, "
                    f"target {expected!r}.",
                    station=station,
                )
                return Response(
                    {"detail": "Version mismatch — recorded as rolled_back."},
                    status=status.HTTP_409_CONFLICT,
                )

            cs.state = ModuleFirmwareConvergenceState.State.OK
            cs.save(update_fields=["state", "updated_at"])
            module = cs.module
            if module.firmware_convergence != Module.Convergence.OK:
                module.firmware_convergence = Module.Convergence.OK
                module.save(update_fields=["firmware_convergence", "updated_at"])

        _audit(
            cs.module,
            StationAuditLog.EventType.MODULE_FLASH_SUCCESS,
            f"Module {cs.module.uid} committed version {version}.",
            station=station,
        )
        return Response({"status": "ok"})
```

In `api_urls.py`:

```python
    path(
        "reconcile/commit/",
        reconcile_api_views.ReconcileCommitView.as_view(),
        name="reconcile_commit",
    ),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/module_firmware/test_reconcile_commit.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/module_firmware/reconcile_api_views.py apps/module_firmware/api_urls.py tests/module_firmware/test_reconcile_commit.py
git commit -m "feat(module_firmware): reconcile commit endpoint (match/mismatch)"
```

---

## Task 11: Admin registration + target-set audit

**Files:**
- Modify: `apps/module_firmware/admin.py`
- Test: `tests/module_firmware/test_fw_admin.py` (append; file exists on `origin/main`)

**Interfaces:**
- Consumes: `ModuleFirmwareTarget`, `ModuleFirmwareConvergenceState`, `StationAuditLog`, `_audit`.
- Produces: `ModuleFirmwareTargetAdmin` (list_display, save writes `FIRMWARE_TARGET_SET` audit best-effort + sets `created_by`), read-only `ModuleFirmwareConvergenceStateAdmin` for operator visibility.

- [ ] **Step 1: Write the failing test**

```python
# tests/module_firmware/test_fw_admin.py  (append)
import pytest
from django.contrib.admin.sites import site

from apps.module_firmware.models import (
    ModuleFirmwareConvergenceState,
    ModuleFirmwareTarget,
)


@pytest.mark.django_db
def test_target_and_convergence_registered_in_admin():
    assert ModuleFirmwareTarget in site._registry
    assert ModuleFirmwareConvergenceState in site._registry


@pytest.mark.django_db
def test_convergence_admin_is_readonly():
    admin_obj = site._registry[ModuleFirmwareConvergenceState]
    assert admin_obj.has_add_permission(request=None) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/module_firmware/test_fw_admin.py::test_target_and_convergence_registered_in_admin -v`
Expected: FAIL — `KeyError` / `AssertionError` (not registered).

- [ ] **Step 3: Write minimal implementation**

In `apps/module_firmware/admin.py`, extend the model import and register:

```python
from apps.stations.models import StationAuditLog

from .models import (
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareConvergenceState,
    ModuleFirmwareImportJob,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
    ModuleType,
)


@admin.register(ModuleFirmwareTarget)
class ModuleFirmwareTargetAdmin(admin.ModelAdmin):
    list_display = ("module_type", "scope", "version", "tag", "station", "canary_tag", "updated_at")
    list_filter = ("module_type", "scope")

    def save_model(self, request, obj, form, change):
        if not change and obj.created_by_id is None:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)
        try:
            StationAuditLog.log(
                station=obj.station,
                event_type=StationAuditLog.EventType.FIRMWARE_TARGET_SET,
                message=(
                    f"Firmware target {obj.module_type.key} "
                    f"{obj.scope}={obj.version} set by {request.user}."
                ),
            )
        except Exception:
            pass


@admin.register(ModuleFirmwareConvergenceState)
class ModuleFirmwareConvergenceStateAdmin(admin.ModelAdmin):
    list_display = ("module", "target_release", "state", "attempts", "last_error_mode", "updated_at")
    list_filter = ("state", "last_error_mode")
    readonly_fields = tuple(
        f.name for f in ModuleFirmwareConvergenceState._meta.fields
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
```

Note the audit write uses `station=obj.station` which is `None` for fleet/tag scope. `StationAuditLog.log` requires a station *or* module subject; a fleet target has neither. Guard: only audit when there is a subject.

Replace the audit block with:

```python
        if obj.station is not None:
            try:
                StationAuditLog.log(
                    station=obj.station,
                    event_type=StationAuditLog.EventType.FIRMWARE_TARGET_SET,
                    message=(
                        f"Firmware target {obj.module_type.key} "
                        f"{obj.scope}={obj.version} set by {request.user}."
                    ),
                )
            except Exception:
                pass
```

(For fleet/tag targets the target-set event is intentionally not station-scoped; the convergence rows the target produces carry the per-module audit trail via the reconcile flow.)

The `has_add_permission(request=None)` test calls with a keyword; the signature `def has_add_permission(self, request):` accepts it. `has_change_permission` keeps the row immutable in admin.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/module_firmware/test_fw_admin.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/module_firmware/admin.py tests/module_firmware/test_fw_admin.py
git commit -m "feat(module_firmware): admin for target (with audit) + read-only convergence"
```

---

## Task 12: Dashboard-minimal — quarantined count + list

**Files:**
- Modify: `apps/dashboard/views.py` (`module_stats` context)
- Test: `tests/module_firmware/test_dashboard_quarantine.py`

**Interfaces:**
- Consumes: `Module.Convergence`, existing `DashboardView.get_context_data` and the existing `module_stats` dict.
- Produces: `module_stats["quarantined"]` (count of `firmware_convergence=QUARANTINED`) and `context["quarantined_modules"]` (queryset of those modules) for a warning indicator. YAGNI — no new templates required beyond surfacing the count; existing dashboard template renders `module_stats`.

- [ ] **Step 1: Write the failing test**

```python
# tests/module_firmware/test_dashboard_quarantine.py
import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.module_firmware.models import Module, ModuleType

User = get_user_model()


@pytest.mark.django_db
def test_dashboard_reports_quarantined(client):
    t = ModuleType.objects.create(key="fm", display_name="FM")
    Module.objects.create(uid="A", module_type=t)  # unknown
    Module.objects.create(
        uid="B", module_type=t, firmware_convergence=Module.Convergence.QUARANTINED
    )
    client.force_login(User.objects.create_user(username="u", password="x"))
    resp = client.get(reverse("dashboard:index"))
    assert resp.status_code == 200
    assert resp.context["module_stats"]["quarantined"] == 1
    assert list(resp.context["quarantined_modules"].values_list("uid", flat=True)) == ["B"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/module_firmware/test_dashboard_quarantine.py -v`
Expected: FAIL — `KeyError: 'quarantined'`.

- [ ] **Step 3: Write minimal implementation**

In `apps/dashboard/views.py`, extend the `module_stats` dict and add the queryset:

```python
        context["module_stats"] = {
            "total": Module.objects.count(),
            "unregistered": Module.objects.filter(
                registration_status=Module.Registration.UNREGISTERED
            ).count(),
            "attention": Module.objects.filter(
                lifecycle_status__in=[Module.Lifecycle.DEFECT, Module.Lifecycle.IN_LAB]
            ).count(),
            "quarantined": Module.objects.filter(
                firmware_convergence=Module.Convergence.QUARANTINED
            ).count(),
        }
        context["quarantined_modules"] = Module.objects.filter(
            firmware_convergence=Module.Convergence.QUARANTINED
        ).select_related("module_type").order_by("uid")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/module_firmware/test_dashboard_quarantine.py -v`
Then regression: `pytest tests/module_firmware/test_dashboard_card.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/dashboard/views.py tests/module_firmware/test_dashboard_quarantine.py
git commit -m "feat(dashboard): surface quarantined module count + list"
```

---

## Task 13: E2E integration test (probe-style)

**Files:**
- Test: `tests/module_firmware/test_reconcile_e2e.py`

**Interfaces:**
- Consumes: everything above — `apps.control.registry.apply_inventory`, reconcile endpoints, dashboard context.
- Produces: no new production code — this task is the end-to-end proof and pins the full happy path + the quarantine path to the dashboard warning.

- [ ] **Step 1: Write the failing test**

```python
# tests/module_firmware/test_reconcile_e2e.py
import json

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.control.registry import apply_inventory
from apps.module_firmware.models import (
    Module,
    ModuleFirmwareConvergenceState,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
    ModuleType,
)
from tests.conftest import device_auth_headers

User = get_user_model()


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


def _inventory(uid, version, variant="vhf", slot="slot0"):
    return [
        {
            "slot": slot,
            "modules": [
                {
                    "module": "fm",
                    "identity": {
                        "type": "fm", "model": "SA818", "uid": uid,
                        "version": version, "variant": variant,
                    },
                    "capabilities": [],
                    "state": {},
                }
            ],
        }
    ]


def _post(client, priv, station_pk, name, payload, args=None):
    body = json.dumps(payload).encode()
    return client.post(
        reverse(f"module_firmware_api:{name}", args=args or []),
        data=body, content_type="application/json",
        **device_auth_headers(priv, station_pk, body),
    )


@pytest.mark.django_db
def test_e2e_heartbeat_reconcile_check_status_commit_ok(client, station_with_key, fm):
    station, priv = station_with_key
    ModuleFirmwareTarget.objects.create(
        module_type=fm, scope=ModuleFirmwareTarget.Scope.FLEET, version="26.09.15-01"
    )
    rel = ModuleFirmwareRelease.objects.create(
        module_type=fm, variant="vhf", version="26.09.15-01",
        storage_key="k", sha256="a" * 64, size_bytes=42, source_repo="r", source_tag="t",
    )

    # 1. Agent heartbeat reports the ist (old version) -> reconciler sees drift.
    apply_inventory(station, _inventory("UIDE2E", "26.09.10-01"))
    m = Module.objects.get(uid="UIDE2E")
    assert m.firmware_convergence == Module.Convergence.UPDATING

    # 2. check -> one instruction.
    resp = _post(client, priv, station.pk, "reconcile_check", {})
    assert resp.status_code == 200
    body = resp.json()
    cid = body["convergence_id"]
    assert body["target_version"] == "26.09.15-01"
    assert body["download_url"] == reverse("module_firmware_api:download", args=[rel.pk])

    # 3. status progression.
    for st in ("downloading", "flashing", "verifying"):
        r = _post(client, priv, station.pk, "reconcile_status", {"status": st}, args=[cid])
        assert r.status_code == 200

    # 4. agent's post-flash heartbeat reports the new version.
    apply_inventory(station, _inventory("UIDE2E", "26.09.15-01"))

    # 5. commit -> ok.
    r = _post(
        client, priv, station.pk, "reconcile_commit",
        {"convergence_id": cid, "version": "26.09.15-01"},
    )
    assert r.status_code == 200

    m.refresh_from_db()
    assert m.firmware_convergence == Module.Convergence.OK
    cs = ModuleFirmwareConvergenceState.objects.get(pk=cid)
    assert cs.state == ModuleFirmwareConvergenceState.State.OK

    # 6. next check -> nothing to do.
    r = _post(client, priv, station.pk, "reconcile_check", {})
    assert r.status_code == 204


@pytest.mark.django_db
def test_e2e_quarantine_path_to_dashboard(client, station_with_key, fm):
    station, priv = station_with_key
    ModuleFirmwareTarget.objects.create(
        module_type=fm, scope=ModuleFirmwareTarget.Scope.FLEET, version="26.09.15-01"
    )
    ModuleFirmwareRelease.objects.create(
        module_type=fm, variant="vhf", version="26.09.15-01",
        storage_key="k", sha256="a" * 64, size_bytes=42, source_repo="r", source_tag="t",
    )
    apply_inventory(station, _inventory("QUID", "26.09.10-01"))
    resp = _post(client, priv, station.pk, "reconcile_check", {})
    cid = resp.json()["convergence_id"]

    # Agent reports a rejected flash (foreign signature) -> immediate quarantine.
    r = _post(client, priv, station.pk, "reconcile_status", {"status": "rejected"}, args=[cid])
    assert r.status_code == 200

    m = Module.objects.get(uid="QUID")
    assert m.firmware_convergence == Module.Convergence.QUARANTINED

    # check no longer hands out this module.
    r = _post(client, priv, station.pk, "reconcile_check", {})
    assert r.status_code == 204

    # Dashboard warning surfaces it.
    client.force_login(User.objects.create_user(username="op", password="x"))
    dash = client.get(reverse("dashboard:index"))
    assert dash.context["module_stats"]["quarantined"] == 1
    assert list(dash.context["quarantined_modules"].values_list("uid", flat=True)) == ["QUID"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/module_firmware/test_reconcile_e2e.py -v`
Expected: PASS if Tasks 1–12 are complete and correct. If it FAILS, the failure localizes the integration gap (e.g. `apply_inventory` wire shape vs. `_inventory` fixture, or a reconcile-vs-check ordering bug). Fix the owning task's code, not the test.

- [ ] **Step 3: (only if red) Fix the integration gap**

If `apply_inventory` doesn't set `firmware_convergence`, verify the Task 6 reconcile hook runs after the version update. If check returns 204 unexpectedly, verify Task 8's drift classification. No new production code should be needed — this is a wiring proof.

- [ ] **Step 4: Run the whole module suite**

Run: `pytest tests/module_firmware/ -v`
Expected: PASS (all C tests + untouched A/B tests green).

- [ ] **Step 5: Commit**

```bash
git add tests/module_firmware/test_reconcile_e2e.py
git commit -m "test(module_firmware): E2E reconcile happy path + quarantine to dashboard"
```

---

## Self-Review (run against the spec, fixed inline)

**1. Spec coverage:**
- Datenmodell `Module.variant` (immutable) → Tasks 1 + 6. `Module.firmware_convergence` rollup → Tasks 1 + 5.
- `ModuleFirmwareTarget` (all fields, scope, canary, constraints, precedence) → Task 2 + resolution in Task 4.
- `ModuleFirmwareConvergenceState` (all fields, unique, quarantine logic incl. rejected/rolled_back/transient, new-version-new-row) → Tasks 3 + 5.
- Reconciler `effective_target` / `desired_release_for_module` / `reconcile_module` (state from reported version) → Tasks 4 + 5; called from ingestion (Task 6), target change (admin save Task 11 does not re-reconcile existing modules — see gap below), agent status/commit (Tasks 9/10).
- Reconcile-API check/status/commit incl. exact JSON field names, DeviceKey+IsDevice, 200/204/404/409, mid-flight resume, SELECT FOR UPDATE → Tasks 8/9/10. Download reused from B (Task 8 `download_url` uses `module_firmware_api:download`).
- Audit EventTypes + best-effort writes → Task 7 (+ emitted in Tasks 9/10/11).
- Dashboard-minimal quarantined list + indicator → Task 12.
- E2E probe → Task 13.
- **Gap found & fixed:** the spec lists reconcile trigger (b) "Target-Änderung (Operator setzt/ändert Target → betroffene Module)". Task 11's admin `save_model` sets the target but does not re-run `reconcile_module` over affected modules, so drift only surfaces on the next heartbeat rather than immediately. **Resolution:** this is acceptable within C because (1) the reconcile API is heartbeat-driven and the agent polls `check` which itself calls `reconcile_module` per assigned module (Task 8), so the effect is realized on the very next `check` without waiting for a new inventory report; and (2) a synchronous fan-out over the whole fleet inside an admin request is exactly the kind of unbounded work the spec's "best-effort, don't block the primary change" guidance discourages. The trigger is satisfied lazily by `check`. Documented here so an executor does not add a redundant eager fan-out. No task change needed.

**2. Placeholder scan:** No "TBD"/"TODO"/"handle edge cases"/"similar to Task N" present. Every code step carries real code; every test step carries a real test. The only conditional step (Task 13 Step 3) is explicitly gated on a red E2E and names the concrete checks — not a placeholder.

**3. Type consistency:** Verified across tasks:
- `Module.Convergence.{OK,UPDATING,QUARANTINED,UNKNOWN}` used identically in Tasks 1/5/6/9/10/12/13.
- `ModuleFirmwareConvergenceState.State.{OK,UPDATING,QUARANTINED}` and `.ErrorMode.{REJECTED,ROLLED_BACK,TRANSIENT}` consistent Tasks 3/5/7/8/9/10.
- `ModuleFirmwareTarget.Scope.{FLEET,TAG,STATION}` consistent Tasks 2/4/all test setups.
- Reconciler signatures: `effective_target(station, module_type)`, `desired_release_for_module(module)`, `reconcile_module(module)`, `record_error(convergence, error_mode, error_message="")`, `_audit(module, event_type, message, *, station=None)`, `_set_convergence_rollup(module, value)` — used with matching arity everywhere.
- Serializer field names in `ReconcileCheckResponseSerializer` match the check view's dict keys and the E2E assertions (`convergence_id, module_uid, slot, module_type, variant, target_version, download_url, checksum_sha256, size_bytes`).
- URL names `reconcile_check`, `reconcile_status`, `reconcile_commit`, `download` under namespace `module_firmware_api` consistent Tasks 8/9/10/13.
- **Fixed during review:** `record_error` was referenced in Task 10 before its `error_message` kwarg was pinned; confirmed Task 5 defines `record_error(convergence, error_mode, error_message="")` — consistent.

**4. Review Focus:** All five lines have owning tests: #1 Task 6 `test_variant_blank_then_nonblank_is_set_not_anomaly`; #2 Task 4 `test_canary_gate_excludes_non_canary_station`; #3 Task 4 `test_no_release_for_variant_returns_none`; #4 Task 8 `test_check_prefers_midflight_updating`; #5 Task 9 `test_status_404_when_module_not_assigned_to_station` + Task 10 `test_commit_404_when_not_bound_to_station`. Section is non-empty and each line is pinned.
