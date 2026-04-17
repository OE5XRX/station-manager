# Server-side Station Provisioning — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a web-UI flow in station-manager that (1) imports signed linux-image releases into Hetzner S3 and (2) generates per-station, identity-baked `.wic.bz2` images for one-time download. Admin-only. No OTA.

**Architecture:** Two new Django apps (`images`, `provisioning`) plus one new docker-compose worker that polls both queues. Image manipulation via `guestfish` (libguestfs). Download via Django streaming proxy from S3 with post-stream cleanup. Release import via a GitHub + cosign verification pipeline.

**Tech Stack:** Django 6.0, django-storages[s3] (already wired), `cryptography` (Ed25519, already used), `libguestfs-tools` (new), `cosign` static binary (new), HTMX (already used for polling).

**Spec:** `docs/superpowers/specs/2026-04-17-server-side-provisioning-design.md`

**Branch:** `feat/server-side-provisioning-spec` (keep plan + code on the same branch).

## Progress

- [x] Task 1 — Scaffold `images` app + `ImageRelease` (commits `011832e`, `f39c8a8`)
- [x] Task 2 — `ImageImportJob` model (commits `646378a`, `b0ad778`)
- [x] Task 3 — github/cosign/storage helpers (commit `de1a9f0`)
- [x] Task 4 — Image import view + form (commit `2c68d79`)
- [x] Task 5 — Image importer worker (commit `a40d619`)
- [ ] Task 6 — Mark-latest + delete actions
- [ ] Task 7 — `ProvisioningJob` model
- [ ] Task 8 — guestfish + config_render helpers
- [ ] Task 9 — Provisioning worker — full pipeline
- [ ] Task 10 — Provisioning views — create, status, download
- [ ] Task 11 — Station-detail provisioning section
- [ ] Task 12 — Sidebar "Images" entry
- [ ] Task 13 — Dockerfile — libguestfs + cosign
- [ ] Task 14 — docker-compose background-worker service
- [ ] Task 15 — E2E verification + final PR

---

## File Structure

### New files

```
apps/images/
    __init__.py
    apps.py
    models.py                                # ImageRelease, ImageImportJob
    admin.py
    forms.py                                 # ImportForm
    views.py                                 # list, import, import-status, mark-latest, delete
    urls.py
    github.py                                # download release assets via gh API
    cosign.py                                # verify-blob wrapper
    storage.py                               # S3 key helpers + default_storage wrappers
    migrations/__init__.py
    migrations/0001_initial.py
    templates/images/image_list.html
    templates/images/_import_form.html
    templates/images/_import_job_status.html

apps/provisioning/
    __init__.py
    apps.py
    models.py                                # ProvisioningJob
    admin.py
    forms.py                                 # ProvisioningForm
    views.py                                 # create, status, download, cancel
    urls.py
    config_render.py                         # render config.yml for a station
    guestfish.py                             # inject files into .wic via libguestfs
    migrations/__init__.py
    migrations/0001_initial.py
    templates/provisioning/_provisioning_section.html
    templates/provisioning/_job_status.html
    templates/provisioning/_install_instructions.html

apps/provisioning/management/__init__.py
apps/provisioning/management/commands/__init__.py
apps/provisioning/management/commands/run_background_jobs.py   # serves both images + provisioning queues

tests/test_images.py
tests/test_provisioning.py
tests/fixtures/tiny.wic                      # tiny pre-made 8 MB disk image, 1 partition = "data", used for guestfish tests
```

### Modified files

```
config/settings/base.py                      # add 'apps.images', 'apps.provisioning' to INSTALLED_APPS
config/urls.py                               # include images + provisioning URL confs
apps/stations/templates/stations/station_detail.html   # include provisioning section for admins
templates/includes/sidebar.html              # add "Images" entry (admin-only)
Dockerfile                                   # install libguestfs-tools + cosign binary
docker-compose.yml                           # add background-worker service
deploy/docker-compose.prod.yml               # add background-worker service
requirements/base.txt                        # no new Python deps expected (boto3 already via django-storages[s3])
.env.example                                 # document S3_IMAGES_PREFIX and LINUX_IMAGE_REPO vars
```

---

## Conventions used in this plan

- Tests live in `/tests/test_<topic>.py`; conftest fixtures are already defined (see `tests/conftest.py` for `admin_user`, `operator_user`, `member_user`, `station`, `station_with_key`).
- Every task ends with `pytest tests/test_<topic>.py -v` passing and a git commit. No other tests run between steps.
- All admin-gated views use `AdminRequiredMixin` from `apps.accounts.views` (the `audit` app already imports it from there).
- i18n: wrap user-facing strings in `gettext_lazy as _` and generate messages once at the end of the phase.
- Ed25519 generation uses `DeviceKey.generate_keypair()` — do NOT reinvent.
- New CharField choices live on the model as `TextChoices`.
- No `# type: ignore`, no `noqa`, no `TODO` left in code.

---

## Phase 1 — `images` app (release import)

### Task 1: Scaffold `images` app with `ImageRelease` model

**Files:**
- Create: `apps/images/__init__.py`, `apps/images/apps.py`, `apps/images/models.py`, `apps/images/admin.py`, `apps/images/urls.py`, `apps/images/views.py`
- Create: `apps/images/migrations/__init__.py`
- Create: `tests/test_images.py`
- Modify: `config/settings/base.py` (INSTALLED_APPS)
- Modify: `config/urls.py` (include images.urls)

- [ ] **Step 1.1: Write failing test for `ImageRelease.is_latest` mutex**

```python
# tests/test_images.py
import pytest
from django.db import IntegrityError

from apps.images.models import ImageRelease


@pytest.mark.django_db
class TestImageRelease:
    def test_mark_latest_flips_previous_for_same_machine(self):
        old = ImageRelease.objects.create(
            tag="v0.9.0",
            machine=ImageRelease.Machine.QEMU,
            s3_key="images/v0.9.0/qemu.wic.bz2",
            sha256="a" * 64,
            size_bytes=1000,
            is_latest=True,
        )
        new = ImageRelease.objects.create(
            tag="v1-alpha",
            machine=ImageRelease.Machine.QEMU,
            s3_key="images/v1-alpha/qemu.wic.bz2",
            sha256="b" * 64,
            size_bytes=2000,
            is_latest=True,
        )
        old.refresh_from_db()
        assert old.is_latest is False
        assert new.is_latest is True

    def test_mark_latest_does_not_affect_other_machine(self):
        qemu = ImageRelease.objects.create(
            tag="v1-alpha",
            machine=ImageRelease.Machine.QEMU,
            s3_key="images/v1-alpha/qemu.wic.bz2",
            sha256="a" * 64,
            size_bytes=1000,
            is_latest=True,
        )
        rpi = ImageRelease.objects.create(
            tag="v1-alpha",
            machine=ImageRelease.Machine.RPI,
            s3_key="images/v1-alpha/rpi.wic.bz2",
            sha256="b" * 64,
            size_bytes=2000,
            is_latest=True,
        )
        qemu.refresh_from_db()
        assert qemu.is_latest is True
        assert rpi.is_latest is True

    def test_tag_machine_is_unique(self):
        ImageRelease.objects.create(
            tag="v1-alpha",
            machine=ImageRelease.Machine.QEMU,
            s3_key="images/v1-alpha/qemu.wic.bz2",
            sha256="a" * 64,
            size_bytes=1000,
        )
        with pytest.raises(IntegrityError):
            ImageRelease.objects.create(
                tag="v1-alpha",
                machine=ImageRelease.Machine.QEMU,
                s3_key="images/v1-alpha/qemu-dup.wic.bz2",
                sha256="c" * 64,
                size_bytes=3000,
            )
```

- [ ] **Step 1.2: Run test — verify it fails (app not registered)**

Run: `pytest tests/test_images.py -v`
Expected: collection error "No module named 'apps.images.models'" or similar.

- [ ] **Step 1.3: Write `apps/images/__init__.py` (empty) and `apps/images/apps.py`**

```python
# apps/images/apps.py
from django.apps import AppConfig


class ImagesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.images"
    verbose_name = "Images"
```

- [ ] **Step 1.4: Write the `ImageRelease` model**

```python
# apps/images/models.py
from django.conf import settings
from django.db import models, transaction
from django.utils.translation import gettext_lazy as _


class ImageRelease(models.Model):
    class Machine(models.TextChoices):
        QEMU = "qemux86-64", _("QEMU x86-64")
        RPI = "raspberrypi4-64", _("Raspberry Pi 4 (64-bit)")

    tag = models.CharField(_("release tag"), max_length=64)
    machine = models.CharField(_("machine"), max_length=32, choices=Machine.choices)
    s3_key = models.CharField(_("S3 object key"), max_length=512)
    sha256 = models.CharField(_("SHA-256"), max_length=64)
    cosign_bundle_s3_key = models.CharField(max_length=512, blank=True)
    size_bytes = models.BigIntegerField(_("size in bytes"))
    is_latest = models.BooleanField(_("latest for this machine"), default=False)
    imported_at = models.DateTimeField(auto_now_add=True)
    imported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="imported_images",
    )

    class Meta:
        verbose_name = _("image release")
        verbose_name_plural = _("image releases")
        constraints = [
            models.UniqueConstraint(fields=["tag", "machine"], name="uniq_tag_per_machine"),
        ]
        ordering = ["-imported_at"]

    def __str__(self):
        return f"{self.tag} ({self.machine})"

    def save(self, *args, **kwargs):
        # Single `is_latest=True` per machine is an application-level invariant;
        # flipping older rows lives next to the write so both paths (admin UI,
        # worker, data migrations) get it for free.
        if self.is_latest:
            with transaction.atomic():
                ImageRelease.objects.filter(
                    machine=self.machine, is_latest=True
                ).exclude(pk=self.pk).update(is_latest=False)
                super().save(*args, **kwargs)
        else:
            super().save(*args, **kwargs)
```

- [ ] **Step 1.5: Register app in `config/settings/base.py`**

