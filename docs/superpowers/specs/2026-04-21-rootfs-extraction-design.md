# Rootfs extraction on ImageRelease import

Date: 2026-04-21

## Problem

The OTA agent's `install_to_slot()` writes the downloaded artifact into a
single A/B partition (`/dev/disk/by-partlabel/root_b`, 1 GiB on both
qemux86-64 and raspberrypi4-64 layouts). Today the download artifact is the
full-disk wic image — partition table plus all four partitions
(`efi`, `root_a`, `root_b`, `data`) — which decompresses to ~4.2 GiB.
Writing that into 1 GiB hits `ENOSPC` 10-20 seconds in:

```
install_to_slot failed: [Errno 28] No space left on device
```

Flashing a fresh disk with this wic is correct (partition table + all
slots get laid down at once), but OTA only needs the rootfs bytes.
Producing a rootfs-only artifact on the linux-image build side would
either fork the release pipeline or duplicate wic-generation logic.
Cleaner: the station-manager already ingests the wic during
`ImageImportJob` processing, so extraction is one more step in the
same worker with no build-side churn.

## Decisions

| Decision | Choice | Reasoning |
| --- | --- | --- |
| Schema | Additive fields on `ImageRelease` (`rootfs_s3_key`, `rootfs_sha256`, `rootfs_size_bytes`) | Keeps `s3_key` semantics = full wic for Provisioning / bare-metal flash. No sub-model migration, no API breakage. |
| Backward compat | Leave existing ImageReleases with empty `rootfs_*`; the deployment-creation guard refuses to make new Deployments against them, so operators get a loud, local error instead of a station retry-loop. | User has 3 existing releases, only `is_latest` matters, and re-import is one admin click. Migration cost not worth it for three rows. |
| Execution | Async in the existing `run_background_jobs` worker | Extraction is 1-3 min CPU + I/O for the qemu image. Keeping it in the view would risk HTTP timeouts and block a Gunicorn worker. The import flow is already async. |
| Extraction tool | Pure Python (GPT parser ≈30 LOC + `bz2.BZ2Compressor`) | No new runtime deps on the server (no sfdisk, no guestfish in the hot path). Pure functions unit-test cleanly with synthetic binary fixtures. Matches the existing `_decompress_to` / `_compress_to_bytes` helpers in the provisioning worker. |
| Failure mode | Fail closed; clean up newly uploaded artifacts when safe | First-import failure: delete uploaded wic + bundle from S3, don't create the ImageRelease row. Re-import failure: atomic rollback on the DB side keeps the existing release row unchanged, and the S3 cleanup skips keys that still-persisting release points at (overwrites may have changed the stable object's content — download-time sha256 mismatch is preferred over deleting a key an existing row still references). Newly-created rows always land OTA-capable; pre-existing rows stay usable for provisioning / bare-metal flash until re-imported. |

## Architecture

One new pure module, three new model fields, one migration, one worker
function grows by ~10 lines. No changes on the agent side.

```
apps/images/
    extraction.py       # NEW: pure module, no Django / S3 / HTTP
    models.py           # +3 fields on ImageRelease
    migrations/0005_…   # NEW

apps/provisioning/management/commands/run_background_jobs.py
    _run_import_job()   # extended: extract + upload after wic upload

apps/deployments/api_views.py
    DeploymentCheckView     # reads rootfs_* instead of s3_*
    DeploymentDownloadView  # streams from rootfs_s3_key, 404 if empty

tests/
    test_images_extraction.py  # NEW: synthetic-wic unit tests
    test_images.py             # +rootfs assertions on the worker
    test_deployments.py        # +rootfs-path endpoint tests
```

## `apps/images/extraction.py`

