import hashlib
from unittest import mock

import pytest
from django.utils import timezone

from apps.images.cosign import CosignVerificationError
from apps.images.github_releases import GitHubRelease
from apps.module_firmware import releases, storage
from apps.module_firmware.models import ModuleFirmwareImportJob, ModuleFirmwareRelease, ModuleType


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(
        key="fm",
        display_name="FM",
        firmware_repo="OE5XRX/FW-RemoteStation",
        release_asset_prefix="fm-sa818",
    )


def _blob(variant):
    return f"signed-{variant}".encode()


def _fake_download(url):
    # url ends with the asset name
    name = url.rsplit("/", 1)[-1]
    if name == "SHA256SUMS":
        lines = []
        for v in ("vhf", "uhf"):
            lines.append(f"{hashlib.sha256(_blob(v)).hexdigest()}  fm-sa818-{v}.signed.bin")
        return ("\n".join(lines) + "\n").encode()
    if name.endswith(".signed.bin"):
        return _blob(name[len("fm-sa818-") : -len(".signed.bin")])
    if name.endswith(".bundle"):
        return b"bundle"
    raise AssertionError(url)


@pytest.mark.django_db
def test_import_creates_release_per_variant(fm):
    job = ModuleFirmwareImportJob.objects.create(
        module_type=fm, source_repo=fm.firmware_repo, tag="26.07.04-01"
    )
    rel = GitHubRelease(
        tag="26.07.04-01",
        html_url="",
        is_latest=True,
        asset_names=frozenset(
            {
                "fm-sa818-vhf.signed.bin",
                "fm-sa818-vhf.signed.bin.bundle",
                "fm-sa818-uhf.signed.bin",
                "fm-sa818-uhf.signed.bin.bundle",
                "SHA256SUMS",
            }
        ),
    )
    with (
        mock.patch("apps.module_firmware.releases.fetch_release_by_tag", return_value=rel),
        mock.patch("apps.module_firmware.releases._download", side_effect=_fake_download),
        mock.patch("apps.module_firmware.releases.verify_blob_identity") as vbi,
        mock.patch("apps.module_firmware.releases.storage.upload_bytes"),
    ):
        releases.import_release_tag(job)
    job.refresh_from_db()
    assert job.status == ModuleFirmwareImportJob.Status.READY
    rels = ModuleFirmwareRelease.objects.filter(module_type=fm, version="26.07.04-01")
    assert set(rels.values_list("variant", flat=True)) == {"vhf", "uhf"}
    # job.release points at the last created variant row
    assert job.release is not None
    assert job.release.version == "26.07.04-01"
    assert job.release.module_type == fm
    assert job.release.variant in {"vhf", "uhf"}
    # cosign identity uses refs/heads/main
    assert "refs/heads/main" in vbi.call_args_list[0].args[2]


@pytest.mark.django_db
def test_import_sha_mismatch_fails(fm):
    job = ModuleFirmwareImportJob.objects.create(
        module_type=fm, source_repo=fm.firmware_repo, tag="26.07.04-01"
    )
    rel = GitHubRelease(
        tag="26.07.04-01",
        html_url="",
        is_latest=True,
        asset_names=frozenset(
            {"fm-sa818-vhf.signed.bin", "fm-sa818-vhf.signed.bin.bundle", "SHA256SUMS"}
        ),
    )

    def bad_dl(url):
        name = url.rsplit("/", 1)[-1]
        if name == "SHA256SUMS":
            return b"deadbeef  fm-sa818-vhf.signed.bin\n"
        return b"whatever"

    with (
        mock.patch("apps.module_firmware.releases.fetch_release_by_tag", return_value=rel),
        mock.patch("apps.module_firmware.releases._download", side_effect=bad_dl),
        mock.patch("apps.module_firmware.releases.storage.upload_bytes"),
    ):
        releases.import_release_tag(job)
    job.refresh_from_db()
    assert job.status == ModuleFirmwareImportJob.Status.FAILED
    assert ModuleFirmwareRelease.objects.count() == 0


# ---------------------------------------------------------------------------
# Finding 1: all-or-nothing — uhf SHA mismatch → vhf row NOT published
# ---------------------------------------------------------------------------


def _two_variant_release(tag="26.07.04-01"):
    return GitHubRelease(
        tag=tag,
        html_url="",
        is_latest=True,
        asset_names=frozenset(
            {
                "fm-sa818-vhf.signed.bin",
                "fm-sa818-vhf.signed.bin.bundle",
                "fm-sa818-uhf.signed.bin",
                "fm-sa818-uhf.signed.bin.bundle",
                "SHA256SUMS",
            }
        ),
    )


