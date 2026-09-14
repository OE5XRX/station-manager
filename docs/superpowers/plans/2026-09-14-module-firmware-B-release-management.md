# Teilbereich B — Firmware-Release-Management — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Modul-Firmware-Releases importieren, auf Server-Storage pinnen und device-key-authentifiziert an Stationen ausliefern — gespiegelt vom Linux-Image-OTA.

**Architecture:** Neue Modelle in A's App `apps/module_firmware` (`ModuleFirmwareRelease`, `ModuleFirmwareImportJob`) + `ModuleType`-Erweiterung. Import läuft async über den bestehenden `run_background_jobs`-Command (reuse `_claim_one_pending`), lädt Release-Assets von GitHub (reuse `apps/images/github_releases`), prüft SHA-256 (via `SHA256SUMS`) + cosign (reuse `apps/images/cosign`, Identity `@refs/heads/main`), pinnt das rohe `.signed.bin` (keine Extraktion). Der Download-Endpoint spiegelt `DeploymentDownloadView` und autorisiert über A's offene Modul-Assignment.

**Tech Stack:** Django 6.0, DRF 3.17, PostgreSQL, Bootstrap 5 + HTMX, pytest + pytest-django, Python 3.14, cosign CLI.

**Spec:** `docs/superpowers/specs/2026-09-14-module-firmware-B-release-management-design.md`

## Global Constraints

- **cosign-Identity (FW):** `^https://github\.com/{repo}/\.github/workflows/release\.yml@refs/heads/main$` — FW-Release ist `workflow_dispatch` auf Default-Branch `main` (bestätigt), NICHT tag-getriggert wie images.
- **Kein `is_latest`, keine Extraktion** — rohes `.signed.bin` wird gepinnt. Desired-State/Reconciler = C.
- **Asset-Naming:** `{release_asset_prefix}[-{variant}].signed.bin` (+ `.bundle`), z.B. `fm-sa818-vhf.signed.bin`. Checksums aus der Release-Datei `SHA256SUMS`.
- **Zugriffsregel:** Lesen = jeder eingeloggte User; Mutationen (Import/Archive/Restore, ModuleType-FW-Felder) = `user.is_staff`.
- **Soft-Delete-Muster** exakt wie `ImageRelease` (`objects`/`all_objects`, `base_manager_name="all_objects"`, idempotente `archive()`/`restore()`).
- **Django-Template-Kommentare:** kein multi-line `{# #}` — `{% comment %}`. CI-Guard aktiv.
- **Frontend-design-Skill** Pflicht für UI-Tasks (Pixel-Agent).
- **Tests:** `pytest` (`DJANGO_SETTINGS_MODULE=config.settings.test`); GH-API/cosign/Storage in Tests gemockt.
- **Commits:** häufig; Footer `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

## File Structure

**Neu:**
- `apps/module_firmware/storage.py` — Key-Schema + `upload_bytes`/`open_stream`/`delete` (mirror `apps/images/storage.py`).
- `apps/module_firmware/releases.py` — Import-Logik: Asset-Parse, Download, SHA256SUMS, cosign, Pin, Row-Anlage.
- `apps/module_firmware/api_views.py` — `ModuleFirmwareDownloadView`.
- `apps/module_firmware/api_urls.py` — DRF-Route.
- `apps/module_firmware/templates/module_firmware/release_list.html`, `_github_releases.html`.
- `tests/module_firmware/test_release_*.py`, `test_fw_import_*.py`, `test_fw_download.py`, `test_fw_views.py`.

**Modifiziert:**
- `apps/module_firmware/models.py` — `ModuleType` (+`firmware_repo`,`release_asset_prefix`); `ModuleFirmwareRelease` (+Manager); `ModuleFirmwareImportJob`.
- `apps/images/cosign.py` — Identity-Core `verify_blob_identity(...)` extrahieren (DRY).
- `apps/provisioning/management/commands/run_background_jobs.py` — `process_pending_module_firmware_imports()` + Tick in `handle()`.
- `apps/module_firmware/admin.py` — Release/ImportJob registrieren; ModuleType-Admin um FW-Felder.
- `apps/module_firmware/views.py` + `urls.py` — Release-Liste, GH-Browser, Import/Archive/Restore.
- `apps/api/urls.py` — `api_urls` includen.

**Import-Datenfluss (Referenz):** `ModuleFirmwareImportJob(module_type, source_repo, tag)` → `releases.import_release_tag(job)`: `github_releases.fetch_releases(source_repo)` → passendes `GitHubRelease` → `SHA256SUMS` + pro Variante `.signed.bin`+`.bundle` von `https://github.com/{repo}/releases/download/{tag}/{name}` laden → SHA-256 gegen `SHA256SUMS` → cosign → pin → `ModuleFirmwareRelease` (all_objects.update_or_create, archived_at=None).

---

## Task 1: ModuleType-Erweiterung (firmware_repo + release_asset_prefix)

**Files:** Modify `apps/module_firmware/models.py`; Test `tests/module_firmware/test_moduletype_fw_fields.py`

**Interfaces:** Produces: `ModuleType.firmware_repo: str`, `ModuleType.release_asset_prefix: str` (beide blank-fähig).

- [ ] **Step 1: Failing test**

```python
# tests/module_firmware/test_moduletype_fw_fields.py
import pytest
from apps.module_firmware.models import ModuleType

@pytest.mark.django_db
def test_moduletype_fw_source_fields():
    t = ModuleType.objects.create(
        key="fm", display_name="FM",
        firmware_repo="OE5XRX/FW-RemoteStation", release_asset_prefix="fm-sa818",
    )
    assert t.firmware_repo == "OE5XRX/FW-RemoteStation"
    assert t.release_asset_prefix == "fm-sa818"

@pytest.mark.django_db
def test_moduletype_fw_fields_optional():
    t = ModuleType.objects.create(key="power", display_name="Power")
    assert t.firmware_repo == "" and t.release_asset_prefix == ""
```

- [ ] **Step 2: Run** → FAIL (Felder fehlen).
- [ ] **Step 3: Felder ergänzen** in `ModuleType`:

```python
    firmware_repo = models.CharField(
        _("firmware repo"), max_length=200, blank=True,
        help_text=_("GitHub owner/repo der signierten FW-Releases, z. B. OE5XRX/FW-RemoteStation"),
    )
    release_asset_prefix = models.CharField(
        _("release asset prefix"), max_length=64, blank=True,
        help_text=_("Asset-Basisname vor -<variant>.signed.bin, z. B. fm-sa818"),
    )
```

- [ ] **Step 4: Migration** — `python manage.py makemigrations module_firmware`
- [ ] **Step 5: Run** → PASS.
- [ ] **Step 6: Commit** — `git add apps/module_firmware/ tests/module_firmware/test_moduletype_fw_fields.py && git commit -m "feat(module_firmware): ModuleType firmware source fields"`

---

## Task 2: `ModuleFirmwareRelease`-Modell + Soft-Delete

**Files:** Modify `apps/module_firmware/models.py`; Test `tests/module_firmware/test_release_model.py`

**Interfaces:** Produces: `ModuleFirmwareRelease(module_type FK PROTECT, variant, version, storage_key, sha256, size_bytes, cosign_bundle_key, source_repo, source_tag, source_github_url, imported_at, imported_by FK SET_NULL, archived_at)`; Manager `objects` (versteckt archiviert) + `all_objects`; `archive()`/`restore()`. Unique `(module_type, variant, version)`.

- [ ] **Step 1: Failing test**

```python
# tests/module_firmware/test_release_model.py
import pytest
from django.db import IntegrityError
from apps.module_firmware.models import ModuleType, ModuleFirmwareRelease

@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")

def _mk(fm, variant="vhf", version="26.07.04-01"):
    return ModuleFirmwareRelease.objects.create(
        module_type=fm, variant=variant, version=version,
        storage_key=f"module_firmware/fm/{version}/{variant}.signed.bin",
        sha256="a"*64, size_bytes=1234,
        cosign_bundle_key=f"module_firmware/fm/{version}/{variant}.signed.bin.bundle",
        source_repo="OE5XRX/FW-RemoteStation", source_tag=version,
    )

@pytest.mark.django_db
def test_unique_type_variant_version(fm):
    _mk(fm)
    with pytest.raises(IntegrityError):
        _mk(fm)

@pytest.mark.django_db
def test_soft_delete_hides_from_objects(fm):
    r = _mk(fm)
    r.archive()
    assert ModuleFirmwareRelease.objects.filter(pk=r.pk).count() == 0
    assert ModuleFirmwareRelease.all_objects.filter(pk=r.pk).count() == 1
    r.restore()
    assert ModuleFirmwareRelease.objects.filter(pk=r.pk).count() == 1
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Modell + Manager** in `apps/module_firmware/models.py` (Imports `from django.db import transaction`, `from django.utils import timezone`, `from django.conf import settings` sicherstellen):

```python
class ModuleFirmwareReleaseManager(models.Manager):
    """Default manager hides archived (soft-deleted) rows."""
    def get_queryset(self):
        return super().get_queryset().filter(archived_at__isnull=True)


class ModuleFirmwareRelease(models.Model):
    module_type = models.ForeignKey(
        ModuleType, on_delete=models.PROTECT, related_name="firmware_releases",
        verbose_name=_("module type"),
    )
    variant = models.CharField(_("variant"), max_length=32, blank=True)
    version = models.CharField(_("version"), max_length=64)
    storage_key = models.CharField(_("storage key"), max_length=512)
    sha256 = models.CharField(_("SHA-256"), max_length=64)
    size_bytes = models.BigIntegerField(_("size in bytes"))
    cosign_bundle_key = models.CharField(_("cosign bundle key"), max_length=512, blank=True)
    source_repo = models.CharField(_("source repo"), max_length=200)
    source_tag = models.CharField(_("source tag"), max_length=64)
    source_github_url = models.CharField(_("source URL"), max_length=512, blank=True)
    imported_at = models.DateTimeField(_("imported at"), auto_now_add=True)
    imported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name=_("imported by"),
    )
    archived_at = models.DateTimeField(_("archived at"), null=True, blank=True)

    objects = ModuleFirmwareReleaseManager()
    all_objects = models.Manager()

    class Meta:
        verbose_name = _("module firmware release")
        verbose_name_plural = _("module firmware releases")
        base_manager_name = "all_objects"
        ordering = ["module_type", "variant", "-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["module_type", "variant", "version"],
                name="uniq_release_per_type_variant_version",
            ),
        ]
        indexes = [models.Index(fields=["module_type", "variant"])]

    def __str__(self):
        return f"{self.module_type.key}/{self.variant or '-'} {self.version}"

    def archive(self):
        if self.archived_at is not None:
            return
        now = timezone.now()
        with transaction.atomic():
            rows = type(self).all_objects.filter(
                pk=self.pk, archived_at__isnull=True
            ).update(archived_at=now)
            if rows == 0:
                self.refresh_from_db(fields=["archived_at"])
                return
            self.archived_at = now

    def restore(self):
        if self.archived_at is None:
            return
        self.archived_at = None
        self.save(update_fields=["archived_at"])
```

- [ ] **Step 4: Migration** — `makemigrations module_firmware`
- [ ] **Step 5: Run** → PASS.
- [ ] **Step 6: Commit** — `feat(module_firmware): ModuleFirmwareRelease + soft-delete`

---

## Task 3: `ModuleFirmwareImportJob`-Modell

**Files:** Modify `apps/module_firmware/models.py`; Test `tests/module_firmware/test_import_job_model.py`

**Interfaces:** Produces: `ModuleFirmwareImportJob(module_type FK, source_repo, tag, status[Status.PENDING/RUNNING/READY/FAILED], error_message, release FK SET_NULL, requested_by FK, created_at, completed_at)`.

- [ ] **Step 1: Failing test**

```python
# tests/module_firmware/test_import_job_model.py
import pytest
from apps.module_firmware.models import ModuleType, ModuleFirmwareImportJob