```python
def extract_rootfs(wic_path: Path, out_path: Path) -> tuple[int, str]:
    """Extract the `root_a` partition from a decompressed wic, bz2-
    compress it, write to out_path. Returns (compressed_size_bytes,
    compressed_sha256_hex).

    Raises ValueError on:
      - not a GPT image (signature mismatch at LBA 1)
      - no partition named 'root_a'
      - root_a start+size exceeds the wic file size
      - root_a does not start with an ext4 superblock magic
    """
```

### GPT parsing

Read 512 bytes at offset `0x200` (LBA 1) → verify signature
`EFI PART`. Partition entries live at LBA 2 onwards, 128 bytes each,
default 128 entries. For each populated entry, read the UTF-16LE name
field (72 bytes = 36 chars max). Match on exact literal `root_a`.

From the matched entry we get `starting_lba` and `ending_lba` in
sectors. Multiply by 512 for byte offsets.

### ext4 magic check

After identifying `root_a`, seek to `starting_lba * 512 + 1080` and
read 2 bytes. Must equal `0x53 0xEF` (little-endian 0xEF53). The
check is cheap (one seek + 2 bytes) and catches silent-corruption
cases — a partition table that points at garbage would produce a
rootfs artifact that bricks the trial boot.

### Streaming compression

Open `wic_path`, seek to `starting_lba * 512`, read in 1 MiB chunks
up to `(ending_lba - starting_lba + 1) * 512` bytes, feed each chunk
through `bz2.BZ2Compressor(9)`, write the compressed bytes to
`out_path`. Track sha256 of the compressed stream as it's written;
track size via `out_path.stat().st_size` at the end (or accumulate).

Peak memory: chunk (1 MiB) + compressor state (~1 MiB).

## `ImageRelease` changes

```python
rootfs_s3_key     = CharField(max_length=512, blank=True, default="")
rootfs_sha256     = CharField(max_length=64, blank=True, default="")
rootfs_size_bytes = BigIntegerField(null=True, blank=True)
```

Migration `apps/images/migrations/0005_imagerelease_rootfs_fields.py`.
Nullable / blank so the migration applies without a backfill; existing
rows land with empty strings + NULL, which DownloadView treats as "not
OTA-ready" (see below).

`s3_key`, `sha256`, `size_bytes` unchanged. They continue to describe
the full wic, which Provisioning and Bare-Metal-Flash both consume.

## `_run_import_job` changes

Extend the existing flow:

```
1. fetch release asset from GitHub
2. cosign verify
3. upload full wic to S3              ← unchanged
4. upload cosign bundle to S3         ← unchanged
5. decompress wic to tempdir          ← NEW (reuses _decompress_to)
6. extract_rootfs → rootfs.bz2        ← NEW
7. upload rootfs.bz2 to S3            ← NEW
8. ImageRelease.update_or_create      ← +3 fields
9. job.status = READY
```

Rollback list pattern: every successful `upload_bytes` appends the key
to a `uploaded_keys = []` list. Any exception inside the try-block
best-effort deletes every key in the list, then sets the job to
FAILED with `str(exc)` as the message. Collapses the current
two-separate-cleanup-loops into one.

Temp-space budget: ~5.5 GiB peak during extraction
(4.2 GiB decompressed wic + 1 GiB raw rootfs + ~200 MB compressed).
All in a single `tempfile.TemporaryDirectory()` that auto-cleans on
both success and failure paths.

## Guarding at deployment-creation time

`ImageRelease` grows a convenience property:

```python
@property
def is_ota_ready(self) -> bool:
    return bool(
        self.rootfs_s3_key
        and self.rootfs_sha256
        and self.rootfs_size_bytes
        and self.rootfs_size_bytes > 0
    )
```

Requires all three rootfs fields populated because the check/download
endpoints serialize the hash + size directly — a partially-populated
row (from a regression, admin mutation, or interrupted migration)
would 500 the response serializer on a NULL size_bytes. Under the
worker's strict-rollback + atomic DB block the three are always
written together, so the extra conditions are defence against
external mutation, not the happy path.

`UpgradeStationView` and `UpgradeGroupView` check
`release.is_ota_ready` before creating the Deployment row. If it's
False, flash an error message to the operator and redirect back:

