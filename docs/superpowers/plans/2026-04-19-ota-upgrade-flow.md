# OS-Image OTA Upgrade Flow — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Dispatch each task to the subagent_type listed under "Agent". Two-stage review after each task: `atlas` (spec compliance) → `audit` (code quality). On UI-heavy tasks (Phase 3), invoke `frontend-design:frontend-design` inside the task to shape the look before coding. Final PR review: `pr-review-toolkit:review-pr` + `security-review`.

**Goal:** Ship the end-to-end OS-image OTA flow described in `docs/superpowers/specs/2026-04-19-ota-upgrade-flow-design.md`: admin triggers per-group or per-station upgrade from the web UI → stations download via authenticated proxy from S3 → A/B slot install + bootloader-based auto-rollback → live status in the dashboard.

**Architecture:** Reuse the existing `apps/deployments` pipeline and `station_agent/ota.py` scaffolding. Swap the `firmware_artifact` FK for `image_release` (new images app, existing). Add a new `apps/rollouts` app for the singleton tag-ordered rollout sequence. Build an Upgrade Dashboard backed by the existing `deployment_status` WebSocket channel. Implement the agent-side `install_to_slot` stub using Python's `bz2.BZ2Decompressor` streamed into the inactive rootfs partition's block device.

**Tech Stack:** Django 6.0 + DRF 3.17 + Channels (existing), Bootstrap 5.3 + HTMX + SortableJS (new for drag-reorder), `boto3` Range-Get (via `default_storage.open(... 'rb')` + seek/read), `bz2` stdlib (streaming decompress), existing Ed25519 DeviceKey auth.

**Spec:** `docs/superpowers/specs/2026-04-19-ota-upgrade-flow-design.md`

**Branch:** `feat/ota-upgrade-flow-spec` (spec already committed; plan + code land on this same branch).

---

## Progress

- [ ] Phase 1 — Data model (Tasks 1-4)
- [ ] Phase 2 — Server-side upgrade logic (Tasks 5-9)
- [ ] Phase 3 — UI (Tasks 10-14)
- [ ] Phase 4 — Agent (Tasks 15-17)
- [ ] Phase 5 — Plumbing + E2E (Tasks 18-19)

---

## File structure

### New files

```
apps/rollouts/
    __init__.py
    apps.py
    models.py                                    # RolloutSequence, RolloutSequenceEntry
    admin.py
    forms.py                                     # RolloutSequenceEntryForm
    views.py                                     # list, add, remove, reorder, upgrade-group, upgrade-station
    urls.py
    grouping.py                                  # station-to-group assignment logic
    migrations/__init__.py
    migrations/0001_initial.py
    migrations/0002_seed_singleton.py            # data migration
    templates/rollouts/upgrade_dashboard.html
    templates/rollouts/sequence_edit.html
    templates/rollouts/_dashboard_row.html       # HTMX partial for live row updates
    templates/rollouts/_station_upgrade_card.html  # include on station detail

tests/test_rollouts.py
```

### Modified files

```
apps/deployments/models.py                       # image_release FK replaces firmware_artifact; SUPERSEDED status
apps/deployments/migrations/0002_*.py            # (name depends on current migration state)
apps/deployments/serializers.py                  # new response shape (image_release)
apps/deployments/api_views.py                    # check + download refactor, Range support
apps/deployments/consumers.py                    # broadcast tag (group) in the payload for dashboard filtering
apps/deployments/forms.py                        # firmware_artifact → image_release
apps/deployments/admin.py                        # swap FK

apps/stations/templates/stations/station_detail.html   # include _station_upgrade_card
apps/stations/views.py                           # context: target image release + recent deployments

templates/includes/sidebar.html                  # new Deployments sub-entries for admins

station_agent/ota.py                             # install_to_slot, opaque download_url, resume download
station_agent/inventory.py                       # report current_version from /etc/os-release
station_agent/agent.py                           # wire up new ota flow (only if necessary)

config/settings/base.py                          # register apps.rollouts
config/urls.py                                   # include apps.rollouts.urls
tests/test_deployments.py                        # updated for image_release + SUPERSEDED

# linux-image repo (separate commit, separate PR):
meta-oe5xrx-remotestation/recipes-core/images/oe5xrx-remotestation-image.bb  # IMAGE_INSTALL += "bzip2"
```

---

## Conventions

- Tests live in `/tests/test_<topic>.py`; conftest fixtures in `tests/conftest.py` provide `admin_user`, `operator_user`, `member_user`, `station`, `station_with_key`, `image_release`, etc.
- Every task ends with `pytest tests/test_<topic>.py -v` passing and a git commit.
- Admin-only views use `AdminRequiredMixin` from `apps.accounts.views`.
- `.venv/bin/pytest` / `.venv/bin/ruff` directly — never `source .venv/bin/activate`.
- `git -C /home/pbuchegger/station-manager ...` or relative paths — never `cd path && ...`.
- i18n: wrap every user-facing string with `gettext_lazy as _` / `{% trans %}`.
- No `# noqa`, no `# type: ignore`, no `TODO`, no placeholder text.
- Audit log on every state-changing admin action: extend `StationAuditLog.EventType` as needed; best-effort try/except around audit-log calls so transient DB failures never mask the real operation's success.
- Queryset access across related fields uses `select_related` / `prefetch_related` to avoid N+1.

---

## Phase 1 — Data model

### Task 1: Scaffold `apps/rollouts` + `RolloutSequence`/`RolloutSequenceEntry`

**Agent:** `gateway`

**Files:**
- Create: `apps/rollouts/{__init__.py,apps.py,models.py,admin.py,urls.py,views.py}`
- Create: `apps/rollouts/migrations/__init__.py`
- Create: `tests/test_rollouts.py`
- Modify: `config/settings/base.py` (add `"apps.rollouts"` to `INSTALLED_APPS`)
- Modify: `config/urls.py` (include `rollouts` urls at `rollouts/`)

- [ ] **Step 1.1: Write failing model tests**

```python
# tests/test_rollouts.py
import pytest
from django.db import IntegrityError

from apps.rollouts.models import RolloutSequence, RolloutSequenceEntry
from apps.stations.models import StationTag


@pytest.mark.django_db
class TestRolloutSequence:
    def test_entries_are_ordered_by_position(self):
        seq = RolloutSequence.objects.create()
        tag_a = StationTag.objects.create(name="alpha")
        tag_b = StationTag.objects.create(name="beta")
        tag_c = StationTag.objects.create(name="gamma")

        RolloutSequenceEntry.objects.create(sequence=seq, tag=tag_b, position=1)
        RolloutSequenceEntry.objects.create(sequence=seq, tag=tag_a, position=0)
        RolloutSequenceEntry.objects.create(sequence=seq, tag=tag_c, position=2)

        ordered = list(seq.entries.values_list("tag__name", flat=True))
        assert ordered == ["alpha", "beta", "gamma"]

    def test_tag_unique_per_sequence(self):
        seq = RolloutSequence.objects.create()
        tag = StationTag.objects.create(name="t1")

        RolloutSequenceEntry.objects.create(sequence=seq, tag=tag, position=0)
        with pytest.raises(IntegrityError):
            RolloutSequenceEntry.objects.create(sequence=seq, tag=tag, position=1)

    def test_position_unique_per_sequence(self):
        seq = RolloutSequence.objects.create()
        t1 = StationTag.objects.create(name="t1")
        t2 = StationTag.objects.create(name="t2")

        RolloutSequenceEntry.objects.create(sequence=seq, tag=t1, position=0)
        with pytest.raises(IntegrityError):
            RolloutSequenceEntry.objects.create(sequence=seq, tag=t2, position=0)
```

- [ ] **Step 1.2: Run — expect ImportError**

```
.venv/bin/pytest tests/test_rollouts.py -v
```

Expected: collection error.

- [ ] **Step 1.3: Create `apps/rollouts/apps.py`**

```python
from django.apps import AppConfig


class RolloutsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.rollouts"
    verbose_name = "Rollouts"
```

Create empty `apps/rollouts/__init__.py`.

- [ ] **Step 1.4: Write models**

```python
# apps/rollouts/models.py
from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class RolloutSequence(models.Model):
    """Singleton-in-practice: system-wide ordered tag list for manual phased
    rollouts. Created once via data migration, edited via the Rollout
    Sequence page.
    """

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_rollout_sequences",
    )

    class Meta:
        verbose_name = _("rollout sequence")
        verbose_name_plural = _("rollout sequences")

    def __str__(self):
        return f"RolloutSequence #{self.pk}"


class RolloutSequenceEntry(models.Model):
    """One tag at one position inside a RolloutSequence."""

    sequence = models.ForeignKey(
        RolloutSequence,
        on_delete=models.CASCADE,
        related_name="entries",
    )
    tag = models.ForeignKey(
        "stations.StationTag",
        on_delete=models.CASCADE,
        related_name="rollout_entries",
    )
    position = models.PositiveSmallIntegerField(_("position"))

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(
                fields=["sequence", "tag"], name="uniq_tag_per_sequence"
            ),
            models.UniqueConstraint(
                fields=["sequence", "position"], name="uniq_position_per_sequence"
            ),
        ]

    def __str__(self):
        return f"{self.position}: {self.tag}"
```

- [ ] **Step 1.5: Stub urls + views + admin**

```python
# apps/rollouts/urls.py
app_name = "rollouts"
urlpatterns = []
```

```python
# apps/rollouts/views.py
# (empty — views land in Tasks 5, 6, 10, 12)
```

```python
# apps/rollouts/admin.py
from django.contrib import admin

from .models import RolloutSequence, RolloutSequenceEntry


class RolloutSequenceEntryInline(admin.TabularInline):
    model = RolloutSequenceEntry
    extra = 0
    ordering = ("position",)


@admin.register(RolloutSequence)
class RolloutSequenceAdmin(admin.ModelAdmin):
    inlines = [RolloutSequenceEntryInline]
    readonly_fields = ("created_at", "updated_at", "updated_by")
```

- [ ] **Step 1.6: Register app + include urls**

In `config/settings/base.py`, append `"apps.rollouts",` to `INSTALLED_APPS` next to the other `apps.*` entries.

In `config/urls.py`, add inside the main `urlpatterns` (within `i18n_patterns`):

```python
path("rollouts/", include("apps.rollouts.urls")),
```

- [ ] **Step 1.7: Generate + run migration**

```
.venv/bin/python manage.py makemigrations rollouts
.venv/bin/pytest tests/test_rollouts.py -v
```

Expected: all 3 tests PASS.

- [ ] **Step 1.8: Lint clean**

```
.venv/bin/ruff check apps/rollouts tests/test_rollouts.py
.venv/bin/ruff format --check apps/rollouts tests/test_rollouts.py
```

Both exit 0.

- [ ] **Step 1.9: Commit**

```bash
git -C /home/pbuchegger/station-manager add apps/rollouts tests/test_rollouts.py config/settings/base.py config/urls.py
git -C /home/pbuchegger/station-manager commit -m "rollouts: scaffold app + RolloutSequence/Entry models"
```

---

### Task 2: Seed the singleton RolloutSequence via data migration

**Agent:** `gateway`

**Files:**
- Create: `apps/rollouts/migrations/0002_seed_singleton.py`
- Modify: `tests/test_rollouts.py`

- [ ] **Step 2.1: Write failing test**

Append to `tests/test_rollouts.py`:

