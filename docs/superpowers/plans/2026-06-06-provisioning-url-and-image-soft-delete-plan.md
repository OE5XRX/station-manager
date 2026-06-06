# Provisioning-URL + ImageRelease Soft-Delete — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship two unrelated patches in one PR: (A) wire `SERVER_PUBLIC_URL` through Django settings so the provisioning worker stops baking the legacy `https://ham.oe5xrx.org` into station images, and (B) give `ImageRelease` a soft-delete (archive/restore) path so operators can hide releases that PROTECT-FK references prevent from being hard-deleted.

**Architecture:**
- Part A — 3 files, ~10 LOC of code + ~30 LOC of tests. Add `SERVER_PUBLIC_URL = os.environ.get(...)` to `config/settings/base.py`, replace the stale `getattr(... "https://ham.oe5xrx.org")` fallback in `apps/provisioning/management/commands/run_background_jobs.py` with `ImproperlyConfigured` fail-loud, regression-test the wire-up.
- Part B — soft-delete pattern that mirrors `apps/sso/models.py:AppGrant.revoked_at`. New `archived_at` field + custom Manager + `archive()`/`restore()` model methods + two new views + URL routes + template changes (button swap + filter toggle). The existing `uniq_tag_per_machine` full-unique constraint stays unchanged — there is exactly one row per `(tag, machine)` and `archived_at` toggles its visibility; the existing `update_or_create` in the import worker auto-restores by including `"archived_at": None` in its `defaults` dict.

**Tech Stack:** Django 6.0, pytest-django, Bricolage Grotesque + IBM Plex design system already in place, uv for venv. No new dependencies. CSP-compliant filter toggle uses the existing `[data-submit-on-change]` helper in `static/js/app.js`.

**Spec:** `docs/superpowers/specs/2026-06-06-provisioning-url-and-image-soft-delete-design.md`

---

## Pre-flight (do once before Task 1)

You should already be in `.worktrees/server-url-and-image-archive/` on branch `fix/provisioning-server-url-and-soft-delete-images` with a working `.venv`. Verify:

- [ ] **Step P1: confirm baseline test suite is green**

  Run: `.venv/bin/pytest tests/ -q --no-header 2>&1 | tail -3`
  Expected: `459 passed` (after PR #62 landed) or similar count, zero failures.

- [ ] **Step P2: confirm spec is committed on this branch**

  Run: `git log --oneline -1 -- docs/superpowers/specs/`
  Expected: a commit with subject starting `spec: SERVER_PUBLIC_URL settings glue`.

If either fails, do not start Task 1.

---

## Task A1: SERVER_PUBLIC_URL wired through settings — failing test

**TDD note:** Using `override_settings(SERVER_PUBLIC_URL="https://remote.oe5xrx.org")` would inject the attribute regardless of whether `config/settings/base.py` reads the env var — that hides the original bug. The **failing test we write first is the empty-string / fail-loud test**: before the fix, the worker silently bakes the empty string into `config.yml` and the job succeeds; after the fix, the worker raises `ImproperlyConfigured`, the outer try/except marks the job FAILED. The happy-path regression test is added in Task A2 after the implementation lands.

**Files:**
- Create: `tests/test_provisioning_server_url.py`

- [ ] **Step 1: write the failing test (fail-loud behaviour)**

```python
# tests/test_provisioning_server_url.py
"""Regression: provisioning bakes settings.SERVER_PUBLIC_URL into
the agent config inside the rootfs.

Before this fix the worker used getattr(settings, "SERVER_PUBLIC_URL",
"https://ham.oe5xrx.org") and settings.SERVER_PUBLIC_URL did not exist
— so every station provisioned after the 2026-05 CAX21→prod-01
migration ended up pointing at the legacy CAX21 hostname.
"""

from unittest.mock import patch

import pytest
from django.test import override_settings

from apps.images.models import ImageRelease
from apps.provisioning.management.commands.run_background_jobs import (
    _run_provisioning_job,
)
from apps.provisioning.models import ProvisioningJob


def _release_and_job(station):
    release = ImageRelease.objects.create(
        tag="v1-alpha",
        machine="qemux86-64",
        s3_key="images/v1-alpha/qemux86-64.wic.bz2",
        sha256="a" * 64,
        size_bytes=100,
    )
    return ProvisioningJob.objects.create(
        station=station,
        image_release=release,
    )


@pytest.fixture
def provisioning_stubs():
    """Stub out the heavy paths in _run_provisioning_job so the test
    only exercises the URL wire-up, not the wic-injection / S3 round-
    trip. ``inject_provisioning_files`` is captured so the test can
    assert what config_yaml it received."""
    with (
        patch(
            "apps.provisioning.management.commands.run_background_jobs.image_storage.open_stream"
        ) as open_stream,
        patch(
            "apps.provisioning.management.commands.run_background_jobs._decompress_to"
        ) as decompress,
        patch(
            "apps.provisioning.management.commands.run_background_jobs._compress_to_bytes"
        ) as compress,
        patch(
            "apps.provisioning.management.commands.run_background_jobs.guestfish.inject_provisioning_files"
        ) as inject,
        patch(
            "apps.provisioning.management.commands.run_background_jobs.image_storage.upload_bytes"
        ),
    ):
        # open_stream is used as a context manager
        open_stream.return_value.__enter__.return_value.read.side_effect = [b"", b""]
        decompress.return_value = None
        compress.return_value = b""
        yield inject


@pytest.mark.django_db
@override_settings(SERVER_PUBLIC_URL="")
def test_provisioning_fails_loud_without_server_public_url(
    station, provisioning_stubs
):
    """An empty SERVER_PUBLIC_URL must mark the ProvisioningJob FAILED
    with a clear error_message rather than producing a config.yml with
    an empty server_url field that the agent would silently fail against.

    This is the canary for the original bug: before the fix the worker
    silently baked whatever (including ``""`` or a stale fallback) into
    config.yml and the job ran to READY. After the fix the worker
    raises ImproperlyConfigured, caught by the existing outer
    try/except and turned into a clean FAILED job."""
    job = _release_and_job(station)

    _run_provisioning_job(job)
    job.refresh_from_db()

    assert job.status == ProvisioningJob.Status.FAILED
    assert "SERVER_PUBLIC_URL" in job.error_message

    # inject_provisioning_files MUST NOT have been called — we abort
    # before any wic mutation.
    assert provisioning_stubs.call_count == 0
```

- [ ] **Step 2: run the test and verify it fails**

  Run: `.venv/bin/pytest tests/test_provisioning_server_url.py::test_provisioning_fails_loud_without_server_public_url -v`
  Expected: FAIL — the current code reads `getattr(settings, "SERVER_PUBLIC_URL", "https://ham.oe5xrx.org")` which, given `override_settings(SERVER_PUBLIC_URL="")`, returns `""` (the override sets the attribute even when the value is empty). The empty string then passes through to `render_config`, the worker runs to READY, and the test's `assert job.status == FAILED` fails.

- [ ] **Step 3: do NOT commit yet — implementation comes in Task A2.**

---

## Task A2: SERVER_PUBLIC_URL settings glue + fail-loud worker

**Files:**
- Modify: `config/settings/base.py` (insert after the existing block of `os.environ.get(...)` reads, around line 240)
- Modify: `apps/provisioning/management/commands/run_background_jobs.py` (line 218)

- [ ] **Step 1: add the settings read**

  Open `config/settings/base.py`. Add this block after the `ALERT_EMAIL_ENABLED = …` line (currently line 234):

```python
# Public base URL for stations to reach this server. Baked into the
# station-agent's config.yml at provisioning time — see
# apps/provisioning/management/commands/run_background_jobs.py and
# apps/provisioning/config_render.py. Empty = provisioning fails loud
# rather than poisoning new images with a stale URL.
SERVER_PUBLIC_URL = os.environ.get("SERVER_PUBLIC_URL", "")
```

- [ ] **Step 2: replace the stale fallback in the worker**

  Open `apps/provisioning/management/commands/run_background_jobs.py`. At the top of the file there is already `from django.conf import settings`. Add this import next to the other `django.core` imports near the top of the file:

```python
from django.core.exceptions import ImproperlyConfigured
```

  Then locate `_run_provisioning_job` (around line 217). The current structure is:

```python
def _run_provisioning_job(job: ProvisioningJob) -> None:
    server_url = getattr(settings, "SERVER_PUBLIC_URL", "https://ham.oe5xrx.org")

    try:
        # ... all the work ...
    except Exception as exc:
        # marks job FAILED with exc as error_message
```

  Replace those first two lines with the version below — **the read + validation move INSIDE the try block** so the existing outer handler turns `ImproperlyConfigured` into a clean `ProvisioningJob.status = FAILED` with the message in `error_message` instead of crashing the worker thread:

```python
def _run_provisioning_job(job: ProvisioningJob) -> None:
    try:
        server_url = settings.SERVER_PUBLIC_URL
        if not server_url:
            # Fail loud rather than silently baking a placeholder/stale
            # URL into the agent config inside the rootfs. The existing
            # outer except clause turns this into a FAILED job with the
            # message visible in the operator UI.
            raise ImproperlyConfigured(
                "SERVER_PUBLIC_URL must be set — provisioning bakes it "
                "into the station-agent config inside the rootfs. Empty "
                "value would silently produce non-functional images."
            )

```

  Note: no trailing semicolon, blank line after the block. The existing `private_pem, public_b64 = DeviceKey.generate_keypair()` line follows immediately after.

- [ ] **Step 3: run the previously failing test and verify it passes**

  Run: `.venv/bin/pytest tests/test_provisioning_server_url.py::test_provisioning_fails_loud_without_server_public_url -v`
  Expected: PASS

- [ ] **Step 4: add the happy-path regression test**

  Append to `tests/test_provisioning_server_url.py`:

```python
@pytest.mark.django_db
@override_settings(SERVER_PUBLIC_URL="https://remote.oe5xrx.org")
def test_provisioning_bakes_server_public_url_from_settings(
    station, provisioning_stubs
):
    """Happy path: the configured URL ends up in the rendered config.yml.

    Note: this test passes both before and after the fix because
    override_settings injects the attribute regardless. It's kept as a
    regression test for the next time someone touches render_config
    or _run_provisioning_job — it pins the contract that the worker
    must round-trip settings.SERVER_PUBLIC_URL into the YAML."""
    job = _release_and_job(station)

    _run_provisioning_job(job)

    config_yaml = provisioning_stubs.call_args.kwargs["config_yaml"]
    assert "server_url: https://remote.oe5xrx.org" in config_yaml
    assert "ham.oe5xrx.org" not in config_yaml
```

- [ ] **Step 5: run both tests and verify both pass**

  Run: `.venv/bin/pytest tests/test_provisioning_server_url.py -v`
  Expected: 2 passed

- [ ] **Step 6: full suite still green**

  Run: `.venv/bin/pytest tests/ -q --no-header 2>&1 | tail -3`
  Expected: `461 passed` (459 baseline + 2 new), zero failures.

- [ ] **Step 7: commit**

```bash
git add config/settings/base.py apps/provisioning/management/commands/run_background_jobs.py tests/test_provisioning_server_url.py
git commit -m "$(cat <<'EOF'
fix(provisioning): wire SERVER_PUBLIC_URL through Django settings

docker-compose sets the SERVER_PUBLIC_URL env-var on the
station_manager container to https://remote.oe5xrx.org, but
config/settings/base.py never read it into Django settings. The
provisioning worker's fallback —
  getattr(settings, "SERVER_PUBLIC_URL", "https://ham.oe5xrx.org")
— therefore always returned the legacy CAX21 hostname, which
got baked into the station-agent's config.yml inside every
rootfs provisioned since the 2026-05 migration.

Fix: add the missing os.environ.get(...) line in
config/settings/base.py and replace the stale getattr fallback
with ImproperlyConfigured fail-loud. Two new regression tests
cover the happy path (URL bakes through) and the empty-string
path (worker aborts before mutating the wic).

Existing stations with the stale config must be re-provisioned
to pick up the fix — done manually per operator.
EOF
)"
```

---

## Task B1: `archived_at` field + migration

**Files:**
- Modify: `apps/images/models.py`
- Create: `apps/images/migrations/0006_imagerelease_archived_at.py` (generated)

- [ ] **Step 1: add the field**

  Open `apps/images/models.py`. Locate the `ImageRelease` model. Add the new field directly after `imported_by` (around line 37) and before the `class Meta:` block:

```python
    archived_at = models.DateTimeField(
        _("archived at"),
        null=True,
        blank=True,
        db_index=True,
        help_text=_(
            "Soft-delete timestamp. Archived releases are hidden from "
            "the default UI list but remain available for any "
            "Deployment or ProvisioningJob that still references them."
        ),
    )
```

- [ ] **Step 2: generate the migration**

  Run: `.venv/bin/python manage.py makemigrations images --name imagerelease_archived_at`
  Expected: a new file `apps/images/migrations/0006_imagerelease_archived_at.py` containing one `AddField` operation.

- [ ] **Step 3: verify the migration applies cleanly**

  Run: `.venv/bin/python manage.py migrate images --plan | tail -5`
  Expected: shows `images.0006_imagerelease_archived_at` as a pending migration.

  Run: `.venv/bin/pytest tests/ -q --no-header 2>&1 | tail -3`
  Expected: `461 passed` — existing tests still pass because the new field is nullable with a default of NULL.

- [ ] **Step 4: commit**

```bash
git add apps/images/models.py apps/images/migrations/0006_imagerelease_archived_at.py
git commit -m "feat(images): add ImageRelease.archived_at soft-delete field

Nullable timestamp + index. No constraint changes — the existing
uniq_tag_per_machine full-unique stays in place so there is
exactly one row per (tag, machine) and archived_at toggles its
visibility. uniq_latest_per_machine partial-unique on is_latest
also stays unchanged.

Field alone has no behaviour yet — follow-up commits add the
Manager, archive()/restore() helpers, views, and UI."
```

---

## Task B2: Custom Manager that hides archived rows by default

**Files:**
- Modify: `apps/images/models.py`
- Create: `tests/test_images_archive.py`

- [ ] **Step 1: write the failing test**

  Create `tests/test_images_archive.py`:

```python
"""Soft-delete (archive/restore) tests for ImageRelease.

Mirrors the AppGrant.revoked_at soft-delete pattern in apps/sso/models.py.
"""

import pytest
from django.utils import timezone

from apps.images.models import ImageRelease


def _make_release(tag="v1-alpha", machine="qemux86-64", is_latest=False, archived=False):
    rel = ImageRelease.objects.create(
        tag=tag,
        machine=machine,
        s3_key=f"images/{tag}/{machine}.wic.bz2",
        sha256="a" * 64,
        size_bytes=100,
        is_latest=is_latest,
    )
    if archived:
        rel.archived_at = timezone.now()
        rel.is_latest = False  # see archive() — never combine archived + latest
        rel.save(update_fields=["archived_at", "is_latest"])
    return rel


@pytest.mark.django_db
def test_default_manager_hides_archived():
    active = _make_release(tag="v1-active")
    _make_release(tag="v1-old", archived=True)

    pks = list(ImageRelease.objects.values_list("pk", flat=True))
    assert pks == [active.pk]


@pytest.mark.django_db
def test_all_objects_manager_returns_everything():
    active = _make_release(tag="v1-active")
    archived = _make_release(tag="v1-old", archived=True)

    pks = set(ImageRelease.all_objects.values_list("pk", flat=True))
    assert pks == {active.pk, archived.pk}
```

- [ ] **Step 2: run the test and verify it fails**

  Run: `.venv/bin/pytest tests/test_images_archive.py::test_default_manager_hides_archived -v`
  Expected: FAIL — currently `ImageRelease.objects` returns both rows because there's no Manager filtering.

  Run: `.venv/bin/pytest tests/test_images_archive.py::test_all_objects_manager_returns_everything -v`
  Expected: FAIL — `ImageRelease.all_objects` doesn't exist yet (`AttributeError`).

- [ ] **Step 3: implement the manager**

  Open `apps/images/models.py`. Above the `class ImageRelease` declaration, add:

```python
class ImageReleaseManager(models.Manager):
    """Default manager: hides archived (soft-deleted) rows.

    Use ``ImageRelease.all_objects`` to get the full set (incl.
    archived) — e.g. the "Show archived" UI toggle, auto-restore
    lookups during re-import, Django admin.
    """

    def get_queryset(self):
        return super().get_queryset().filter(archived_at__isnull=True)
```

  Inside the `ImageRelease` model, just before the `class Meta:` block, add:

```python
    objects = ImageReleaseManager()
    all_objects = models.Manager()
```

- [ ] **Step 4: run the tests and verify they pass**

  Run: `.venv/bin/pytest tests/test_images_archive.py -v`
  Expected: 2 passed

- [ ] **Step 5: verify foreign-key resolution is not affected**

  FK lookups go through the DB directly, not through the default manager — so a `Deployment.image_release` pointing at an archived release still resolves. Verify via the existing deployment tests:

  Run: `.venv/bin/pytest tests/ -k "deployment" -q --no-header 2>&1 | tail -3`
  Expected: all green.

- [ ] **Step 6: commit**

```bash
git add apps/images/models.py tests/test_images_archive.py
git commit -m "feat(images): default-manager hides archived ImageRelease rows

Adds ImageReleaseManager whose get_queryset() filters
archived_at__isnull=True. Mirrors AppGrant's soft-delete
convention in apps/sso/models.py.

ImageRelease.objects → active rows only (UI list, KPI counts).
ImageRelease.all_objects → everything (admin, archived-toggle,
auto-restore lookup).

FK references on Deployment / ProvisioningJob / Station continue
to resolve regardless of archived_at because Django's FK lookup
goes through the DB directly and ignores the default manager."
```

---

## Task B3: `archive()` / `restore()` model methods

**Files:**
- Modify: `apps/images/models.py`
- Modify: `tests/test_images_archive.py`

- [ ] **Step 1: write the failing tests**

  Append to `tests/test_images_archive.py`:

```python
@pytest.mark.django_db
def test_archive_sets_archived_at_and_clears_is_latest():
    rel = _make_release(tag="v1-current", is_latest=True)

    rel.archive()
    rel.refresh_from_db()

    assert rel.archived_at is not None
    assert rel.is_latest is False


@pytest.mark.django_db
def test_archive_is_idempotent():
    rel = _make_release(tag="v1-current", archived=True)
    first_ts = rel.archived_at

    rel.archive()
    rel.refresh_from_db()

    # archive() on an already-archived row must not bump the timestamp
    # — restoring then re-archiving should be the only path to a new ts.
    assert rel.archived_at == first_ts


@pytest.mark.django_db
def test_restore_clears_archived_at():
    rel = _make_release(tag="v1-old", archived=True)

    rel.restore()
    rel.refresh_from_db()

    assert rel.archived_at is None
    # restore() must NOT silently re-promote to latest — that's a
    # separate operator action.
    assert rel.is_latest is False


@pytest.mark.django_db
def test_restore_is_idempotent_on_active_row():
    rel = _make_release(tag="v1-current")

    rel.restore()
    rel.refresh_from_db()

    assert rel.archived_at is None
```

- [ ] **Step 2: run the tests and verify they fail**

  Run: `.venv/bin/pytest tests/test_images_archive.py -v -k "archive or restore"`
  Expected: FAIL — methods `archive()` / `restore()` don't exist yet (`AttributeError`).

- [ ] **Step 3: implement the methods**

  Open `apps/images/models.py`. Add to the top:

```python
from django.utils import timezone
```

  (only if not already imported). Then, inside the `ImageRelease` model, append these methods after the existing `save()` override (the method that handles the `is_latest` uniqueness):

```python
    def archive(self):
        """Soft-delete this release. Idempotent.

        Atomically stamps ``archived_at`` and clears ``is_latest`` —
        a "latest archived" row would be semantically nonsensical and
        would also stop the partial unique index from doing useful
        work on the next ``mark_as_latest`` operation.
        """
        if self.archived_at is not None:
            return  # idempotent: don't bump the timestamp

        with transaction.atomic():
            self.archived_at = timezone.now()
            update_fields = ["archived_at"]
            if self.is_latest:
                self.is_latest = False
                update_fields.append("is_latest")
            self.save(update_fields=update_fields)

    def restore(self):
        """Undo a previous archive. Idempotent.

        Does NOT touch ``is_latest`` — re-promotion to "latest" after
        a restore is an explicit operator action via the existing
        Mark-Latest button. Restoring a previously-archived release
        must not silently steal the latest bit from whatever is
        currently active.
        """
        if self.archived_at is None:
            return  # idempotent

        self.archived_at = None
        self.save(update_fields=["archived_at"])
```

  Ensure `transaction` is imported at the top of the file (it likely already is, since `save()` uses `transaction.atomic()`):

```python
from django.db import models, transaction
```

- [ ] **Step 4: run the tests and verify they pass**

  Run: `.venv/bin/pytest tests/test_images_archive.py -v`
  Expected: all tests pass (default-manager, all_objects, archive/restore).

- [ ] **Step 5: commit**

```bash
git add apps/images/models.py tests/test_images_archive.py
git commit -m "feat(images): add ImageRelease.archive() / restore() methods

archive() atomically stamps archived_at + clears is_latest so a
'latest archived' row can never exist (would break the partial
unique index on the next mark-latest).

restore() flips archived_at back to NULL but deliberately leaves
is_latest=False — re-promotion after restore is an explicit
operator action via the existing Mark-Latest button.

Both methods are idempotent on repeat calls."
```

---

## Task B4: `ImageArchiveView` — view + URL + test

**Files:**
- Modify: `apps/images/views.py`
- Modify: `apps/images/urls.py`
- Modify: `tests/test_images_archive.py`

- [ ] **Step 1: write the failing tests**

  Append to `tests/test_images_archive.py`:

```python
from django.urls import reverse


@pytest.mark.django_db
def test_archive_view_archives_release(client, admin_user):
    rel = _make_release(tag="v1-current", is_latest=True)

    client.force_login(admin_user)
    resp = client.post(reverse("images:archive", kwargs={"pk": rel.pk}))

    assert resp.status_code == 302
    rel.refresh_from_db()
    assert rel.archived_at is not None
    assert rel.is_latest is False


@pytest.mark.django_db
def test_archive_view_requires_admin(client, member_user):
    rel = _make_release(tag="v1-current")

    client.force_login(member_user)
    resp = client.post(reverse("images:archive", kwargs={"pk": rel.pk}))

    # AdminRequiredMixin raises PermissionDenied for authenticated
    # non-admins → 403.
    assert resp.status_code == 403
    rel.refresh_from_db()
    assert rel.archived_at is None


@pytest.mark.django_db
def test_archive_view_404_on_unknown_pk(client, admin_user):
    client.force_login(admin_user)
    resp = client.post(reverse("images:archive", kwargs={"pk": 99999}))

    assert resp.status_code == 404


@pytest.mark.django_db
def test_archive_view_works_on_release_with_referenced_deployment(
    client, admin_user, station
):
    """The whole point of archive vs hard-delete: archive succeeds
    even when Deployment/ProvisioningJob FKs would PROTECT a delete."""
    from apps.deployments.models import Deployment

    rel = _make_release(tag="v1-shipped")
    Deployment.objects.create(
        image_release=rel,
        target_type=Deployment.TargetType.ALL,
        created_by=admin_user,
    )

    client.force_login(admin_user)
    resp = client.post(reverse("images:archive", kwargs={"pk": rel.pk}))

    assert resp.status_code == 302
    rel.refresh_from_db()
    assert rel.archived_at is not None
```

- [ ] **Step 2: run the tests and verify they fail**

  Run: `.venv/bin/pytest tests/test_images_archive.py -v -k "archive_view"`
  Expected: FAIL with `NoReverseMatch` — the URL `images:archive` is not registered.

- [ ] **Step 3: implement the view**

  Open `apps/images/views.py`. At the bottom of the file (after `_delete_blockers`), add:

```python
class ImageArchiveView(AdminRequiredMixin, View):
    """Soft-delete a release. Always succeeds (vs hard delete which
    PROTECT-FKs from Deployment/ProvisioningJob can block).

    Operates on ``all_objects`` so the same view also accepts an
    already-archived row (idempotent) — but the UI only renders the
    Archive button for active rows, so in practice this is the
    active-row entry path.
    """

    def post(self, request, pk):
        release = get_object_or_404(ImageRelease.all_objects, pk=pk)
        release.archive()
        messages.success(
            request,
            _("Release %(tag)s archived.") % {"tag": release.tag},
        )
        return redirect("images:list")
```

- [ ] **Step 4: register the URL**

  Open `apps/images/urls.py`. Add this entry inside `urlpatterns`, right after the `delete/` line:

```python
    path("<int:pk>/archive/", views.ImageArchiveView.as_view(), name="archive"),
```

- [ ] **Step 5: run the tests and verify they pass**

  Run: `.venv/bin/pytest tests/test_images_archive.py -v -k "archive_view"`
  Expected: 4 tests pass.

- [ ] **Step 6: commit**

```bash
git add apps/images/views.py apps/images/urls.py tests/test_images_archive.py
git commit -m "feat(images): ImageArchiveView + images:archive URL

POST /images/<pk>/archive/ — admin-only, soft-deletes the
release via ImageRelease.archive(), redirects with success flash.
Always succeeds regardless of Deployment/ProvisioningJob FK
references — that's the point of soft-delete over the hard
ImageDeleteView path (which stays hardened-but-PROTECT-blocked
in this PR's scope)."
```

---

## Task B5: `ImageRestoreView` — view + URL + test

**Files:**
- Modify: `apps/images/views.py`
- Modify: `apps/images/urls.py`
- Modify: `tests/test_images_archive.py`

- [ ] **Step 1: write the failing tests**

  Append to `tests/test_images_archive.py`:

```python
@pytest.mark.django_db
def test_restore_view_restores_release(client, admin_user):
    rel = _make_release(tag="v1-old", archived=True)

    client.force_login(admin_user)
    resp = client.post(reverse("images:restore", kwargs={"pk": rel.pk}))

    assert resp.status_code == 302
    rel.refresh_from_db()
    assert rel.archived_at is None
    # restore() must NOT auto-promote to latest
    assert rel.is_latest is False


@pytest.mark.django_db
def test_restore_view_requires_admin(client, member_user):
    rel = _make_release(tag="v1-old", archived=True)

    client.force_login(member_user)
    resp = client.post(reverse("images:restore", kwargs={"pk": rel.pk}))

    assert resp.status_code == 403


@pytest.mark.django_db
def test_restore_view_404_on_unknown_pk(client, admin_user):
    client.force_login(admin_user)
    resp = client.post(reverse("images:restore", kwargs={"pk": 99999}))

    assert resp.status_code == 404
```

- [ ] **Step 2: run the tests and verify they fail**

  Run: `.venv/bin/pytest tests/test_images_archive.py -v -k "restore_view"`
  Expected: FAIL with `NoReverseMatch`.

- [ ] **Step 3: implement the view**

  Add to `apps/images/views.py` after `ImageArchiveView`:

```python
class ImageRestoreView(AdminRequiredMixin, View):
    """Undo a previous archive. Operates on ``all_objects`` because
    archived rows are hidden from the default manager."""

    def post(self, request, pk):
        release = get_object_or_404(ImageRelease.all_objects, pk=pk)
        release.restore()
        messages.success(
            request,
            _("Release %(tag)s restored.") % {"tag": release.tag},
        )
        return redirect("images:list")
```

- [ ] **Step 4: register the URL**

  In `apps/images/urls.py`, add after the `archive/` line:

```python
    path("<int:pk>/restore/", views.ImageRestoreView.as_view(), name="restore"),
```

- [ ] **Step 5: run the tests and verify they pass**

  Run: `.venv/bin/pytest tests/test_images_archive.py -v -k "restore_view"`
  Expected: 3 tests pass.

- [ ] **Step 6: commit**

```bash
git add apps/images/views.py apps/images/urls.py tests/test_images_archive.py
git commit -m "feat(images): ImageRestoreView + images:restore URL

POST /images/<pk>/restore/ — admin-only, sets archived_at = NULL
via ImageRelease.restore(). Does NOT re-promote to is_latest;
operator uses the existing Mark-Latest button if they want that."
```

---

## Task B6: Auto-restore on re-import

**Files:**
- Modify: `apps/provisioning/management/commands/run_background_jobs.py` (the `_run_import_job` function — *not* the provisioning function from Task A2)
- Modify: `tests/test_images_archive.py`

- [ ] **Step 1: write the failing test**

  Append to `tests/test_images_archive.py`:

```python
from unittest.mock import patch

from apps.images.models import ImageImportJob


@pytest.fixture
def import_stubs():
    """Stub the heavy paths in _run_import_job so we exercise only
    the DB write — no GitHub fetch, no cosign, no S3, no rootfs
    extraction. Each fake return is just enough for the worker to
    reach update_or_create."""
    fake_asset = type(
        "FakeAsset",
        (),
        {
            "wic_bytes": b"",
            "bundle_bytes": b"",
            "sha256": "b" * 64,
        },
    )()
    with (
        patch(
            "apps.provisioning.management.commands.run_background_jobs.github.fetch_release_asset",
            return_value=fake_asset,
        ),
        patch(
            "apps.provisioning.management.commands.run_background_jobs.cosign.verify_blob"
        ),
        patch(
            "apps.provisioning.management.commands.run_background_jobs.image_storage.upload_bytes"
        ),
        patch(
            "apps.provisioning.management.commands.run_background_jobs._decompress_to"
        ),
        patch(
            "apps.provisioning.management.commands.run_background_jobs.extraction.extract_rootfs",
            return_value=(0, "c" * 64),
        ),
    ):
        yield


@pytest.mark.django_db
def test_reimport_auto_restores_archived_release(import_stubs, admin_user):
    from apps.provisioning.management.commands.run_background_jobs import (
        _run_import_job,
    )

    archived = _make_release(tag="v1-alpha", archived=True)
    archived_pk = archived.pk

    job = ImageImportJob.objects.create(
        tag="v1-alpha",
        machine="qemux86-64",
        mark_as_latest=False,
        requested_by=admin_user,
    )
    _run_import_job(job)

    # update_or_create returned the SAME row (full-unique on
    # tag/machine guarantees that), with archived_at cleared.
    archived.refresh_from_db()
    assert archived.pk == archived_pk  # not a brand-new row
    assert archived.archived_at is None
```

- [ ] **Step 2: run the test and verify it fails**

  Run: `.venv/bin/pytest tests/test_images_archive.py::test_reimport_auto_restores_archived_release -v`
  Expected: FAIL — currently `update_or_create(... defaults={...})` does NOT include `archived_at`, so the archived flag stays set after re-import.

- [ ] **Step 3: implement the auto-restore**

  Open `apps/provisioning/management/commands/run_background_jobs.py`. In `_run_import_job` (around line 130-140), locate the `update_or_create` call's `defaults` dict and add the `archived_at` key:

```python
            release, _created = ImageRelease.objects.update_or_create(
                tag=job.tag,
                machine=job.machine,
                defaults={
                    "s3_key": wic_key,
                    "cosign_bundle_s3_key": bundle_key,
                    "sha256": asset.sha256,
                    "size_bytes": len(asset.wic_bytes),
                    "rootfs_s3_key": rootfs_key,
                    "rootfs_sha256": rootfs_sha,
                    "rootfs_size_bytes": rootfs_size,
                    "is_latest": job.mark_as_latest,
                    "imported_by": job.requested_by,
                    # Re-importing a previously-archived release auto-
                    # restores it — keeps audit trail intact while
                    # giving the operator the natural "I want this
                    # back" path. Active-row updates are a no-op
                    # (the field was already NULL).
                    "archived_at": None,
                },
            )
```

  **CRITICAL: the queryset on the line `ImageRelease.objects.update_or_create(...)` uses the default manager which filters archived rows OUT.** That means the archived row will not be found via `objects` and `update_or_create` would attempt to CREATE — but the `uniq_tag_per_machine` full-unique constraint then errors. Two-step fix:

  Change the call to use the `all_objects` manager:

```python
            release, _created = ImageRelease.all_objects.update_or_create(
                tag=job.tag,
                machine=job.machine,
                defaults={
```

  Same `defaults` block (now including `archived_at: None`). The result `release` is a regular model instance — subsequent `job.image_release = release` works regardless of which manager you went through.

- [ ] **Step 4: run the test and verify it passes**

  Run: `.venv/bin/pytest tests/test_images_archive.py::test_reimport_auto_restores_archived_release -v`
  Expected: PASS

- [ ] **Step 5: full suite still green**

  Run: `.venv/bin/pytest tests/ -q --no-header 2>&1 | tail -3`
  Expected: all green, current count + new tests.

- [ ] **Step 6: commit**

```bash
git add apps/provisioning/management/commands/run_background_jobs.py tests/test_images_archive.py
git commit -m "feat(images): re-import auto-restores archived release

ImageImportJob worker uses update_or_create on
(tag, machine) — adding archived_at=None to the defaults dict
gives the operator the natural 'I want this back' path: archive
v1-alpha, decide later you need it, re-import from GitHub, the
existing row gets resurrected with the (possibly updated) S3
key + SHA + size in place.

Switched the manager to all_objects on this code path because
the default manager hides archived rows — without that
update_or_create would never find the archived row to update
and would try to CREATE, hitting the uniq_tag_per_machine
constraint."
```

---

## Task B7: Image-list view — honour `?show_archived=1` toggle

**Files:**
- Modify: `apps/images/views.py`
- Modify: `tests/test_images_archive.py`

- [ ] **Step 1: write the failing tests**

  Append to `tests/test_images_archive.py`:

```python
@pytest.mark.django_db
def test_image_list_hides_archived_by_default(client, admin_user):
    active = _make_release(tag="v1-active")
    _make_release(tag="v1-old", archived=True)

    client.force_login(admin_user)
    resp = client.get(reverse("images:list"))

    assert resp.status_code == 200
    body = resp.content.decode()
    assert "v1-active" in body
    assert "v1-old" not in body


@pytest.mark.django_db
def test_image_list_shows_archived_with_query_param(client, admin_user):
    _make_release(tag="v1-active")
    _make_release(tag="v1-old", archived=True)

    client.force_login(admin_user)
    resp = client.get(reverse("images:list") + "?show_archived=1")

    assert resp.status_code == 200
    body = resp.content.decode()
    assert "v1-active" in body
    assert "v1-old" in body
```

- [ ] **Step 2: run the tests and verify they fail**

  Run: `.venv/bin/pytest tests/test_images_archive.py -v -k "image_list"`
  Expected: first test PASSes (default manager already hides archived), second test FAILs (no query-param handling yet).

- [ ] **Step 3: implement the toggle**

  Open `apps/images/views.py`. Replace the `ImageListView` class body with:

```python
class ImageListView(AdminRequiredMixin, ListView):
    model = ImageRelease
    template_name = "images/image_list.html"
    context_object_name = "releases"

    def _show_archived(self) -> bool:
        return self.request.GET.get("show_archived") == "1"

    def get_queryset(self):
        # all_objects when the toggle is on so archived rows appear
        # alongside active; default manager (objects) otherwise.
        manager = ImageRelease.all_objects if self._show_archived() else ImageRelease.objects
        return manager.all()

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["import_form"] = ImageImportForm()
        ctx["recent_jobs"] = ImageImportJob.objects.order_by("-created_at")[:10]
        # KPI tile aggregates — always over the ACTIVE set (the default
        # manager) regardless of the show_archived toggle. KPIs should
        # describe the operational state, not the toggle's UI mode.
        ctx["latest_total"] = ImageRelease.objects.filter(is_latest=True).count()
        ctx["pending_jobs"] = ImageImportJob.objects.filter(
            status__in=[
                ImageImportJob.Status.PENDING,
                ImageImportJob.Status.RUNNING,
            ],
        ).count()
        ctx["storage_backend_label"] = _storage_backend_label()
        ctx["show_archived"] = self._show_archived()
        return ctx
```

- [ ] **Step 4: run the tests and verify they pass**

  Run: `.venv/bin/pytest tests/test_images_archive.py -v -k "image_list"`
  Expected: both tests pass.

- [ ] **Step 5: commit**

```bash
git add apps/images/views.py tests/test_images_archive.py
git commit -m "feat(images): image-list honours ?show_archived=1 toggle

Default queryset hides archived rows (via the new
ImageReleaseManager). ?show_archived=1 switches to all_objects
so archived appears alongside active for restore/inspection.

KPI tile counts (Releases on file, Latest-marked, Pending
imports) always reflect the active set — they describe
operational state, not the toggle's UI mode."
```

---

## Task B8: Template — swap Delete button for Archive + add toggle + render archived rows

**Files:**
- Modify: `apps/images/templates/images/image_list.html`

The template currently has (around line 162-180) the delete-button form. After the changes it must have: an archive button on active rows, a restore button on archived rows, a "Show archived" toggle in a filter-bar above the panel, and visual distinction for archived rows.

- [ ] **Step 1: replace the delete button with an archive button**

  Open `apps/images/templates/images/image_list.html`. Find the form at the end of each release row (the one with `action="{% url 'images:delete' rel.pk %}"`). Replace **the entire `<td class="actions">…</td>`** block with:

```html
          <td class="actions">
            {% if rel.archived_at %}
              <form method="post"
                    action="{% url 'images:restore' rel.pk %}"
                    style="display:inline;margin:0;">
                {% csrf_token %}
                <button type="submit" class="btn btn-sm btn-ghost" title="{% trans 'Restore release' %}">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                    <path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5h5"/>
                  </svg>
                  <span class="visually-hidden">{% trans "Restore" %}</span>
                </button>
              </form>
            {% else %}
              <form method="post"
                    action="{% url 'images:archive' rel.pk %}"
                    style="display:inline;margin:0;"
                    data-confirm="{% trans 'Archive this release? It stays available to any deployment that references it, but is hidden from the list.' %}">
                {% csrf_token %}
                <button type="submit" class="btn btn-sm btn-ghost" title="{% trans 'Archive release' %}">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                    <rect x="3" y="4" width="18" height="4" rx="1"/><path d="M5 8v11a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8"/><path d="M10 12h4"/>
                  </svg>
                  <span class="visually-hidden">{% trans "Archive" %}</span>
                </button>
              </form>
            {% endif %}
          </td>
```

  Note: the icon for archive is an inbox/archive-box; restore is a curved-arrow refresh icon. Both inherit `currentColor` so the existing `btn-ghost` palette applies.

- [ ] **Step 2: add the "Show archived" toggle above the releases panel**

  Find the line just before `<section class="panel mb-24">` (the "Imported images" panel). Insert above it:

```html
<form method="get" class="filter-bar mb-14" style="justify-content:flex-end;">
  <label class="row-gap-8" style="cursor:pointer;font-size:13px;color:var(--ink-1);">
    <input type="checkbox"
           name="show_archived"
           value="1"
           data-submit-on-change
           {% if show_archived %}checked{% endif %}>
    <span>{% trans "Show archived" %}</span>
  </label>
</form>
```

  The `data-submit-on-change` attribute is picked up by the existing delegated listener in `static/js/app.js` (line ~366) which calls `form.requestSubmit()` on change — CSP-safe, no inline JS.

- [ ] **Step 3: visually distinguish archived rows**

  In the row template (the `<tr>` for `{% for rel in releases %}`), add a class:

```html
        <tr {% if rel.archived_at %}class="is-archived"{% endif %}>
```

  In the "Imported" column cell, append the archived timestamp when set:

```html
          <td class="t-mono-sm t-muted" data-label="{% trans 'Imported' %}">
            {{ rel.imported_at|date:"Y-m-d H:i" }}
            {% if rel.archived_at %}
              <div style="color:var(--ink-3);font-size:10.5px;letter-spacing:0.08em;">
                {% trans "archived" %} {{ rel.archived_at|date:"Y-m-d" }}
              </div>
            {% endif %}
          </td>
```

  Add the muted-row CSS to `static/css/app.css`, appended at the bottom:

```css
/* Archived ImageRelease rows in the Image Releases table */
.t-table tr.is-archived td { opacity: 0.55; }
.t-table tr.is-archived td .frame-id { color: var(--ink-3); }
```

- [ ] **Step 4: verify the template renders cleanly**

  Run:
  ```bash
  .venv/bin/python -c "
  import os, django
  os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.test')
  django.setup()
  from django.template.loader import get_template
  get_template('images/image_list.html')
  print('OK')
  "
  ```
  Expected: `OK`

- [ ] **Step 5: full test suite still green**

  Run: `.venv/bin/pytest tests/ -q --no-header 2>&1 | tail -3`
  Expected: all tests pass.

- [ ] **Step 6: commit**

```bash
git add apps/images/templates/images/image_list.html static/css/app.css
git commit -m "feat(images): UI for archive/restore + show-archived toggle

image_list.html:
- Row action: trash → archive (inbox icon, data-confirm
  reworded). Archived rows show a Restore action instead
  (curved-arrow icon, no confirm needed).
- New filter-bar above the releases panel: checkbox
  'Show archived' with [data-submit-on-change] so the form
  auto-submits via the existing CSP-safe delegated listener in
  static/js/app.js.
- Archived rows render with class is-archived (muted opacity)
  and show the archived-at date below the imported-at timestamp.

app.css: .t-table tr.is-archived rule for the muted look."
```

---

## Task B9: Integration tests for archive workflow

**Files:**
- Modify: `tests/test_images_archive.py`

- [ ] **Step 1: write the integration test**

  Append to `tests/test_images_archive.py`:

```python
@pytest.mark.django_db
def test_archive_then_reimport_workflow(import_stubs, admin_user, client):
    """End-to-end: operator archives a release, later re-imports
    the same tag, observes the row is restored with updated fields
    and surfaces in the default (non-archived) UI list again."""
    rel = _make_release(tag="v1-alpha")
    rel_pk = rel.pk

    # Step 1: operator archives via the UI
    client.force_login(admin_user)
    resp = client.post(reverse("images:archive", kwargs={"pk": rel_pk}))
    assert resp.status_code == 302
    rel.refresh_from_db()
    assert rel.archived_at is not None

    # Default list view no longer shows v1-alpha
    resp = client.get(reverse("images:list"))
    assert b"v1-alpha" not in resp.content

    # Step 2: re-import the same tag from "GitHub"
    from apps.images.models import ImageImportJob
    from apps.provisioning.management.commands.run_background_jobs import (
        _run_import_job,
    )

    job = ImageImportJob.objects.create(
        tag="v1-alpha",
        machine="qemux86-64",
        mark_as_latest=False,
        requested_by=admin_user,
    )
    _run_import_job(job)

    # The very same row (same pk) is back to active
    rel.refresh_from_db()
    assert rel.pk == rel_pk
    assert rel.archived_at is None

    # And it surfaces in the default list view
    resp = client.get(reverse("images:list"))
    assert b"v1-alpha" in resp.content
```

- [ ] **Step 2: run the test**

  Run: `.venv/bin/pytest tests/test_images_archive.py::test_archive_then_reimport_workflow -v`
  Expected: PASS

- [ ] **Step 3: full suite green**

  Run: `.venv/bin/pytest tests/ -q --no-header 2>&1 | tail -3`
  Expected: all green.

- [ ] **Step 4: commit**

```bash
git add tests/test_images_archive.py
git commit -m "test(images): end-to-end archive → reimport → restore workflow

Walks the full operator path: archive via UI, confirm hidden
from default list, reimport same tag, confirm the existing row
restored (same pk, archived_at NULL) and reappears in the
default list."
```

---

## Task PR1: PR-ready check + push

- [ ] **Step 1: lint & format**

  Run: `.venv/bin/ruff check apps/ config/ tests/ 2>&1 | tail -5`
  Expected: `All checks passed!`

- [ ] **Step 2: full test suite green**

  Run: `.venv/bin/pytest tests/ -q --no-header 2>&1 | tail -3`
  Expected: all green, expected count = 459 baseline + 19 new across A2 / B2 / B3 / B4 / B5 / B6 / B7 / B9 = **478 passed**.

- [ ] **Step 3: review the commit graph**

  Run: `git log --oneline main..HEAD`
  Expected, in order (oldest at bottom):
  1. `spec: SERVER_PUBLIC_URL settings glue + ImageRelease soft-delete`
  2. `fix(provisioning): wire SERVER_PUBLIC_URL through Django settings`
  3. `feat(images): add ImageRelease.archived_at soft-delete field`
  4. `feat(images): default-manager hides archived ImageRelease rows`
  5. `feat(images): add ImageRelease.archive() / restore() methods`
  6. `feat(images): ImageArchiveView + images:archive URL`
  7. `feat(images): ImageRestoreView + images:restore URL`
  8. `feat(images): re-import auto-restores archived release`
  9. `feat(images): image-list honours ?show_archived=1 toggle`
  10. `feat(images): UI for archive/restore + show-archived toggle`
  11. `test(images): end-to-end archive → reimport → restore workflow`

- [ ] **Step 4: push and open PR**

```bash
git push -u origin fix/provisioning-server-url-and-soft-delete-images
gh pr create --title "fix(provisioning): SERVER_PUBLIC_URL settings glue + soft-delete ImageRelease" --body "$(cat <<'EOF'
## Summary

Two unrelated patches bundled per operator request, separate commits per change:

**Part A — SERVER_PUBLIC_URL settings glue** (`fix(provisioning): wire SERVER_PUBLIC_URL through Django settings`)
Root cause: docker-compose sets `SERVER_PUBLIC_URL=https://remote.oe5xrx.org` on the container, `config/settings/base.py` never read it into Django settings, so the provisioning worker's `getattr(settings, "SERVER_PUBLIC_URL", "https://ham.oe5xrx.org")` always fell back to the legacy CAX21 hostname. Every station provisioned since the 2026-05 migration ended up with the old URL baked into its agent config. Fixed by adding the missing `os.environ.get(...)` line and replacing the stale `getattr` fallback with `ImproperlyConfigured` fail-loud.

**Part B — ImageRelease soft-delete (archive/restore)**
After PR #62 hardened the hard-delete path it now correctly refuses to delete releases referenced by `Deployment` / `ProvisioningJob`. Operators still want an "out of the list" path that preserves referential integrity — mirrors the existing `AppGrant.revoked_at` soft-delete pattern. Adds `archived_at` field + custom Manager + `archive()` / `restore()` methods + `ImageArchiveView` / `ImageRestoreView` + UI toggle + auto-restore on re-import.

## Test plan

- [x] `pytest tests/` green (~475 tests after additions)
- [x] `ruff check` clean
- [ ] Manually verify: archive a release with a deployment → row disappears from list, deployment still references it
- [ ] Manually verify: re-import the archived tag → row restored, surfaces in default list
- [ ] Manually verify: ?show_archived=1 toggle shows archived rows with Restore button
- [ ] After merge + image build + deploy: re-provision a Proxmox station; verify the rendered `config.yml` shows `server_url: https://remote.oe5xrx.org`

## Spec & Plan

- Spec: [`docs/superpowers/specs/2026-06-06-provisioning-url-and-image-soft-delete-design.md`](docs/superpowers/specs/2026-06-06-provisioning-url-and-image-soft-delete-design.md)
- Plan: [`docs/superpowers/plans/2026-06-06-provisioning-url-and-image-soft-delete-plan.md`](docs/superpowers/plans/2026-06-06-provisioning-url-and-image-soft-delete-plan.md)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

  Expected: PR URL printed.

---