- `UpgradeStationView`: `_("Release %(tag)s is not prepared for OTA — re-import it first.")`
- `UpgradeGroupView`: skip affected releases, tally them into the
  "skipped" counter alongside already-on-latest stations, extend the
  existing success-message summary.

Why at creation time, not in CheckView: the existing agent's
`check_for_update()` treats any status other than 200/204 as a
"logged warning, try again next poll" — so a 409 from CheckView would
loop the station forever. Refusing at creation time keeps the error
loud and local to the operator instead of silent and remote.

## DeploymentCheck / DeploymentDownload changes

Today `DeploymentCheckView` returns:
```json
{ "size_bytes": …, "checksum_sha256": …, "download_url": "/api/v1/deployments/{id}/download/" }
```
The three values come from `ImageRelease.s3_key` / `.sha256` /
`.size_bytes`. After this change, they come from the `rootfs_*`
counterparts.

`DeploymentDownloadView` streams from `rootfs_s3_key` instead of
`s3_key`. Everything else (Range, Accept-Ranges, chunked streaming)
stays.

Because the creation-time guard prevents Deployments from being made
against non-OTA-ready releases, the Check/Download views should not
encounter an empty `rootfs_s3_key` in practice. But for
defense-in-depth (race with an admin manually deleting
`rootfs_s3_key`, data migration going wrong):

- **Check**: log an error and return `204 No Content`. The agent
  treats 204 as "no deployment for me" and stops; the Deployment
  stays in its current DB state for the operator to investigate.
  (Not 409 — that would trigger the retry-loop.)
- **Download**: return `404 Not Found`. The agent's existing
  download-failure path reports FAILED and stops.

## Error handling

All of these land the job in FAILED with a useful `error_message`,
and every S3 key uploaded so far is best-effort deleted. No
ImageRelease is created.

| Source | Exception | Message in job |
| --- | --- | --- |
| cosign verify | verification error | (as today) |
| wic decompress | `OSError` from bz2.open | `"bz2 decompress failed: {exc}"` |
| GPT header missing | `ValueError` from extractor | `"not a GPT image"` |
| `root_a` missing | `ValueError` from extractor | `"no partition named 'root_a'"` |
| Partition bounds | `ValueError` from extractor | `"root_a (start={s}, end={e}) exceeds wic size {n}"` |
| ext4 magic | `ValueError` from extractor | `"root_a is not ext4 (magic mismatch)"` |
| rootfs upload | `OSError` or S3 backend error | `"rootfs upload failed: {exc}"` |
| DB write | `IntegrityError` etc | (as today) |

Worker thread does not die on any of these — the outer try/except in
`_run_import_job` already catches `Exception`, updates the job, and
the main loop picks up the next job.

## Tests

### `tests/test_images_extraction.py` (new)

Build a synthetic wic in-memory (or in tmp_path):