```python
@pytest.mark.django_db(transaction=True)
class TestSingletonSeed:
    def test_exactly_one_sequence_exists_after_migrations(self):
        # django_db rolls back — check via a fresh count.
        assert RolloutSequence.objects.count() == 1

    def test_current_sequence_helper_returns_the_singleton(self):
        from apps.rollouts.models import current_sequence

        seq1 = current_sequence()
        seq2 = current_sequence()
        assert seq1 == seq2
        assert RolloutSequence.objects.count() == 1
```

- [ ] **Step 2.2: Run — fails**

```
.venv/bin/pytest tests/test_rollouts.py::TestSingletonSeed -v
```

Expected: fails (`current_sequence` not defined; count != 1).

- [ ] **Step 2.3: Add `current_sequence` helper**

Append to `apps/rollouts/models.py`:

```python
def current_sequence() -> RolloutSequence:
    """Return the singleton RolloutSequence, creating it on first access."""
    seq, _ = RolloutSequence.objects.get_or_create(pk=1)
    return seq
```

- [ ] **Step 2.4: Write the data migration**

```python
# apps/rollouts/migrations/0002_seed_singleton.py
from django.db import migrations


def seed_singleton(apps, schema_editor):
    RolloutSequence = apps.get_model("rollouts", "RolloutSequence")
    RolloutSequence.objects.get_or_create(pk=1)


def unseed_singleton(apps, schema_editor):
    RolloutSequence = apps.get_model("rollouts", "RolloutSequence")
    RolloutSequence.objects.filter(pk=1).delete()


class Migration(migrations.Migration):
    dependencies = [("rollouts", "0001_initial")]
    operations = [migrations.RunPython(seed_singleton, unseed_singleton)]
```

- [ ] **Step 2.5: Run — passes**

```
.venv/bin/pytest tests/test_rollouts.py -v
```

All PASS.

- [ ] **Step 2.6: Commit**

```bash
git -C /home/pbuchegger/station-manager add apps/rollouts tests/test_rollouts.py
git -C /home/pbuchegger/station-manager commit -m "rollouts: seed singleton sequence + current_sequence() helper"
```

---

### Task 3: Replace `Deployment.firmware_artifact` with `image_release` + SUPERSEDED status

**Agent:** `gateway`

**Files:**
- Modify: `apps/deployments/models.py`
- Create: `apps/deployments/migrations/0003_swap_to_image_release.py` (exact number depends on current state — check `ls apps/deployments/migrations/` first)
- Modify: `apps/deployments/admin.py`, `apps/deployments/forms.py`
- Modify: `tests/test_deployments.py`

- [ ] **Step 3.1: Write failing tests**

Add to `tests/test_deployments.py`:

```python
@pytest.mark.django_db
class TestDeploymentImageReleaseFK:
    def test_deployment_uses_image_release(self, image_release, station, admin_user):
        from apps.deployments.models import Deployment

        dep = Deployment.objects.create(
            image_release=image_release,
            target_type=Deployment.TargetType.STATION,
            target_station=station,
            created_by=admin_user,
        )
        assert dep.image_release == image_release
        assert not hasattr(dep, "firmware_artifact")

    def test_superseded_status_exists(self):
        from apps.deployments.models import DeploymentResult

        assert DeploymentResult.Status.SUPERSEDED == "superseded"
        assert "superseded" in dict(DeploymentResult.Status.choices)

    def test_image_release_protect_on_delete(self, image_release, station, admin_user):
        from django.db.models.deletion import ProtectedError

        from apps.deployments.models import Deployment

        Deployment.objects.create(
            image_release=image_release,
            target_type=Deployment.TargetType.STATION,
            target_station=station,
            created_by=admin_user,
        )
        with pytest.raises(ProtectedError):
            image_release.delete()
```

The existing `deployment` fixture in `tests/conftest.py` uses `firmware_artifact=...` — you must update it now so other tests don't regress. Replace the `firmware_artifact=firmware_artifact` arg with `image_release=image_release` and add an `image_release` fixture if not present (copy from `tests/test_provisioning.py`).

- [ ] **Step 3.2: Run — fails**

```
.venv/bin/pytest tests/test_deployments.py -v
```

Expected: import/attribute errors.

- [ ] **Step 3.3: Model changes**

In `apps/deployments/models.py`:

1. Drop the `from apps.firmware.models import FirmwareArtifact` import.
2. Replace the `firmware_artifact` FK:

```python
image_release = models.ForeignKey(
    "images.ImageRelease",
    verbose_name=_("image release"),
    on_delete=models.PROTECT,
    related_name="deployments",
)
```

3. Update `__str__`:

```python
def __str__(self):
    return f"Deployment #{self.pk} - {self.image_release} ({self.get_status_display()})"
```

4. Add `SUPERSEDED` to `DeploymentResult.Status`:

```python
class Status(models.TextChoices):
    PENDING = "pending", _("Pending")
    DOWNLOADING = "downloading", _("Downloading")
    INSTALLING = "installing", _("Installing")
    REBOOTING = "rebooting", _("Rebooting")
    VERIFYING = "verifying", _("Verifying")
    SUCCESS = "success", _("Success")
    FAILED = "failed", _("Failed")
    ROLLED_BACK = "rolled_back", _("Rolled Back")
    CANCELLED = "cancelled", _("Cancelled")
    SUPERSEDED = "superseded", _("Superseded")
```

- [ ] **Step 3.4: Update admin + form**

In `apps/deployments/admin.py`: wherever `firmware_artifact` is referenced in `list_display` / `readonly_fields` etc., replace with `image_release`.

In `apps/deployments/forms.py`:

```python
class DeploymentForm(forms.ModelForm):
    class Meta:
        model = Deployment
        fields = [
            "image_release",
            "target_type",
            "target_tag",
            "target_station",
            "strategy",
            "phase_config",
        ]
        widgets = {
            "image_release": forms.Select(attrs={"class": "form-select"}),
            # rest unchanged
            "target_type": forms.Select(attrs={"class": "form-select"}),
            "target_tag": forms.Select(attrs={"class": "form-select"}),
            "target_station": forms.Select(attrs={"class": "form-select"}),
            "strategy": forms.Select(attrs={"class": "form-select"}),
            "phase_config": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 3,
                    "placeholder": _('e.g. {"batch_size": 2, "delay_seconds": 3600}'),
                }
            ),
        }
    # clean() stays unchanged
```

- [ ] **Step 3.5: Migration**

```
.venv/bin/python manage.py makemigrations deployments
```

This will produce something like `0003_remove_deployment_firmware_artifact_and_more.py`. If the auto-generated output renames the FK (instead of drop+add), that's fine — field name changed from `firmware_artifact` to `image_release` with a different target model. If the auto output is confusing, hand-edit to be explicit:

```python
# Pseudocode of the operations expected:
operations = [
    migrations.RemoveField(model_name="deployment", name="firmware_artifact"),
    migrations.AddField(
        model_name="deployment",
        name="image_release",
        field=models.ForeignKey(
            on_delete=django.db.models.deletion.PROTECT,
            related_name="deployments",
            to="images.imagerelease",
            verbose_name="image release",
        ),
    ),
    migrations.AlterField(
        model_name="deploymentresult",
        name="status",
        field=models.CharField(
            choices=[
                ("pending", "Pending"),
                ("downloading", "Downloading"),
                ("installing", "Installing"),
                ("rebooting", "Rebooting"),
                ("verifying", "Verifying"),
                ("success", "Success"),
                ("failed", "Failed"),
                ("rolled_back", "Rolled Back"),
                ("cancelled", "Cancelled"),
                ("superseded", "Superseded"),
            ],
            default="pending",
            max_length=16,
        ),
    ),
]
```

Existing Deployment rows reference a `firmware_artifact` that's about to be dropped. In practice this project has no production Deployments yet (never exercised end-to-end), so dropping the column is safe. If you find any rows during development, `Deployment.objects.all().delete()` them before the migration runs.

- [ ] **Step 3.6: Fix the conftest fixture**

In `tests/conftest.py`:

```python
@pytest.fixture
def deployment(image_release, station, operator_user):
    """An in-progress Deployment targeting a single station."""
    dep = Deployment.objects.create(
        image_release=image_release,
        target_type=Deployment.TargetType.STATION,
        target_station=station,
        status=Deployment.Status.IN_PROGRESS,
        created_by=operator_user,
    )
    return dep
```

Add an `image_release` fixture here too (move it out of test_provisioning.py so both test files share it):

```python
@pytest.fixture
def image_release(db):
    from apps.images.models import ImageRelease

    return ImageRelease.objects.create(
        tag="v1-alpha",
        machine="qemux86-64",
        s3_key="images/v1-alpha/qemux86-64.wic.bz2",
        sha256="a" * 64,
        size_bytes=1000,
        is_latest=True,
    )
```

Delete the duplicate in `tests/test_provisioning.py`.

- [ ] **Step 3.7: Run tests**

```
.venv/bin/pytest tests/ -q
```

Expected: all pass. If `tests/test_deployments.py` fails because the existing tests reference `firmware_artifact`, fix each reference — the test assertions about what fields exist were testing the previous shape. Switch to `image_release`.

- [ ] **Step 3.8: Lint**

```
.venv/bin/ruff check apps/deployments tests
.venv/bin/ruff format --check apps/deployments tests
```

- [ ] **Step 3.9: Commit**

```bash
git -C /home/pbuchegger/station-manager add apps/deployments tests
git -C /home/pbuchegger/station-manager commit -m "deployments: swap firmware_artifact FK for image_release + SUPERSEDED"
```

---

### Task 4: Supersession helper + transaction

**Agent:** `gateway`

**Files:**
- Create: `apps/deployments/supersession.py`
- Modify: `tests/test_deployments.py`

- [ ] **Step 4.1: Test**

Append to `tests/test_deployments.py`:

```python
@pytest.mark.django_db
class TestSupersession:
    def _second_release(self):
        from apps.images.models import ImageRelease

        # Unset the fixture's is_latest so we don't hit the partial-unique index.
        ImageRelease.objects.filter(is_latest=True, machine="qemux86-64").update(
            is_latest=False
        )
        return ImageRelease.objects.create(
            tag="v1-beta",
            machine="qemux86-64",
            s3_key="images/v1-beta/qemux86-64.wic.bz2",
            sha256="b" * 64,
            size_bytes=1000,
            is_latest=True,
        )

    def test_pending_result_gets_superseded(self, image_release, station, admin_user):
        from apps.deployments.models import Deployment, DeploymentResult
        from apps.deployments.supersession import supersede_pending_for_station

        dep1 = Deployment.objects.create(
            image_release=image_release,
            target_type=Deployment.TargetType.STATION,
            target_station=station,
            created_by=admin_user,
        )
        r1 = DeploymentResult.objects.create(deployment=dep1, station=station)

        newer = self._second_release()
        dep2 = Deployment.objects.create(
            image_release=newer,
            target_type=Deployment.TargetType.STATION,
            target_station=station,
            created_by=admin_user,
        )
        superseded = supersede_pending_for_station(station=station, new_deployment=dep2)
        assert superseded == [r1.pk]

        r1.refresh_from_db()
        assert r1.status == DeploymentResult.Status.SUPERSEDED

    def test_active_result_blocks_new_deployment(
        self, image_release, station, admin_user
    ):
        from apps.deployments.models import Deployment, DeploymentResult
        from apps.deployments.supersession import (
            ActiveDeploymentConflict,
            supersede_pending_for_station,
        )

        dep1 = Deployment.objects.create(
            image_release=image_release,
            target_type=Deployment.TargetType.STATION,
            target_station=station,
            created_by=admin_user,
        )
        DeploymentResult.objects.create(
            deployment=dep1,
            station=station,
            status=DeploymentResult.Status.INSTALLING,
        )

        newer = self._second_release()
        dep2 = Deployment.objects.create(
            image_release=newer,
            target_type=Deployment.TargetType.STATION,
            target_station=station,
            created_by=admin_user,
        )

        with pytest.raises(ActiveDeploymentConflict):
            supersede_pending_for_station(station=station, new_deployment=dep2)
```