@pytest.mark.django_db
def test_all_or_nothing_second_variant_mismatch(fm):
    """If one variant SHA mismatches, NO row must be published (no partial release).

    Finding 5: uploads are deferred until AFTER all variants are verified, so
    storage.upload_bytes must NOT be called at all when any variant fails.
    No cleanup (delete) is needed either because nothing was written.
    """
    job = ModuleFirmwareImportJob.objects.create(
        module_type=fm, source_repo=fm.firmware_repo, tag="26.07.04-01"
    )

    def dl_vhf_bad(url):
        name = url.rsplit("/", 1)[-1]
        if name == "SHA256SUMS":
            # uhf is correct, vhf has a bad hash → vhf fails in the stage loop
            uhf_hex = hashlib.sha256(_blob("uhf")).hexdigest()
            return (
                f"{uhf_hex}  fm-sa818-uhf.signed.bin\ndeadbeef  fm-sa818-vhf.signed.bin\n"
            ).encode()
        return _fake_download(url)

    with (
        mock.patch(
            "apps.module_firmware.releases.fetch_release_by_tag",
            return_value=_two_variant_release(),
        ),
        mock.patch("apps.module_firmware.releases._download", side_effect=dl_vhf_bad),
        mock.patch("apps.module_firmware.releases.verify_blob_identity"),
        mock.patch("apps.module_firmware.releases.storage.upload_bytes") as mock_upload,
        mock.patch("apps.module_firmware.releases.storage.delete") as mock_delete,
    ):
        releases.import_release_tag(job)

    job.refresh_from_db()
    assert job.status == ModuleFirmwareImportJob.Status.FAILED
    # No release rows at all — neither variant published
    assert ModuleFirmwareRelease.objects.count() == 0
    # Finding 5: stage loop aborts before any upload — zero canonical objects written
    mock_upload.assert_not_called()
    # No cleanup needed because nothing was written
    mock_delete.assert_not_called()


# ---------------------------------------------------------------------------
# Finding 5: archived variant canonical key NOT overwritten when sibling fails
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_archived_variant_not_overwritten_when_sibling_fails(fm):
    """Archived vhf row exists.  uhf fails cosign.  vhf canonical must NOT be
    overwritten — upload_bytes must not be called at all, and vhf stays archived
    with its original sha unchanged.
    """
    archived_vhf = ModuleFirmwareRelease.all_objects.create(
        module_type=fm,
        variant="vhf",
        version="26.07.04-01",
        storage_key=storage.release_key(fm.key, "26.07.04-01", "vhf"),
        sha256="a" * 64,
        size_bytes=10,
        source_repo=fm.firmware_repo,
        source_tag="26.07.04-01",
        archived_at=timezone.now(),
    )
    job = ModuleFirmwareImportJob.objects.create(
        module_type=fm, source_repo=fm.firmware_repo, tag="26.07.04-01"
    )

    def verify_uhf_fails(blob, bundle, identity):
        # uhf blob starts with "signed-uhf"; raise on it
        if blob == _blob("uhf"):
            raise ValueError("cosign verification failed for uhf")

    with (
        mock.patch(
            "apps.module_firmware.releases.fetch_release_by_tag",
            return_value=_two_variant_release(),
        ),
        mock.patch("apps.module_firmware.releases._download", side_effect=_fake_download),
        mock.patch(
            "apps.module_firmware.releases.verify_blob_identity",
            side_effect=verify_uhf_fails,
        ),
        mock.patch("apps.module_firmware.releases.storage.upload_bytes") as mock_upload,
    ):
        releases.import_release_tag(job)

    job.refresh_from_db()
    assert job.status == ModuleFirmwareImportJob.Status.FAILED
    # Finding 5: no upload at all — vhf canonical key is intact
    mock_upload.assert_not_called()
    # vhf row still archived with unchanged sha
    archived_vhf.refresh_from_db()
    assert archived_vhf.archived_at is not None
    assert archived_vhf.sha256 == "a" * 64