Find the `INSTALLED_APPS` list, add `"apps.images",` near the other `apps.*` entries.

- [ ] **Step 1.6: Create migration and run it against the test DB**

Run: `python manage.py makemigrations images`
Run: `pytest tests/test_images.py -v`
Expected: all 3 tests PASS.

- [ ] **Step 1.7: Admin registration**

```python
# apps/images/admin.py
from django.contrib import admin

from .models import ImageRelease


@admin.register(ImageRelease)
class ImageReleaseAdmin(admin.ModelAdmin):
    list_display = ("tag", "machine", "is_latest", "size_bytes", "imported_at", "imported_by")
    list_filter = ("machine", "is_latest")
    search_fields = ("tag",)
    readonly_fields = ("imported_at", "imported_by", "sha256", "s3_key", "cosign_bundle_s3_key", "size_bytes")
```

- [ ] **Step 1.8: URL conf stub**

```python
# apps/images/urls.py
from django.urls import path

app_name = "images"
urlpatterns: list = []
```

Modify `config/urls.py`: inside the main `urlpatterns`, add

```python
path("images/", include("apps.images.urls")),
```

next to the other app includes.

- [ ] **Step 1.9: Commit**

```bash
git add apps/images config/settings/base.py config/urls.py tests/test_images.py
git commit -m "images: scaffold app + ImageRelease model with latest-mutex"
```

---

### Task 2: `ImageImportJob` model

**Files:**
- Modify: `apps/images/models.py`
- Modify: `apps/images/admin.py`
- Modify: `tests/test_images.py`
- Create: new migration

- [ ] **Step 2.1: Add failing test for job status transitions**

Append to `tests/test_images.py`:

```python
@pytest.mark.django_db
class TestImageImportJob:
    def test_job_defaults(self, admin_user):
        from apps.images.models import ImageImportJob

        job = ImageImportJob.objects.create(
            tag="v1-alpha",
            machine="qemux86-64",
            requested_by=admin_user,
        )
        assert job.status == ImageImportJob.Status.PENDING
        assert job.error_message == ""
        assert job.image_release is None

    def test_terminal_statuses(self, admin_user):
        from apps.images.models import ImageImportJob

        job = ImageImportJob.objects.create(
            tag="v1-alpha",
            machine="qemux86-64",
            requested_by=admin_user,
        )
        job.status = ImageImportJob.Status.READY
        job.save()
        assert ImageImportJob.Status.READY in dict(ImageImportJob.Status.choices)
```

- [ ] **Step 2.2: Run — expect failure (model not defined)**

Run: `pytest tests/test_images.py::TestImageImportJob -v`
Expected: `ImportError`.

- [ ] **Step 2.3: Add `ImageImportJob` to `apps/images/models.py`**

```python
class ImageImportJob(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        RUNNING = "running", _("Running")
        READY = "ready", _("Ready")
        FAILED = "failed", _("Failed")

    tag = models.CharField(max_length=64)
    machine = models.CharField(max_length=32, choices=ImageRelease.Machine.choices)
    mark_as_latest = models.BooleanField(default=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    error_message = models.TextField(blank=True)
    image_release = models.ForeignKey(
        "ImageRelease",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="import_jobs",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"import {self.tag}/{self.machine} ({self.status})"
```

- [ ] **Step 2.4: Register in admin**

Add to `apps/images/admin.py`:

```python
from .models import ImageImportJob


@admin.register(ImageImportJob)
class ImageImportJobAdmin(admin.ModelAdmin):
    list_display = ("tag", "machine", "status", "created_at", "requested_by")
    list_filter = ("status", "machine")
    readonly_fields = ("tag", "machine", "status", "created_at", "completed_at", "image_release", "requested_by", "error_message")
```

- [ ] **Step 2.5: Generate + run migration + tests**

Run: `python manage.py makemigrations images`
Run: `pytest tests/test_images.py -v`
Expected: all 5 tests PASS.

- [ ] **Step 2.6: Commit**

```bash
git add apps/images tests/test_images.py
git commit -m "images: add ImageImportJob state model"
```

---

### Task 3: `github.py`, `cosign.py`, `storage.py` helpers

**Files:**
- Create: `apps/images/github.py`, `apps/images/cosign.py`, `apps/images/storage.py`
- Modify: `tests/test_images.py`

- [ ] **Step 3.1: Tests for `storage.py` S3 key layout**

```python
# append to tests/test_images.py
class TestStorageKeys:
    def test_release_key_layout(self):
        from apps.images.storage import release_key, release_bundle_key

        assert release_key("v1-alpha", "qemux86-64") == "images/v1-alpha/qemux86-64.wic.bz2"
        assert release_bundle_key("v1-alpha", "qemux86-64") == "images/v1-alpha/qemux86-64.wic.bz2.bundle"
```

- [ ] **Step 3.2: Run — fails (module missing)**

Run: `pytest tests/test_images.py::TestStorageKeys -v`
Expected: ImportError.

- [ ] **Step 3.3: Write `apps/images/storage.py`**

```python
# apps/images/storage.py
from __future__ import annotations

from django.core.files.storage import default_storage


def release_key(tag: str, machine: str) -> str:
    return f"images/{tag}/{machine}.wic.bz2"


def release_bundle_key(tag: str, machine: str) -> str:
    return f"{release_key(tag, machine)}.bundle"


def upload_bytes(key: str, data: bytes) -> None:
    """Upload bytes to S3 (or local media) under `key`, overwriting."""
    from django.core.files.base import ContentFile

    if default_storage.exists(key):
        default_storage.delete(key)
    default_storage.save(key, ContentFile(data))


def open_stream(key: str):
    """Return a file-like opened on the stored object."""
    return default_storage.open(key, "rb")


def delete(key: str) -> None:
    if default_storage.exists(key):
        default_storage.delete(key)
```

- [ ] **Step 3.4: Run — test passes**

Run: `pytest tests/test_images.py::TestStorageKeys -v`
Expected: PASS.

- [ ] **Step 3.5: Tests for `github.py` — mocked HTTP**

```python
class TestGithubRelease:
    def test_fetch_parses_sha256_sidecar(self, tmp_path, monkeypatch):
        from apps.images import github

        captured = {}

        def fake_urlopen(url, *a, **kw):
            captured.setdefault("urls", []).append(url)
            body = {
                ".wic.bz2": b"fakewicbody",
                ".sha256": (
                    "31df7e5dbcef8c6eaa9f21ccdaf4ad71b9b5c1aa6b39d02b8d0fa1e29f1cca65  "
                    "oe5xrx-qemux86-64-v1-alpha.wic.bz2\n"
                ).encode(),
                ".bundle": b"fakebundle",
            }
            for suffix, payload in body.items():
                if url.endswith(suffix):
                    import io

                    return io.BytesIO(payload)
            raise AssertionError(f"unexpected url {url}")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

        asset = github.fetch_release_asset("OE5XRX/linux-image", "v1-alpha", "qemux86-64")
        assert asset.wic_bytes == b"fakewicbody"
        assert asset.sha256 == "31df7e5dbcef8c6eaa9f21ccdaf4ad71b9b5c1aa6b39d02b8d0fa1e29f1cca65"
        assert asset.bundle_bytes == b"fakebundle"
```

- [ ] **Step 3.6: Run — fails**

Run: `pytest tests/test_images.py::TestGithubRelease -v`
Expected: ImportError.

- [ ] **Step 3.7: Write `apps/images/github.py`**

```python
# apps/images/github.py
from __future__ import annotations

import hashlib
import urllib.request
from dataclasses import dataclass


RELEASE_URL_FMT = (
    "https://github.com/{repo}/releases/download/{tag}/oe5xrx-{machine}-{tag}.{ext}"
)


@dataclass
class ReleaseAsset:
    wic_bytes: bytes
    sha256: str
    bundle_bytes: bytes


def _get(url: str) -> bytes:
    with urllib.request.urlopen(url) as resp:
        return resp.read()


def fetch_release_asset(repo: str, tag: str, machine: str) -> ReleaseAsset:
    base = RELEASE_URL_FMT.format(repo=repo, tag=tag, machine=machine, ext="wic.bz2")
    wic_bytes = _get(base)
    sha_text = _get(base + ".sha256").decode("utf-8").strip()
    # Format from `sha256sum`: "<64-hex>  <filename>"
    sha256 = sha_text.split()[0]
    if len(sha256) != 64:
        raise ValueError(f"malformed .sha256 sidecar: {sha_text!r}")
    if hashlib.sha256(wic_bytes).hexdigest() != sha256:
        raise ValueError("sha256 mismatch: the downloaded .wic.bz2 is corrupt or tampered")
    bundle_bytes = _get(base + ".bundle")
    return ReleaseAsset(wic_bytes=wic_bytes, sha256=sha256, bundle_bytes=bundle_bytes)
```

- [ ] **Step 3.8: Run — passes**

Run: `pytest tests/test_images.py::TestGithubRelease -v`
Expected: PASS.

- [ ] **Step 3.9: Tests for `cosign.py` — subprocess mocked**

```python
class TestCosignVerify:
    def test_verify_invokes_cosign_binary(self, tmp_path, monkeypatch):
        from apps.images import cosign

        calls = []

        def fake_run(cmd, *a, **kw):
            calls.append(cmd)

            class Result:
                returncode = 0
                stdout = b""
                stderr = b""

            return Result()

        monkeypatch.setattr("subprocess.run", fake_run)
        cosign.verify_blob(
            blob_bytes=b"fake",
            bundle_bytes=b"also-fake",
            repo="OE5XRX/linux-image",
            tag="v1-alpha",
        )
        assert len(calls) == 1
        cmd = calls[0]
        assert cmd[0] == "cosign"
        assert "--bundle" in cmd
        # Identity should pin to this tag
        expected_regexp = (
            "https://github.com/OE5XRX/linux-image/.github/workflows/release.yml@refs/tags/v1-alpha"
        )
        assert expected_regexp in cmd

    def test_verify_raises_on_nonzero(self, monkeypatch):
        from apps.images import cosign

        def fake_run(cmd, *a, **kw):
            class Result:
                returncode = 1
                stdout = b""
                stderr = b"no valid signature"

            return Result()

        monkeypatch.setattr("subprocess.run", fake_run)
        with pytest.raises(cosign.CosignVerificationError):
            cosign.verify_blob(
                blob_bytes=b"fake",
                bundle_bytes=b"bad-bundle",
                repo="OE5XRX/linux-image",
                tag="v1-alpha",
            )
```