- [ ] **Step 4.2: Run — fails**

Expected: ImportError.

- [ ] **Step 4.3: Write the helper**

```python
# apps/deployments/supersession.py
from __future__ import annotations

from django.db import transaction

from apps.deployments.models import Deployment, DeploymentResult


class ActiveDeploymentConflict(Exception):
    """Raised when a station has a deployment beyond PENDING, which cannot be superseded."""


def supersede_pending_for_station(
    *,
    station,
    new_deployment: Deployment,
) -> list[int]:
    """Mark any PENDING DeploymentResult for `station` (other than for
    `new_deployment`) as SUPERSEDED. Raise ActiveDeploymentConflict if a
    non-PENDING, non-terminal result exists.

    Runs in a transaction with SELECT FOR UPDATE so concurrent calls don't race.
    """
    with transaction.atomic():
        active_statuses = {
            DeploymentResult.Status.DOWNLOADING,
            DeploymentResult.Status.INSTALLING,
            DeploymentResult.Status.REBOOTING,
            DeploymentResult.Status.VERIFYING,
        }

        qs = (
            DeploymentResult.objects.select_for_update()
            .filter(station=station)
            .exclude(deployment=new_deployment)
        )

        to_supersede = []
        for r in qs:
            if r.status == DeploymentResult.Status.PENDING:
                to_supersede.append(r.pk)
            elif r.status in active_statuses:
                raise ActiveDeploymentConflict(
                    f"Station {station.pk} is mid-deployment "
                    f"({r.get_status_display()} on deployment #{r.deployment_id})"
                )

        if to_supersede:
            DeploymentResult.objects.filter(pk__in=to_supersede).update(
                status=DeploymentResult.Status.SUPERSEDED,
            )
    return to_supersede
```

- [ ] **Step 4.4: Run — passes**

```
.venv/bin/pytest tests/test_deployments.py::TestSupersession -v
```

- [ ] **Step 4.5: Commit**

```bash
git -C /home/pbuchegger/station-manager add apps/deployments tests/test_deployments.py
git -C /home/pbuchegger/station-manager commit -m "deployments: supersession helper + active-conflict check"
```

---

## Phase 2 — Server-side upgrade logic

### Task 5: Station-to-group assignment (rollouts.grouping)

**Agent:** `gateway`

**Files:**
- Create: `apps/rollouts/grouping.py`
- Modify: `tests/test_rollouts.py`

- [ ] **Step 5.1: Tests**

```python
# append to tests/test_rollouts.py
@pytest.mark.django_db
class TestGrouping:
    def _station_with_tags(self, *tag_names):
        from apps.stations.models import Station, StationTag

        station = Station.objects.create(name=f"S-{tag_names[0] if tag_names else 'none'}")
        for n in tag_names:
            tag, _ = StationTag.objects.get_or_create(name=n)
            station.tags.add(tag)
        return station

    def test_first_matching_tag_wins(self):
        from apps.rollouts.grouping import group_stations_by_sequence
        from apps.rollouts.models import current_sequence, RolloutSequenceEntry
        from apps.stations.models import StationTag

        t_test = StationTag.objects.create(name="test")
        t_easy = StationTag.objects.create(name="easy")
        seq = current_sequence()
        seq.entries.all().delete()
        RolloutSequenceEntry.objects.create(sequence=seq, tag=t_test, position=0)
        RolloutSequenceEntry.objects.create(sequence=seq, tag=t_easy, position=1)

        s = self._station_with_tags("test", "easy")
        grouped = group_stations_by_sequence([s])
        # s must appear ONLY in the 'test' bucket, not in 'easy' too.
        assert grouped["test"] == [s]
        assert grouped["easy"] == []

    def test_unassigned_bucket(self):
        from apps.rollouts.grouping import group_stations_by_sequence
        from apps.rollouts.models import current_sequence

        seq = current_sequence()
        seq.entries.all().delete()
        s = self._station_with_tags()  # no tags
        grouped = group_stations_by_sequence([s])
        assert grouped["__unassigned__"] == [s]
```

- [ ] **Step 5.2: Write the module**

```python
# apps/rollouts/grouping.py
from __future__ import annotations

from collections import OrderedDict
from typing import Iterable

from .models import current_sequence

UNASSIGNED_KEY = "__unassigned__"


def group_stations_by_sequence(stations: Iterable) -> "OrderedDict[str, list]":
    """Bucket each station by the first sequence tag it carries.

    Returns an OrderedDict with one key per sequence entry (in position
    order), each mapping to a list of Stations, plus an UNASSIGNED_KEY
    bucket at the end for stations with no matching tag.
    """
    seq = current_sequence()
    ordered_tag_names = list(
        seq.entries.select_related("tag").values_list("tag__name", flat=True)
    )

    buckets: OrderedDict[str, list] = OrderedDict()
    for name in ordered_tag_names:
        buckets[name] = []
    buckets[UNASSIGNED_KEY] = []

    for station in stations:
        station_tag_names = set(station.tags.values_list("name", flat=True))
        placed = False
        for name in ordered_tag_names:
            if name in station_tag_names:
                buckets[name].append(station)
                placed = True
                break
        if not placed:
            buckets[UNASSIGNED_KEY].append(station)

    return buckets
```

- [ ] **Step 5.3: Run + commit**

```
.venv/bin/pytest tests/test_rollouts.py::TestGrouping -v
```

```bash
git -C /home/pbuchegger/station-manager add apps/rollouts tests/test_rollouts.py
git -C /home/pbuchegger/station-manager commit -m "rollouts: station-to-group assignment (first-match-wins)"
```

---

### Task 6: Upgrade-action views (group + single)

**Agent:** `gateway`

**Files:**
- Modify: `apps/rollouts/views.py`, `apps/rollouts/urls.py`
- Modify: `tests/test_rollouts.py`

- [ ] **Step 6.1: Tests**

```python
# append
@pytest.mark.django_db
class TestUpgradeActions:
    def test_admin_can_upgrade_single_station(self, client, admin_user, station, image_release):
        from apps.deployments.models import Deployment, DeploymentResult
        from django.urls import reverse

        station.current_image_release = None  # not on target
        station.save(update_fields=["current_image_release"])

        client.force_login(admin_user)
        response = client.post(
            reverse("rollouts:upgrade_station", args=[station.pk]),
        )
        assert response.status_code == 302
        dep = Deployment.objects.get(target_station=station)
        assert dep.image_release == image_release
        assert DeploymentResult.objects.filter(deployment=dep, station=station).exists()

    def test_operator_cannot_upgrade(self, client, operator_user, station, image_release):
        from django.urls import reverse

        client.force_login(operator_user)
        response = client.post(reverse("rollouts:upgrade_station", args=[station.pk]))
        assert response.status_code == 403

    def test_upgrade_group_creates_one_deployment_per_machine(
        self, client, admin_user, image_release, db
    ):
        from apps.deployments.models import Deployment
        from apps.images.models import ImageRelease
        from apps.rollouts.models import current_sequence, RolloutSequenceEntry
        from apps.stations.models import Station, StationTag
        from django.urls import reverse

        # one qemu image (fixture) + one rpi image
        rpi = ImageRelease.objects.create(
            tag="v1-alpha",
            machine="raspberrypi4-64",
            s3_key="images/v1-alpha/raspberrypi4-64.wic.bz2",
            sha256="c" * 64,
            size_bytes=1000,
            is_latest=True,
        )

        tag = StationTag.objects.create(name="test")
        seq = current_sequence()
        seq.entries.all().delete()
        RolloutSequenceEntry.objects.create(sequence=seq, tag=tag, position=0)

        s_qemu = Station.objects.create(name="Q")
        s_qemu.tags.add(tag)
        # The station's machine is tracked via current_image_release; if None,
        # the view uses ImageRelease(is_latest=True) matching by machine via
        # Station.machine (a direct field — Task 6.4 adds this).

        client.force_login(admin_user)
        response = client.post(
            reverse("rollouts:upgrade_group", args=["test"]),
        )
        assert response.status_code == 302
        # Check the qemu station got a deployment
        dep_qemu = Deployment.objects.filter(target_tag=tag, image_release=image_release).first()
        assert dep_qemu is not None
```

(Skips some edge cases that get covered in integration — focus on shape.)

- [ ] **Step 6.2: Write views**

```python
# apps/rollouts/views.py
import logging

from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.accounts.views import AdminRequiredMixin
from apps.deployments.models import Deployment, DeploymentResult
from apps.deployments.supersession import (
    ActiveDeploymentConflict,
    supersede_pending_for_station,
)
from apps.images.models import ImageRelease
from apps.stations.models import Station, StationAuditLog, StationTag

logger = logging.getLogger(__name__)


def _best_effort_audit_log(*, station, event_type, message, user=None):
    """Audit logging must never break the real operation."""
    try:
        StationAuditLog.log(
            station=station,
            event_type=event_type,
            message=message,
            user=user,
        )
    except Exception as exc:
        logger.warning(
            "Audit log write failed (%s): %s", event_type, exc
        )


def _target_release_for(station) -> ImageRelease | None:
    """Look up the latest ImageRelease for the station's machine.

    Machine is taken from station.current_image_release.machine; if the
    station has never been provisioned through our flow, return None.
    """
    current = getattr(station, "current_image_release", None)
    if current is None:
        return None
    return ImageRelease.objects.filter(
        machine=current.machine, is_latest=True
    ).first()


class UpgradeStationView(AdminRequiredMixin, View):
    """Create a Deployment targeting exactly this one station."""

    def post(self, request, station_pk):
        station = get_object_or_404(Station, pk=station_pk)
        target = _target_release_for(station)
        if target is None:
            messages.error(
                request,
                _("No image release available for this station's machine."),
            )
            return redirect("stations:station_detail", pk=station.pk)

        if station.current_image_release_id == target.pk:
            messages.info(request, _("Station is already on the latest release."))
            return redirect("stations:station_detail", pk=station.pk)

        try:
            with transaction.atomic():
                dep = Deployment.objects.create(
                    image_release=target,
                    target_type=Deployment.TargetType.STATION,
                    target_station=station,
                    status=Deployment.Status.IN_PROGRESS,
                    created_by=request.user,
                )
                result = DeploymentResult.objects.create(
                    deployment=dep,
                    station=station,
                    status=DeploymentResult.Status.PENDING,
                    previous_version=station.current_os_version or "",
                )
                supersede_pending_for_station(station=station, new_deployment=dep)
        except ActiveDeploymentConflict as exc:
            messages.error(request, str(exc))
            return redirect("stations:station_detail", pk=station.pk)

        _best_effort_audit_log(
            station=station,
            event_type=StationAuditLog.EventType.FIRMWARE_UPDATE,
            message=(
                f"Upgrade triggered: {station.current_os_version or '?'} "
                f"→ {target.tag} (deployment #{dep.pk}) by {request.user.username}"
            ),
            user=request.user,
        )
        messages.success(
            request, _("Upgrade to %(tag)s queued.") % {"tag": target.tag}
        )
        return redirect("stations:station_detail", pk=station.pk)


class UpgradeGroupView(AdminRequiredMixin, View):
    """Create Deployments for every station carrying the given tag (grouped
    by machine: one Deployment per (tag, machine) tuple).
    """

    def post(self, request, tag_slug):
        tag = get_object_or_404(StationTag, name=tag_slug)
        stations = list(
            Station.objects.filter(tags=tag).select_related("current_image_release")
        )
        if not stations:
            messages.info(request, _("No stations carry this tag."))
            return redirect("rollouts:upgrade_dashboard")

        # Bucket by machine.
        by_machine: dict[str, list] = {}
        for s in stations:
            if not s.current_image_release:
                continue
            by_machine.setdefault(s.current_image_release.machine, []).append(s)

        created = 0
        skipped = 0
        with transaction.atomic():
            for machine, machine_stations in by_machine.items():
                target = ImageRelease.objects.filter(
                    machine=machine, is_latest=True
                ).first()
                if target is None:
                    skipped += len(machine_stations)
                    continue
                dep = Deployment.objects.create(
                    image_release=target,
                    target_type=Deployment.TargetType.TAG,
                    target_tag=tag,
                    status=Deployment.Status.IN_PROGRESS,
                    created_by=request.user,
                )
                for s in machine_stations:
                    if s.current_image_release_id == target.pk:
                        skipped += 1
                        continue
                    DeploymentResult.objects.create(
                        deployment=dep,
                        station=s,
                        status=DeploymentResult.Status.PENDING,
                        previous_version=s.current_os_version or "",
                    )
                    try:
                        supersede_pending_for_station(
                            station=s, new_deployment=dep
                        )
                    except ActiveDeploymentConflict:
                        # Drop this station from the deployment — it will
                        # be picked up next time.
                        DeploymentResult.objects.filter(
                            deployment=dep, station=s
                        ).delete()
                        skipped += 1
                        continue
                    _best_effort_audit_log(
                        station=s,
                        event_type=StationAuditLog.EventType.FIRMWARE_UPDATE,
                        message=(
                            f"Upgrade triggered (group '{tag.name}'): "
                            f"{s.current_os_version or '?'} → {target.tag} "
                            f"(deployment #{dep.pk}) by {request.user.username}"
                        ),
                        user=request.user,
                    )
                    created += 1

        messages.success(
            request,
            _("Queued %(n)d upgrades (%(s)d skipped)") % {"n": created, "s": skipped},
        )
        return redirect("rollouts:upgrade_dashboard")
```