@pytest.mark.django_db
def test_import_job_defaults():
    t = ModuleType.objects.create(key="fm", display_name="FM")
    j = ModuleFirmwareImportJob.objects.create(
        module_type=t, source_repo="OE5XRX/FW-RemoteStation", tag="26.07.04-01")
    assert j.status == ModuleFirmwareImportJob.Status.PENDING
    assert j.error_message == "" and j.completed_at is None
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Modell**:

```python
class ModuleFirmwareImportJob(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        RUNNING = "running", _("Running")
        READY = "ready", _("Ready")
        FAILED = "failed", _("Failed")

    module_type = models.ForeignKey(
        ModuleType, on_delete=models.CASCADE, related_name="firmware_import_jobs",
        verbose_name=_("module type"),
    )
    source_repo = models.CharField(_("source repo"), max_length=200)
    tag = models.CharField(_("release tag"), max_length=64)
    status = models.CharField(
        _("status"), max_length=16, choices=Status.choices, default=Status.PENDING)
    error_message = models.TextField(_("error message"), blank=True)
    release = models.ForeignKey(
        "ModuleFirmwareRelease", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="import_jobs", verbose_name=_("release"),
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name=_("requested by"),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    completed_at = models.DateTimeField(_("completed at"), null=True, blank=True)

    class Meta:
        verbose_name = _("module firmware import job")
        verbose_name_plural = _("module firmware import jobs")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.module_type.key} {self.tag} [{self.status}]"
```

> Hinweis: `release` ist ein einzelner FK, aber ein Tag erzeugt N Varianten-Releases. Der FK zeigt auf das zuletzt angelegte (Referenz/Provenance); die vollständige Zuordnung ist über `source_tag`+`module_type` abfragbar. Kein Multi-Release-FK nötig (YAGNI).

- [ ] **Step 4: Migration** → **Step 5: Run** → PASS → **Step 6: Commit** `feat(module_firmware): ModuleFirmwareImportJob`

---

## Task 4: `storage.py` (Key-Schema + I/O)

**Files:** Create `apps/module_firmware/storage.py`; Test `tests/module_firmware/test_fw_storage.py`

**Interfaces:** Produces: `release_key(type_key, version, variant) -> str`, `bundle_key(type_key, version, variant) -> str`, `upload_bytes(key, data)`, `open_stream(key)`, `delete(key)`.

- [ ] **Step 1: Failing test**

```python
# tests/module_firmware/test_fw_storage.py
from apps.module_firmware import storage

def test_key_scheme():
    assert storage.release_key("fm", "26.07.04-01", "vhf") == "module_firmware/fm/26.07.04-01/vhf.signed.bin"
    assert storage.bundle_key("fm", "26.07.04-01", "vhf") == "module_firmware/fm/26.07.04-01/vhf.signed.bin.bundle"

def test_key_scheme_bandless():
    assert storage.release_key("power", "1.0.0-00", "") == "module_firmware/power/1.0.0-00/_.signed.bin"
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implementieren** (mirror `apps/images/storage.py`; band-lose Variante → `_` als Segment, damit der Key nie kollabiert):

```python
from __future__ import annotations
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage


def _variant_seg(variant: str) -> str:
    return variant or "_"

def release_key(type_key: str, version: str, variant: str) -> str:
    return f"module_firmware/{type_key}/{version}/{_variant_seg(variant)}.signed.bin"

def bundle_key(type_key: str, version: str, variant: str) -> str:
    return f"{release_key(type_key, version, variant)}.bundle"

def upload_bytes(key: str, data: bytes) -> None:
    if default_storage.exists(key):
        default_storage.delete(key)
    default_storage.save(key, ContentFile(data))

def open_stream(key: str):
    return default_storage.open(key, "rb")

def delete(key: str) -> None:
    if default_storage.exists(key):
        default_storage.delete(key)
```

- [ ] **Step 4: Run** → PASS → **Step 5: Commit** `feat(module_firmware): release storage keys + I/O`

---

## Task 5: cosign-Identity-Core (DRY-Refactor) + FW-Wrapper

**Files:** Modify `apps/images/cosign.py`; Test `tests/module_firmware/test_fw_cosign.py`

**Interfaces:** Produces (in `apps/images/cosign.py`): `verify_blob_identity(blob_bytes, bundle_bytes, identity_regexp: str) -> None` (der subprocess-Core). Bestehendes `verify_blob(...)` ruft es mit dem tags-Regexp. Neu (in `apps/module_firmware/releases.py`, Task 7): `_fw_identity_regexp(repo) -> str`.

- [ ] **Step 1: Failing test** (mockt subprocess, prüft dass die heads/main-Identity durchgereicht wird)

```python
# tests/module_firmware/test_fw_cosign.py
from unittest import mock
from apps.images import cosign

def test_verify_blob_identity_passes_regexp_to_cosign():
    with mock.patch("apps.images.cosign.subprocess.run") as run:
        run.return_value = mock.Mock(returncode=0, stderr=b"")
        cosign.verify_blob_identity(b"blob", b"bundle", r"^https://x/main$")
        args = run.call_args[0][0]
        assert "--certificate-identity-regexp" in args
        assert args[args.index("--certificate-identity-regexp") + 1] == r"^https://x/main$"
        assert "--certificate-oidc-issuer" in args