# ---------------------------------------------------------------------------
# Finding 2a: idempotent skip — active variant not re-downloaded
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_idempotent_skip_active_variant(fm):
    """Re-importing a tag with an already-ACTIVE row must skip download+upload."""
    existing = ModuleFirmwareRelease.objects.create(
        module_type=fm,
        variant="vhf",
        version="26.07.04-01",
        storage_key="module_firmware/fm/26.07.04-01/vhf.signed.bin",
        sha256="aa" * 32,
        size_bytes=10,
        source_repo=fm.firmware_repo,
        source_tag="26.07.04-01",
    )
    job = ModuleFirmwareImportJob.objects.create(
        module_type=fm, source_repo=fm.firmware_repo, tag="26.07.04-01"
    )
    rel = GitHubRelease(
        tag="26.07.04-01",
        html_url="",
        is_latest=True,
        asset_names=frozenset(
            {"fm-sa818-vhf.signed.bin", "fm-sa818-vhf.signed.bin.bundle", "SHA256SUMS"}
        ),
    )

    with (
        mock.patch("apps.module_firmware.releases.fetch_release_by_tag", return_value=rel),
        mock.patch("apps.module_firmware.releases._download") as mock_dl,
        mock.patch("apps.module_firmware.releases.storage.upload_bytes") as mock_upload,
    ):
        releases.import_release_tag(job)

    job.refresh_from_db()
    assert job.status == ModuleFirmwareImportJob.Status.READY
    # No download or upload for the already-active variant
    mock_dl.assert_not_called()
    mock_upload.assert_not_called()
    # Storage key and sha256 unchanged
    existing.refresh_from_db()
    assert existing.storage_key == "module_firmware/fm/26.07.04-01/vhf.signed.bin"
    assert existing.sha256 == "aa" * 32


# ---------------------------------------------------------------------------
# Finding 2b: archived restore+re-pin — row is restored and re-uploaded
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_archived_variant_is_restored_and_repinned(fm):
    """An archived row must be restored (archived_at=None) and re-uploaded."""
    archived = ModuleFirmwareRelease.all_objects.create(
        module_type=fm,
        variant="vhf",
        version="26.07.04-01",
        storage_key="old-key",
        sha256="bb" * 32,
        size_bytes=7,
        source_repo=fm.firmware_repo,
        source_tag="26.07.04-01",
        archived_at=timezone.now(),
    )
    job = ModuleFirmwareImportJob.objects.create(
        module_type=fm, source_repo=fm.firmware_repo, tag="26.07.04-01"
    )
    rel = GitHubRelease(
        tag="26.07.04-01",
        html_url="",
        is_latest=True,
        asset_names=frozenset(
            {"fm-sa818-vhf.signed.bin", "fm-sa818-vhf.signed.bin.bundle", "SHA256SUMS"}
        ),
    )
    with (
        mock.patch("apps.module_firmware.releases.fetch_release_by_tag", return_value=rel),
        mock.patch("apps.module_firmware.releases._download", side_effect=_fake_download),
        mock.patch("apps.module_firmware.releases.verify_blob_identity"),
        mock.patch("apps.module_firmware.releases.storage.upload_bytes") as mock_upload,
    ):
        releases.import_release_tag(job)

    job.refresh_from_db()
    assert job.status == ModuleFirmwareImportJob.Status.READY
    # Row must be restored
    archived.refresh_from_db()
    assert archived.archived_at is None
    # New storage key pinned
    assert archived.storage_key != "old-key"
    # Upload was called
    assert mock_upload.called


# ---------------------------------------------------------------------------
# Finding 5 (regression): cosign failure → job FAILED, no DB rows, no uploads
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_cosign_failure_marks_job_failed_no_rows_no_uploads(fm):
    """verify_blob_identity raises CosignVerificationError → job FAILED,
    no ModuleFirmwareRelease rows, and storage.upload_bytes never called.
    """
    job = ModuleFirmwareImportJob.objects.create(
        module_type=fm, source_repo=fm.firmware_repo, tag="26.07.04-01"
    )
    rel = GitHubRelease(
        tag="26.07.04-01",
        html_url="",
        is_latest=True,
        asset_names=frozenset(
            {"fm-sa818-vhf.signed.bin", "fm-sa818-vhf.signed.bin.bundle", "SHA256SUMS"}
        ),
    )

    with (
        mock.patch("apps.module_firmware.releases.fetch_release_by_tag", return_value=rel),
        mock.patch("apps.module_firmware.releases._download", side_effect=_fake_download),
        mock.patch(
            "apps.module_firmware.releases.verify_blob_identity",
            side_effect=CosignVerificationError("bad sig"),
        ),
        mock.patch("apps.module_firmware.releases.storage.upload_bytes") as mock_upload,
    ):
        releases.import_release_tag(job)

    job.refresh_from_db()
    assert job.status == ModuleFirmwareImportJob.Status.FAILED
    assert "bad sig" in job.error_message
    # No release rows published
    assert ModuleFirmwareRelease.objects.count() == 0
    # Stage loop aborts at verify step — no upload ever reached
    mock_upload.assert_not_called()