- [ ] **Step 6.3: URLs**

```python
# apps/rollouts/urls.py
from django.urls import path

from . import views

app_name = "rollouts"
urlpatterns = [
    path(
        "upgrade/station/<int:station_pk>/",
        views.UpgradeStationView.as_view(),
        name="upgrade_station",
    ),
    path(
        "upgrade/group/<str:tag_slug>/",
        views.UpgradeGroupView.as_view(),
        name="upgrade_group",
    ),
]
```

- [ ] **Step 6.4: Commit**

```
.venv/bin/pytest tests/test_rollouts.py tests/test_deployments.py -v
```

```bash
git -C /home/pbuchegger/station-manager add apps/rollouts tests/test_rollouts.py
git -C /home/pbuchegger/station-manager commit -m "rollouts: upgrade-station and upgrade-group views"
```

---

### Task 7: `DeploymentCheckView` refactor — opaque URL + current_version + image_release shape

**Agent:** `gateway`

**Files:**
- Modify: `apps/deployments/api_views.py`, `apps/deployments/serializers.py`
- Modify: `tests/test_deployments.py` (or tests/test_api.py — wherever existing deployment-check tests live)

- [ ] **Step 7.1: Tests**

```python
@pytest.mark.django_db
class TestDeploymentCheckNewShape:
    def test_response_shape_with_image_release(
        self, client, station_with_key, image_release, admin_user
    ):
        import json

        from apps.deployments.models import Deployment, DeploymentResult
        from django.urls import reverse

        from tests.conftest import device_auth_headers

        station, priv = station_with_key
        dep = Deployment.objects.create(
            image_release=image_release,
            target_type=Deployment.TargetType.STATION,
            target_station=station,
            status=Deployment.Status.IN_PROGRESS,
            created_by=admin_user,
        )
        DeploymentResult.objects.create(
            deployment=dep, station=station, status=DeploymentResult.Status.PENDING
        )

        body = json.dumps({"current_version": "v1-alpha"}).encode()
        headers = device_auth_headers(priv, station.pk, body)
        r = client.post(
            reverse("api:deployment-check"),
            data=body,
            content_type="application/json",
            **headers,
        )
        assert r.status_code == 200
        data = r.json()
        assert data["target_tag"] == image_release.tag
        assert data["checksum_sha256"] == image_release.sha256
        assert data["size_bytes"] == image_release.size_bytes
        assert data["download_url"].endswith(f"/deployments/{dep.pk}/download/")
```

- [ ] **Step 7.2: Serializer update**

```python
# apps/deployments/serializers.py
from rest_framework import serializers

from apps.deployments.models import DeploymentResult


class DeploymentCheckResponseSerializer(serializers.Serializer):
    deployment_result_id = serializers.IntegerField()
    deployment_id = serializers.IntegerField()
    target_tag = serializers.CharField()
    checksum_sha256 = serializers.CharField()
    size_bytes = serializers.IntegerField()
    download_url = serializers.CharField()


class DeploymentStatusUpdateSerializer(serializers.Serializer):
    status = serializers.ChoiceField(
        choices=[
            DeploymentResult.Status.DOWNLOADING,
            DeploymentResult.Status.INSTALLING,
            DeploymentResult.Status.REBOOTING,
            DeploymentResult.Status.VERIFYING,
            DeploymentResult.Status.FAILED,
            DeploymentResult.Status.ROLLED_BACK,
        ]
    )
    error_message = serializers.CharField(required=False, default="", allow_blank=True)


class DeploymentCommitSerializer(serializers.Serializer):
    version = serializers.CharField(max_length=100)


class DeploymentCheckRequestSerializer(serializers.Serializer):
    current_version = serializers.CharField(max_length=100, required=False, default="")
```

- [ ] **Step 7.3: View change**

Replace `DeploymentCheckView` body:

```python
class DeploymentCheckView(APIView):
    """Station-agent polls to see if a deployment is pending for it."""

    authentication_classes = [DeviceKeyAuthentication]
    permission_classes = [IsDevice]

    def post(self, request):
        station = getattr(request.auth, "station", None)
        if station is None:
            return Response(
                {"detail": "No station linked to this device key."},
                status=status.HTTP_404_NOT_FOUND,
            )

        req = DeploymentCheckRequestSerializer(data=request.data)
        req.is_valid(raise_exception=True)
        # current_version parsed for forward compatibility (deltas); not
        # used in the MVP beyond audit.
        _ = req.validated_data["current_version"]

        result = (
            DeploymentResult.objects.filter(
                station=station,
                status=DeploymentResult.Status.PENDING,
                deployment__status=Deployment.Status.IN_PROGRESS,
            )
            .select_related("deployment__image_release")
            .order_by("deployment__created_at")
            .first()
        )

        if result is None:
            return Response(status=status.HTTP_204_NO_CONTENT)

        image = result.deployment.image_release
        data = DeploymentCheckResponseSerializer(
            {
                "deployment_result_id": result.pk,
                "deployment_id": result.deployment_id,
                "target_tag": image.tag,
                "checksum_sha256": image.sha256,
                "size_bytes": image.size_bytes,
                "download_url": f"/api/v1/deployments/{result.deployment_id}/download/",
            }
        ).data
        return Response(data)
```

**Breaking URL change:** check endpoint moves from GET to POST (body carries `current_version`). Also rename the API URL from `deployments/check/` GET to `deployments/check/` POST. The existing route pattern stays; only the method changes. Agent (Task 16) is updated to match.

- [ ] **Step 7.4: Commit**

```
.venv/bin/pytest tests/test_deployments.py -v
```

```bash
git -C /home/pbuchegger/station-manager add apps/deployments tests
git -C /home/pbuchegger/station-manager commit -m "deployments: check endpoint returns image_release + accepts current_version"
```

---

### Task 8: `DeploymentDownloadView` refactor — stream from S3 + Range support

**Agent:** `gateway`

**Files:**
- Modify: `apps/deployments/api_views.py`, `apps/deployments/api_urls.py`
- Modify: `tests/test_deployments.py`

- [ ] **Step 8.1: Tests**

```python
@pytest.mark.django_db
class TestDeploymentDownload:
    def test_full_download_streams_from_s3(
        self, client, station_with_key, image_release, admin_user, monkeypatch
    ):
        from django.urls import reverse
        import io

        from apps.deployments.models import Deployment, DeploymentResult
        from tests.conftest import device_auth_headers

        station, priv = station_with_key
        dep = Deployment.objects.create(
            image_release=image_release,
            target_type=Deployment.TargetType.STATION,
            target_station=station,
            status=Deployment.Status.IN_PROGRESS,
            created_by=admin_user,
        )
        DeploymentResult.objects.create(
            deployment=dep, station=station, status=DeploymentResult.Status.PENDING
        )

        monkeypatch.setattr(
            "apps.images.storage.open_stream", lambda key: io.BytesIO(b"IMAGE" * 10)
        )
        headers = device_auth_headers(priv, station.pk, b"")
        r = client.get(
            reverse("api:deployment-download", args=[dep.pk]), **headers
        )
        assert r.status_code == 200
        assert b"".join(r.streaming_content) == b"IMAGE" * 10

    def test_download_rejects_other_station(
        self, client, station_with_key, image_release, admin_user, db
    ):
        import base64
        import hashlib
        import time

        from apps.api.models import DeviceKey
        from apps.deployments.models import Deployment, DeploymentResult
        from apps.stations.models import Station
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from django.urls import reverse

        station_a, _ = station_with_key
        dep = Deployment.objects.create(
            image_release=image_release,
            target_type=Deployment.TargetType.STATION,
            target_station=station_a,
            status=Deployment.Status.IN_PROGRESS,
            created_by=admin_user,
        )
        DeploymentResult.objects.create(
            deployment=dep, station=station_a,
            status=DeploymentResult.Status.PENDING,
        )

        # A different station with its own key.
        other = Station.objects.create(name="Other")
        priv_b = Ed25519PrivateKey.generate()
        from cryptography.hazmat.primitives.serialization import (
            Encoding, NoEncryption, PrivateFormat, PublicFormat,
        )
        pub = priv_b.public_key().public_bytes(
            encoding=Encoding.Raw, format=PublicFormat.Raw
        )
        DeviceKey.objects.create(
            station=other, current_public_key=base64.b64encode(pub).decode("ascii")
        )
        body_hash = hashlib.sha256(b"").hexdigest()
        ts = str(time.time())
        sig = base64.b64encode(
            priv_b.sign(f"{ts}:{body_hash}".encode())
        ).decode("ascii")
        r = client.get(
            reverse("api:deployment-download", args=[dep.pk]),
            HTTP_AUTHORIZATION=f"DeviceKey {other.pk}",
            HTTP_X_DEVICE_SIGNATURE=sig,
            HTTP_X_DEVICE_TIMESTAMP=ts,
        )
        assert r.status_code == 403
```

- [ ] **Step 8.2: Rewrite the view**