```

- [ ] **Step 2: Run** → FAIL (`verify_blob_identity` fehlt).
- [ ] **Step 3: Refactor** `apps/images/cosign.py` — den subprocess-Block in `verify_blob_identity` herausziehen; `verify_blob` baut nur noch den tags-Regexp und ruft den Core:

```python
def verify_blob_identity(blob_bytes: bytes, bundle_bytes: bytes, identity_regexp: str) -> None:
    """Verify a cosign-signed blob against an explicit certificate-identity regexp."""
    with tempfile.TemporaryDirectory() as tmp:
        blob_path = Path(tmp) / "blob"
        bundle_path = Path(tmp) / "bundle"
        blob_path.write_bytes(blob_bytes)
        bundle_path.write_bytes(bundle_bytes)
        cmd = [
            "cosign", "verify-blob",
            "--bundle", str(bundle_path),
            "--certificate-identity-regexp", identity_regexp,
            "--certificate-oidc-issuer", COSIGN_OIDC_ISSUER,
            str(blob_path),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=_COSIGN_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            raise CosignVerificationError(f"cosign verify-blob timed out after {_COSIGN_TIMEOUT}s") from exc
        if result.returncode != 0:
            raise CosignVerificationError(
                f"cosign verify-blob failed: {result.stderr.decode('utf-8', 'replace')}")


def verify_blob(blob_bytes: bytes, bundle_bytes: bytes, repo: str, tag: str) -> None:
    identity_regexp = (
        rf"^https://github\.com/{re.escape(repo)}"
        rf"/\.github/workflows/release\.yml@refs/tags/{re.escape(tag)}$"
    )
    verify_blob_identity(blob_bytes, bundle_bytes, identity_regexp)
```

- [ ] **Step 4: Run** → PASS. Zusätzlich bestehende Image-cosign-Tests: `pytest -k cosign` → PASS (Regression: `verify_blob` unverändert im Verhalten).
- [ ] **Step 5: Commit** `refactor(images): extract cosign verify_blob_identity core for reuse`

---

## Task 6: Asset-Parsing (Variante aus Release-Assets)

**Files:** Create `apps/module_firmware/releases.py` (Teil 1); Test `tests/module_firmware/test_asset_parse.py`

**Interfaces:** Produces: `parse_variant_assets(asset_names: set[str], prefix: str) -> list[VariantAsset]` mit `VariantAsset(variant: str, signed_name: str, bundle_name: str)`; `parse_sha256sums(text: str) -> dict[str, str]`.

- [ ] **Step 1: Failing test**

```python
# tests/module_firmware/test_asset_parse.py
from apps.module_firmware.releases import parse_variant_assets, parse_sha256sums

def test_parse_two_variants():
    names = {
        "fm-sa818-vhf.signed.bin", "fm-sa818-vhf.signed.bin.bundle",
        "fm-sa818-uhf.signed.bin", "fm-sa818-uhf.signed.bin.bundle",
        "fm-sa818-vhf.mcuboot.hex", "SHA256SUMS", "SHA256SUMS.bundle",
    }
    got = {v.variant: (v.signed_name, v.bundle_name) for v in parse_variant_assets(names, "fm-sa818")}
    assert got == {
        "vhf": ("fm-sa818-vhf.signed.bin", "fm-sa818-vhf.signed.bin.bundle"),
        "uhf": ("fm-sa818-uhf.signed.bin", "fm-sa818-uhf.signed.bin.bundle"),
    }

def test_parse_bandless():
    names = {"power.signed.bin", "power.signed.bin.bundle"}
    got = parse_variant_assets(names, "power")
    assert len(got) == 1 and got[0].variant == "" and got[0].signed_name == "power.signed.bin"

def test_parse_skips_signed_without_bundle():
    assert parse_variant_assets({"fm-sa818-vhf.signed.bin"}, "fm-sa818") == []

def test_parse_sha256sums():
    text = "aaaa  fm-sa818-vhf.signed.bin\nbbbb  fm-sa818-uhf.signed.bin\n"
    d = parse_sha256sums(text)
    assert d["fm-sa818-vhf.signed.bin"] == "aaaa"
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: `apps/module_firmware/releases.py` (Parsing-Teil)**

```python
from __future__ import annotations
from dataclasses import dataclass

_SIGNED_SUFFIX = ".signed.bin"


@dataclass(frozen=True)
class VariantAsset:
    variant: str
    signed_name: str
    bundle_name: str


def parse_variant_assets(asset_names: set[str], prefix: str) -> list[VariantAsset]:
    """From a release's asset names, extract (variant, signed, bundle) tuples.

    Matches ``{prefix}[-{variant}].signed.bin`` where a matching
    ``.bundle`` sibling exists. A bare ``{prefix}.signed.bin`` yields
    variant "". A prefix that is only a substring of a longer token
    (e.g. ``fm-sa818x-...``) is rejected — the char after the prefix
    must be '-' or the suffix itself.
    """
    out: list[VariantAsset] = []
    for name in sorted(asset_names):
        if not (name.startswith(prefix) and name.endswith(_SIGNED_SUFFIX)):
            continue
        middle = name[len(prefix):-len(_SIGNED_SUFFIX)]
        if middle == "":
            variant = ""
        elif middle.startswith("-"):
            variant = middle[1:]
            if not variant:
                continue
        else:
            continue  # prefix is a proper substring of a longer token
        bundle_name = name + ".bundle"
        if bundle_name not in asset_names:
            continue
        out.append(VariantAsset(variant=variant, signed_name=name, bundle_name=bundle_name))
    return out


def parse_sha256sums(text: str) -> dict[str, str]:
    """Parse ``<hex>  <filename>`` lines into {filename: hex}."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            result[parts[-1]] = parts[0]
    return result
```

- [ ] **Step 4: Run** → PASS → **Step 5: Commit** `feat(module_firmware): release asset + SHA256SUMS parsing`

---

## Task 7: Import-Orchestrator (`import_release_tag`)

**Files:** Modify `apps/module_firmware/releases.py`; Test `tests/module_firmware/test_import_release.py`

**Interfaces:** Consumes: `parse_variant_assets`, `parse_sha256sums`, `apps.images.github_releases.fetch_releases`, `apps.images.cosign.verify_blob_identity`, `apps.module_firmware.storage`. Produces: `import_release_tag(job: ModuleFirmwareImportJob) -> None` (setzt Job READY/FAILED, legt/aktualisiert `ModuleFirmwareRelease` je Variante). Helper `_fw_identity_regexp(repo, branch="main") -> str`, `_download(url) -> bytes`, `_asset_url(repo, tag, name) -> str`.

- [ ] **Step 1: Failing test** (GH + cosign + download gemockt)

```python
# tests/module_firmware/test_import_release.py
import hashlib
from unittest import mock
import pytest
from apps.module_firmware.models import ModuleType, ModuleFirmwareRelease, ModuleFirmwareImportJob
from apps.module_firmware import releases
from apps.images.github_releases import GitHubRelease

@pytest.fixture
def fm(db):
    return ModuleType.objects.create(
        key="fm", display_name="FM",
        firmware_repo="OE5XRX/FW-RemoteStation", release_asset_prefix="fm-sa818")

def _blob(variant): return f"signed-{variant}".encode()

def _fake_download(url):
    # url ends with the asset name
    name = url.rsplit("/", 1)[-1]
    if name == "SHA256SUMS":
        lines = []
        for v in ("vhf", "uhf"):
            lines.append(f"{hashlib.sha256(_blob(v)).hexdigest()}  fm-sa818-{v}.signed.bin")
        return ("\n".join(lines) + "\n").encode()
    if name.endswith(".signed.bin"):
        return _blob(name[len("fm-sa818-"):-len(".signed.bin")])
    if name.endswith(".bundle"):
        return b"bundle"
    raise AssertionError(url)

@pytest.mark.django_db
def test_import_creates_release_per_variant(fm):
    job = ModuleFirmwareImportJob.objects.create(
        module_type=fm, source_repo=fm.firmware_repo, tag="26.07.04-01")
    rel = GitHubRelease(
        tag="26.07.04-01", html_url="", is_latest=True,
        asset_names=frozenset({
            "fm-sa818-vhf.signed.bin", "fm-sa818-vhf.signed.bin.bundle",
            "fm-sa818-uhf.signed.bin", "fm-sa818-uhf.signed.bin.bundle", "SHA256SUMS"}))
    with mock.patch("apps.module_firmware.releases.fetch_releases", return_value=[rel]), \
         mock.patch("apps.module_firmware.releases._download", side_effect=_fake_download), \
         mock.patch("apps.module_firmware.releases.verify_blob_identity") as vbi, \
         mock.patch("apps.module_firmware.releases.storage.upload_bytes"):
        releases.import_release_tag(job)
    job.refresh_from_db()
    assert job.status == ModuleFirmwareImportJob.Status.READY
    rels = ModuleFirmwareRelease.objects.filter(module_type=fm, version="26.07.04-01")
    assert set(rels.values_list("variant", flat=True)) == {"vhf", "uhf"}
    # cosign identity uses refs/heads/main
    assert "refs/heads/main" in vbi.call_args_list[0].args[2]

@pytest.mark.django_db
def test_import_sha_mismatch_fails(fm):
    job = ModuleFirmwareImportJob.objects.create(
        module_type=fm, source_repo=fm.firmware_repo, tag="26.07.04-01")
    rel = GitHubRelease(tag="26.07.04-01", html_url="", is_latest=True,
        asset_names=frozenset({"fm-sa818-vhf.signed.bin", "fm-sa818-vhf.signed.bin.bundle", "SHA256SUMS"}))
    def bad_dl(url):
        name = url.rsplit("/", 1)[-1]
        if name == "SHA256SUMS":
            return b"deadbeef  fm-sa818-vhf.signed.bin\n"
        return b"whatever"
    with mock.patch("apps.module_firmware.releases.fetch_releases", return_value=[rel]), \
         mock.patch("apps.module_firmware.releases._download", side_effect=bad_dl), \
         mock.patch("apps.module_firmware.releases.storage.upload_bytes"):
        releases.import_release_tag(job)
    job.refresh_from_db()
    assert job.status == ModuleFirmwareImportJob.Status.FAILED
    assert ModuleFirmwareRelease.objects.count() == 0
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Orchestrator ergänzen** in `apps/module_firmware/releases.py`:

```python
import hashlib
import re
import urllib.request

from django.db import transaction
from django.utils import timezone

from apps.images.cosign import verify_blob_identity
from apps.images.github_releases import fetch_releases
from . import storage
from .models import ModuleFirmwareImportJob, ModuleFirmwareRelease

_DOWNLOAD_TIMEOUT = 60
_DEFAULT_BRANCH = "main"  # FW-RemoteStation release.yml runs workflow_dispatch on main


def _fw_identity_regexp(repo: str, branch: str = _DEFAULT_BRANCH) -> str:
    return (
        rf"^https://github\.com/{re.escape(repo)}"
        rf"/\.github/workflows/release\.yml@refs/heads/{re.escape(branch)}$"
    )

def _asset_url(repo: str, tag: str, name: str) -> str:
    return f"https://github.com/{repo}/releases/download/{tag}/{name}"

def _download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT) as resp:
        return resp.read()


