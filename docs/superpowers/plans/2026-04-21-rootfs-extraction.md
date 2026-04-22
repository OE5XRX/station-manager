# Rootfs extraction on ImageRelease import — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On ImageRelease import, extract the `root_a` partition from the downloaded wic and expose it as a second S3 artifact so OTA agents can download a payload that fits into their 1 GiB A/B slot.

**Architecture:** One new pure Python module (`apps/images/extraction.py`) parses GPT + streams the root partition through bz2. `ImageRelease` grows three `rootfs_*` fields. The existing `_run_import_job` worker gains an extraction + upload step with a strict rollback list. DeploymentCheck/Download read `rootfs_*` instead of `s3_*`; the rollouts views refuse to create deployments against non-OTA-ready releases. No agent-side change.

**Tech Stack:** Python 3.13, Django 6.0, pytest-django, stdlib `bz2` / `struct` / `hashlib` / `pathlib`.

**Design spec:** `docs/superpowers/specs/2026-04-21-rootfs-extraction-design.md`

---

## Task 1: Pure rootfs extractor module + unit tests

**Goal:** Ship `apps/images/extraction.py` with a single public function `extract_rootfs(wic_path, out_path) -> (size, sha256)`. Full TDD coverage using synthetic binary fixtures, zero Django / S3 / HTTP contact.

**Files:**
- Create: `apps/images/extraction.py`
- Create: `tests/test_images_extraction.py`

### Steps

- [ ] **Step 1: Write the synthetic-wic builder + happy-path test (failing)**

Create `tests/test_images_extraction.py`:

```python
"""Unit tests for apps.images.extraction.

The test helper `_build_synthetic_wic` constructs a minimal but valid
GPT image (MBR + GPT header + one partition entry + payload) in-memory.
That lets us verify `extract_rootfs`'s binary parsing without having
to keep a 4 GiB fixture under version control.
"""

from __future__ import annotations

import bz2
import hashlib
import struct
from pathlib import Path

import pytest

from apps.images.extraction import extract_rootfs

_SECTOR = 512


def _build_synthetic_wic(
    tmp_path: Path,
    *,
    partition_name: str = "root_a",
    start_lba: int = 8,
    end_lba: int = 15,  # 8 sectors = 4 kB partition
    ext4_magic: bool = True,
    signature: bytes = b"EFI PART",
    total_sectors: int = 64,
) -> tuple[Path, bytes]:
    """Build a minimal GPT-formatted file and return (path, partition_bytes).

    The caller gets the raw partition payload back so the happy-path test
    can compare extracted (decompressed) bytes against it.
    """
    assert total_sectors * _SECTOR >= (end_lba + 1) * _SECTOR, "sparse room needed"
    buf = bytearray(total_sectors * _SECTOR)

    # LBA 1 (offset 0x200): GPT header — only the fields our parser reads
    # are filled; CRCs stay zero (parser doesn't verify them).
    header = bytearray(92)
    header[0:8] = signature
    header[8:12] = struct.pack("<I", 0x00010000)   # revision
    header[12:16] = struct.pack("<I", 92)           # header size
    header[72:80] = struct.pack("<Q", 2)            # partition entry start LBA
    header[80:84] = struct.pack("<I", 1)            # num partition entries
    header[84:88] = struct.pack("<I", 128)          # partition entry size
    buf[_SECTOR : _SECTOR + 92] = header

    # LBA 2 (offset 0x400): one partition entry, 128 bytes.
    entry = bytearray(128)
    entry[32:40] = struct.pack("<Q", start_lba)
    entry[40:48] = struct.pack("<Q", end_lba)
    name_utf16 = partition_name.encode("utf-16-le")
    entry[56 : 56 + len(name_utf16)] = name_utf16
    buf[2 * _SECTOR : 2 * _SECTOR + 128] = entry

    # Partition payload: a recognizable pattern so we can round-trip check.
    partition_offset = start_lba * _SECTOR
    partition_size = (end_lba - start_lba + 1) * _SECTOR
    pattern = bytes(range(256))
    repeats = partition_size // 256
    tail = partition_size % 256
    payload = pattern * repeats + pattern[:tail]
    buf[partition_offset : partition_offset + partition_size] = payload

    # ext4 superblock magic at offset 1080 from partition start.
    if ext4_magic:
        buf[partition_offset + 1080 : partition_offset + 1082] = b"\x53\xEF"

    wic = tmp_path / "synthetic.wic"
    wic.write_bytes(bytes(buf))
    return wic, bytes(payload)


def test_extract_rootfs_round_trip(tmp_path):
    wic, partition_bytes = _build_synthetic_wic(tmp_path)
    out = tmp_path / "rootfs.bz2"

    size_bytes, sha256_hex = extract_rootfs(wic, out)

    # The compressed blob round-trips to the raw partition.
    decompressed = bz2.decompress(out.read_bytes())
    assert decompressed == partition_bytes

    # Returned metadata matches the actual compressed file on disk.
    assert size_bytes == out.stat().st_size
    assert sha256_hex == hashlib.sha256(out.read_bytes()).hexdigest()
```

- [ ] **Step 2: Run the test to confirm it fails with ImportError**

```
cd ~/station-manager && .venv/bin/pytest tests/test_images_extraction.py::test_extract_rootfs_round_trip -v
```

Expected: `ModuleNotFoundError: No module named 'apps.images.extraction'`.

- [ ] **Step 3: Implement `extract_rootfs`**

Create `apps/images/extraction.py`:

```python
"""Extract the `root_a` partition from a GPT-formatted wic image,
bz2-compressed, for use as the OTA download artifact.

Keeps the full wic (partition table + all partitions) available for
bare-metal flashing; the OTA agent only needs the rootfs bytes that
go onto an A/B slot, and producing that artifact here avoids a
dependency on the linux-image build pipeline to ship two outputs.
"""

from __future__ import annotations

import bz2
import hashlib
import struct
from pathlib import Path

_SECTOR = 512
_GPT_SIGNATURE = b"EFI PART"
_ROOT_PARTITION_NAME = "root_a"
_EXT4_MAGIC = b"\x53\xEF"
_EXT4_MAGIC_OFFSET = 1080
_CHUNK = 1 << 20  # 1 MiB read/compress chunk


def extract_rootfs(wic_path: Path, out_path: Path) -> tuple[int, str]:
    """Extract the `root_a` partition from ``wic_path``, bz2-compress
    it, and write the result to ``out_path``.

    Returns the (compressed_size_bytes, compressed_sha256_hex) of the
    written file. Raises ValueError on:

    - not a GPT image (header signature mismatch)
    - no partition named 'root_a'
    - the partition's declared range exceeds the wic file size
    - the partition does not begin with an ext4 superblock magic
      (the cheapest sanity check against a corrupt partition table)
    """
    wic_path = Path(wic_path)
    out_path = Path(out_path)

    with open(wic_path, "rb") as src:
        start_lba, end_lba = _locate_root_partition(src)
        _verify_bounds(wic_path, start_lba, end_lba)
        _verify_ext4_magic(src, start_lba)
        return _compress_partition(src, out_path, start_lba, end_lba)


def _locate_root_partition(src) -> tuple[int, int]:
    """Parse the GPT header + partition entries, return (start_lba,
    end_lba) of the partition named `root_a`.
    """
    src.seek(_SECTOR)  # LBA 1
    header = src.read(96)
    if header[0:8] != _GPT_SIGNATURE:
        raise ValueError("not a GPT image")

    # Only the fields we need from the 92-byte GPT header. CRCs are
    # intentionally not verified — this file came from a cosign-verified
    # release asset, we're inside the import worker, and the downstream
    # ext4 magic check catches the "header pointed us at garbage"
    # failure modes that matter to us.
    entry_start_lba = struct.unpack("<Q", header[72:80])[0]
    num_entries = struct.unpack("<I", header[80:84])[0]
    entry_size = struct.unpack("<I", header[84:88])[0]

    src.seek(entry_start_lba * _SECTOR)
    for _ in range(num_entries):
        entry = src.read(entry_size)
        if len(entry) != entry_size:
            break  # truncated, treat as no match
        name = entry[56:128].decode("utf-16-le").rstrip("\x00")
        if name == _ROOT_PARTITION_NAME:
            start_lba = struct.unpack("<Q", entry[32:40])[0]
            end_lba = struct.unpack("<Q", entry[40:48])[0]
            return start_lba, end_lba

    raise ValueError(f"no partition named {_ROOT_PARTITION_NAME!r}")


def _verify_bounds(wic_path: Path, start_lba: int, end_lba: int) -> None:
    file_size = wic_path.stat().st_size
    partition_end_byte = (end_lba + 1) * _SECTOR
    if partition_end_byte > file_size:
        raise ValueError(
            f"{_ROOT_PARTITION_NAME} (start={start_lba}, end={end_lba}) "
            f"exceeds wic size {file_size}"
        )


def _verify_ext4_magic(src, start_lba: int) -> None:
    src.seek(start_lba * _SECTOR + _EXT4_MAGIC_OFFSET)
    magic = src.read(2)
    if magic != _EXT4_MAGIC:
        raise ValueError(
            f"{_ROOT_PARTITION_NAME} is not ext4 "
            f"(magic at byte +{_EXT4_MAGIC_OFFSET} was {magic!r}, "
            f"expected {_EXT4_MAGIC!r})"
        )


def _compress_partition(src, out_path: Path, start_lba: int, end_lba: int) -> tuple[int, str]:
    src.seek(start_lba * _SECTOR)
    remaining = (end_lba - start_lba + 1) * _SECTOR
    compressor = bz2.BZ2Compressor(9)
    sha = hashlib.sha256()
    size = 0

    with open(out_path, "wb") as dst:
        while remaining > 0:
            chunk = src.read(min(_CHUNK, remaining))
            if not chunk:
                raise ValueError(
                    f"{_ROOT_PARTITION_NAME} read ended early "
                    f"({remaining} bytes unread)"
                )
            remaining -= len(chunk)
            compressed = compressor.compress(chunk)
            if compressed:
                dst.write(compressed)
                sha.update(compressed)
                size += len(compressed)
        tail = compressor.flush()
        if tail:
            dst.write(tail)
            sha.update(tail)
            size += len(tail)

    return size, sha.hexdigest()
```

- [ ] **Step 4: Run the happy-path test to confirm PASS**

```
cd ~/station-manager && .venv/bin/pytest tests/test_images_extraction.py::test_extract_rootfs_round_trip -v
```

Expected: `1 passed`.

- [ ] **Step 5: Add the four failure-mode tests**

Append to `tests/test_images_extraction.py`:

```python
def test_extract_rootfs_rejects_missing_partition(tmp_path):
    wic, _ = _build_synthetic_wic(tmp_path, partition_name="rootfs")
    out = tmp_path / "rootfs.bz2"

    with pytest.raises(ValueError, match=r"no partition named 'root_a'"):
        extract_rootfs(wic, out)


def test_extract_rootfs_rejects_bad_ext4_magic(tmp_path):
    wic, _ = _build_synthetic_wic(tmp_path, ext4_magic=False)
    out = tmp_path / "rootfs.bz2"

    with pytest.raises(ValueError, match=r"not ext4"):
        extract_rootfs(wic, out)


def test_extract_rootfs_rejects_non_gpt(tmp_path):
    wic, _ = _build_synthetic_wic(tmp_path, signature=b"\x00" * 8)
    out = tmp_path / "rootfs.bz2"

    with pytest.raises(ValueError, match=r"not a GPT image"):
        extract_rootfs(wic, out)


def test_extract_rootfs_rejects_out_of_bounds(tmp_path):
    # end_lba points past the 64-sector file (start=8, end=999).
    wic, _ = _build_synthetic_wic(tmp_path, start_lba=8, end_lba=999)
    out = tmp_path / "rootfs.bz2"

    with pytest.raises(ValueError, match=r"exceeds wic size"):
        extract_rootfs(wic, out)
```

- [ ] **Step 6: Run the full test module to confirm all 5 pass**

```
cd ~/station-manager && .venv/bin/pytest tests/test_images_extraction.py -v
```

Expected: `5 passed`.

- [ ] **Step 7: Ruff + full suite sanity check**

```
cd ~/station-manager && .venv/bin/ruff format apps/images/extraction.py tests/test_images_extraction.py && \
  .venv/bin/ruff check apps/images/extraction.py tests/test_images_extraction.py && \
  .venv/bin/pytest -x -q 2>&1 | tail -3
```

Expected: `All checks passed!`, tests count = previous total + 5 passed, no regressions.