- [ ] **Step 3.10: Run — fails**

Run: `pytest tests/test_images.py::TestCosignVerify -v`
Expected: ImportError.

- [ ] **Step 3.11: Write `apps/images/cosign.py`**

```python
# apps/images/cosign.py
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


class CosignVerificationError(RuntimeError):
    pass


COSIGN_OIDC_ISSUER = "https://token.actions.githubusercontent.com"


def verify_blob(
    blob_bytes: bytes,
    bundle_bytes: bytes,
    repo: str,
    tag: str,
) -> None:
    """Verify a cosign-signed blob against its GitHub Actions OIDC identity.

    Raises:
        CosignVerificationError: if verification fails for any reason.
    """
    identity_regexp = (
        f"https://github.com/{repo}/.github/workflows/release.yml@refs/tags/{tag}"
    )
    with tempfile.TemporaryDirectory() as tmp:
        blob_path = Path(tmp) / "blob"
        bundle_path = Path(tmp) / "bundle"
        blob_path.write_bytes(blob_bytes)
        bundle_path.write_bytes(bundle_bytes)
        cmd = [
            "cosign",
            "verify-blob",
            "--bundle",
            str(bundle_path),
            "--certificate-identity-regexp",
            identity_regexp,
            "--certificate-oidc-issuer",
            COSIGN_OIDC_ISSUER,
            str(blob_path),
        ]
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            raise CosignVerificationError(
                f"cosign verify-blob failed: {result.stderr.decode('utf-8', 'replace')}"
            )
```

- [ ] **Step 3.12: Run — passes**

Run: `pytest tests/test_images.py -v`
Expected: all tests PASS.

- [ ] **Step 3.13: Commit**

```bash
git add apps/images tests/test_images.py
git commit -m "images: add github/cosign/storage helpers"
```

---

### Task 4: Image import view + form

**Files:**
- Create: `apps/images/forms.py`
- Modify: `apps/images/views.py`, `apps/images/urls.py`
- Create: `apps/images/templates/images/image_list.html`, `apps/images/templates/images/_import_form.html`
- Modify: `tests/test_images.py`

- [ ] **Step 4.1: Test — admin can POST import, creates PENDING job**

```python
class TestImportView:
    def test_admin_can_create_import_job(self, client, admin_user):
        from apps.images.models import ImageImportJob

        client.force_login(admin_user)
        response = client.post(
            "/images/import/",
            {"tag": "v1-alpha", "machine": "qemux86-64", "mark_as_latest": "on"},
        )
        assert response.status_code == 302  # redirect to list
        job = ImageImportJob.objects.get()
        assert job.tag == "v1-alpha"
        assert job.machine == "qemux86-64"
        assert job.mark_as_latest is True
        assert job.status == ImageImportJob.Status.PENDING
        assert job.requested_by == admin_user

    def test_operator_cannot_create_import_job(self, client, operator_user):
        client.force_login(operator_user)
        response = client.post(
            "/images/import/",
            {"tag": "v1-alpha", "machine": "qemux86-64"},
        )
        # AdminRequiredMixin returns 403 for non-admin
        assert response.status_code == 403

    def test_anonymous_redirected_to_login(self, client):
        response = client.post("/images/import/", {"tag": "v1-alpha", "machine": "qemux86-64"})
        assert response.status_code == 302
        assert "/accounts/login" in response["Location"]
```

- [ ] **Step 4.2: Run — fails (no URL)**

Run: `pytest tests/test_images.py::TestImportView -v`
Expected: 404 or routing error.

- [ ] **Step 4.3: Write the form**

```python
# apps/images/forms.py
from django import forms
from django.utils.translation import gettext_lazy as _

from .models import ImageRelease


class ImageImportForm(forms.Form):
    tag = forms.CharField(
        label=_("Tag"),
        max_length=64,
        help_text=_("GitHub release tag, e.g. v1-alpha"),
    )
    machine = forms.ChoiceField(
        label=_("Machine"),
        choices=ImageRelease.Machine.choices,
    )
    mark_as_latest = forms.BooleanField(
        label=_("Mark as latest for this machine"),
        required=False,
        initial=True,
    )
```

- [ ] **Step 4.4: Write the list + import views**

```python
# apps/images/views.py
from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views.generic import FormView, ListView

from apps.accounts.views import AdminRequiredMixin

from .forms import ImageImportForm
from .models import ImageImportJob, ImageRelease


class ImageListView(AdminRequiredMixin, ListView):
    model = ImageRelease
    template_name = "images/image_list.html"
    context_object_name = "releases"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["import_form"] = ImageImportForm()
        ctx["recent_jobs"] = ImageImportJob.objects.order_by("-created_at")[:10]
        return ctx


class ImageImportView(AdminRequiredMixin, FormView):
    form_class = ImageImportForm
    template_name = "images/image_list.html"
    success_url = reverse_lazy("images:list")

    def form_valid(self, form):
        ImageImportJob.objects.create(
            tag=form.cleaned_data["tag"],
            machine=form.cleaned_data["machine"],
            mark_as_latest=form.cleaned_data["mark_as_latest"],
            requested_by=self.request.user,
        )
        messages.success(
            self.request,
            _("Import queued. It will appear below in a minute or two."),
        )
        return super().form_valid(form)
```

- [ ] **Step 4.5: Wire URLs**

```python
# apps/images/urls.py
from django.urls import path

from . import views

app_name = "images"

urlpatterns = [
    path("", views.ImageListView.as_view(), name="list"),
    path("import/", views.ImageImportView.as_view(), name="import"),
]
```

- [ ] **Step 4.6: Minimal list template (enough to render the form)**

```html
<!-- apps/images/templates/images/image_list.html -->
{% extends "base.html" %}
{% load i18n %}

{% block content %}
<h1>{% trans "Image Releases" %}</h1>

<section>
  <h2>{% trans "Import from GitHub" %}</h2>
  <form method="post" action="{% url 'images:import' %}">
    {% csrf_token %}
    {{ import_form.as_p }}
    <button type="submit">{% trans "Queue import" %}</button>
  </form>
</section>

<section>
  <h2>{% trans "Imported images" %}</h2>
  <table>
    <thead>
      <tr>
        <th>{% trans "Tag" %}</th>
        <th>{% trans "Machine" %}</th>
        <th>{% trans "Latest" %}</th>
        <th>{% trans "Size" %}</th>
        <th>{% trans "Imported" %}</th>
      </tr>
    </thead>
    <tbody>
      {% for rel in releases %}
      <tr>
        <td>{{ rel.tag }}</td>
        <td>{{ rel.get_machine_display }}</td>
        <td>{% if rel.is_latest %}✓{% endif %}</td>
        <td>{{ rel.size_bytes|filesizeformat }}</td>
        <td>{{ rel.imported_at|date:"Y-m-d H:i" }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</section>

<section>
  <h2>{% trans "Recent import jobs" %}</h2>
  <ul>
    {% for job in recent_jobs %}
    <li>{{ job.tag }} / {{ job.machine }} — {{ job.get_status_display }} ({{ job.created_at|date:"H:i" }})
      {% if job.error_message %}<br><small>{{ job.error_message }}</small>{% endif %}
    </li>
    {% endfor %}
  </ul>
</section>
{% endblock %}
```

- [ ] **Step 4.7: Run — tests pass**

Run: `pytest tests/test_images.py -v`
Expected: all tests PASS.

- [ ] **Step 4.8: Commit**

```bash
git add apps/images tests/test_images.py
git commit -m "images: import view + form + list page"
```

---

### Task 5: Image importer worker