def import_release_tag(job: ModuleFirmwareImportJob) -> None:
    repo = job.source_repo
    prefix = job.module_type.release_asset_prefix
    uploaded: list[str] = []
    try:
        if not prefix:
            raise ValueError(f"ModuleType {job.module_type.key} has no release_asset_prefix")
        releases_list = fetch_releases(repo)
        gh = next((r for r in releases_list if r.tag == job.tag), None)
        if gh is None:
            raise ValueError(f"release tag {job.tag} not found in {repo}")
        variants = parse_variant_assets(set(gh.asset_names), prefix)
        if not variants:
            raise ValueError(f"no {prefix}-*.signed.bin (+bundle) assets in {job.tag}")
        sums = parse_sha256sums(_download(_asset_url(repo, job.tag, "SHA256SUMS")).decode("utf-8"))
        identity = _fw_identity_regexp(repo)
        last_release = None
        for va in variants:
            blob = _download(_asset_url(repo, job.tag, va.signed_name))
            expected = sums.get(va.signed_name)
            if not expected or hashlib.sha256(blob).hexdigest() != expected:
                raise ValueError(f"sha256 mismatch for {va.signed_name}")
            bundle = _download(_asset_url(repo, job.tag, va.bundle_name))
            verify_blob_identity(blob, bundle, identity)
            skey = storage.release_key(job.module_type.key, job.tag, va.variant)
            bkey = storage.bundle_key(job.module_type.key, job.tag, va.variant)
            storage.upload_bytes(skey, blob); uploaded.append(skey)
            storage.upload_bytes(bkey, bundle); uploaded.append(bkey)
            with transaction.atomic():
                last_release, _ = ModuleFirmwareRelease.all_objects.update_or_create(
                    module_type=job.module_type, variant=va.variant, version=job.tag,
                    defaults={
                        "storage_key": skey, "sha256": expected, "size_bytes": len(blob),
                        "cosign_bundle_key": bkey, "source_repo": repo, "source_tag": job.tag,
                        "source_github_url": _asset_url(repo, job.tag, va.signed_name),
                        "imported_by": job.requested_by, "archived_at": None,
                    },
                )
        job.release = last_release
        job.status = ModuleFirmwareImportJob.Status.READY
        job.completed_at = timezone.now()
        job.save(update_fields=["release", "status", "completed_at"])
    except Exception as exc:
        # Best-effort cleanup of keys we uploaded that no active release row references.
        in_use = set(
            ModuleFirmwareRelease.all_objects.filter(
                module_type=job.module_type, version=job.tag
            ).values_list("storage_key", flat=True)
        ) | set(
            ModuleFirmwareRelease.all_objects.filter(
                module_type=job.module_type, version=job.tag
            ).values_list("cosign_bundle_key", flat=True)
        )
        for key in uploaded:
            if key in in_use:
                continue
            try:
                storage.delete(key)
            except Exception:
                pass
        job.status = ModuleFirmwareImportJob.Status.FAILED
        job.error_message = str(exc)
        job.completed_at = timezone.now()
        job.save(update_fields=["status", "error_message", "completed_at"])