```python
# in apps/deployments/api_views.py — replace DeploymentDownloadView
import re

from django.http import StreamingHttpResponse

from apps.images import storage as image_storage


class DeploymentDownloadView(APIView):
    """Stream the deployment's image from S3 to the requesting station.

    Authz: only the station the deployment targets (via a DeploymentResult
    row in an active state) may download.
    Authn: DeviceKeyAuthentication — existing Ed25519 signature.
    Range/If-Range: supported for resumable transfers on flaky links.
    """

    authentication_classes = [DeviceKeyAuthentication]
    permission_classes = [IsDevice]

    CHUNK = 1 << 20

    def get(self, request, pk):
        station = getattr(request.auth, "station", None)
        if station is None:
            return Response(
                {"detail": "No station linked to this device key."},
                status=status.HTTP_404_NOT_FOUND,
            )

        active_statuses = [
            DeploymentResult.Status.PENDING,
            DeploymentResult.Status.DOWNLOADING,
            DeploymentResult.Status.INSTALLING,
            DeploymentResult.Status.REBOOTING,
        ]
        result = (
            DeploymentResult.objects.select_related("deployment__image_release")
            .filter(deployment_id=pk, station=station, status__in=active_statuses)
            .first()
        )
        if result is None:
            return Response(
                {"detail": "No active deployment for this station on this deployment id."},
                status=status.HTTP_403_FORBIDDEN,
            )

        image = result.deployment.image_release
        stream = image_storage.open_stream(image.s3_key)
        total_size = image.size_bytes or 0

        # Optional Range support — translate HTTP Range into a seek on the stream.
        range_header = request.META.get("HTTP_RANGE", "")
        start = 0
        end = total_size - 1 if total_size else None
        http_status = 200
        length = total_size
        if range_header:
            m = re.match(r"bytes=(\d+)-(\d*)", range_header)
            if m:
                start = int(m.group(1))
                end_g = m.group(2)
                if end_g:
                    end = int(end_g)
                try:
                    stream.seek(start)
                except Exception:
                    # Fallback: read-and-discard (default_storage backends
                    # don't all support seek; boto3 S3 does).
                    stream.close()
                    stream = image_storage.open_stream(image.s3_key)
                    discarded = 0
                    while discarded < start:
                        chunk = stream.read(min(self.CHUNK, start - discarded))
                        if not chunk:
                            break
                        discarded += len(chunk)
                http_status = 206
                length = (end - start + 1) if end is not None else None

        def iterator():
            remaining = length
            try:
                while True:
                    to_read = self.CHUNK if remaining is None else min(self.CHUNK, remaining)
                    if to_read <= 0:
                        break
                    chunk = stream.read(to_read)
                    if not chunk:
                        break
                    if remaining is not None:
                        remaining -= len(chunk)
                    yield chunk
            finally:
                stream.close()

        filename = f"oe5xrx-{image.machine}-{image.tag}.wic.bz2"
        safe_name = re.sub(r'["\r\n]', "_", filename) or "image.wic.bz2"
        response = StreamingHttpResponse(
            iterator(), status=http_status, content_type="application/x-bzip2"
        )
        response["Content-Disposition"] = f'attachment; filename="{safe_name}"'
        response["Accept-Ranges"] = "bytes"
        if length is not None:
            response["Content-Length"] = str(length)
        if http_status == 206 and end is not None:
            response["Content-Range"] = f"bytes {start}-{end}/{total_size or '*'}"
        return response
```

- [ ] **Step 8.3: URL path takes deployment_id (not result_id)**

Check `apps/deployments/api_urls.py` — the download URL was registered with `<int:pk>` under the result. Change it to resolve on `deployment_id`:

```python
# apps/deployments/api_urls.py (in the urlpatterns list, replace the old download entry)
path(
    "deployments/<int:pk>/download/",
    api_views.DeploymentDownloadView.as_view(),
    name="deployment-download",
),
```

Ensure the name `api:deployment-download` matches the tests.

- [ ] **Step 8.4: Commit**

```
.venv/bin/pytest tests/test_deployments.py -v
```

```bash
git -C /home/pbuchegger/station-manager add apps/deployments tests
git -C /home/pbuchegger/station-manager commit -m "deployments: download view streams from S3 + Range support + strict authz"
```

---

### Task 9: Commit view sets `Station.current_image_release` on success

**Agent:** `gateway`

**Files:**
- Modify: `apps/deployments/api_views.py`
- Modify: `tests/test_deployments.py`

- [ ] **Step 9.1: Test**

```python
@pytest.mark.django_db
class TestCommitSetsCurrentImage:
    def test_commit_updates_current_image_release(
        self, client, station_with_key, image_release, admin_user
    ):
        from apps.deployments.models import Deployment, DeploymentResult
        from django.urls import reverse
        import json

        from tests.conftest import device_auth_headers

        station, priv = station_with_key
        dep = Deployment.objects.create(
            image_release=image_release,
            target_type=Deployment.TargetType.STATION,
            target_station=station,
            status=Deployment.Status.IN_PROGRESS,
            created_by=admin_user,
        )
        DeploymentResult.objects.create(
            deployment=dep, station=station,
            status=DeploymentResult.Status.REBOOTING,
        )
        body = json.dumps({"version": image_release.tag}).encode()
        headers = device_auth_headers(priv, station.pk, body)
        r = client.post(
            reverse("api:deployment-commit"), data=body,
            content_type="application/json", **headers,
        )
        assert r.status_code == 200
        station.refresh_from_db()
        assert station.current_image_release_id == image_release.pk
```

- [ ] **Step 9.2: View extension**

In `DeploymentCommitView.post`, after setting `result.status=SUCCESS`, add:

```python
# Update the station's "provisioned with" pointer so the UI reflects
# what's running on disk right now.
from django.utils import timezone

result.station.current_image_release = result.deployment.image_release
result.station.updated_at = timezone.now()
result.station.save(update_fields=["current_image_release", "updated_at"])
```

- [ ] **Step 9.3: Commit**

```bash
git -C /home/pbuchegger/station-manager add apps/deployments tests
git -C /home/pbuchegger/station-manager commit -m "deployments: commit view bumps Station.current_image_release"
```

---

## Phase 3 — UI

Before writing the Upgrade Dashboard template (Task 10), the implementing subagent **must invoke the `frontend-design:frontend-design` skill** to produce a design proposal (layout, density, typography, empty states) consistent with the existing station-detail "Operator's Console" visual language. Use the station-detail page (`apps/stations/templates/stations/station_detail.html`) + the Images page as references. Emerging mockup lives in `apps/rollouts/templates/rollouts/upgrade_dashboard.html` directly — no separate design doc.

### Task 10: Upgrade Dashboard view + template

**Agent:** `pixel`

**Files:**
- Modify: `apps/rollouts/views.py` (add `UpgradeDashboardView`)
- Modify: `apps/rollouts/urls.py`
- Create: `apps/rollouts/templates/rollouts/upgrade_dashboard.html`
- Create: `apps/rollouts/templates/rollouts/_dashboard_row.html`
- Modify: `templates/includes/sidebar.html` (add admin-only Deployments sub-entry)
- Modify: `tests/test_rollouts.py`

- [ ] **Step 10.1: View test**

```python
@pytest.mark.django_db
class TestUpgradeDashboard:
    def test_admin_sees_groups(self, client, admin_user, station, image_release):
        from apps.rollouts.models import current_sequence, RolloutSequenceEntry
        from apps.stations.models import StationTag
        from django.urls import reverse

        tag = StationTag.objects.create(name="test-stations")
        station.tags.add(tag)
        station.current_image_release = None
        station.save(update_fields=["current_image_release"])
        seq = current_sequence()
        RolloutSequenceEntry.objects.create(sequence=seq, tag=tag, position=0)

        client.force_login(admin_user)
        response = client.get(reverse("rollouts:upgrade_dashboard"))
        assert response.status_code == 200
        assert b"test-stations" in response.content
        # The "Upgrade group" button for this group must render.
        assert b"upgrade_group" in response.content or b"Upgrade group" in response.content

    def test_operator_forbidden(self, client, operator_user):
        from django.urls import reverse

        client.force_login(operator_user)
        response = client.get(reverse("rollouts:upgrade_dashboard"))
        assert response.status_code == 403
```

- [ ] **Step 10.2: View**

```python
# append to apps/rollouts/views.py
from django.views.generic import TemplateView

from apps.images.models import ImageRelease
from apps.rollouts.grouping import UNASSIGNED_KEY, group_stations_by_sequence


class UpgradeDashboardView(AdminRequiredMixin, TemplateView):
    template_name = "rollouts/upgrade_dashboard.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        stations = list(
            Station.objects.select_related("current_image_release").prefetch_related("tags")
        )
        grouped = group_stations_by_sequence(stations)

        latest_per_machine: dict[str, ImageRelease] = {
            r.machine: r
            for r in ImageRelease.objects.filter(is_latest=True)
        }

        # Build row dicts: split each group into pending vs up-to-date.
        rows_by_group: list[tuple[str, list, list]] = []
        up_to_date: list = []
        for group_key, stations_in_group in grouped.items():
            pending = []
            for s in stations_in_group:
                target = (
                    latest_per_machine.get(s.current_image_release.machine)
                    if s.current_image_release
                    else None
                )
                if target and s.current_image_release_id == target.pk:
                    up_to_date.append((s, target))
                else:
                    pending.append((s, target))
            display_name = (
                "Unassigned" if group_key == UNASSIGNED_KEY else group_key
            )
            rows_by_group.append((group_key, display_name, pending))

        ctx["groups"] = rows_by_group
        ctx["up_to_date"] = up_to_date
        ctx["latest_per_machine"] = latest_per_machine
        ctx["unassigned_key"] = UNASSIGNED_KEY
        return ctx
```

Add the URL:

```python
# apps/rollouts/urls.py
urlpatterns = [
    path("upgrade/", views.UpgradeDashboardView.as_view(), name="upgrade_dashboard"),
    # ... the two from Task 6 ...
]
```

- [ ] **Step 10.3: Template — main dashboard**

```html
<!-- apps/rollouts/templates/rollouts/upgrade_dashboard.html -->
{% extends "base.html" %}
{% load i18n %}

{% block content %}
<h1>{% trans "Upgrade Dashboard" %}</h1>

<div class="panel mb-20">
  <strong>{% trans "Latest images" %}:</strong>
  {% for machine, release in latest_per_machine.items %}
    <span class="badge">{{ machine }} → {{ release.tag }}</span>
  {% empty %}
    <em>{% trans "No image releases imported yet." %}</em>
  {% endfor %}
</div>

{% for group_key, display_name, pending in groups %}
  <section class="panel" id="group-{{ group_key }}">
    <div class="panel-head">
      <h2>
        {{ display_name }}
        <small>({{ pending|length }} {% trans "pending" %})</small>
      </h2>
      {% if pending and group_key != unassigned_key %}
        <form method="post" action="{% url 'rollouts:upgrade_group' tag_slug=group_key %}"
              onsubmit="return confirm('{% blocktrans with n=pending|length g=display_name %}Upgrade {{ n }} stations in {{ g }}?{% endblocktrans %}');">
          {% csrf_token %}
          <button type="submit" class="btn">{% trans "Upgrade group" %}</button>
        </form>
      {% endif %}
    </div>
    <table class="table station-table">
      <tbody id="rows-{{ group_key }}">
        {% for station, target in pending %}
          {% include "rollouts/_dashboard_row.html" with station=station target=target %}
        {% endfor %}
      </tbody>
    </table>
  </section>
{% endfor %}

<details class="panel">
  <summary><strong>{% trans "Up to date" %}</strong> ({{ up_to_date|length }})</summary>
  <table class="table station-table">
    <tbody>
      {% for station, target in up_to_date %}
        <tr>
          <td><a href="{% url 'stations:station_detail' pk=station.pk %}">{{ station.name }}</a></td>
          <td>{{ station.current_image_release.machine|default:'-' }}</td>
          <td>{{ station.current_image_release.tag|default:'-' }}</td>
          <td>✓</td>
        </tr>
      {% endfor %}
    </tbody>
  </table>
</details>

<script nonce="{{ csp_nonce }}">
(function () {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(proto + '//' + location.host + '/ws/deployments/');
  ws.addEventListener('message', (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      const r = msg.result;
      if (!r) return;
      const row = document.querySelector(`tr[data-station-id="${r.station_id}"]`);
      if (!row) return;
      const statusCell = row.querySelector('.status-cell');
      if (statusCell) statusCell.textContent = r.status;
    } catch (_) { /* ignore */ }
  });
})();
</script>
{% endblock %}
```