- [ ] **Step 8: Commit**

```
cd ~/station-manager && \
  git add apps/images/extraction.py tests/test_images_extraction.py && \
  git commit -m "apps/images: pure-Python GPT extractor for root_a partition

Adds extract_rootfs(wic_path, out_path) -> (size, sha256). Parses the
GPT header and partition entry array, matches the partition named
'root_a' exactly, sanity-checks the declared range against the file
size and the ext4 superblock magic at byte +1080, then stream-
compresses the partition bytes through bz2 level 9 into the output
path. Peak memory ~2 MiB (chunk + compressor state). CRCs are not
verified — the wic came from a cosign-verified release asset, so the
integrity signal lives one layer up, and the ext4 magic check catches
the corrupt-partition-table failure modes that would actually brick a
trial boot.

Five tests cover: round-trip happy path, missing partition, missing
ext4 magic, non-GPT header, out-of-bounds declared range. Synthetic
wic builder keeps the fixtures in the test source instead of binary
blobs under version control."
```

---

## Task 2: ImageRelease rootfs fields + is_ota_ready + migration

**Goal:** Three additive fields on `ImageRelease` + an `is_ota_ready` convenience property + a migration. All fields nullable/blank so existing rows survive without backfill; the guard logic in later tasks treats empty fields as "not OTA-ready".

**Files:**
- Modify: `apps/images/models.py`
- Create: `apps/images/migrations/0005_imagerelease_rootfs_fields.py`

### Steps

- [ ] **Step 1: Add the fields + property to the model**

In `apps/images/models.py`, inside `class ImageRelease`, below the existing `size_bytes` line (currently line 16), add:

```python
    rootfs_s3_key = models.CharField(
        _("rootfs S3 object key"),
        max_length=512,
        blank=True,
        default="",
        help_text=_(
            "S3 key for the extracted root_a partition, bz2-compressed. "
            "Empty means this release has not been processed for OTA yet "
            "(re-import required)."
        ),
    )
    rootfs_sha256 = models.CharField(
        _("rootfs SHA-256"), max_length=64, blank=True, default=""
    )
    rootfs_size_bytes = models.BigIntegerField(
        _("rootfs size in bytes"), null=True, blank=True
    )
```

Then, below the `def __str__` method, add:

```python
    @property
    def is_ota_ready(self) -> bool:
        """True iff the rootfs artifact has been extracted and uploaded.

        OTA deployments against this release are only viable when this
        returns True. Provisioning / bare-metal flash only need the
        full wic (``s3_key``), so an ``is_ota_ready == False`` release
        is still usable for those flows.
        """
        return bool(self.rootfs_s3_key)
```

- [ ] **Step 2: Generate the migration**

```
cd ~/station-manager && .venv/bin/python manage.py makemigrations images --settings=config.settings.test
```

Expected: creates `apps/images/migrations/0005_imagerelease_rootfs_fields.py`.

- [ ] **Step 3: Inspect the generated migration — no surprises**

Run:

```
cat ~/station-manager/apps/images/migrations/0005_imagerelease_rootfs_fields.py
```

Expected: three `AddField` operations, nothing else. Property changes don't affect migrations. If anything unexpected is in there (renames, removes), stop and investigate — `is_ota_ready` is a `@property` so Django must not see it as a field.

- [ ] **Step 4: Run migrations + full test suite to confirm schema is sane**

```
cd ~/station-manager && .venv/bin/pytest -x -q 2>&1 | tail -3
```

Expected: all existing tests still pass. New fields are nullable/blank, so no test should break.

- [ ] **Step 5: Ruff**

```
cd ~/station-manager && .venv/bin/ruff format apps/images/models.py apps/images/migrations/0005_imagerelease_rootfs_fields.py && \
  .venv/bin/ruff check apps/images/models.py
```

Expected: `All checks passed!`.

- [ ] **Step 6: Commit**

```
cd ~/station-manager && \
  git add apps/images/models.py apps/images/migrations/0005_imagerelease_rootfs_fields.py && \
  git commit -m "apps/images: rootfs_* fields + is_ota_ready on ImageRelease

Additive: the existing s3_key/sha256/size_bytes keep describing the
full wic (which Provisioning and bare-metal flash consume). The new
rootfs_s3_key/rootfs_sha256/rootfs_size_bytes describe the extracted
root partition artifact that OTA agents download. Existing rows stay
with empty strings + NULL; is_ota_ready returns False for them, and
downstream views refuse to create OTA deployments against those until
they are re-imported. Migration 0005 only adds the fields — no
backfill, no data migration."
```

---

## Task 3: Worker integration — extract, upload, rollback-list

**Goal:** Extend `_run_import_job` to decompress the wic, call `extract_rootfs`, upload the rootfs artifact, and populate the new model fields. Replace the current two-separate-cleanup-loops with a single rollback list so every already-uploaded S3 key is cleaned up on any failure.

**Files:**
- Modify: `apps/provisioning/management/commands/run_background_jobs.py`
- Modify: `apps/images/storage.py` (one tiny helper for the rootfs S3 key)
- Modify: `tests/test_images.py`

### Steps

- [ ] **Step 1: Add `release_rootfs_key` helper**

In `apps/images/storage.py`, below the existing `release_bundle_key` function (line 11-12), add:

```python
def release_rootfs_key(tag: str, machine: str) -> str:
    """S3 key for the extracted root partition artifact.

    Lives alongside the full wic under the same ``images/<tag>/``
    prefix so cleanup of a given release can delete the whole folder.
    """
    return f"images/{tag}/{machine}.rootfs.bz2"
```

- [ ] **Step 2: Write the first integration test (failing)**

Open `tests/test_images.py`. Find where the existing `_run_import_job` tests live (search for `fetch_release_asset` or `run_import_job`). Add a new test class or append tests in the existing one — follow the file's prevailing shape. Here is the self-contained test code to add (adjust imports / class structure to match the file):