```

- [ ] **Step 4: Run** → PASS → **Step 5: Commit** `feat(module_firmware): import_release_tag (download+sha+cosign+pin, per variant)`

---

## Task 8: In `run_background_jobs` verdrahten

**Files:** Modify `apps/provisioning/management/commands/run_background_jobs.py`; Test `tests/module_firmware/test_import_worker_tick.py`

**Interfaces:** Consumes: `_claim_one_pending`, `releases.import_release_tag`. Produces: `process_pending_module_firmware_imports()`; Aufruf in `Command.handle()`.

- [ ] **Step 1: Failing test**

```python
# tests/module_firmware/test_import_worker_tick.py
from unittest import mock
import pytest
from apps.module_firmware.models import ModuleType, ModuleFirmwareImportJob
from apps.provisioning.management.commands.run_background_jobs import (
    process_pending_module_firmware_imports)

@pytest.mark.django_db
def test_tick_claims_and_runs_pending():
    t = ModuleType.objects.create(key="fm", display_name="FM", release_asset_prefix="fm-sa818")
    job = ModuleFirmwareImportJob.objects.create(module_type=t, source_repo="r", tag="26.07.04-01")
    with mock.patch(
        "apps.provisioning.management.commands.run_background_jobs.mfw_releases.import_release_tag"
    ) as imp:
        process_pending_module_firmware_imports()
    assert imp.call_count == 1
    assert imp.call_args.args[0].pk == job.pk
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Verdrahten** — in `run_background_jobs.py` importieren `from apps.module_firmware import releases as mfw_releases` und `from apps.module_firmware.models import ModuleFirmwareImportJob`; ergänzen:

```python
def process_pending_module_firmware_imports() -> None:
    while (job := _claim_one_pending(ModuleFirmwareImportJob)) is not None:
        mfw_releases.import_release_tag(job)
```

und in `Command.handle()` in die Tick-Schleife aufnehmen (neben `process_pending_image_imports()`):

```python
            process_pending_image_imports()
            process_pending_module_firmware_imports()
            process_pending_provisioning_jobs()
            cleanup_expired_provisioning_outputs()
```

- [ ] **Step 4: Run** → PASS. Regression: `pytest -k "background or import"` → PASS.
- [ ] **Step 5: Commit** `feat(module_firmware): wire firmware import into run_background_jobs`

---

## Task 9: Device-Download-Endpoint

**Files:** Create `apps/module_firmware/api_views.py`, `apps/module_firmware/api_urls.py`; Modify `apps/api/urls.py`; Test `tests/module_firmware/test_fw_download.py`

**Interfaces:** Produces: `GET /api/v1/module-firmware/<int:pk>/download/` → `ModuleFirmwareDownloadView` (DeviceKeyAuth + IsDevice). Authz: offene `ModuleAssignmentHistory` an der Station mit `module.module_type == release.module_type`. Range/Streaming exakt gespiegelt von `DeploymentDownloadView`.

- [ ] **Step 1: Failing test**

```python
# tests/module_firmware/test_fw_download.py
import pytest
from django.urls import reverse
from unittest import mock
from apps.module_firmware.models import (
    ModuleType, Module, ModuleAssignmentHistory, ModuleFirmwareRelease)

@pytest.fixture
def setup(db, station_factory, device_key_factory):
    # device_key_factory: liefert (station, auth-headers) — bestehende Test-Fixture prüfen.
    t = ModuleType.objects.create(key="fm", display_name="FM")
    rel = ModuleFirmwareRelease.objects.create(
        module_type=t, variant="vhf", version="26.07.04-01",
        storage_key="module_firmware/fm/26.07.04-01/vhf.signed.bin", sha256="a"*64,
        size_bytes=5, source_repo="r", source_tag="26.07.04-01")
    return t, rel

@pytest.mark.django_db
def test_download_forbidden_without_matching_assignment(client, setup, station_with_device_key):
    t, rel = setup
    station, headers = station_with_device_key  # signierte device-key-Header-Fixture
    resp = client.get(reverse("module_firmware_api:download", args=[rel.pk]), **headers)
    assert resp.status_code == 403

@pytest.mark.django_db
def test_download_streams_with_assignment(client, setup, station_with_device_key):
    t, rel = setup
    station, headers = station_with_device_key
    mod = Module.objects.create(uid="U1", module_type=t)
    ModuleAssignmentHistory.objects.create(module=mod, station=station, slot="slot1")
    with mock.patch("apps.module_firmware.api_views.storage.open_stream",
                    return_value=__import__("io").BytesIO(b"hello")):
        resp = client.get(reverse("module_firmware_api:download", args=[rel.pk]), **headers)
    assert resp.status_code == 200
    assert b"".join(resp.streaming_content) == b"hello"
```