- [ ] **Step 10.4: Row partial**

```html
<!-- apps/rollouts/templates/rollouts/_dashboard_row.html -->
{% load i18n %}
<tr data-station-id="{{ station.pk }}">
  <td><a href="{% url 'stations:station_detail' pk=station.pk %}">{{ station.name }}</a></td>
  <td>{{ station.current_image_release.machine|default:'-' }}</td>
  <td>
    {{ station.current_image_release.tag|default:'-' }}
    {% if target %} → <strong>{{ target.tag }}</strong>{% endif %}
  </td>
  <td class="status-cell">
    {% if station.status == "online" %}○{% else %}○ offline{% endif %}
  </td>
</tr>
```

- [ ] **Step 10.5: Sidebar entry**

In `templates/includes/sidebar.html`, inside the existing `{% if user.role == 'admin' %}` block, add:

```html
<a class="nav-link" href="{% url 'rollouts:upgrade_dashboard' %}">
  <svg class="icon"><!-- keep existing icon markup pattern --></svg>
  <span>{% trans "Upgrades" %}</span>
</a>
```

- [ ] **Step 10.6: Run, lint, commit**

```
.venv/bin/pytest tests/test_rollouts.py -v
.venv/bin/ruff check apps/rollouts tests/test_rollouts.py
.venv/bin/ruff format --check apps/rollouts tests/test_rollouts.py
```

```bash
git -C /home/pbuchegger/station-manager add apps/rollouts templates/includes/sidebar.html tests/test_rollouts.py
git -C /home/pbuchegger/station-manager commit -m "rollouts: Upgrade Dashboard view + template + sidebar entry"
```

---

### Task 11: Station-to-dashboard-row live updates via the existing WS channel

**Agent:** `gateway`

**Files:**
- Modify: `apps/deployments/consumers.py`
- Modify: `tests/test_deployments.py` (simple unit test on the broadcast payload)

- [ ] **Step 11.1: Test**

```python
def test_broadcast_includes_machine_and_tag(station, image_release, admin_user, monkeypatch):
    from apps.deployments.consumers import broadcast_deployment_status
    from apps.deployments.models import Deployment, DeploymentResult

    captured = {}
    def fake_group_send(group, event):
        captured["event"] = event

    monkeypatch.setattr(
        "apps.deployments.consumers.async_to_sync",
        lambda fn: (lambda *a, **k: fn(*a, **k)),
    )
    monkeypatch.setattr(
        "apps.deployments.consumers.get_channel_layer",
        lambda: type("CL", (), {"group_send": staticmethod(fake_group_send)})(),
    )

    dep = Deployment.objects.create(
        image_release=image_release,
        target_type=Deployment.TargetType.STATION,
        target_station=station,
        status=Deployment.Status.IN_PROGRESS,
        created_by=admin_user,
    )
    result = DeploymentResult.objects.create(
        deployment=dep, station=station, status=DeploymentResult.Status.INSTALLING,
    )
    broadcast_deployment_status(dep, result=result)

    payload = captured["event"]["data"]
    assert payload["result"]["station_id"] == station.pk
    assert payload["result"]["tag"] == image_release.tag
    assert payload["result"]["machine"] == image_release.machine
```

- [ ] **Step 11.2: Extend broadcast_deployment_status**

Replace the `result` branch in `broadcast_deployment_status`:

```python
if result is not None:
    image = deployment.image_release
    data["result"] = {
        "id": result.id,
        "station_id": result.station_id,
        "station_name": result.station.name if hasattr(result, "station") else "",
        "status": result.status,
        "error_message": result.error_message or "",
        "started_at": result.started_at.isoformat() if result.started_at else None,
        "completed_at": result.completed_at.isoformat() if result.completed_at else None,
        "tag": image.tag if image else "",
        "machine": image.machine if image else "",
    }
```

- [ ] **Step 11.3: Commit**

```bash
git -C /home/pbuchegger/station-manager add apps/deployments tests/test_deployments.py
git -C /home/pbuchegger/station-manager commit -m "deployments: broadcast includes tag + machine for dashboard"
```

---

### Task 12: Rollout Sequence edit page (drag-reorder + add/remove)

**Agent:** `pixel`

**Files:**
- Modify: `apps/rollouts/views.py`, `apps/rollouts/urls.py`
- Create: `apps/rollouts/templates/rollouts/sequence_edit.html`
- Create: `apps/rollouts/forms.py`
- Modify: `tests/test_rollouts.py`

- [ ] **Step 12.1: Tests**

```python
@pytest.mark.django_db
class TestSequenceEdit:
    def test_add_entry(self, client, admin_user):
        from apps.rollouts.models import current_sequence
        from apps.stations.models import StationTag
        from django.urls import reverse

        current_sequence().entries.all().delete()
        tag = StationTag.objects.create(name="test")
        client.force_login(admin_user)
        response = client.post(
            reverse("rollouts:sequence_add"),
            {"tag": tag.pk},
        )
        assert response.status_code == 302
        seq = current_sequence()
        assert seq.entries.count() == 1

    def test_remove_entry(self, client, admin_user):
        from apps.rollouts.models import current_sequence, RolloutSequenceEntry
        from apps.stations.models import StationTag
        from django.urls import reverse

        tag = StationTag.objects.create(name="test")
        seq = current_sequence()
        seq.entries.all().delete()
        entry = RolloutSequenceEntry.objects.create(sequence=seq, tag=tag, position=0)
        client.force_login(admin_user)
        response = client.post(reverse("rollouts:sequence_remove", args=[entry.pk]))
        assert response.status_code == 302
        assert not RolloutSequenceEntry.objects.filter(pk=entry.pk).exists()

    def test_reorder(self, client, admin_user):
        from apps.rollouts.models import current_sequence, RolloutSequenceEntry
        from apps.stations.models import StationTag
        from django.urls import reverse

        seq = current_sequence()
        seq.entries.all().delete()
        t1 = StationTag.objects.create(name="t1")
        t2 = StationTag.objects.create(name="t2")
        e1 = RolloutSequenceEntry.objects.create(sequence=seq, tag=t1, position=0)
        e2 = RolloutSequenceEntry.objects.create(sequence=seq, tag=t2, position=1)

        client.force_login(admin_user)
        response = client.post(
            reverse("rollouts:sequence_reorder"),
            {"order": f"{e2.pk},{e1.pk}"},
        )
        assert response.status_code == 200
        e1.refresh_from_db()
        e2.refresh_from_db()
        assert e1.position == 1
        assert e2.position == 0
```

- [ ] **Step 12.2: Views**

```python
# append to apps/rollouts/views.py
from django.db.models import Max
from django.http import HttpResponse, HttpResponseBadRequest
from django.views.generic import FormView

from .models import RolloutSequenceEntry, current_sequence
from .forms import SequenceAddForm


class SequenceEditView(AdminRequiredMixin, TemplateView):
    template_name = "rollouts/sequence_edit.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        seq = current_sequence()
        ctx["sequence"] = seq
        ctx["entries"] = seq.entries.select_related("tag").order_by("position")
        ctx["add_form"] = SequenceAddForm(sequence=seq)
        return ctx


class SequenceAddView(AdminRequiredMixin, View):
    def post(self, request):
        seq = current_sequence()
        form = SequenceAddForm(request.POST, sequence=seq)
        if form.is_valid():
            next_pos = (seq.entries.aggregate(Max("position"))["position__max"] or -1) + 1
            RolloutSequenceEntry.objects.create(
                sequence=seq, tag=form.cleaned_data["tag"], position=next_pos,
            )
            seq.updated_by = request.user
            seq.save(update_fields=["updated_by", "updated_at"])
        return redirect("rollouts:sequence_edit")


class SequenceRemoveView(AdminRequiredMixin, View):
    def post(self, request, entry_pk):
        seq = current_sequence()
        entry = get_object_or_404(RolloutSequenceEntry, pk=entry_pk, sequence=seq)
        entry.delete()
        # Normalize positions (0..N-1) after removal so a later add/reorder
        # never collides with a gap.
        with transaction.atomic():
            for idx, e in enumerate(seq.entries.order_by("position")):
                if e.position != idx:
                    e.position = idx
                    e.save(update_fields=["position"])
        seq.updated_by = request.user
        seq.save(update_fields=["updated_by", "updated_at"])
        return redirect("rollouts:sequence_edit")


class SequenceReorderView(AdminRequiredMixin, View):
    def post(self, request):
        seq = current_sequence()
        order_str = request.POST.get("order", "")
        if not order_str:
            return HttpResponseBadRequest("order required")
        try:
            order_ids = [int(x) for x in order_str.split(",") if x]
        except ValueError:
            return HttpResponseBadRequest("order must be ids")
        existing = {e.pk: e for e in seq.entries.all()}
        if set(order_ids) != set(existing.keys()):
            return HttpResponseBadRequest("order must match existing entries")
        with transaction.atomic():
            # First pass: offset positions to avoid unique collisions, then set real values.
            for e in existing.values():
                e.position = e.position + 10000
                e.save(update_fields=["position"])
            for idx, pk in enumerate(order_ids):
                e = existing[pk]
                e.position = idx
                e.save(update_fields=["position"])
        seq.updated_by = request.user
        seq.save(update_fields=["updated_by", "updated_at"])
        return HttpResponse(status=200)
```

- [ ] **Step 12.3: Form**

```python
# apps/rollouts/forms.py
from django import forms
from django.utils.translation import gettext_lazy as _

from apps.stations.models import StationTag


class SequenceAddForm(forms.Form):
    tag = forms.ModelChoiceField(queryset=StationTag.objects.none(), label=_("Tag"))

    def __init__(self, *args, sequence=None, **kwargs):
        super().__init__(*args, **kwargs)
        if sequence is None:
            self.fields["tag"].queryset = StationTag.objects.all()
        else:
            used = sequence.entries.values_list("tag_id", flat=True)
            self.fields["tag"].queryset = StationTag.objects.exclude(pk__in=used)
```

- [ ] **Step 12.4: URLs**

```python
# apps/rollouts/urls.py — append
urlpatterns += [
    path("sequence/", views.SequenceEditView.as_view(), name="sequence_edit"),
    path("sequence/add/", views.SequenceAddView.as_view(), name="sequence_add"),
    path(
        "sequence/remove/<int:entry_pk>/",
        views.SequenceRemoveView.as_view(),
        name="sequence_remove",
    ),
    path("sequence/reorder/", views.SequenceReorderView.as_view(), name="sequence_reorder"),
]
```

- [ ] **Step 12.5: Template with drag-reorder**