- 32 kB total
- bytes 0..511: MBR-protective (can be zeros, GPT doesn't care)
- LBA 1 (offset 0x200): GPT header with correct signature, CRC not
  verified by our parser (pragma: we trust the import process)
- LBA 2 (offset 0x400): one 128-byte partition entry named `root_a`
  (UTF-16LE), starting_lba=8, ending_lba=47 (40 sectors = 20 kB)
- offset 0x1000..: 20 kB of synthetic payload with ext4 magic
  (`0x53 0xEF`) at offset 1080 from partition start

Tests:

- `test_extract_rootfs_round_trip`: synthetic wic in, bz2 out.
  Decompress the output and assert it equals the original partition
  bytes. Assert returned size + sha256 match the written file.
- `test_extract_rootfs_rejects_missing_partition`: same but
  partition entry renamed to `rootfs`. Assert `ValueError("no
  partition named 'root_a'")`.
- `test_extract_rootfs_rejects_bad_ext4_magic`: root_a present, but
  the bytes at offset 1080 are `0x00 0x00`. Assert `ValueError`.
- `test_extract_rootfs_rejects_non_gpt`: zero the signature. Assert
  `ValueError("not a GPT image")`.
- `test_extract_rootfs_rejects_out_of_bounds`: partition entry
  `ending_lba` points past file size. Assert `ValueError`.

### `tests/test_images.py` (extended)

- `test_run_import_job_populates_rootfs_fields`: mock
  `github.fetch_release_asset` to return a real synthetic
  wic-with-root_a blob, mock cosign.verify_blob, mock
  `image_storage.upload_bytes` + `open_stream`, assert
  ImageRelease got `rootfs_s3_key`, `rootfs_sha256`,
  `rootfs_size_bytes` populated and they match the
  extraction.
- `test_run_import_job_rolls_back_on_extraction_failure`: patch
  `extract_rootfs` to raise. Assert:
  - job.status == FAILED
  - job.error_message contains the reason
  - `image_storage.delete` was called for wic_key AND bundle_key
  - No ImageRelease row exists for this tag+machine

### `tests/test_deployments.py` (extended)

- `test_deployment_check_returns_rootfs_metadata`: a release with
  populated `rootfs_*`; check response has those values, not the
  full-wic values.
- `test_deployment_check_returns_204_for_release_without_rootfs`:
  defense-in-depth — even though the creation-time guard should
  prevent this state, verify CheckView returns 204 (not 200) when
  `rootfs_s3_key` is empty.
- `test_deployment_download_streams_rootfs_artifact`: active
  deployment on a release with populated `rootfs_*`;
  DownloadView streams from `rootfs_s3_key`. Verified by asserting
  `image_storage.open_stream` was called with the rootfs key.
- `test_deployment_download_returns_404_for_release_without_rootfs`:
  defense-in-depth — DownloadView rejects releases without rootfs_*
  with 404 (agent's existing failure path).

### `tests/test_rollouts.py` (extended)

- `test_upgrade_station_refuses_release_without_rootfs`: operator
  clicks "Upgrade this station" while the latest release has empty
  `rootfs_s3_key`. No Deployment / DeploymentResult rows are
  created; an error flash message with the re-import hint is
  rendered; redirect back to the station detail page.
- `test_upgrade_group_skips_releases_without_rootfs`: a group
  upgrade where one machine's latest release is OTA-ready and
  another's isn't. The ready one gets a Deployment, the other is
  counted in the "skipped" tally in the summary flash.

## Out of scope

- UI redesign of the station detail page — tracked separately.
- Data migration for existing ImageReleases — operator triggers
  re-import manually; `update_or_create` inside `_run_import_job`
  handles the upsert.
- Resumable / chunked rootfs upload to S3 — the artifact is
  ~200-300 MB bz2, single upload is fine. Revisit if image size grows.
- Signing the rootfs artifact separately (cosign). The full wic's
  cosign bundle stays the authority; the rootfs is derived from it
  and the server is trusted to do the derivation. Signing the
  rootfs would require a keystore on the server, which is a much
  bigger change.

## Migration / rollout

1. Merge this change. Existing releases keep working for
   Provisioning / flash (unchanged `s3_key`); attempts to deploy
   them OTA are blocked at the creation-time guard with a "re-import
   required" flash message.
2. Re-import `2026.04.21-04` via the admin "Queue import" button.
   Worker does the extraction. ImageRelease row gets populated.
3. The VM with the still-PENDING deployment #3 picks up the next
   check poll, downloads the rootfs (now ~300 MB bz2 instead of
   4.2 GiB), `install_to_slot` writes into root_b (fits in 1 GiB),
   bootloader arms for trial boot, station reboots, verify+commit
   cycle completes. End-to-end OTA.

No agent change needed. `station-agent` already downloads whatever
artifact the server hands it, and `install_to_slot` (post-PR #27) is
indifferent to multi-stream or single-stream bz2 content.