> Step 1 des Ausführenden: die reale device-key-Test-Fixture ermitteln (wie `apps/api`/deployments-Download-Tests signierte Requests bauen) und `station_with_device_key`/`headers` entsprechend verwenden.

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: View** `apps/module_firmware/api_views.py` — Authz + Storage-Open selbst; **den Range-/Streaming-Block VERBATIM aus `apps/deployments/api_views.py::DeploymentDownloadView` übernehmen** (nur `total_size = release.size_bytes`, `stream = storage.open_stream(release.storage_key)`, Filename `f"{type_key}-{variant}-{version}.signed.bin"`, `content_type="application/octet-stream"`):

```python
import io, logging, re
from django.http import StreamingHttpResponse
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from apps.api.authentication import DeviceKeyAuthentication
from apps.api.permissions import IsDevice
from apps.module_firmware.models import ModuleAssignmentHistory, ModuleFirmwareRelease
from apps.module_firmware import storage

logger = logging.getLogger(__name__)


class ModuleFirmwareDownloadView(APIView):
    authentication_classes = [DeviceKeyAuthentication]
    permission_classes = [IsDevice]
    CHUNK = 1 << 20

    def get(self, request, pk):
        station = getattr(request.auth, "station", None)
        if station is None:
            return Response({"detail": "No station linked to this device key."},
                            status=status.HTTP_403_FORBIDDEN)
        release = ModuleFirmwareRelease.objects.filter(pk=pk).first()  # objects: hides archived
        if release is None:
            return Response({"detail": "Release not found."}, status=status.HTTP_404_NOT_FOUND)
        has_assignment = ModuleAssignmentHistory.objects.filter(
            station=station, to_ts__isnull=True,
            module__module_type_id=release.module_type_id,
        ).exists()
        if not has_assignment:
            return Response({"detail": "No matching module assignment for this station."},
                            status=status.HTTP_403_FORBIDDEN)
        try:
            stream = storage.open_stream(release.storage_key)
        except Exception as exc:
            logger.error("FW download: cannot open %s: %s", release.storage_key, exc)
            return Response({"detail": "Artifact unavailable from storage backend."},
                            status=status.HTTP_502_BAD_GATEWAY)
        total_size = release.size_bytes if release.size_bytes and release.size_bytes > 0 else None
        filename = f"{release.module_type.key}-{release.variant or 'x'}-{release.version}.signed.bin"
        # --- BEGIN: Range/Streaming block copied verbatim from DeploymentDownloadView ---
        # (parse HTTP_RANGE → 200/206/416, seek, StreamingHttpResponse in CHUNK, headers)
        # Change ONLY: use `total_size`, `stream`, `filename`, content_type below.
        # --- END ---
```

> Der Ausführende kopiert den kompletten Range-/Response-Block (ab `range_header = request.META.get("HTTP_RANGE", "")` bis zur `return StreamingHttpResponse(...)`) 1:1 aus `DeploymentDownloadView`, ersetzt `image.rootfs_size_bytes`→`total_size`, den Content-Disposition-Filename→`filename`, `content_type`→`"application/octet-stream"`. Die 416/seek-Fallback-Semantik bleibt unverändert (Bandwidth-DoS-Schutz).

`apps/module_firmware/api_urls.py`:

```python
from django.urls import path
from . import api_views
app_name = "module_firmware_api"
urlpatterns = [
    path("<int:pk>/download/", api_views.ModuleFirmwareDownloadView.as_view(), name="download"),
]
```

`apps/api/urls.py`: `path("v1/module-firmware/", include("apps.module_firmware.api_urls")),` ergänzen.

- [ ] **Step 4: Run** → PASS (403 ohne Assignment, Stream mit; ergänze einen Range-206-Test analog zum Deployment-Download-Test).
- [ ] **Step 5: Commit** `feat(module_firmware): device-key firmware download endpoint (assignment authz + range)`

---

## Task 10: Admin (Release + ImportJob + ModuleType-Felder)

**Files:** Modify `apps/module_firmware/admin.py`; Test `tests/module_firmware/test_fw_admin.py`

**Interfaces:** Produces: Admin-Registrierung `ModuleFirmwareRelease` (readonly Storage/sha/cosign, `all_objects`-Queryset, archive/restore-Actions), `ModuleFirmwareImportJob` (readonly Status/Log); ModuleType-Admin um `firmware_repo`/`release_asset_prefix`.

- [ ] **Step 1: Failing test**

```python
# tests/module_firmware/test_fw_admin.py
from django.contrib import admin
from apps.module_firmware.models import ModuleFirmwareRelease, ModuleFirmwareImportJob

def test_models_registered():
    assert ModuleFirmwareRelease in admin.site._registry
    assert ModuleFirmwareImportJob in admin.site._registry
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Admin** in `apps/module_firmware/admin.py` ergänzen:

```python
@admin.register(ModuleFirmwareRelease)
class ModuleFirmwareReleaseAdmin(admin.ModelAdmin):
    list_display = ("module_type", "variant", "version", "size_bytes", "imported_at", "archived_at")
    list_filter = ("module_type", "variant", "archived_at")
    search_fields = ("version", "source_tag")
    readonly_fields = ("storage_key", "sha256", "cosign_bundle_key", "size_bytes",
                       "source_repo", "source_tag", "source_github_url", "imported_at", "imported_by")
    actions = ["archive_selected", "restore_selected"]

    def get_queryset(self, request):
        return self.model.all_objects.all()

    @admin.action(description="Archivieren")
    def archive_selected(self, request, queryset):
        for r in queryset:
            r.archive()

    @admin.action(description="Wiederherstellen")
    def restore_selected(self, request, queryset):
        for r in queryset:
            r.restore()


@admin.register(ModuleFirmwareImportJob)
class ModuleFirmwareImportJobAdmin(admin.ModelAdmin):
    list_display = ("module_type", "tag", "status", "created_at", "completed_at")
    list_filter = ("status", "module_type")
    readonly_fields = ("status", "error_message", "release", "requested_by", "created_at", "completed_at")