```html
<!-- apps/rollouts/templates/rollouts/sequence_edit.html -->
{% extends "base.html" %}
{% load i18n %}

{% block content %}
<h1>{% trans "Rollout Sequence" %}</h1>
<p>{% trans "Drag rows to change the order. Stations without a matching tag fall into the 'Unassigned' bucket on the dashboard." %}</p>

<ul id="sequence-list">
  {% for entry in entries %}
    <li data-entry-id="{{ entry.pk }}">
      <span class="grip">≡</span>
      <span class="tag-name">{{ entry.tag.name }}</span>
      <form method="post" action="{% url 'rollouts:sequence_remove' entry_pk=entry.pk %}" style="display:inline">
        {% csrf_token %}
        <button type="submit" class="btn btn-small">{% trans "Remove" %}</button>
      </form>
    </li>
  {% endfor %}
</ul>

<h2>{% trans "Add tag" %}</h2>
<form method="post" action="{% url 'rollouts:sequence_add' %}">
  {% csrf_token %}
  {{ add_form.tag }}
  <button type="submit" class="btn">{% trans "Add" %}</button>
</form>

<script src="https://cdn.jsdelivr.net/npm/sortablejs@1.15.2/Sortable.min.js" nonce="{{ csp_nonce }}"></script>
<script nonce="{{ csp_nonce }}">
(function () {
  const list = document.getElementById('sequence-list');
  if (!list) return;
  const csrf = document.querySelector('[name=csrfmiddlewaretoken]').value;
  Sortable.create(list, {
    handle: '.grip',
    animation: 120,
    onEnd: async function () {
      const ids = Array.from(list.children).map(li => li.dataset.entryId).join(',');
      const fd = new FormData();
      fd.set('order', ids);
      const r = await fetch('{% url 'rollouts:sequence_reorder' %}', {
        method: 'POST',
        body: fd,
        headers: { 'X-CSRFToken': csrf },
      });
      if (!r.ok) { alert('reorder failed'); location.reload(); }
    },
  });
})();
</script>
{% endblock %}
```

- [ ] **Step 12.6: Sidebar link + commit**

In the admin sidebar block, add a second nav entry below "Upgrades":

```html
<a class="nav-link" href="{% url 'rollouts:sequence_edit' %}">
  <svg class="icon"><!-- icon --></svg>
  <span>{% trans "Rollout Sequence" %}</span>
</a>
```

```
.venv/bin/pytest tests/test_rollouts.py -v
.venv/bin/ruff check apps/rollouts
.venv/bin/ruff format --check apps/rollouts
```

```bash
git -C /home/pbuchegger/station-manager add apps/rollouts templates/includes/sidebar.html tests/test_rollouts.py
git -C /home/pbuchegger/station-manager commit -m "rollouts: Sequence edit page with drag-reorder"
```

---

### Task 13: Station detail "Upgrade" card

**Agent:** `pixel`

**Files:**
- Create: `apps/rollouts/templates/rollouts/_station_upgrade_card.html`
- Modify: `apps/stations/views.py`, `apps/stations/templates/stations/station_detail.html`
- Modify: `tests/test_rollouts.py`

- [ ] **Step 13.1: Test**

```python
@pytest.mark.django_db
class TestStationUpgradeCard:
    def test_admin_sees_upgrade_button(self, client, admin_user, station, image_release):
        from django.urls import reverse
        station.current_image_release = image_release
        station.save(update_fields=["current_image_release"])
        # Flip to a newer release
        from apps.images.models import ImageRelease
        ImageRelease.objects.filter(is_latest=True, machine="qemux86-64").update(is_latest=False)
        newer = ImageRelease.objects.create(
            tag="v2",
            machine="qemux86-64",
            s3_key="images/v2/qemu.wic.bz2",
            sha256="z" * 64,
            size_bytes=1,
            is_latest=True,
        )
        client.force_login(admin_user)
        r = client.get(reverse("stations:station_detail", pk=station.pk))
        assert r.status_code == 200
        assert b"Upgrade this station" in r.content
        assert b"v2" in r.content

    def test_already_on_latest_disables_button(
        self, client, admin_user, station, image_release
    ):
        from django.urls import reverse
        station.current_image_release = image_release  # which is_latest=True
        station.save(update_fields=["current_image_release"])
        client.force_login(admin_user)
        r = client.get(reverse("stations:station_detail", pk=station.pk))
        assert r.status_code == 200
        assert b"Already on latest" in r.content
```

- [ ] **Step 13.2: Card partial**

```html
<!-- apps/rollouts/templates/rollouts/_station_upgrade_card.html -->
{% load i18n %}
{% if user.role == 'admin' %}
<section class="panel mt-20">
  <h2>{% trans "Upgrade" %}</h2>
  {% if upgrade_target %}
    {% if station.current_image_release_id == upgrade_target.pk %}
      <p><em>{% trans "Already on latest" %} ({{ upgrade_target.tag }}).</em></p>
    {% else %}
      <p>
        {% trans "Current" %}: <code>{{ station.current_image_release.tag|default:'-' }}</code><br>
        {% trans "Target" %}: <code>{{ upgrade_target.tag }}</code> ({% trans "latest for" %} {{ upgrade_target.machine }})
      </p>
      <form method="post" action="{% url 'rollouts:upgrade_station' station_pk=station.pk %}"
            onsubmit="return confirm('{% blocktrans with t=upgrade_target.tag %}Upgrade this station to {{ t }}?{% endblocktrans %}');">
        {% csrf_token %}
        <button type="submit" class="btn btn-primary">
          {% blocktrans with t=upgrade_target.tag %}Upgrade this station to {{ t }}{% endblocktrans %}
        </button>
      </form>
    {% endif %}
  {% else %}
    <p><em>{% trans "No image release imported yet for this station's machine." %}</em></p>
  {% endif %}

  <h3>{% trans "Recent deployments" %}</h3>
  <table class="table">
    {% for d in recent_deployments %}
      <tr>
        <td>{{ d.created_at|date:"Y-m-d H:i" }}</td>
        <td>{{ d.previous_version|default:'?' }} → {{ d.deployment.image_release.tag }}</td>
        <td>{{ d.get_status_display }}</td>
      </tr>
    {% empty %}
      <tr><td colspan="3"><em>{% trans "None" %}</em></td></tr>
    {% endfor %}
  </table>
</section>
{% endif %}
```

- [ ] **Step 13.3: Context extension**

In `apps/stations/views.py`, `StationDetailView.get_context_data`:

```python
from apps.images.models import ImageRelease
from apps.deployments.models import DeploymentResult

if self.request.user.role == "admin":
    current = self.object.current_image_release
    if current is not None:
        context["upgrade_target"] = ImageRelease.objects.filter(
            machine=current.machine, is_latest=True
        ).first()
    else:
        context["upgrade_target"] = None
    context["recent_deployments"] = (
        DeploymentResult.objects.filter(station=self.object)
        .select_related("deployment__image_release")
        .order_by("-pk")[:5]
    )
```

- [ ] **Step 13.4: Include on station_detail**

Inside the existing admin-only area of `apps/stations/templates/stations/station_detail.html`, next to the Provisioning section include, add:

```html
{% include "rollouts/_station_upgrade_card.html" %}
```

- [ ] **Step 13.5: Commit**

```
.venv/bin/pytest tests/test_rollouts.py -v
```

```bash
git -C /home/pbuchegger/station-manager add apps/rollouts apps/stations tests/test_rollouts.py
git -C /home/pbuchegger/station-manager commit -m "rollouts: station-detail Upgrade card"
```

---

### Task 14: Sidebar polish + end-of-phase review

**Agent:** `atlas` (review-only)

Run:

```
.venv/bin/pytest tests/ -q
.venv/bin/ruff check apps tests
.venv/bin/ruff format --check apps tests
```

No code changes. Atlas verifies the Phase-3 slice renders end-to-end as a thin vertical slice: can an admin load the dashboard, see groups, click "Upgrade group", see a deployment created? Reports any gaps before Phase 4.

---

## Phase 4 — Agent

### Task 15: `install_to_slot` implementation

**Agent:** `gateway`