**Files:**
- Create: `apps/provisioning/management/__init__.py`, `apps/provisioning/management/commands/__init__.py`, `apps/provisioning/management/commands/run_background_jobs.py`
- Create: `apps/provisioning/__init__.py`, `apps/provisioning/apps.py`
- Modify: `config/settings/base.py` (register provisioning app — we'll flesh it out in Phase 2, scaffolding now makes the management command discoverable)
- Modify: `tests/test_images.py`

- [ ] **Step 5.1: Test — worker processes a pending job end-to-end (mocked GitHub + S3 + cosign)**

```python
class TestImageImporterWorker:
    def test_pending_job_becomes_ready_and_creates_release(
        self, admin_user, monkeypatch, settings
    ):
        from apps.images import github
        from apps.images.models import ImageImportJob, ImageRelease
        from apps.provisioning.management.commands.run_background_jobs import (
            process_pending_image_imports,
        )

        settings.LINUX_IMAGE_REPO = "OE5XRX/linux-image"

        job = ImageImportJob.objects.create(
            tag="v1-alpha",
            machine="qemux86-64",
            mark_as_latest=True,
            requested_by=admin_user,
        )

        monkeypatch.setattr(
            github,
            "fetch_release_asset",
            lambda repo, tag, machine: github.ReleaseAsset(
                wic_bytes=b"wic",
                sha256="e" * 64,
                bundle_bytes=b"bundle",
            ),
        )
        monkeypatch.setattr(
            "apps.images.cosign.verify_blob",
            lambda **kw: None,
        )

        uploads = []

        def fake_upload(key, data):
            uploads.append((key, data))

        monkeypatch.setattr("apps.images.storage.upload_bytes", fake_upload)

        process_pending_image_imports()

        job.refresh_from_db()
        assert job.status == ImageImportJob.Status.READY
        assert job.image_release is not None
        assert job.image_release.tag == "v1-alpha"
        assert job.image_release.is_latest is True
        assert ImageRelease.objects.count() == 1
        assert ("images/v1-alpha/qemux86-64.wic.bz2", b"wic") in uploads
        assert ("images/v1-alpha/qemux86-64.wic.bz2.bundle", b"bundle") in uploads

    def test_cosign_failure_marks_job_failed_and_skips_release(
        self, admin_user, monkeypatch, settings
    ):
        from apps.images import github, cosign
        from apps.images.models import ImageImportJob, ImageRelease
        from apps.provisioning.management.commands.run_background_jobs import (
            process_pending_image_imports,
        )

        settings.LINUX_IMAGE_REPO = "OE5XRX/linux-image"
        job = ImageImportJob.objects.create(
            tag="v1-alpha",
            machine="qemux86-64",
            requested_by=admin_user,
        )

        monkeypatch.setattr(
            github,
            "fetch_release_asset",
            lambda repo, tag, machine: github.ReleaseAsset(
                wic_bytes=b"wic",
                sha256="e" * 64,
                bundle_bytes=b"bundle",
            ),
        )

        def bad_verify(**kw):
            raise cosign.CosignVerificationError("signature mismatch")

        monkeypatch.setattr("apps.images.cosign.verify_blob", bad_verify)
        monkeypatch.setattr("apps.images.storage.upload_bytes", lambda k, d: None)

        process_pending_image_imports()

        job.refresh_from_db()
        assert job.status == ImageImportJob.Status.FAILED
        assert "signature mismatch" in job.error_message
        assert ImageRelease.objects.count() == 0
```

- [ ] **Step 5.2: Run — fails (module missing)**

Run: `pytest tests/test_images.py::TestImageImporterWorker -v`
Expected: ImportError.

- [ ] **Step 5.3: Scaffold `provisioning` app skeleton (just enough for the management command)**

```python
# apps/provisioning/__init__.py
```

```python
# apps/provisioning/apps.py
from django.apps import AppConfig


class ProvisioningConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.provisioning"
    verbose_name = "Provisioning"
```

Add `"apps.provisioning",` to `INSTALLED_APPS` in `config/settings/base.py`.

- [ ] **Step 5.4: Write management command + processor**

```python
# apps/provisioning/management/__init__.py
```

```python
# apps/provisioning/management/commands/__init__.py
```

```python
# apps/provisioning/management/commands/run_background_jobs.py
from __future__ import annotations

import time
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.images import cosign, github, storage as image_storage
from apps.images.models import ImageImportJob, ImageRelease


class Command(BaseCommand):
    help = "Process queued image imports and provisioning jobs."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="Run continuously")
        parser.add_argument("--interval", type=int, default=5, help="Seconds between ticks")

    def handle(self, *args, **opts):
        while True:
            process_pending_image_imports()
            # process_pending_provisioning_jobs() — added in Phase 2
            # cleanup_expired_provisioning_outputs() — added in Phase 2
            if not opts["loop"]:
                return
            time.sleep(opts["interval"])


def process_pending_image_imports() -> None:
    repo = getattr(settings, "LINUX_IMAGE_REPO", "OE5XRX/linux-image")

    for job in ImageImportJob.objects.filter(status=ImageImportJob.Status.PENDING).order_by("created_at"):
        job.status = ImageImportJob.Status.RUNNING
        job.save(update_fields=["status"])

        try:
            asset = github.fetch_release_asset(repo=repo, tag=job.tag, machine=job.machine)
            cosign.verify_blob(
                blob_bytes=asset.wic_bytes,
                bundle_bytes=asset.bundle_bytes,
                repo=repo,
                tag=job.tag,
            )
            wic_key = image_storage.release_key(job.tag, job.machine)
            bundle_key = image_storage.release_bundle_key(job.tag, job.machine)
            image_storage.upload_bytes(wic_key, asset.wic_bytes)
            image_storage.upload_bytes(bundle_key, asset.bundle_bytes)

            release = ImageRelease.objects.create(
                tag=job.tag,
                machine=job.machine,
                s3_key=wic_key,
                cosign_bundle_s3_key=bundle_key,
                sha256=asset.sha256,
                size_bytes=len(asset.wic_bytes),
                is_latest=job.mark_as_latest,
                imported_by=job.requested_by,
            )
            job.image_release = release
            job.status = ImageImportJob.Status.READY
            job.completed_at = timezone.now()
            job.save(update_fields=["image_release", "status", "completed_at"])
        except Exception as exc:
            job.status = ImageImportJob.Status.FAILED
            job.error_message = str(exc)
            job.completed_at = timezone.now()
            job.save(update_fields=["status", "error_message", "completed_at"])
```

- [ ] **Step 5.5: Run tests**

Run: `pytest tests/test_images.py -v`
Expected: all tests PASS (including the new importer tests).

- [ ] **Step 5.6: Commit**

```bash
git add apps/provisioning apps/images config/settings/base.py tests/test_images.py
git commit -m "images: run_background_jobs worker processes import queue"
```

---

### Task 6: Images list page — mark latest + delete

**Files:**
- Modify: `apps/images/views.py`, `apps/images/urls.py`, `apps/images/templates/images/image_list.html`
- Modify: `tests/test_images.py`

- [ ] **Step 6.1: Tests — mark-latest flips, delete removes from S3 and DB**

```python
class TestImageManagement:
    def test_mark_latest_flips(self, client, admin_user):
        from apps.images.models import ImageRelease

        ImageRelease.objects.create(
            tag="v0.9.0", machine="qemux86-64",
            s3_key="images/v0.9.0/qemux86-64.wic.bz2", sha256="a" * 64,
            size_bytes=1, is_latest=True,
        )
        new = ImageRelease.objects.create(
            tag="v1-alpha", machine="qemux86-64",
            s3_key="images/v1-alpha/qemux86-64.wic.bz2", sha256="b" * 64,
            size_bytes=2, is_latest=False,
        )
        client.force_login(admin_user)
        response = client.post(f"/images/{new.pk}/mark-latest/")
        assert response.status_code == 302
        new.refresh_from_db()
        assert new.is_latest is True
        old = ImageRelease.objects.get(tag="v0.9.0")
        assert old.is_latest is False

    def test_delete_release_removes_s3_and_db(self, client, admin_user, monkeypatch):
        from apps.images.models import ImageRelease

        deleted_keys = []
        monkeypatch.setattr(
            "apps.images.storage.delete",
            lambda key: deleted_keys.append(key),
        )

        rel = ImageRelease.objects.create(
            tag="v1-alpha", machine="qemux86-64",
            s3_key="images/v1-alpha/qemux86-64.wic.bz2",
            cosign_bundle_s3_key="images/v1-alpha/qemux86-64.wic.bz2.bundle",
            sha256="a" * 64, size_bytes=100,
        )
        client.force_login(admin_user)
        response = client.post(f"/images/{rel.pk}/delete/")
        assert response.status_code == 302
        assert ImageRelease.objects.count() == 0
        assert "images/v1-alpha/qemux86-64.wic.bz2" in deleted_keys
        assert "images/v1-alpha/qemux86-64.wic.bz2.bundle" in deleted_keys
```

- [ ] **Step 6.2: Run — fails (URLs missing)**

Run: `pytest tests/test_images.py::TestImageManagement -v`
Expected: 404.

- [ ] **Step 6.3: Add views**

Append to `apps/images/views.py`:

```python
from django.shortcuts import get_object_or_404
from django.views import View

from . import storage


class ImageMarkLatestView(AdminRequiredMixin, View):
    def post(self, request, pk):
        release = get_object_or_404(ImageRelease, pk=pk)
        release.is_latest = True
        release.save()
        messages.success(request, _("Marked as latest."))
        return redirect("images:list")


class ImageDeleteView(AdminRequiredMixin, View):
    def post(self, request, pk):
        release = get_object_or_404(ImageRelease, pk=pk)
        storage.delete(release.s3_key)
        if release.cosign_bundle_s3_key:
            storage.delete(release.cosign_bundle_s3_key)
        release.delete()
        messages.success(request, _("Release deleted."))
        return redirect("images:list")
```

- [ ] **Step 6.4: Wire URLs**

```python
# apps/images/urls.py
urlpatterns = [
    path("", views.ImageListView.as_view(), name="list"),
    path("import/", views.ImageImportView.as_view(), name="import"),
    path("<int:pk>/mark-latest/", views.ImageMarkLatestView.as_view(), name="mark_latest"),
    path("<int:pk>/delete/", views.ImageDeleteView.as_view(), name="delete"),
]
```

- [ ] **Step 6.5: Extend template (add per-row buttons)**

In `image_list.html`, replace the `<tbody>` row with:

```html
{% for rel in releases %}
<tr>
  <td>{{ rel.tag }}</td>
  <td>{{ rel.get_machine_display }}</td>
  <td>{% if rel.is_latest %}✓{% else %}
    <form method="post" action="{% url 'images:mark_latest' rel.pk %}" style="display:inline">
      {% csrf_token %}
      <button type="submit">{% trans "Mark latest" %}</button>
    </form>
  {% endif %}</td>
  <td>{{ rel.size_bytes|filesizeformat }}</td>
  <td>{{ rel.imported_at|date:"Y-m-d H:i" }}</td>
  <td>
    <form method="post" action="{% url 'images:delete' rel.pk %}" style="display:inline"
          onsubmit="return confirm('{% trans "Delete this release from S3 and DB?" %}');">
      {% csrf_token %}
      <button type="submit">{% trans "Delete" %}</button>
    </form>
  </td>
</tr>
{% endfor %}
```

- [ ] **Step 6.6: Run tests**

Run: `pytest tests/test_images.py -v`
Expected: all PASS.

- [ ] **Step 6.7: Commit**

```bash
git add apps/images tests/test_images.py
git commit -m "images: mark-latest + delete actions on list page"
```

---

## Phase 2 — `provisioning` app (bundle generation)

### Task 7: `ProvisioningJob` model

**Files:**
- Modify: `apps/provisioning/models.py` (new), `apps/provisioning/admin.py`, `apps/provisioning/__init__.py`
- Create: `tests/test_provisioning.py`

- [ ] **Step 7.1: Test — model invariants + expiry default**

```python
# tests/test_provisioning.py
from datetime import timedelta

import pytest
from django.utils import timezone


@pytest.fixture
def image_release(db):
    from apps.images.models import ImageRelease

    return ImageRelease.objects.create(
        tag="v1-alpha", machine="qemux86-64",
        s3_key="images/v1-alpha/qemux86-64.wic.bz2",
        sha256="a" * 64, size_bytes=1000, is_latest=True,
    )


@pytest.mark.django_db
class TestProvisioningJob:
    def test_defaults(self, station, image_release, admin_user):
        from apps.provisioning.models import ProvisioningJob

        job = ProvisioningJob.objects.create(
            station=station,
            image_release=image_release,
            requested_by=admin_user,
        )
        assert job.status == ProvisioningJob.Status.PENDING
        assert job.output_s3_key == ""
        assert job.error_message == ""
        assert job.expires_at is None
        assert job.id is not None  # UUID default

    def test_uuid_primary_key(self, station, image_release, admin_user):
        from apps.provisioning.models import ProvisioningJob
        import uuid

        job = ProvisioningJob.objects.create(
            station=station, image_release=image_release, requested_by=admin_user,
        )
        assert isinstance(job.id, uuid.UUID)
```

- [ ] **Step 7.2: Run — fails**

Run: `pytest tests/test_provisioning.py -v`
Expected: ImportError.

- [ ] **Step 7.3: Write the model**

```python
# apps/provisioning/models.py
import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class ProvisioningJob(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        RUNNING = "running", _("Running")
        READY = "ready", _("Ready")
        DOWNLOADED = "downloaded", _("Downloaded")
        EXPIRED = "expired", _("Expired")
        FAILED = "failed", _("Failed")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    station = models.ForeignKey(
        "stations.Station", on_delete=models.CASCADE, related_name="provisioning_jobs"
    )
    image_release = models.ForeignKey(
        "images.ImageRelease", on_delete=models.PROTECT, related_name="provisioning_jobs"
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    error_message = models.TextField(blank=True)
    output_s3_key = models.CharField(max_length=512, blank=True)
    output_size_bytes = models.BigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    ready_at = models.DateTimeField(null=True, blank=True)
    downloaded_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = _("provisioning job")
        verbose_name_plural = _("provisioning jobs")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.station.name} / {self.image_release.tag} ({self.status})"
```

- [ ] **Step 7.4: Admin registration**

```python
# apps/provisioning/admin.py
from django.contrib import admin

from .models import ProvisioningJob


@admin.register(ProvisioningJob)
class ProvisioningJobAdmin(admin.ModelAdmin):
    list_display = ("id", "station", "image_release", "status", "created_at", "requested_by")
    list_filter = ("status",)
    readonly_fields = tuple(f.name for f in ProvisioningJob._meta.get_fields() if not f.many_to_many)
```

- [ ] **Step 7.5: Migration + run**

Run: `python manage.py makemigrations provisioning`
Run: `pytest tests/test_provisioning.py -v`
Expected: PASS.

- [ ] **Step 7.6: Commit**

```bash
git add apps/provisioning tests/test_provisioning.py
git commit -m "provisioning: add ProvisioningJob model"
```

---

### Task 8: `guestfish` + `config_render` helpers

**Files:**
- Create: `apps/provisioning/guestfish.py`, `apps/provisioning/config_render.py`
- Create: `tests/fixtures/tiny.wic.bz2` (built once, checked in)
- Modify: `tests/test_provisioning.py`

**Fixture prep (one-time, not a pytest step):**

> Before writing code for this task, build the fixture:
>
> ```bash
> dd if=/dev/zero of=/tmp/tiny.wic bs=1M count=8
> sfdisk /tmp/tiny.wic <<EOF
> label: gpt
> ,,L
> EOF
> # Format partition 1 as ext4 using libguestfs (so tests can mount it)
> guestfish --rw -a /tmp/tiny.wic run : mkfs ext4 /dev/sda1
> bzip2 /tmp/tiny.wic   # produces /tmp/tiny.wic.bz2
> cp /tmp/tiny.wic.bz2 tests/fixtures/tiny.wic.bz2
> ```
>
> Check the fixture into git. It's a few kB compressed.

- [ ] **Step 8.1: Test — config_render produces expected YAML**

```python
# append to tests/test_provisioning.py
class TestConfigRender:
    def test_render_produces_expected_fields(self, station):
        from apps.provisioning.config_render import render_config

        yaml_text = render_config(
            server_url="https://ham.oe5xrx.org",
            station_id=station.id,
        )
        assert f"station_id: {station.id}" in yaml_text
        assert "server_url: https://ham.oe5xrx.org" in yaml_text
        assert "ed25519_key_path: /etc/station-agent/device_key.pem" in yaml_text
        assert "terminal_enabled: true" in yaml_text
```

- [ ] **Step 8.2: Write `config_render.py`**

```python
# apps/provisioning/config_render.py
from textwrap import dedent


def render_config(*, server_url: str, station_id: int) -> str:
    return dedent(
        f"""\
        server_url: {server_url}
        station_id: {station_id}
        ed25519_key_path: /etc/station-agent/device_key.pem
        heartbeat_interval: 60
        ota_check_interval: 5
        download_dir: /tmp/station-agent
        log_level: INFO
        terminal_enabled: true
        terminal_shell: /bin/bash
        bootloader: auto
        """
    )
```

- [ ] **Step 8.3: Run — passes**

Run: `pytest tests/test_provisioning.py::TestConfigRender -v`
Expected: PASS.

- [ ] **Step 8.4: Test — guestfish writes files into partition 1 of the tiny.wic fixture**

```python
class TestGuestfishInject:
    def test_inject_files_into_data_partition(self, tmp_path):
        import bz2
        import subprocess
        from pathlib import Path

        from apps.provisioning.guestfish import inject_provisioning_files

        src = Path(__file__).parent / "fixtures" / "tiny.wic.bz2"
        wic_path = tmp_path / "tiny.wic"
        wic_path.write_bytes(bz2.decompress(src.read_bytes()))

        inject_provisioning_files(
            wic_path=wic_path,
            partition_device="/dev/sda1",
            config_yaml="server_url: https://x\n",
            private_key_pem=b"-----BEGIN PRIVATE KEY-----\nAAA\n-----END PRIVATE KEY-----\n",
        )

        # Read back via guestfish to verify
        result = subprocess.run(
            [
                "guestfish", "--ro", "-a", str(wic_path),
                "run", ":", "mount", "/dev/sda1", "/", ":",
                "cat", "/etc-overlay/station-agent/config.yml",
            ],
            capture_output=True, check=True,
        )
        assert b"server_url: https://x" in result.stdout
```

- [ ] **Step 8.5: Run — fails (module missing)**

Run: `pytest tests/test_provisioning.py::TestGuestfishInject -v`
Expected: ImportError.

- [ ] **Step 8.6: Write `guestfish.py`**

```python
# apps/provisioning/guestfish.py
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


# Partition index where `data` lives per the wks layouts in
# meta-oe5xrx-remotestation/wic/:
#   x86-64: 4 partitions (EFI, rootfs-A, rootfs-B, data) -> /dev/sda4
#   RPi:    8 partitions, data is last                   -> /dev/sda8
DATA_PARTITION = {
    "qemux86-64": "/dev/sda4",
    "raspberrypi4-64": "/dev/sda8",
}


class GuestfishError(RuntimeError):
    pass


def inject_provisioning_files(
    *,
    wic_path: Path,
    partition_device: str,
    config_yaml: str,
    private_key_pem: bytes,
) -> None:
    """Mount the data partition of `wic_path` and write the provisioning bundle."""
    with tempfile.TemporaryDirectory() as tmp:
        config_path = Path(tmp) / "config.yml"
        key_path = Path(tmp) / "device_key.pem"
        config_path.write_text(config_yaml)
        key_path.write_bytes(private_key_pem)

        script = "\n".join(
            [
                "run",
                f"mount {partition_device} /",
                "mkdir-p /etc-overlay/station-agent",
                f"upload {config_path} /etc-overlay/station-agent/config.yml",
                f"upload {key_path} /etc-overlay/station-agent/device_key.pem",
                "chmod 0600 /etc-overlay/station-agent/device_key.pem",
                "umount-all",
            ]
        )
        result = subprocess.run(
            ["guestfish", "--rw", "-a", str(wic_path)],
            input=script.encode(),
            capture_output=True,
        )
        if result.returncode != 0:
            raise GuestfishError(
                f"guestfish failed ({result.returncode}): {result.stderr.decode('utf-8', 'replace')}"
            )


def data_partition_for(machine: str) -> str:
    try:
        return DATA_PARTITION[machine]
    except KeyError:
        raise ValueError(f"unsupported machine: {machine}") from None
```

- [ ] **Step 8.7: Adjust test to use single-partition fixture**

The `tiny.wic` fixture has 1 partition, and we'll pass `/dev/sda1` in the test. In production, `data_partition_for()` maps the machine to `/dev/sda4` or `/dev/sda8`. That's what Task 10 passes to `inject_provisioning_files`.

- [ ] **Step 8.8: Run — passes**

Run: `pytest tests/test_provisioning.py::TestGuestfishInject -v`
Expected: PASS, assuming `guestfish` is installed locally (will be in the Dockerfile in Phase 4; developer running tests needs `sudo apt install libguestfs-tools`).

- [ ] **Step 8.9: Commit**

```bash
git add apps/provisioning tests/test_provisioning.py tests/fixtures/tiny.wic.bz2
git commit -m "provisioning: config_render + guestfish injection helper"
```

---

### Task 9: Provisioning worker — full pipeline

**Files:**
- Modify: `apps/provisioning/management/commands/run_background_jobs.py`
- Modify: `apps/provisioning/models.py` (nothing — just import)
- Modify: `tests/test_provisioning.py`

- [ ] **Step 9.1: Test — full pipeline (station-with-key becomes a ready job)**

```python
class TestProvisioningWorker:
    def test_pending_job_pipeline_goes_ready(
        self, station, image_release, admin_user, monkeypatch, settings
    ):
        from apps.api.models import DeviceKey
        from apps.provisioning.management.commands.run_background_jobs import (
            process_pending_provisioning_jobs,
        )
        from apps.provisioning.models import ProvisioningJob

        settings.SERVER_PUBLIC_URL = "https://ham.oe5xrx.org"

        # Stubs for IO-bound steps
        monkeypatch.setattr(
            "apps.images.storage.open_stream",
            lambda key: __import__("io").BytesIO(b"FAKEWICBZ2BYTES"),
        )
        monkeypatch.setattr(
            "apps.provisioning.management.commands.run_background_jobs._decompress_to",
            lambda src, dst: dst.write_bytes(b"FAKEWIC"),
        )
        monkeypatch.setattr(
            "apps.provisioning.guestfish.inject_provisioning_files",
            lambda **kw: None,
        )
        monkeypatch.setattr(
            "apps.provisioning.management.commands.run_background_jobs._compress_to_bytes",
            lambda path: b"FAKEWICBZ2",
        )
        uploaded = {}
        monkeypatch.setattr(
            "apps.images.storage.upload_bytes",
            lambda key, data: uploaded.setdefault(key, data),
        )

        job = ProvisioningJob.objects.create(
            station=station, image_release=image_release, requested_by=admin_user,
        )

        process_pending_provisioning_jobs()

        job.refresh_from_db()
        assert job.status == ProvisioningJob.Status.READY
        assert job.output_s3_key.startswith("provisioning/")
        assert job.output_s3_key.endswith(".wic.bz2")
        assert job.output_size_bytes == len(b"FAKEWICBZ2")
        assert job.expires_at is not None
        # DeviceKey was created (public half stored server-side)
        assert DeviceKey.objects.filter(station=station).exists()
```

- [ ] **Step 9.2: Run — fails**

Run: `pytest tests/test_provisioning.py::TestProvisioningWorker -v`
Expected: ImportError for `process_pending_provisioning_jobs`.

- [ ] **Step 9.3: Extend the management command with the provisioning pipeline**

Replace the handler body and add the new processor in `apps/provisioning/management/commands/run_background_jobs.py`:

```python
# top of file — add imports
import bz2
import tempfile
from datetime import timedelta
from pathlib import Path

from django.conf import settings

from apps.api.models import DeviceKey
from apps.images import storage as image_storage
from apps.provisioning import guestfish
from apps.provisioning.config_render import render_config
from apps.provisioning.models import ProvisioningJob

# handler: replace the "process provisioning..." placeholder comments
# in the while loop with real calls.
```

Add these functions below `process_pending_image_imports`:

```python
PROVISIONING_EXPIRY = timedelta(hours=1)


def _decompress_to(src_path: Path, dst_path: Path) -> None:
    with bz2.open(src_path, "rb") as src, open(dst_path, "wb") as dst:
        while chunk := src.read(1 << 20):
            dst.write(chunk)


def _compress_to_bytes(src_path: Path) -> bytes:
    return bz2.compress(src_path.read_bytes(), compresslevel=9)


def _provisioning_output_key(job: ProvisioningJob) -> str:
    tag = job.image_release.tag
    machine = job.image_release.machine
    return f"provisioning/{job.id}/oe5xrx-station-{job.station_id}-{machine}-{tag}.wic.bz2"


def process_pending_provisioning_jobs() -> None:
    server_url = getattr(settings, "SERVER_PUBLIC_URL", "https://ham.oe5xrx.org")

    for job in ProvisioningJob.objects.filter(status=ProvisioningJob.Status.PENDING).order_by("created_at"):
        job.status = ProvisioningJob.Status.RUNNING
        job.save(update_fields=["status"])

        try:
            private_pem, public_b64 = DeviceKey.generate_keypair()
            DeviceKey.objects.update_or_create(
                station=job.station,
                defaults={"current_public_key": public_b64, "is_active": True, "next_public_key": None},
            )

            with tempfile.TemporaryDirectory() as tmp_str:
                tmp = Path(tmp_str)
                compressed_in = tmp / "base.wic.bz2"
                decompressed = tmp / "work.wic"

                with image_storage.open_stream(job.image_release.s3_key) as src, open(compressed_in, "wb") as dst:
                    for chunk in iter(lambda: src.read(1 << 20), b""):
                        dst.write(chunk)

                _decompress_to(compressed_in, decompressed)

                guestfish.inject_provisioning_files(
                    wic_path=decompressed,
                    partition_device=guestfish.data_partition_for(job.image_release.machine),
                    config_yaml=render_config(
                        server_url=server_url,
                        station_id=job.station_id,
                    ),
                    private_key_pem=private_pem,
                )

                out_bytes = _compress_to_bytes(decompressed)

            out_key = _provisioning_output_key(job)
            image_storage.upload_bytes(out_key, out_bytes)

            now = timezone.now()
            job.output_s3_key = out_key
            job.output_size_bytes = len(out_bytes)
            job.status = ProvisioningJob.Status.READY
            job.ready_at = now
            job.expires_at = now + PROVISIONING_EXPIRY
            job.save(update_fields=[
                "output_s3_key", "output_size_bytes",
                "status", "ready_at", "expires_at",
            ])
        except Exception as exc:
            job.status = ProvisioningJob.Status.FAILED
            job.error_message = str(exc)
            job.save(update_fields=["status", "error_message"])


def cleanup_expired_provisioning_outputs() -> None:
    now = timezone.now()
    # Downloaded files — delete the S3 object once.
    for job in ProvisioningJob.objects.filter(
        status=ProvisioningJob.Status.DOWNLOADED,
    ).exclude(output_s3_key=""):
        image_storage.delete(job.output_s3_key)
        ProvisioningJob.objects.filter(pk=job.pk).update(output_s3_key="")

    # Expired before download.
    stale = ProvisioningJob.objects.filter(
        status=ProvisioningJob.Status.READY, expires_at__lt=now,
    )
    for job in stale:
        if job.output_s3_key:
            image_storage.delete(job.output_s3_key)
        job.status = ProvisioningJob.Status.EXPIRED
        job.output_s3_key = ""
        job.save(update_fields=["status", "output_s3_key"])
```

And update `handle()` to call both new processors:

```python
def handle(self, *args, **opts):
    while True:
        process_pending_image_imports()
        process_pending_provisioning_jobs()
        cleanup_expired_provisioning_outputs()
        if not opts["loop"]:
            return
        time.sleep(opts["interval"])
```

- [ ] **Step 9.4: Run tests**

Run: `pytest tests/test_provisioning.py -v`
Expected: PASS.

- [ ] **Step 9.5: Commit**

```bash
git add apps/provisioning tests/test_provisioning.py
git commit -m "provisioning: worker pipeline (key-gen, guestfish, S3) + cleanup"
```

---

### Task 10: Provisioning views — create, status, download

**Files:**
- Create: `apps/provisioning/forms.py`, `apps/provisioning/urls.py`
- Modify: `apps/provisioning/views.py` (new), `config/urls.py`
- Modify: `tests/test_provisioning.py`

- [ ] **Step 10.1: Tests — create job, status endpoint, download lifecycle**

```python
class TestProvisioningViews:
    def test_admin_creates_provisioning_job(self, client, admin_user, station, image_release):
        from apps.provisioning.models import ProvisioningJob

        client.force_login(admin_user)
        response = client.post(
            f"/provisioning/station/{station.pk}/new/",
            {"image_release": image_release.pk},
        )
        assert response.status_code == 302
        assert ProvisioningJob.objects.filter(station=station).count() == 1
        job = ProvisioningJob.objects.get()
        assert job.status == ProvisioningJob.Status.PENDING

    def test_operator_cannot_create_job(self, client, operator_user, station, image_release):
        client.force_login(operator_user)
        response = client.post(
            f"/provisioning/station/{station.pk}/new/",
            {"image_release": image_release.pk},
        )
        assert response.status_code == 403

    def test_status_endpoint_returns_partial(self, client, admin_user, station, image_release):
        from apps.provisioning.models import ProvisioningJob

        job = ProvisioningJob.objects.create(
            station=station, image_release=image_release, requested_by=admin_user,
        )
        client.force_login(admin_user)
        response = client.get(f"/provisioning/{job.id}/status/")
        assert response.status_code == 200
        assert b"pending" in response.content.lower() or b"running" in response.content.lower()

    def test_download_ready_job_streams_and_marks_downloaded(
        self, client, admin_user, station, image_release, monkeypatch
    ):
        from apps.provisioning.models import ProvisioningJob
        from django.utils import timezone
        from datetime import timedelta

        job = ProvisioningJob.objects.create(
            station=station, image_release=image_release, requested_by=admin_user,
            status=ProvisioningJob.Status.READY,
            output_s3_key="provisioning/abc/test.wic.bz2",
            output_size_bytes=10,
            ready_at=timezone.now(),
            expires_at=timezone.now() + timedelta(hours=1),
        )
        monkeypatch.setattr(
            "apps.images.storage.open_stream",
            lambda key: __import__("io").BytesIO(b"0123456789"),
        )
        client.force_login(admin_user)
        response = client.get(f"/provisioning/{job.id}/download/")
        assert response.status_code == 200
        assert b"".join(response.streaming_content) == b"0123456789"
        job.refresh_from_db()
        assert job.status == ProvisioningJob.Status.DOWNLOADED
        assert job.downloaded_at is not None

    def test_download_expired_job_returns_410(
        self, client, admin_user, station, image_release
    ):
        from apps.provisioning.models import ProvisioningJob
        from django.utils import timezone
        from datetime import timedelta

        job = ProvisioningJob.objects.create(
            station=station, image_release=image_release, requested_by=admin_user,
            status=ProvisioningJob.Status.READY,
            output_s3_key="provisioning/abc/test.wic.bz2",
            output_size_bytes=10,
            ready_at=timezone.now() - timedelta(hours=2),
            expires_at=timezone.now() - timedelta(hours=1),
        )
        client.force_login(admin_user)
        response = client.get(f"/provisioning/{job.id}/download/")
        assert response.status_code == 410
```

- [ ] **Step 10.2: Run — fails**

Run: `pytest tests/test_provisioning.py::TestProvisioningViews -v`
Expected: 404s.

- [ ] **Step 10.3: Form**

```python
# apps/provisioning/forms.py
from django import forms
from django.utils.translation import gettext_lazy as _

from apps.images.models import ImageRelease


class ProvisioningForm(forms.Form):
    image_release = forms.ModelChoiceField(
        label=_("Image version"),
        queryset=ImageRelease.objects.all(),
    )
```

- [ ] **Step 10.4: Views**

```python
# apps/provisioning/views.py
from django.http import HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View

from apps.accounts.views import AdminRequiredMixin
from apps.images import storage as image_storage
from apps.images.models import ImageRelease
from apps.stations.models import Station

from .forms import ProvisioningForm
from .models import ProvisioningJob


class CreateProvisioningJobView(AdminRequiredMixin, View):
    def post(self, request, station_pk):
        station = get_object_or_404(Station, pk=station_pk)
        form = ProvisioningForm(request.POST)
        if not form.is_valid():
            return HttpResponse("invalid form", status=400)
        ProvisioningJob.objects.create(
            station=station,
            image_release=form.cleaned_data["image_release"],
            requested_by=request.user,
        )
        return redirect("stations:station_detail", pk=station.pk)


class ProvisioningJobStatusView(AdminRequiredMixin, View):
    """HTMX-polled partial showing the job's current state."""

    def get(self, request, pk):
        job = get_object_or_404(ProvisioningJob, pk=pk)
        return render(request, "provisioning/_job_status.html", {"job": job})


class ProvisioningJobDownloadView(AdminRequiredMixin, View):
    CHUNK = 1 << 20  # 1 MiB

    def get(self, request, pk):
        job = get_object_or_404(ProvisioningJob, pk=pk)
        if job.status != ProvisioningJob.Status.READY:
            return HttpResponse("not ready", status=409)
        if job.expires_at and job.expires_at < timezone.now():
            return HttpResponse("expired", status=410)

        stream = image_storage.open_stream(job.output_s3_key)

        def iterator():
            try:
                while chunk := stream.read(self.CHUNK):
                    yield chunk
            finally:
                stream.close()
                # Mark downloaded only after a successful full iteration.
                ProvisioningJob.objects.filter(pk=job.pk, status=ProvisioningJob.Status.READY).update(
                    status=ProvisioningJob.Status.DOWNLOADED,
                    downloaded_at=timezone.now(),
                )

        filename = job.output_s3_key.split("/")[-1]
        response = StreamingHttpResponse(iterator(), content_type="application/x-bzip2")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        if job.output_size_bytes:
            response["Content-Length"] = str(job.output_size_bytes)
        return response
```

- [ ] **Step 10.5: URLs + include**

```python
# apps/provisioning/urls.py
from django.urls import path

from . import views

app_name = "provisioning"

urlpatterns = [
    path("station/<int:station_pk>/new/", views.CreateProvisioningJobView.as_view(), name="new"),
    path("<uuid:pk>/status/", views.ProvisioningJobStatusView.as_view(), name="status"),
    path("<uuid:pk>/download/", views.ProvisioningJobDownloadView.as_view(), name="download"),
]
```

Add to `config/urls.py` inside the main urlpatterns:

```python
path("provisioning/", include("apps.provisioning.urls")),
```

- [ ] **Step 10.6: Minimal status template (enough for test)**

```html
<!-- apps/provisioning/templates/provisioning/_job_status.html -->
{% load i18n %}
<div id="provisioning-status">
  <strong>{% trans "Status:" %}</strong> {{ job.get_status_display|lower }}
  {% if job.status == job.Status.READY %}
    — <a href="{% url 'provisioning:download' job.id %}">{% trans "Download" %}</a>
  {% endif %}
  {% if job.error_message %}<br><small>{{ job.error_message }}</small>{% endif %}
</div>
```

- [ ] **Step 10.7: Run tests**

Run: `pytest tests/test_provisioning.py -v`
Expected: all PASS.

- [ ] **Step 10.8: Commit**

```bash
git add apps/provisioning config/urls.py tests/test_provisioning.py
git commit -m "provisioning: create-job, status, download views"
```

---

## Phase 3 — UI integration

### Task 11: Station-detail provisioning section + install instructions

**Files:**
- Create: `apps/provisioning/templates/provisioning/_provisioning_section.html`
- Create: `apps/provisioning/templates/provisioning/_install_instructions.html`
- Modify: `apps/stations/templates/stations/station_detail.html`
- Modify: `apps/stations/views.py` (include provisioning context)
- Modify: `tests/test_provisioning.py`

- [ ] **Step 11.1: Test — station detail renders the provisioning form for admin, not for operator**

```python
class TestStationDetailIntegration:
    def test_admin_sees_provisioning_section(self, client, admin_user, station, image_release):
        client.force_login(admin_user)
        response = client.get(f"/stations/{station.pk}/")
        assert response.status_code == 200
        assert b"Provisioning" in response.content
        assert b"Generate provisioning bundle" in response.content

    def test_operator_does_not_see_provisioning_section(
        self, client, operator_user, station, image_release
    ):
        client.force_login(operator_user)
        response = client.get(f"/stations/{station.pk}/")
        assert response.status_code == 200
        assert b"Generate provisioning bundle" not in response.content
```

- [ ] **Step 11.2: Run — fails**

Run: `pytest tests/test_provisioning.py::TestStationDetailIntegration -v`

- [ ] **Step 11.3: Write the section template**

```html
<!-- apps/provisioning/templates/provisioning/_provisioning_section.html -->
{% load i18n %}
{% if user.role == "admin" %}
<section class="card mt-20">
  <h2>{% trans "Provisioning" %}</h2>

  {% if active_provisioning_job %}
    <div hx-get="{% url 'provisioning:status' active_provisioning_job.id %}"
         hx-trigger="load, every 3s"
         hx-swap="innerHTML">
      {% include "provisioning/_job_status.html" with job=active_provisioning_job %}
    </div>
  {% else %}
    <form method="post" action="{% url 'provisioning:new' station.pk %}">
      {% csrf_token %}
      <label>{% trans "Image version" %}:
        <select name="image_release" required>
          {% for img in image_releases %}
            <option value="{{ img.pk }}" {% if img.is_latest %}selected{% endif %}>
              {{ img.tag }} — {{ img.get_machine_display }}{% if img.is_latest %} ({% trans "latest" %}){% endif %}
            </option>
          {% endfor %}
        </select>
      </label>
      <button type="submit">{% trans "Generate provisioning bundle" %}</button>
    </form>
  {% endif %}

  {% include "provisioning/_install_instructions.html" %}
</section>
{% endif %}
```

- [ ] **Step 11.4: Install instructions partial (3 tabs)**

```html
<!-- apps/provisioning/templates/provisioning/_install_instructions.html -->
{% load i18n %}
<h3>{% trans "Install instructions" %}</h3>
<details>
  <summary>{% trans "Raspberry Pi CM4 (real hardware)" %}</summary>
  <pre>
# Flash directly onto the SD card (replace /dev/sdX with your card):
bzcat oe5xrx-station-{{ station.pk }}-*.wic.bz2 | sudo dd of=/dev/sdX bs=4M status=progress conv=fsync
# Insert + power on the CM4. The station reports online within ~1 min.
  </pre>
</details>
<details>
  <summary>{% trans "Proxmox VM" %}</summary>
  <pre>
bunzip2 oe5xrx-station-{{ station.pk }}-*.wic.bz2
# On the Proxmox host (replace VMID + STORAGE):
qm importdisk VMID oe5xrx-station-{{ station.pk }}-*.wic STORAGE
# Configure the VM: BIOS=OVMF, machine=q35, attach disk as virtio, start it.
  </pre>
</details>
<details>
  <summary>{% trans "QEMU direct (dev)" %}</summary>
  <pre>
# Place the .wic.bz2 into the linux-image repo's cache dir and run:
# (see linux-image/scripts/run-qemu.sh for details)
bunzip2 oe5xrx-station-{{ station.pk }}-*.wic.bz2
qemu-system-x86_64 -drive file=oe5xrx-station-{{ station.pk }}-*.wic,if=virtio,format=raw \
  -m 1024 -smp 2 -cpu IvyBridge -machine q35 \
  -device virtio-net-pci,netdev=n0 -netdev user,id=n0,hostfwd=tcp::2222-:22
  </pre>
</details>
```

- [ ] **Step 11.5: Wire into station-detail**

In `apps/stations/templates/stations/station_detail.html`, add before `{% endblock %}`:

```html
{% include "provisioning/_provisioning_section.html" %}
```

In `apps/stations/views.py`, in `StationDetailView.get_context_data`, add:

```python
from apps.images.models import ImageRelease
from apps.provisioning.models import ProvisioningJob

if self.request.user.role == "admin":
    context["image_releases"] = ImageRelease.objects.order_by("machine", "-is_latest", "-imported_at")
    context["active_provisioning_job"] = (
        ProvisioningJob.objects.filter(
            station=self.object,
            status__in=[ProvisioningJob.Status.PENDING, ProvisioningJob.Status.RUNNING, ProvisioningJob.Status.READY],
        )
        .order_by("-created_at")
        .first()
    )
```

- [ ] **Step 11.6: Run tests**

Run: `pytest tests/test_provisioning.py -v`
Expected: PASS.

- [ ] **Step 11.7: Commit**

```bash
git add apps/provisioning apps/stations tests/test_provisioning.py
git commit -m "provisioning: station-detail section + install instructions"
```

---

### Task 12: Sidebar "Images" entry (admin-only)

**Files:**
- Modify: `templates/includes/sidebar.html`
- Modify: `tests/test_images.py`

- [ ] **Step 12.1: Test — sidebar shows "Images" to admin, hides from operator**

```python
# append to tests/test_images.py
class TestSidebar:
    def test_admin_sees_images_link(self, client, admin_user):
        client.force_login(admin_user)
        response = client.get("/")  # dashboard
        assert response.status_code == 200
        assert b'href="/images/"' in response.content

    def test_operator_does_not_see_images_link(self, client, operator_user):
        client.force_login(operator_user)
        response = client.get("/")
        assert b'href="/images/"' not in response.content
```

- [ ] **Step 12.2: Edit `templates/includes/sidebar.html`**

Inside the existing `{% if user.role == 'admin' %}` block (which is already there — we saw it in the codebase around line 68), add:

```html
<a class="nav-link" href="{% url 'images:list' %}">
  <i class="bi bi-hdd"></i>
  <span>{% trans "Images" %}</span>
</a>
```

- [ ] **Step 12.3: Run — passes**

Run: `pytest tests/test_images.py::TestSidebar -v`
Expected: PASS.

- [ ] **Step 12.4: Commit**

```bash
git add templates/includes/sidebar.html tests/test_images.py
git commit -m "ui: sidebar Images entry for admin"
```

---

## Phase 4 — Plumbing

### Task 13: Dockerfile — libguestfs + cosign

**Files:**
- Modify: `Dockerfile`

- [ ] **Step 13.1: Edit Dockerfile**

Replace the existing `apt-get install` block with:

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq-dev \
        libguestfs-tools \
        linux-image-generic \
        ca-certificates \
        curl \
    && curl -fsSL https://github.com/sigstore/cosign/releases/latest/download/cosign-linux-amd64 \
        -o /usr/local/bin/cosign \
    && chmod +x /usr/local/bin/cosign \
    && rm -rf /var/lib/apt/lists/*
```

Context:

- `libguestfs-tools` is needed for `guestfish`.
- `linux-image-generic` ships a readable kernel — libguestfs requires a kernel at `/boot/vmlinuz-*` to bootstrap its appliance. Slim doesn't have one.
- `cosign` binary is pulled directly from the latest release (static Go binary, ~60 MB).

- [ ] **Step 13.2: Build and smoke-test locally**

Run:
```bash
docker build -t station-manager:test .
docker run --rm station-manager:test guestfish --version
docker run --rm station-manager:test cosign version
```
Expected: both print version strings without error.

- [ ] **Step 13.3: Commit**

```bash
git add Dockerfile
git commit -m "docker: install libguestfs-tools + cosign binary"
```

---

### Task 14: docker-compose services + env

**Files:**
- Modify: `docker-compose.yml`, `deploy/docker-compose.prod.yml`, `.env.example`

- [ ] **Step 14.1: Add `background-worker` to `docker-compose.yml`**

Under the `services:` section (next to `station-monitor`), add:

```yaml
  background-worker:
    build:
      context: .
    command: python manage.py run_background_jobs --loop --interval 5
    volumes:
      - .:/app
    env_file:
      - .env
    environment:
      - DJANGO_SETTINGS_MODULE=config.settings.dev
    depends_on:
      - db
      - redis
```

- [ ] **Step 14.2: Add `background-worker` to `deploy/docker-compose.prod.yml`**

Under the `services:` section (alongside `alert-monitor`), add:

```yaml
  background-worker:
    image: ghcr.io/oe5xrx/station-manager:latest
    restart: unless-stopped
    command: python manage.py run_background_jobs --loop --interval 5
    env_file:
      - .env
    environment:
      - DJANGO_SETTINGS_MODULE=config.settings.prod
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_started
```

And update the deploy step in `.github/workflows/deploy.yml`:

Find the line starting `docker compose -f docker-compose.prod.yml up -d --force-recreate web db redis nginx station-monitor alert-monitor` and append ` background-worker` to it.

- [ ] **Step 14.3: Document new env vars in `.env.example`**

Append:

```
# Provisioning / images
LINUX_IMAGE_REPO=OE5XRX/linux-image
SERVER_PUBLIC_URL=https://ham.oe5xrx.org
```

- [ ] **Step 14.4: Spot-check compose parses**

Run: `docker compose -f deploy/docker-compose.prod.yml config | grep -A2 background-worker`
Expected: emits the service block.

- [ ] **Step 14.5: Commit**

```bash
git add docker-compose.yml deploy/docker-compose.prod.yml .github/workflows/deploy.yml .env.example
git commit -m "deploy: add background-worker service to compose + CI"
```

---

## Phase 5 — End-to-end verification and PR

### Task 15: E2E checklist (manual) + final PR

**Files:**
- Modify: `docs/superpowers/plans/2026-04-17-server-side-provisioning.md` (append "verification log" section filled in as you run it — optional)

- [ ] **Step 15.1: Deploy the branch to staging (or local prod stack)**

```bash
docker compose -f docker-compose.yml up -d --build
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser   # if needed
```

Open `http://localhost:8000/images/` as admin.

- [ ] **Step 15.2: Import a real release**

In the UI: tag `v1-alpha`, machine `qemux86-64`, "mark as latest" on. Submit.

Within ~2 min the `background-worker` should pull the asset, verify sha256 + cosign, upload to S3, create the ImageRelease. Refresh the page — row appears, `✓` in the Latest column.

Verify on the command line:
```bash
docker compose logs background-worker --tail 50
```

Expected: no errors, line "status → ready" for the job.

- [ ] **Step 15.3: Create a test station**

Via existing UI: Stations → Create → name="QEMU-1", callsign="OE5XRX-1". Save.

- [ ] **Step 15.4: Generate provisioning bundle**

On the station detail page, expand the new "Provisioning" section. Select `v1-alpha (latest)`. Click "Generate provisioning bundle".

Expect HTMX polling to show: pending → running → ready. Total time ~1-2 min (bzip2 compression is the slow step).

- [ ] **Step 15.5: Download + boot in Proxmox**

Click "Download". Verify file is `oe5xrx-station-<id>-qemux86-64-v1-alpha.wic.bz2`, ~70 MB.

```bash
bunzip2 oe5xrx-station-*.wic.bz2
# In Proxmox, create a VM with:
#   BIOS: OVMF
#   Machine: q35
#   No disk (we import next)
qm importdisk <vmid> oe5xrx-station-*.wic <storage>
# Attach the imported disk as virtio, set boot order to it.
qm start <vmid>
```

- [ ] **Step 15.6: Verify station comes online**

In the station-manager UI, within ~2 min of boot, the "QEMU-1" station should:
- Go green / "Online"
- Show inventory (hostname, uptime, module_versions)
- Terminal button should open a working shell

- [ ] **Step 15.7: Verify cleanup**

Back in station-manager, the `ProvisioningJob` should now be in status `downloaded`. Within another worker tick (5 s) the `output_s3_key` should be emptied and the S3 object gone.

Verify in S3:
```bash
aws --endpoint-url "$S3_ENDPOINT_URL" s3 ls s3://$S3_BUCKET_NAME/provisioning/ --recursive
```
Expected: no files (or only files for still-active jobs).

- [ ] **Step 15.8: Open the PR**

```bash
gh pr create --base main \
  --title "Server-side station provisioning (images + provisioning apps)" \
  --body-file - <<'EOF'
## What

Adds two Django apps:

- **`images`** — admin UI to import signed linux-image releases from GitHub.
  Verifies sha256 + cosign keyless signature against the release repo's OIDC
  identity, stores the .wic.bz2 on Hetzner S3, tracks `is_latest` per machine.

- **`provisioning`** — per-station bundle generation. Admin picks a machine +
  image version, a background worker generates a new Ed25519 keypair, injects
  `config.yml` + `device_key.pem` into the image's data partition via
  `guestfish`, re-compresses, and exposes a one-time download. The S3 object
  is deleted after download or after 1 h.

One new docker-compose service (`background-worker`) drives both queues.

## Why

Previously onboarding a station required manual Yocto builds and hand-writing
the config into the data partition. Now: click "Import" once per release, then
click "Generate bundle" per station. Matches real-hardware and QEMU/Proxmox
testing flows with no code divergence.

## Spec / plan

- `docs/superpowers/specs/2026-04-17-server-side-provisioning-design.md`
- `docs/superpowers/plans/2026-04-17-server-side-provisioning.md`

## Testing

- Unit: `tests/test_images.py`, `tests/test_provisioning.py` — full coverage of
  models, views, worker pipeline, guestfish injection (against a tiny fixture),
  cosign verification, S3 lifecycle.
- Manual E2E: imported v1-alpha, provisioned a test station, booted in Proxmox,
  station reported online, terminal worked, S3 object auto-cleaned. Checklist
  in the plan doc, step 15.

## Breaking changes

None. Existing `DeviceKey` generation path in `apps/stations` is untouched
(only the new provisioning flow creates keys via `DeviceKey.generate_keypair()`).

## New deps / infra

- `libguestfs-tools`, `linux-image-generic` (for guestfish kernel), `cosign`
  static binary → Dockerfile.
- New env var: `LINUX_IMAGE_REPO` (default `OE5XRX/linux-image`), `SERVER_PUBLIC_URL`.
- New docker-compose service: `background-worker`.
EOF
```

- [ ] **Step 15.9: Let CI run; resolve anything the reviewer flags**

---

## Self-review notes

- **Spec coverage:** Each spec section maps to at least one task —
  Image Registry (Tasks 1-6), Provisioning Service (Tasks 7-10),
  UI integration (Tasks 11-12), Plumbing (Tasks 13-14), E2E (Task 15).
- **Types:** `ImageRelease.Machine` choices match what `apps/provisioning/guestfish.py::DATA_PARTITION` expects (`qemux86-64`, `raspberrypi4-64`).
- **Keys:** `ProvisioningJob.output_s3_key` emptied on both `downloaded` and `expired` branches in `cleanup_expired_provisioning_outputs`.
- **No placeholders** remain.

## Execution choice

Plan complete and saved to `docs/superpowers/plans/2026-04-17-server-side-provisioning.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