```

ModuleType-Admin (falls vorhanden) um `firmware_repo`, `release_asset_prefix` in `fields`/`list_display` erweitern (Step 1 verifizieren, wo ModuleType registriert ist).

- [ ] **Step 4: Run** → PASS → **Step 5: Commit** `feat(module_firmware): admin for releases + import jobs`

---

## Task 11: Release-Liste + GitHub-Browser + Import/Archive-UI

> **REQUIRED:** `Skill("frontend-design")` vor UI-Arbeit.

**Files:** Modify `apps/module_firmware/views.py`, `apps/module_firmware/urls.py`; Create `templates/module_firmware/release_list.html`, `templates/module_firmware/_github_releases.html`; Test `tests/module_firmware/test_fw_views.py`

**Interfaces:** Produces: `ModuleFirmwareReleaseListView` (`module_firmware:release_list`, LoginRequired, read-all), `GithubFirmwareReleasesPartialView` (HTMX, staff), `FirmwareImportView` (POST, staff → queued `ModuleFirmwareImportJob`), `FirmwareArchiveView`/`FirmwareRestoreView` (POST, staff).

- [ ] **Step 1: Failing test**

```python
# tests/module_firmware/test_fw_views.py
import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from apps.module_firmware.models import ModuleType, ModuleFirmwareRelease, ModuleFirmwareImportJob
User = get_user_model()

@pytest.fixture
def rel(db):
    t = ModuleType.objects.create(key="fm", display_name="FM",
        firmware_repo="OE5XRX/FW-RemoteStation", release_asset_prefix="fm-sa818")
    return ModuleFirmwareRelease.objects.create(
        module_type=t, variant="vhf", version="26.07.04-01",
        storage_key="k", sha256="a"*64, size_bytes=5, source_repo="r", source_tag="26.07.04-01")

@pytest.mark.django_db
def test_list_visible_to_any_user(client, rel):
    client.force_login(User.objects.create_user(username="u", password="x"))
    resp = client.get(reverse("module_firmware:release_list"))
    assert resp.status_code == 200 and b"26.07.04-01" in resp.content

@pytest.mark.django_db
def test_import_is_staff_only(client, rel):
    client.force_login(User.objects.create_user(username="u", password="x"))
    resp = client.post(reverse("module_firmware:import"),
                       {"module_type": rel.module_type_id, "tag": "26.07.04-02"})
    assert resp.status_code == 403
    client.force_login(User.objects.create_user(username="s", password="x", is_staff=True))
    resp = client.post(reverse("module_firmware:import"),
                       {"module_type": rel.module_type_id, "tag": "26.07.04-02"})
    assert resp.status_code in (302, 200)
    assert ModuleFirmwareImportJob.objects.filter(tag="26.07.04-02").exists()
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Views** (`views.py`) — `ListView` (read-all) + Staff-gated Mutations (`UserPassesTestMixin` mit `raise_exception=True`, `test_func=lambda: self.request.user.is_staff`). `FirmwareImportView.post` validiert `module_type` + `tag`, snapshottet `source_repo=module_type.firmware_repo`, erstellt `ModuleFirmwareImportJob(status=PENDING, requested_by=request.user)`. GH-Browser-Partial ruft `github_releases.fetch_releases(module_type.firmware_repo)` und markiert bereits importierte Tags (Abgleich gegen `ModuleFirmwareRelease`/laufende Jobs). Archive/Restore rufen `release.archive()`/`.restore()`.

- [ ] **Step 4: URLs** (`urls.py`, `app_name = "module_firmware"` — bereits aus A vorhanden): ergänzen
  `release_list` (`firmware/`), `import` (`firmware/import/`), GH-Partial (`firmware/github/`), `archive`/`restore` (`firmware/<pk>/archive|restore/`). In `config/urls.py` ist `apps.module_firmware.urls` bereits included (A) — neue Pfade erscheinen darunter.

- [ ] **Step 5: Templates** — `release_list.html` (Tabelle: Typ/Variante/Version/Größe/imported_at/archiviert; Filter Typ/Variante/`show_archived`; GH-Browser-Panel via HTMX; Archive/Restore-Buttons nur für staff) + `_github_releases.html` (Release-Zeilen mit Import-Button, markiert importierte). `{% comment %}` statt `{# #}`. Frontend-design-Skill für Layout.

- [ ] **Step 6: Run** → PASS → **Step 7: Volltest** `pytest tests/module_firmware/ -v` + `python manage.py makemigrations --check --dry-run`.
- [ ] **Step 8: Commit** `feat(module_firmware): release list + GitHub browser + import/archive UI`

---

## Abschluss

- [ ] **Gesamter Testlauf:** `pytest` grün + `makemigrations --check --dry-run` sauber + `ruff`/Template-Guard grün.
- [ ] **Übergabe** an Kontor-Kind (Manager-Muster): PR → CI grün → Copilot-Loop bis 0 → „done".

## Self-Review-Notiz (bereits durchgeführt)

- **Spec-Abdeckung:** ModuleType-Erweiterung (T1), Release/ImportJob-Modelle + Soft-Delete (T2/T3), Storage (T4), cosign heads/main (T5/T7), Asset-Parse pro Variante (T6), Import pro Tag + SHA256SUMS + Idempotenz/Restore (T7), Worker-Tick (T8), Serve mit Assignment-Authz + Range (T9), Admin (T10), UI Lesen-alle/Mutation-staff + GH-Browser (T11). Deferred FW-Release-Abhängigkeit dokumentiert.
- **Type-Konsistenz:** `import_release_tag(job)`, `parse_variant_assets(names, prefix)→[VariantAsset]`, `parse_sha256sums(text)→dict`, `verify_blob_identity(blob,bundle,identity_regexp)`, `storage.release_key/bundle_key/upload_bytes/open_stream/delete` durchgängig T4–T9. `ModuleFirmwareRelease.objects` (versteckt archiviert) im Download (404 für archiviert), `all_objects` im Import (Restore).
- **Verifikationspflichten:** device-key-Test-Fixture (T9 Step 1), ModuleType-Admin-Registrierungsort (T10 Step 3), Range-Block-Copy aus DeploymentDownloadView (T9 Step 3), `app_name`/URL-Include-Status aus A (T11 Step 4) — je als „verifizieren" markiert.
```