**Files:**
- Modify: `station_agent/ota.py`
- Modify: `tests/test_ota_install.py` (new, since the existing `tests/` doesn't cover the agent in isolation)

- [ ] **Step 15.1: Test against a fake block device (regular file)**

```python
# tests/test_ota_install.py
import bz2
import os
from pathlib import Path

import pytest

pytest.importorskip("station_agent.ota")


def test_install_to_slot_decompresses_bz2_and_writes_bytes(tmp_path):
    from station_agent.ota import install_to_slot

    payload = b"hello world" * 4096
    src = tmp_path / "image.wic.bz2"
    src.write_bytes(bz2.compress(payload))

    target = tmp_path / "fake-slot.bin"
    target.write_bytes(b"\x00" * len(payload))  # pre-size, simulate block device

    install_to_slot(src, str(target))

    # The target should now contain the decompressed bytes at the start.
    actual = target.read_bytes()
    assert actual[: len(payload)] == payload


def test_install_to_slot_writes_in_chunks(tmp_path, monkeypatch):
    # Confirm the implementation streams (doesn't read everything into memory).
    from station_agent import ota

    payload = b"x" * (4 << 20)  # 4 MiB
    src = tmp_path / "image.wic.bz2"
    src.write_bytes(bz2.compress(payload))

    target = tmp_path / "fake-slot.bin"
    target.write_bytes(b"\x00" * len(payload))

    # Count read calls. With chunks of 1 MiB we expect at least 4.
    original_read = ota._stream_read
    call_count = {"n": 0}
    def counted_read(fh, n):
        call_count["n"] += 1
        return original_read(fh, n)
    monkeypatch.setattr(ota, "_stream_read", counted_read)

    ota.install_to_slot(src, str(target))
    assert call_count["n"] >= 4
```

- [ ] **Step 15.2: Implementation**

Append to `station_agent/ota.py`:

```python
import bz2

_STREAM_CHUNK = 1 << 20  # 1 MiB


def _stream_read(fh, n: int) -> bytes:
    # Indirection so tests can count reads.
    return fh.read(n)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def install_to_slot(wic_bz2_path, partition_device: str) -> None:
    """Stream-decompress a .wic.bz2 into a block device.

    `partition_device` is e.g. "/dev/sda4". The caller is responsible
    for making sure this is the inactive slot — typically derived via
    bootloader.get_inactive_slot() + a machine-specific slot→device map.

    Raises OSError on I/O failure.
    """
    decomp = bz2.BZ2Decompressor()
    with open(str(wic_bz2_path), "rb") as src:
        fd = os.open(partition_device, os.O_WRONLY | os.O_SYNC)
        try:
            while True:
                chunk = _stream_read(src, _STREAM_CHUNK)
                if not chunk:
                    tail = decomp.flush()
                    if tail:
                        _write_all(fd, tail)
                    break
                decompressed = decomp.decompress(chunk)
                if decompressed:
                    _write_all(fd, decompressed)
            os.fsync(fd)
        finally:
            os.close(fd)
```

- [ ] **Step 15.3: Run + commit**

```
.venv/bin/pytest tests/test_ota_install.py -v
```

```bash
git -C /home/pbuchegger/station-manager add station_agent tests/test_ota_install.py
git -C /home/pbuchegger/station-manager commit -m "station_agent: install_to_slot — streaming bz2 → block device"
```

---

### Task 16: Opaque download URL + resume support + version reporting

**Agent:** `gateway`

**Files:**
- Modify: `station_agent/ota.py`, `station_agent/http_client.py`, `station_agent/inventory.py`, `station_agent/agent.py`
- Modify: `tests/test_ota_install.py`

- [ ] **Step 16.1: Tests**

```python
def test_download_resumes_on_partial(tmp_path, monkeypatch):
    """When a .part file exists, the next download pass sends Range."""
    from station_agent import ota

    dest = tmp_path / "image.wic.bz2"
    partial = bytes(range(100))
    dest.write_bytes(partial)

    captured = {}
    class FakeResp:
        status_code = 206
        headers = {}
        def __init__(self, tail):
            self._tail = tail
        def iter_content(self, chunk_size):
            yield self._tail
        def close(self):
            pass

    class FakeClient:
        def request(self, method, url, **kw):
            captured["headers"] = kw.get("headers") or {}
            tail = bytes(range(100, 200))
            return FakeResp(tail)

    ok = ota.download_firmware_resumable(
        http_client=FakeClient(),
        download_url="/path",
        expected_checksum="",  # skip checksum for this unit — covered elsewhere
        dest_path=str(dest),
        resume=True,
    )
    assert ok is True
    assert dest.read_bytes() == bytes(range(200))
    assert captured["headers"].get("Range", "").startswith("bytes=100-")


def test_inventory_reports_current_version(tmp_path, monkeypatch):
    import station_agent.inventory as inv

    os_release = tmp_path / "os-release"
    os_release.write_text(
        'NAME="Poky"\n'
        'PRETTY_NAME="OE5XRX Remote Station v1-beta"\n'
        'OE5XRX_RELEASE="v1-beta"\n'
    )
    monkeypatch.setattr(inv, "_OS_RELEASE_PATH", str(os_release))
    assert inv.get_current_version() == "v1-beta"
```

- [ ] **Step 16.2: http_client and ota changes**

Add a helper in `ota.py` that supports resume:

```python
def download_firmware_resumable(
    http_client,
    download_url: str,
    expected_checksum: str,
    dest_path: str,
    *,
    resume: bool = True,
) -> bool:
    """Like download_firmware, but appends to a partial dest file when present.

    download_url is used verbatim — callers must not parse or rebuild it.
    """
    import hashlib
    headers = {}
    existing_len = 0
    mode = "wb"
    if resume and os.path.exists(dest_path):
        existing_len = os.path.getsize(dest_path)
        if existing_len > 0:
            headers["Range"] = f"bytes={existing_len}-"
            mode = "ab"

    resp = http_client.request("GET", download_url, stream=True, headers=headers)
    if resp is None:
        return False

    if resp.status_code == 200:
        # Server refused the Range request — restart from zero.
        mode = "wb"
        existing_len = 0
    elif resp.status_code != 206 and resp.status_code != 200:
        logger.error("Firmware download failed: %s", resp.status_code)
        return False

    try:
        with open(dest_path, mode) as f:
            for chunk in resp.iter_content(chunk_size=_STREAM_CHUNK):
                if chunk:
                    f.write(chunk)
    finally:
        try:
            resp.close()
        except Exception:
            pass

    if expected_checksum:
        h = hashlib.sha256()
        with open(dest_path, "rb") as f:
            while True:
                chunk = f.read(_STREAM_CHUNK)
                if not chunk:
                    break
                h.update(chunk)
        if h.hexdigest() != expected_checksum:
            logger.error(
                "Checksum mismatch: expected %s got %s",
                expected_checksum, h.hexdigest(),
            )
            try:
                os.remove(dest_path)
            except OSError:
                pass
            return False
    return True
```

Ensure `http_client.request` accepts/forwards a `headers=` kwarg. If not, extend it — small change in `station_agent/http_client.py`.

- [ ] **Step 16.3: inventory**

```python
# station_agent/inventory.py — add at top
_OS_RELEASE_PATH = "/etc/os-release"


def get_current_version() -> str:
    try:
        with open(_OS_RELEASE_PATH) as f:
            for line in f:
                if line.startswith("OE5XRX_RELEASE="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return ""
```

Use it wherever the agent assembles its check-request body (see `agent.py` polling code).

- [ ] **Step 16.4: agent wire-up**

In `station_agent/agent.py` (wherever `check_for_update` is called), pass the current version:

```python
from .inventory import get_current_version

# build request body
current_version = get_current_version()
# rewrite check_for_update to POST with the body and honor the opaque download_url
```

Rewrite `check_for_update` in `ota.py` to POST:

```python
def check_for_update(config, http_client):
    current_version = get_current_version()  # imported from .inventory
    body = {"current_version": current_version}
    response = http_client.request(
        "POST", "/api/v1/deployments/check/", json_data=body
    )
    if response is None or response.status_code == 204:
        return None
    if response.status_code != 200:
        logger.warning(
            "Unexpected status from deployment check: %s", response.status_code
        )
        return None
    try:
        return response.json()
    except ValueError:
        return None
```

- [ ] **Step 16.5: Commit**

```
.venv/bin/pytest tests/test_ota_install.py -v
```

```bash
git -C /home/pbuchegger/station-manager add station_agent tests/test_ota_install.py
git -C /home/pbuchegger/station-manager commit -m "station_agent: resume-able download + opaque URL + current_version"
```

---

### Task 17: Agent review pass (no code, just tests)

**Agent:** `audit`

Run the agent-level tests + import test to confirm nothing is broken:

```
.venv/bin/pytest tests/test_ota_install.py tests/test_terminal_agent.py -v
.venv/bin/ruff check station_agent
```

No new code. Audit reports any API-shape issues between server (Tasks 7-8) and agent (Tasks 15-16). Fix inline if found, otherwise move on.

---

## Phase 5 — Plumbing + E2E

### Task 18: `IMAGE_INSTALL += "bzip2"` in linux-image

**Agent:** `forge`

Separate repo — `/home/pbuchegger/OE5XRX/linux-image`.

**Files:**
- Modify: `meta-oe5xrx-remotestation/recipes-core/images/oe5xrx-remotestation-image.bb`

- [ ] **Step 18.1: Add package**

Branch + one-line change: add `"bzip2"` to `IMAGE_INSTALL:append`.

Find the existing `IMAGE_INSTALL:append = "..."` line (or similar) and add `bzip2` to the list, or add a new append if there isn't one:

```
IMAGE_INSTALL:append = " bzip2"
```

- [ ] **Step 18.2: Commit + push + PR**

```bash
git -C /home/pbuchegger/OE5XRX/linux-image checkout -b feat/image-install-bzip2
git -C /home/pbuchegger/OE5XRX/linux-image add meta-oe5xrx-remotestation/recipes-core/images/oe5xrx-remotestation-image.bb
git -C /home/pbuchegger/OE5XRX/linux-image commit -m "image: install bzip2 for manual debug"
git -C /home/pbuchegger/OE5XRX/linux-image push -u origin feat/image-install-bzip2
gh pr create --repo OE5XRX/linux-image --title "image: install bzip2 CLI" --body "Convenience for debugging downloaded images on-device. Agent uses stdlib bz2 internally."
```

Merge + tag a new release before Task 19.

---

### Task 19: End-to-end verification + final PR

**Agent:** `general-purpose` (manual steps + PR author)

- [ ] **Step 19.1: Stage the whole branch locally**

Ensure `feat/ota-upgrade-flow-spec` has all Phase 1-4 commits + the spec. Run the full suite:

```
.venv/bin/pytest tests/ -q
.venv/bin/ruff check apps tests station_agent
.venv/bin/ruff format --check apps tests station_agent
```

All green.

- [ ] **Step 19.2: Manual E2E on the live stack**

Requires a running Proxmox VM that can reach ham.oe5xrx.org (i.e. the boot-watchdog issue from linux-image#14 is out of scope; assume the station boots cleanly).

1. Import a *second* release (e.g. v2-alpha) via `/images/`.
2. Go to `/rollouts/sequence/`, add `test-stations`, drag to top.
3. Tag the QEMU test station with `test-stations`.
4. Visit `/rollouts/upgrade/` — expect the station in the `test-stations` group, showing `v1-Gamma → v2-alpha`.
5. Click "Upgrade group". Confirm. Observe live row status flip through `downloading → installing → rebooting → committed`.
6. SSH into the VM (or serial): `cat /etc/os-release` shows `OE5XRX_RELEASE="v2-alpha"`.
7. `grub-editenv list` shows `bootcount=0`, `upgrade_available=0`.
8. Back in the dashboard: station now appears under "Up to date". Station Detail page shows v2-alpha both as "Reported OS version" and "Provisioned with (via upgrade)".

- [ ] **Step 19.3: Open PR**

```bash
gh pr create --repo OE5XRX/station-manager --base main --head feat/ota-upgrade-flow-spec \
  --title "OS-image OTA upgrade flow" \
  --body-file - <<'EOF'
## What

Implements the admin-triggered, tag-orchestrated OS-image OTA flow
designed in docs/superpowers/specs/2026-04-19-ota-upgrade-flow-design.md.

- Deployments now target ImageRelease (the unified catalog built
  during provisioning) instead of FirmwareArtifact.
- New apps/rollouts with a singleton RolloutSequence, drag-reorder
  edit page, and an Upgrade Dashboard that groups stations by the
  first matching sequence tag.
- Single-station upgrade from the station detail page.
- Agent-side install_to_slot implemented (streaming bz2 → block
  device); resumable downloads; opaque download URLs; current_version
  reported from /etc/os-release (OE5XRX_RELEASE).
- S3 downloads routed through the server with DeviceKey authentication;
  stations never see S3 credentials.

## Review

Please review with particular attention to:

- Supersession semantics (apps/deployments/supersession.py) — the
  transaction boundary must prevent a race between a station polling
  and an admin cancelling.
- The Range-Get implementation on DeploymentDownloadView — S3 backend
  behaviour varies with django-storages.
- The live WebSocket payload extensions (broadcast_deployment_status)
  and their matching dashboard-side parser.

## Ships with

OE5XRX/linux-image#<bzip2 PR>.

## Out of scope

- Module firmware OTA (future spec).
- Boot-commit watchdog (OE5XRX/linux-image#14).
- Delta transfers (interface supports, implementation later).
EOF
```

Assign reviewers + let Copilot reviewer run. Run `pr-review-toolkit:review-pr` + `security-review` on the diff. Address findings, merge.

- [ ] **Step 19.4: Post-merge**

Watch the auto-deploy. Re-run the E2E checklist one more time on the deployed build to confirm nothing regressed during the final merge commit. Close the plan by marking all Progress checkboxes above ticked.

---

## Self-review notes

**Spec coverage:**
- Data model changes (Deployment.image_release, SUPERSEDED, RolloutSequence) → Tasks 1-3
- Supersession → Task 4
- Station-to-group (first-match-wins, unassigned) → Task 5
- Upgrade-group + upgrade-station views → Task 6
- DeploymentCheckView new shape → Task 7
- DeploymentDownloadView (S3 proxy, Range) → Task 8
- Commit → Station.current_image_release → Task 9
- Dashboard view + template + live WS row updates → Tasks 10-11
- Rollout Sequence edit (drag-reorder) → Task 12
- Station detail Upgrade card → Task 13
- install_to_slot + resume + current_version → Tasks 15-16
- bzip2 in image → Task 18
- E2E → Task 19

**Type consistency:** `image_release` FK name is identical across models, views, templates, serializers, and agent response docs. `current_sequence()` helper used in Tasks 2, 5, 12. `supersede_pending_for_station` signature `(*, station, new_deployment)` matches across Tasks 4 and 6.

**Placeholders:** No TODOs, no "similar to above", no "handle errors appropriately" — every code step shows the code.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-19-ota-upgrade-flow.md`.

**Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task (types pre-selected in each task), two-stage review (atlas → audit), fix loops between.

**2. Inline Execution** — Execute here using `superpowers:executing-plans`, batched with checkpoints.

Which approach?