```python
class TestRunImportJobRootfsExtraction:
    """Integration tests for the rootfs-extraction step in the worker."""

    @pytest.fixture
    def synthetic_wic_bytes(self, tmp_path):
        """A valid bz2-compressed synthetic wic with a root_a partition."""
        import bz2 as bz2_mod

        from tests.test_images_extraction import _build_synthetic_wic

        wic, _ = _build_synthetic_wic(tmp_path)
        return bz2_mod.compress(wic.read_bytes())

    def test_run_import_job_populates_rootfs_fields(
        self, db, synthetic_wic_bytes, monkeypatch
    ):
        from apps.images import cosign, github
        from apps.images import storage as image_storage
        from apps.images.models import ImageImportJob, ImageRelease
        from apps.provisioning.management.commands.run_background_jobs import (
            _run_import_job,
        )

        # Collect uploaded keys so we can assert the rootfs one is there.
        uploaded: dict[str, bytes] = {}

        def fake_fetch(repo, tag, machine):
            return github.ReleaseAsset(
                wic_bytes=synthetic_wic_bytes,
                sha256="a" * 64,
                bundle_bytes=b"fake-bundle",
            )

        def fake_upload(key, data):
            uploaded[key] = data

        def fake_open(key):
            from io import BytesIO

            return BytesIO(uploaded[key])

        monkeypatch.setattr(github, "fetch_release_asset", fake_fetch)
        monkeypatch.setattr(cosign, "verify_blob", lambda **kw: None)
        monkeypatch.setattr(image_storage, "upload_bytes", fake_upload)
        monkeypatch.setattr(image_storage, "open_stream", fake_open)

        job = ImageImportJob.objects.create(
            tag="test-1",
            machine=ImageRelease.Machine.QEMU,
            status=ImageImportJob.Status.RUNNING,
        )
        _run_import_job(job)
        job.refresh_from_db()

        assert job.status == ImageImportJob.Status.READY, job.error_message
        release = ImageRelease.objects.get(tag="test-1", machine=ImageRelease.Machine.QEMU)
        assert release.rootfs_s3_key == "images/test-1/qemux86-64.rootfs.bz2"
        assert release.rootfs_s3_key in uploaded
        assert release.rootfs_size_bytes == len(uploaded[release.rootfs_s3_key])
        assert len(release.rootfs_sha256) == 64
        assert release.is_ota_ready is True
```

- [ ] **Step 3: Run the new test — expect failure**

```
cd ~/station-manager && .venv/bin/pytest tests/test_images.py::TestRunImportJobRootfsExtraction::test_run_import_job_populates_rootfs_fields -v
```

Expected: FAIL — current `_run_import_job` doesn't do extraction, so `release.rootfs_s3_key` is `""`.

- [ ] **Step 4: Extend `_run_import_job` with extraction + rollback list**

In `apps/provisioning/management/commands/run_background_jobs.py`, update the imports at the top of the file:

```python
from apps.images import cosign, extraction, github
```

Replace the existing `_run_import_job` function (starts around line 93) with:

```python
def _run_import_job(job: ImageImportJob) -> None:
    repo = getattr(settings, "LINUX_IMAGE_REPO", "OE5XRX/linux-image")
    uploaded_keys: list[str] = []
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
        rootfs_key = image_storage.release_rootfs_key(job.tag, job.machine)

        image_storage.upload_bytes(wic_key, asset.wic_bytes)
        uploaded_keys.append(wic_key)
        image_storage.upload_bytes(bundle_key, asset.bundle_bytes)
        uploaded_keys.append(bundle_key)

        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            compressed_in = tmp / "base.wic.bz2"
            decompressed = tmp / "base.wic"
            rootfs_out = tmp / "rootfs.bz2"

            compressed_in.write_bytes(asset.wic_bytes)
            _decompress_to(compressed_in, decompressed)

            rootfs_size, rootfs_sha = extraction.extract_rootfs(
                decompressed, rootfs_out
            )
            image_storage.upload_bytes(rootfs_key, rootfs_out.read_bytes())
            uploaded_keys.append(rootfs_key)

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
            },
        )
        job.image_release = release
        job.status = ImageImportJob.Status.READY
        job.completed_at = timezone.now()
        job.save(update_fields=["image_release", "status", "completed_at"])
    except Exception as exc:
        # Strict rollback: any success upstream still leaves S3 clean,
        # and no half-populated ImageRelease row ever appears.
        for key in uploaded_keys:
            try:
                image_storage.delete(key)
            except Exception:
                pass
        job.status = ImageImportJob.Status.FAILED
        job.error_message = str(exc)
        job.completed_at = timezone.now()
        job.save(update_fields=["status", "error_message", "completed_at"])
```

- [ ] **Step 5: Run the new test — expect PASS**

```
cd ~/station-manager && .venv/bin/pytest tests/test_images.py::TestRunImportJobRootfsExtraction::test_run_import_job_populates_rootfs_fields -v
```

Expected: `1 passed`.

- [ ] **Step 6: Add the rollback test**

Append to `TestRunImportJobRootfsExtraction` in `tests/test_images.py`:

```python
    def test_run_import_job_rolls_back_on_extraction_failure(
        self, db, synthetic_wic_bytes, monkeypatch
    ):
        from apps.images import cosign, extraction, github
        from apps.images import storage as image_storage
        from apps.images.models import ImageImportJob, ImageRelease
        from apps.provisioning.management.commands.run_background_jobs import (
            _run_import_job,
        )

        uploaded: dict[str, bytes] = {}
        deleted: list[str] = []

        monkeypatch.setattr(
            github,
            "fetch_release_asset",
            lambda **kw: github.ReleaseAsset(
                wic_bytes=synthetic_wic_bytes,
                sha256="a" * 64,
                bundle_bytes=b"fake-bundle",
            ),
        )
        monkeypatch.setattr(cosign, "verify_blob", lambda **kw: None)
        monkeypatch.setattr(
            image_storage,
            "upload_bytes",
            lambda key, data: uploaded.__setitem__(key, data),
        )
        monkeypatch.setattr(image_storage, "delete", lambda key: deleted.append(key))

        def boom(*args, **kwargs):
            raise ValueError("synthetic: extraction exploded")

        monkeypatch.setattr(extraction, "extract_rootfs", boom)

        job = ImageImportJob.objects.create(
            tag="fail-1",
            machine=ImageRelease.Machine.QEMU,
            status=ImageImportJob.Status.RUNNING,
        )
        _run_import_job(job)
        job.refresh_from_db()

        assert job.status == ImageImportJob.Status.FAILED
        assert "extraction exploded" in job.error_message

        # Every already-uploaded key was cleaned up (wic + bundle; the
        # rootfs key was never uploaded because extraction raised before
        # that step). Order reflects the insertion order in uploaded_keys.
        wic_key = image_storage.release_key("fail-1", ImageRelease.Machine.QEMU)
        bundle_key = image_storage.release_bundle_key("fail-1", ImageRelease.Machine.QEMU)
        assert wic_key in deleted
        assert bundle_key in deleted

        # No half-populated ImageRelease row.
        assert not ImageRelease.objects.filter(
            tag="fail-1", machine=ImageRelease.Machine.QEMU
        ).exists()
```

