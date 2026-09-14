import hashlib
from unittest import mock

import pytest

from apps.images.github_releases import GitHubRelease
from apps.module_firmware import releases
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
        mock.patch("apps.module_firmware.releases.fetch_releases", return_value=[rel]),
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
        mock.patch("apps.module_firmware.releases.fetch_releases", return_value=[rel]),
        mock.patch("apps.module_firmware.releases._download", side_effect=bad_dl),
        mock.patch("apps.module_firmware.releases.storage.upload_bytes"),
    ):
        releases.import_release_tag(job)
    job.refresh_from_db()
    assert job.status == ModuleFirmwareImportJob.Status.FAILED
    assert ModuleFirmwareRelease.objects.count() == 0