- [ ] **Step 7: Run both worker integration tests**

```
cd ~/station-manager && .venv/bin/pytest tests/test_images.py::TestRunImportJobRootfsExtraction -v
```

Expected: `2 passed`.

- [ ] **Step 8: Full suite + ruff**

```
cd ~/station-manager && .venv/bin/ruff format apps/images/storage.py apps/provisioning/management/commands/run_background_jobs.py tests/test_images.py && \
  .venv/bin/ruff check apps/images/ apps/provisioning/management/ tests/test_images.py && \
  .venv/bin/pytest -x -q 2>&1 | tail -3
```

Expected: clean ruff, all tests pass.

- [ ] **Step 9: Commit**

```
cd ~/station-manager && \
  git add apps/images/storage.py apps/provisioning/management/commands/run_background_jobs.py tests/test_images.py && \
  git commit -m "import worker: extract + upload rootfs, rollback on any failure

Extends _run_import_job with three steps after the full-wic upload:
decompress the wic into a TemporaryDirectory, call
apps.images.extraction.extract_rootfs to produce rootfs.bz2, upload
that to images/<tag>/<machine>.rootfs.bz2, and populate the new
rootfs_* columns on ImageRelease.

The two existing separate cleanup loops (one for wic+bundle on DB
failure, one for provisioning output on FK-save failure) collapse
into a single uploaded_keys list that the outer except drains.
Strict rollback: if anything from fetch through DB-write fails,
every S3 key we uploaded is best-effort deleted and no
ImageRelease row is created. Keeps the invariant that every
ImageRelease row in the DB is OTA-ready."
```

---

## Task 4: Deployment API — read `rootfs_*` instead of `s3_*`

**Goal:** `DeploymentCheckView` returns the rootfs metadata; `DeploymentDownloadView` streams the rootfs artifact. Both paths return safe defensive responses (204 / 404) if a release somehow lacks `rootfs_s3_key` — but the Task-5 creation guard should prevent that from happening in practice.

**Files:**
- Modify: `apps/deployments/api_views.py`
- Modify: `tests/test_deployments.py`

### Steps

- [ ] **Step 1: Write the first new endpoint test (failing)**

Find the `TestDeploymentCheck` class in `tests/test_deployments.py` (around the top). Add this test:

```python
    def test_check_returns_rootfs_metadata_not_full_wic(
        self, client, station_with_key, deployment_result
    ):
        """After the extraction change, the check response must point
        the agent at the rootfs artifact, not the full wic."""
        station, private_key = station_with_key
        release = deployment_result.deployment.image_release
        release.rootfs_s3_key = "images/test/qemux86-64.rootfs.bz2"
        release.rootfs_sha256 = "c" * 64
        release.rootfs_size_bytes = 250_000_000
        release.save(
            update_fields=["rootfs_s3_key", "rootfs_sha256", "rootfs_size_bytes"]
        )

        body = json.dumps({"current_version": ""}).encode("utf-8")
        response = client.post(
            reverse("api:deployment_check"),
            data=body,
            content_type="application/json",
            **device_auth_headers(private_key, station.pk, body),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["checksum_sha256"] == "c" * 64
        assert data["size_bytes"] == 250_000_000
        # The wic values must NOT leak through.
        assert data["checksum_sha256"] != release.sha256
```

- [ ] **Step 2: Run the new test to confirm it fails**

```
cd ~/station-manager && .venv/bin/pytest tests/test_deployments.py::TestDeploymentCheck::test_check_returns_rootfs_metadata_not_full_wic -v
```

Expected: FAIL — current view returns `image.sha256` / `image.size_bytes`.

- [ ] **Step 3: Update `DeploymentCheckView` to use rootfs fields**

In `apps/deployments/api_views.py`, replace the block at line 75-90 (from `if result is None:` through the `return Response(data)`) with:

```python
        if result is None:
            return Response(status=status.HTTP_204_NO_CONTENT)

        image = result.deployment.image_release
        if not image.rootfs_s3_key:
            # Defense-in-depth: the creation-time guard in
            # UpgradeStationView/UpgradeGroupView should prevent
            # deployments from being created against non-OTA-ready
            # releases. If one still reached us (admin deleted the
            # field, data migration regression), refuse with 204
            # instead of 200 so the agent does not retry-loop on a
            # 409. The operator sees the Deployment row stuck in
            # PENDING and can investigate.
            logger.error(
                "DeploymentCheck: release %s has empty rootfs_s3_key; "
                "Deployment %d cannot proceed",
                image.tag,
                result.deployment_id,
            )
            return Response(status=status.HTTP_204_NO_CONTENT)

        data = DeploymentCheckResponseSerializer(
            {
                "deployment_result_id": result.pk,
                "deployment_id": result.deployment_id,
                "deployment_result_status": result.status,
                "target_tag": image.tag,
                "checksum_sha256": image.rootfs_sha256,
                "size_bytes": image.rootfs_size_bytes,
                "download_url": f"/api/v1/deployments/{result.deployment_id}/download/",
            }
        ).data
        return Response(data)
```

- [ ] **Step 4: Run the new test to confirm PASS + no existing check tests break**

```
cd ~/station-manager && .venv/bin/pytest tests/test_deployments.py::TestDeploymentCheck -v
```

Expected: the new test passes. Existing check tests that assert `size_bytes == release.size_bytes` will now fail — see Step 5.

- [ ] **Step 5: Fix existing check tests that still compare against full-wic values**

Search for tests in `TestDeploymentCheck` that assert against `release.size_bytes` or `release.sha256`. The existing `test_check_returns_pending_deployment` test (near line 12 of the file) asserts `data["size_bytes"] == release.size_bytes`. Update the fixture so the `deployment_result` fixture's release has rootfs_* populated, OR inline-patch the release in the test:

Look at the `deployment_result` / `image_release` fixtures (usually in `tests/conftest.py`). If the `image_release` fixture doesn't set rootfs_*, add them:

In `tests/conftest.py`, find the `image_release` fixture and extend it:

```python
@pytest.fixture
def image_release(db):
    return ImageRelease.objects.create(
        tag="v1-alpha",
        machine="qemux86-64",
        s3_key="images/v1-alpha/qemux86-64.wic.bz2",
        sha256="a" * 64,
        size_bytes=1000,
        is_latest=True,
        # NEW: make the fixture OTA-ready by default.
        rootfs_s3_key="images/v1-alpha/qemux86-64.rootfs.bz2",
        rootfs_sha256="b" * 64,
        rootfs_size_bytes=500,
    )
```

Then update tests that asserted against the old wic values:

```python
        assert data["checksum_sha256"] == release.rootfs_sha256
        assert data["size_bytes"] == release.rootfs_size_bytes
```

Also run the suite to see which assertions break and fix each:

```
cd ~/station-manager && .venv/bin/pytest tests/test_deployments.py -x -q 2>&1 | tail -30
```

Make the minimum set of changes that gets the existing tests green against the new rootfs_* behaviour. Do not change unrelated tests.

- [ ] **Step 6: Add the defense-in-depth CheckView test**

Append to `TestDeploymentCheck`:

```python
    def test_check_returns_204_when_release_has_no_rootfs(
        self, client, station_with_key, deployment_result
    ):
        """Defense-in-depth: even if a deployment somehow got created
        against a non-OTA-ready release, the check endpoint must
        return 204 (not 200, not 409) so the agent falls back to the
        no-deployment-for-me path instead of retry-looping."""
        station, private_key = station_with_key
        release = deployment_result.deployment.image_release
        release.rootfs_s3_key = ""
        release.save(update_fields=["rootfs_s3_key"])

        body = json.dumps({"current_version": ""}).encode("utf-8")
        response = client.post(
            reverse("api:deployment_check"),
            data=body,
            content_type="application/json",
            **device_auth_headers(private_key, station.pk, body),
        )
        assert response.status_code == 204
```

Run: `cd ~/station-manager && .venv/bin/pytest tests/test_deployments.py::TestDeploymentCheck::test_check_returns_204_when_release_has_no_rootfs -v`

Expected: PASS.

- [ ] **Step 7: Write the DownloadView tests (failing)**

Find `TestDeploymentDownload` (or the equivalent name — search `class.*Download`) in `tests/test_deployments.py`. Add:

```python
    def test_download_streams_rootfs_artifact(
        self, client, station_with_key, deployment_result, monkeypatch
    ):
        from io import BytesIO

        from apps.deployments import api_views
        from apps.images import storage as image_storage

        station, private_key = station_with_key
        release = deployment_result.deployment.image_release
        # Put the result into an active state; download view only
        # serves resumable/active statuses.
        deployment_result.status = deployment_result.Status.DOWNLOADING
        deployment_result.save(update_fields=["status"])

        captured: dict[str, str] = {}

        def fake_open(key):
            captured["key"] = key
            return BytesIO(b"rootfs-bytes")

        monkeypatch.setattr(image_storage, "open_stream", fake_open)

        response = client.get(
            reverse(
                "api:deployment_download",
                kwargs={"pk": deployment_result.deployment_id},
            ),
            **device_auth_headers(
                private_key,
                station.pk,
                b"",
                method="GET",
                path=f"/api/v1/deployments/{deployment_result.deployment_id}/download/",
            ),
        )
        assert response.status_code == 200
        # DownloadView must stream from the rootfs_s3_key, not s3_key.
        assert captured["key"] == release.rootfs_s3_key
        assert captured["key"] != release.s3_key

    def test_download_returns_404_when_release_has_no_rootfs(
        self, client, station_with_key, deployment_result
    ):
        station, private_key = station_with_key
        deployment_result.status = deployment_result.Status.DOWNLOADING
        deployment_result.save(update_fields=["status"])
        release = deployment_result.deployment.image_release
        release.rootfs_s3_key = ""
        release.save(update_fields=["rootfs_s3_key"])

        response = client.get(
            reverse(
                "api:deployment_download",
                kwargs={"pk": deployment_result.deployment_id},
            ),
            **device_auth_headers(
                private_key,
                station.pk,
                b"",
                method="GET",
                path=f"/api/v1/deployments/{deployment_result.deployment_id}/download/",
            ),
        )
        assert response.status_code == 404
```

> **Note on `device_auth_headers`:** the helper signature already exists in `tests/conftest.py`. If it doesn't accept `method` / `path` kwargs for GET requests, stop and read the helper — adjust the test call site to whatever the helper expects. The point of the test is that the request is authenticated as the station; the exact wire format is a detail.

- [ ] **Step 8: Run the download tests to confirm failure**

```
cd ~/station-manager && .venv/bin/pytest tests/test_deployments.py -k download -v 2>&1 | tail -20
```

Expected: the new tests fail — current view opens `image.s3_key` and has no rootfs_s3_key handling.

- [ ] **Step 9: Update `DeploymentDownloadView`**

In `apps/deployments/api_views.py`, locate the `image = result.deployment.image_release` line inside `DeploymentDownloadView.get` (around line 368). Replace the next block up to the `stream = image_storage.open_stream(...)` call with:

```python
        image = result.deployment.image_release
        if not image.rootfs_s3_key:
            # Defense-in-depth — the creation-time guard should keep
            # us out of this branch, but we refuse rather than stream
            # the full wic (which is 4× the target slot size) if
            # something regressed.
            logger.error(
                "DeploymentDownload: release %s has empty rootfs_s3_key; "
                "deployment %d cannot be served",
                image.tag,
                result.deployment_id,
            )
            return Response(
                {"detail": "Release not prepared for OTA."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Fail loud but controlled on storage issues — a 500 without a
        # response body makes agents retry blindly and leaves operators
        # poking stack traces in sentry. Map missing-key / permission
        # errors to 502 Bad Gateway (server knows about the deployment,
        # the upstream object store doesn't have what we need).
        try:
            stream = image_storage.open_stream(image.rootfs_s3_key)
        except Exception as exc:
            logger.error(
                "Failed to open rootfs %s for deployment %s: %s",
                image.rootfs_s3_key,
                result.deployment_id,
                exc,
            )
            return Response(
                {"detail": "Image artifact unavailable from storage backend."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
```

And replace the `total_size = image.size_bytes ...` line a few lines later with:

```python
        total_size = (
            image.rootfs_size_bytes
            if image.rootfs_size_bytes and image.rootfs_size_bytes > 0
            else None
        )
```

And the `filename` assignment a bit further down:

```python
        filename = f"oe5xrx-{image.machine}-{image.tag}.rootfs.bz2"
```

- [ ] **Step 10: Run all deployment tests**

```
cd ~/station-manager && .venv/bin/pytest tests/test_deployments.py -v 2>&1 | tail -20
```

Expected: all pass. If older tests break because they asserted a filename ending in `.wic.bz2`, update their expected filename to end in `.rootfs.bz2` — that's the new reality.

- [ ] **Step 11: Full suite + ruff**

```
cd ~/station-manager && .venv/bin/ruff format apps/deployments/api_views.py tests/test_deployments.py tests/conftest.py && \
  .venv/bin/ruff check apps/deployments/ tests/test_deployments.py && \
  .venv/bin/pytest -x -q 2>&1 | tail -3
```

Expected: clean ruff, all tests pass.

- [ ] **Step 12: Commit**

```
cd ~/station-manager && \
  git add apps/deployments/api_views.py tests/test_deployments.py tests/conftest.py && \
  git commit -m "DeploymentCheck/Download: serve rootfs_*, not the full wic

CheckView returns image.rootfs_sha256 and image.rootfs_size_bytes in
the JSON body. DownloadView streams from image.rootfs_s3_key. The
download filename now ends in .rootfs.bz2 to reflect what's actually
being sent.

Defense-in-depth on both paths: if a release somehow has an empty
rootfs_s3_key (creation-time guard regression, admin mutation, data
migration bug), Check returns 204 (not 200, not 409 — a 409 would
make the agent retry-loop) and Download returns 404. The operator
sees a Deployment stuck in PENDING on the dashboard and investigates
instead of hearing the station beat against the server."
```

---

## Task 5: UpgradeStation/UpgradeGroup creation-time guard

**Goal:** Refuse to create new Deployments against releases that have not been processed for OTA yet. Loud, local error for the operator beats a silent retry-loop on the station.

**Files:**
- Modify: `apps/rollouts/views.py`
- Modify: `tests/test_rollouts.py`

### Steps

- [ ] **Step 1: Write the UpgradeStation refusal test (failing)**

In `tests/test_rollouts.py`, find `class TestUpgradeActions` (around line 118 from earlier inspection). Add:

```python
    def test_upgrade_station_refuses_release_without_rootfs(
        self, client, admin_user, station, image_release
    ):
        """If the latest release hasn't been extracted for OTA yet,
        UpgradeStationView must refuse at creation time instead of
        creating a Deployment the station can never install."""
        from django.urls import reverse

        from apps.deployments.models import Deployment
        from apps.images.models import ImageRelease

        station.current_image_release = image_release
        station.save(update_fields=["current_image_release"])

        # Pretend a newer release exists, but hasn't been processed
        # for OTA (rootfs_s3_key empty).
        ImageRelease.objects.filter(is_latest=True, machine="qemux86-64").update(
            is_latest=False
        )
        ImageRelease.objects.create(
            tag="v2-unprocessed",
            machine="qemux86-64",
            s3_key="images/v2/qemu.wic.bz2",
            sha256="z" * 64,
            size_bytes=1,
            is_latest=True,
            # rootfs_* deliberately left empty.
        )

        client.force_login(admin_user)
        response = client.post(
            reverse("rollouts:upgrade_station", kwargs={"station_pk": station.pk}),
            follow=True,
        )
        assert response.status_code == 200

        # No Deployment / DeploymentResult row was created.
        assert not Deployment.objects.filter(target_station=station).exists()

        # A flash error message names the tag and asks the operator
        # to re-import.
        msgs = [str(m) for m in response.context["messages"]]
        assert any("v2-unprocessed" in m and "re-import" in m.lower() for m in msgs)
```

- [ ] **Step 2: Run the test, confirm failure**

```
cd ~/station-manager && .venv/bin/pytest tests/test_rollouts.py::TestUpgradeActions::test_upgrade_station_refuses_release_without_rootfs -v
```

Expected: FAIL — current view happily creates the Deployment.

- [ ] **Step 3: Add the guard to `UpgradeStationView`**

In `apps/rollouts/views.py`, find `UpgradeStationView.post` (around line 100 after the earlier refactor). Between the `target = ImageRelease.objects.filter(...).first()` block and the `if station.current_image_release_id == target.pk:` check, add:

```python
        if not target.is_ota_ready:
            messages.error(
                request,
                _(
                    "Release %(tag)s is not prepared for OTA — "
                    "re-import it first."
                )
                % {"tag": target.tag},
            )
            return redirect("stations:station_detail", pk=station.pk)
```

- [ ] **Step 4: Run the test, confirm PASS**

```
cd ~/station-manager && .venv/bin/pytest tests/test_rollouts.py::TestUpgradeActions::test_upgrade_station_refuses_release_without_rootfs -v
```

Expected: PASS.

- [ ] **Step 5: Write the UpgradeGroup skip test (failing)**

Append to the same test class:

```python
    def test_upgrade_group_skips_releases_without_rootfs(
        self, client, admin_user, make_station_tag, image_release
    ):
        """A group upgrade targeting two machines where one's latest
        release is OTA-ready and the other's isn't must create a
        Deployment for the ready one and tally the non-ready one into
        the 'skipped' summary.

        The qemu station is on `image_release` (older, OTA-ready via
        the fixture updated in Task 4), upgrading to qemu_ready. The
        rpi station is on rpi_not_ready, which is_latest but has
        empty rootfs_* → must be skipped.
        """
        from django.urls import reverse

        from apps.deployments.models import Deployment
        from apps.images.models import ImageRelease
        from apps.stations.models import Station

        tag = make_station_tag("group-a")

        ImageRelease.objects.filter(is_latest=True, machine="qemux86-64").update(
            is_latest=False
        )
        qemu_ready = ImageRelease.objects.create(
            tag="qemu-ready",
            machine="qemux86-64",
            s3_key="wic",
            sha256="a" * 64,
            size_bytes=1,
            rootfs_s3_key="rootfs",
            rootfs_sha256="b" * 64,
            rootfs_size_bytes=1,
            is_latest=True,
        )
        rpi_not_ready = ImageRelease.objects.create(
            tag="rpi-not-ready",
            machine="raspberrypi4-64",
            s3_key="wic2",
            sha256="c" * 64,
            size_bytes=1,
            is_latest=True,
            # rootfs_* deliberately empty.
        )

        s_qemu = Station.objects.create(
            name="qemu-station",
            callsign="Q1TEST",
            current_image_release=image_release,  # older qemu release
        )
        s_qemu.tags.add(tag)
        s_rpi = Station.objects.create(
            name="rpi-station",
            callsign="R1TEST",
            current_image_release=rpi_not_ready,
        )
        s_rpi.tags.add(tag)

        client.force_login(admin_user)
        response = client.post(
            reverse("rollouts:upgrade_group", kwargs={"tag_slug": tag.slug}),
            follow=True,
        )
        assert response.status_code == 200

        # Exactly one Deployment — for qemu, because rpi's release isn't OTA-ready.
        deployments = Deployment.objects.all()
        assert deployments.count() == 1
        assert deployments.first().image_release_id == qemu_ready.pk

        # Summary message: "Queued 1 upgrades (1 skipped)".
        msgs = [str(m) for m in response.context["messages"]]
        assert any("Queued 1" in m and "1 skipped" in m for m in msgs), msgs
```

> **Note:** The fixture might need a fresh `ImageRelease` row for rpi; the unique constraint is `(tag, machine)`. Adjust the test fixture if it collides with migration-seeded rows.

- [ ] **Step 6: Run the test, confirm failure**

```
cd ~/station-manager && .venv/bin/pytest tests/test_rollouts.py::TestUpgradeActions::test_upgrade_group_skips_releases_without_rootfs -v
```

Expected: FAIL — the rpi Deployment is created even though its release isn't OTA-ready.

- [ ] **Step 7: Add the guard to `UpgradeGroupView`**

In `apps/rollouts/views.py`, find `UpgradeGroupView.post`. Inside the per-machine loop (search for `machine_stations`), at the point where we resolve the `target` ImageRelease for the machine and before the Deployment create, add:

```python
                if not target.is_ota_ready:
                    skipped += len(machine_stations)
                    continue
```

Make sure `target` is the resolved `ImageRelease` for this machine; read the existing loop carefully so the variable name matches. The existing `skipped` counter (seen in the read-through earlier) is already there; we're just adding one more reason to increment it.

- [ ] **Step 8: Run the test, confirm PASS**

```
cd ~/station-manager && .venv/bin/pytest tests/test_rollouts.py::TestUpgradeActions::test_upgrade_group_skips_releases_without_rootfs -v
```

Expected: PASS.

- [ ] **Step 9: Run the full rollouts suite**

```
cd ~/station-manager && .venv/bin/pytest tests/test_rollouts.py -v 2>&1 | tail -10
```

Expected: all pass, including the existing `test_admin_can_upgrade_single_station` (its fixture's release should already be OTA-ready after Task 4 fixed the fixture).

- [ ] **Step 10: Full suite + ruff**

```
cd ~/station-manager && .venv/bin/ruff format apps/rollouts/views.py tests/test_rollouts.py && \
  .venv/bin/ruff check apps/rollouts/views.py tests/test_rollouts.py && \
  .venv/bin/pytest -x -q 2>&1 | tail -3
```

Expected: all green.

- [ ] **Step 11: Commit**

```
cd ~/station-manager && \
  git add apps/rollouts/views.py tests/test_rollouts.py && \
  git commit -m "rollouts: refuse to deploy releases that are not OTA-ready

UpgradeStationView short-circuits with a flash error when the latest
release for the station's machine has empty rootfs_s3_key, so the
operator sees the problem immediately instead of the station
retry-looping against a download that can't satisfy its 1 GiB slot.

UpgradeGroupView folds that same check into the per-machine loop and
tallies skipped stations into the existing 'N upgrades queued (M
skipped)' summary message.

Prefers local, loud errors over silent remote retry storms."
```

---

## Task 6: Push feature branch + open PR

**Goal:** End-to-end review-ready change.

### Steps

- [ ] **Step 1: Push the branch**

```
cd ~/station-manager && git push -u origin feat/rootfs-extraction-spec
```

- [ ] **Step 2: Open the PR**

```
cd ~/station-manager && gh pr create \
  --title "apps/images: extract rootfs on ImageRelease import (fixes OTA ENOSPC)" \
  --body "$(cat <<'EOF'
## Summary

Extracts the root_a partition from the downloaded wic during
ImageRelease import and stores it as a second S3 artifact. OTA
agents now download that artifact (~200-300 MB bz2, 1 GiB raw)
instead of the full wic (~70 MB bz2, 4.2 GiB raw), which means
install_to_slot finally fits into the 1 GiB A/B slots.

## Design

Spec: \`docs/superpowers/specs/2026-04-21-rootfs-extraction-design.md\`
Plan: \`docs/superpowers/plans/2026-04-21-rootfs-extraction.md\`

Additive schema (three new \`rootfs_*\` fields on \`ImageRelease\`);
existing \`s3_key\` keeps describing the full wic for bare-metal
flash / provisioning. Async extraction in the existing worker.
Pure-Python GPT parser, no new runtime deps.

## Test plan

- [x] Unit tests for the pure extractor (5 cases: round-trip,
      missing partition, bad ext4 magic, non-GPT, out-of-bounds).
- [x] Worker integration: populates rootfs_* on success, strict
      rollback on failure.
- [x] API: CheckView returns rootfs metadata, DownloadView streams
      rootfs artifact, both defend against empty rootfs_s3_key.
- [x] Rollouts: UpgradeStation/UpgradeGroup refuse non-OTA-ready
      releases with operator-visible errors.
- [ ] After merge: re-import 2026.04.21-04 via admin, reflash the
      affected Proxmox VM, watch the still-pending Deployment #3
      drive through DOWNLOADING → INSTALLING → REBOOTING →
      VERIFYING → SUCCESS.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Expected: PR URL printed to stdout.

- [ ] **Step 3: Done**

Await review. If copilot-loop runs, iterate per that flow.

---

## Out of scope (tracked elsewhere)

- Station detail UI redesign — separate spec + plan to come.
- Data migration for existing pre-rootfs ImageReleases — operator
  manually re-imports via the admin "Queue import" button.
- rootfs artifact cosign-signing — see spec's "Out of scope".
