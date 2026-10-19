"""Optional native_sim artifact: import + storage + download (Teil D, Task S2)."""

import hashlib
import io
from unittest import mock

import pytest
from django.urls import reverse

from apps.images.cosign import CosignVerificationError
from apps.images.github_releases import GitHubRelease
from apps.module_firmware import releases, storage
from apps.module_firmware.models import (
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareImportJob,
    ModuleFirmwareRelease,
    ModuleType,
)
from tests.conftest import device_auth_headers

TAG = "26.07.04-01"
SIGNED = b"signed-vhf"
SIM = b"\x7fELF-native-sim-vhf"


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(
        key="fm",
        display_name="FM",
        firmware_repo="OE5XRX/FW-RemoteStation",
        release_asset_prefix="fm-sa818",
    )


def _gh(names):
    return GitHubRelease(tag=TAG, html_url="", is_latest=True, asset_names=frozenset(names))


_WITH_SIM = {
    "fm-sa818-vhf.signed.bin",
    "fm-sa818-vhf.signed.bin.bundle",
    "fm-sa818-vhf.native_sim",
    "fm-sa818-vhf.native_sim.bundle",
    "SHA256SUMS",
}
_NO_SIM = {"fm-sa818-vhf.signed.bin", "fm-sa818-vhf.signed.bin.bundle", "SHA256SUMS"}


def _make_download(sim_sha=None):
    sim_sha = sim_sha or hashlib.sha256(SIM).hexdigest()

    def dl(url):
        name = url.rsplit("/", 1)[-1]
        if name == "SHA256SUMS":
            return (
                f"{hashlib.sha256(SIGNED).hexdigest()}  fm-sa818-vhf.signed.bin\n"
                f"{sim_sha}  fm-sa818-vhf.native_sim\n"
            ).encode()
        if name == "fm-sa818-vhf.signed.bin":
            return SIGNED
        if name == "fm-sa818-vhf.native_sim":
            return SIM
        if name.endswith(".bundle"):
            return b"bundle-" + name.encode()
        raise AssertionError(url)

    return dl


def _run_import(fm, names, download, vbi_side_effect=None):
    job = ModuleFirmwareImportJob.objects.create(
        module_type=fm, source_repo=fm.firmware_repo, tag=TAG
    )
    uploaded: list[str] = []
    deleted: list[str] = []
    with (
        mock.patch("apps.module_firmware.releases.fetch_release_by_tag", return_value=_gh(names)),
        mock.patch("apps.module_firmware.releases._download", side_effect=download),
        mock.patch(
            "apps.module_firmware.releases.verify_blob_identity", side_effect=vbi_side_effect
        ) as vbi,
        mock.patch(
            "apps.module_firmware.releases.storage.upload_bytes",
            side_effect=lambda k, d: uploaded.append(k),
        ),
        mock.patch(
            "apps.module_firmware.releases.storage.delete",
            side_effect=lambda k: deleted.append(k),
        ),
    ):
        releases.import_release_tag(job)
    job.refresh_from_db()
    return job, vbi, uploaded, deleted


# --- storage / parsing ------------------------------------------------------


def test_sim_release_key_layout():
    assert storage.sim_release_key("fm", TAG, "vhf") == f"module_firmware/fm/{TAG}/vhf.native_sim"
    assert storage.sim_release_key("fm", TAG, "") == f"module_firmware/fm/{TAG}/_.native_sim"
    assert storage.sim_bundle_key("fm", TAG, "vhf") == (
        f"module_firmware/fm/{TAG}/vhf.native_sim.bundle"
    )


def test_parse_sim_assets():
    names = {
        "fm-sa818.native_sim",
        "fm-sa818.native_sim.bundle",
        "fm-sa818-vhf.native_sim",
        "fm-sa818-vhf.native_sim.bundle",
        "fm-sa818-uhf.native_sim",  # no bundle -> ignored
        "fm-sa818x-vhf.native_sim",  # longer token -> ignored
        "fm-sa818x-vhf.native_sim.bundle",
        "fm-sa818-../x.native_sim",  # unsafe variant -> ignored
        "fm-sa818-../x.native_sim.bundle",
        "fm-sa818-vhf.signed.bin",
        "fm-sa818-vhf.signed.bin.bundle",
    }
    assert releases.parse_sim_assets(names, "fm-sa818") == {
        "": "fm-sa818.native_sim",
        "vhf": "fm-sa818-vhf.native_sim",
    }


# --- import -----------------------------------------------------------------


@pytest.mark.django_db
def test_import_pulls_native_sim_when_present(fm):
    job, vbi, uploaded, _ = _run_import(fm, _WITH_SIM, _make_download())
    assert job.status == ModuleFirmwareImportJob.Status.READY, job.error_message
    rel = ModuleFirmwareRelease.objects.get(module_type=fm, variant="vhf", version=TAG)
    assert rel.has_sim_artifact
    assert rel.sim_sha256 == hashlib.sha256(SIM).hexdigest()
    assert rel.sim_size_bytes == len(SIM)
    assert rel.sim_storage_key == storage.sim_release_key("fm", TAG, "vhf")
    assert rel.sim_cosign_bundle_key == storage.sim_bundle_key("fm", TAG, "vhf")
    assert rel.sim_storage_key in uploaded
    assert rel.sim_cosign_bundle_key in uploaded
    # The sim blob was cosign-verified with the same identity as the signed.bin.
    blobs = [c.args[0] for c in vbi.call_args_list]
    assert SIM in blobs and SIGNED in blobs
    identities = {c.args[2] for c in vbi.call_args_list}
    assert len(identities) == 1
    sim_call = next(c for c in vbi.call_args_list if c.args[0] == SIM)
    assert sim_call.args[1] == b"bundle-fm-sa818-vhf.native_sim.bundle"


@pytest.mark.django_db
def test_import_without_native_sim_ok(fm):
    job, _, _, _ = _run_import(fm, _NO_SIM, _make_download())
    assert job.status == ModuleFirmwareImportJob.Status.READY, job.error_message
    rel = ModuleFirmwareRelease.objects.get(module_type=fm, variant="vhf", version=TAG)
    assert rel.sim_storage_key == ""
    assert not rel.has_sim_artifact


@pytest.mark.django_db
def test_import_native_sim_sha_mismatch_fails_job(fm):
    job, _, uploaded, deleted = _run_import(fm, _WITH_SIM, _make_download(sim_sha="0" * 64))
    assert job.status == ModuleFirmwareImportJob.Status.FAILED
    assert "native_sim" in job.error_message
    assert ModuleFirmwareRelease.all_objects.count() == 0
    # Every upload (if any) has been cleaned up.
    assert set(uploaded) <= set(deleted)


@pytest.mark.django_db
def test_import_native_sim_cosign_fail_fails_job(fm):
    def vbi(blob, bundle, identity):
        if blob == SIM:
            raise CosignVerificationError("bad identity")

    job, _, uploaded, deleted = _run_import(fm, _WITH_SIM, _make_download(), vbi_side_effect=vbi)
    assert job.status == ModuleFirmwareImportJob.Status.FAILED
    assert ModuleFirmwareRelease.all_objects.count() == 0
    assert set(uploaded) <= set(deleted)


@pytest.mark.django_db
def test_import_failure_after_upload_cleans_sim_keys(fm):
    """A failure in the publish phase (after uploads) removes the sim keys too."""
    with mock.patch.object(
        ModuleFirmwareRelease.all_objects, "update_or_create", side_effect=RuntimeError("db boom")
    ):
        job, _, uploaded, deleted = _run_import(fm, _WITH_SIM, _make_download())
    assert job.status == ModuleFirmwareImportJob.Status.FAILED
    assert storage.sim_release_key("fm", TAG, "vhf") in uploaded
    assert set(uploaded) <= set(deleted)


@pytest.mark.django_db
def test_native_sim_without_bundle_ignored(fm):
    names = (_WITH_SIM - {"fm-sa818-vhf.native_sim.bundle"}) | set()
    job, vbi, uploaded, _ = _run_import(fm, names, _make_download())
    assert job.status == ModuleFirmwareImportJob.Status.READY, job.error_message
    rel = ModuleFirmwareRelease.objects.get(module_type=fm, variant="vhf", version=TAG)
    assert not rel.has_sim_artifact
    assert all(c.args[0] != SIM for c in vbi.call_args_list)
    assert not any(k.endswith(".native_sim") for k in uploaded)


@pytest.mark.django_db
def test_reimport_archived_clears_stale_sim_fields(fm):
    """Restoring an archived row via re-import overwrites the sim fields (clears them)."""
    rel = ModuleFirmwareRelease.all_objects.create(
        module_type=fm,
        variant="vhf",
        version=TAG,
        storage_key="old",
        sha256="b" * 64,
        size_bytes=1,
        source_repo="r",
        source_tag=TAG,
        sim_storage_key="stale.native_sim",
        sim_sha256="c" * 64,
        sim_size_bytes=3,
        sim_cosign_bundle_key="stale.native_sim.bundle",
    )
    rel.archive()
    job, _, _, _ = _run_import(fm, _NO_SIM, _make_download())
    assert job.status == ModuleFirmwareImportJob.Status.READY, job.error_message
    rel.refresh_from_db()
    assert rel.archived_at is None
    assert rel.sim_storage_key == ""
    assert rel.sim_sha256 == ""
    assert rel.sim_size_bytes is None
    assert rel.sim_cosign_bundle_key == ""


# --- download ---------------------------------------------------------------


@pytest.fixture
def sim_release(fm):
    return ModuleFirmwareRelease.objects.create(
        module_type=fm,
        variant="vhf",
        version=TAG,
        storage_key="module_firmware/fm/x/vhf.signed.bin",
        sha256="a" * 64,
        size_bytes=5,
        source_repo="r",
        source_tag=TAG,
        sim_storage_key="module_firmware/fm/x/vhf.native_sim",
        sim_sha256="d" * 64,
        sim_size_bytes=len(SIM),
        sim_cosign_bundle_key="module_firmware/fm/x/vhf.native_sim.bundle",
    )


@pytest.fixture
def assigned(station_with_key, fm):
    station, priv = station_with_key
    mod = Module.objects.create(uid="U-SIM", module_type=fm)
    ModuleAssignmentHistory.objects.create(module=mod, station=station, slot="slot1")
    return station, priv


def _open_stream_for(release):
    def _open(key):
        if key == release.sim_storage_key:
            return io.BytesIO(SIM)
        if key == release.storage_key:
            return io.BytesIO(b"hello")
        raise AssertionError(key)

    return _open


def _get(client, station, priv, pk, query="", **extra):
    url = reverse("module_firmware_api:download", args=[pk]) + query
    return client.get(url, **extra, **device_auth_headers(priv, station.pk, b""))


@pytest.mark.django_db
def test_download_sim_artifact(client, assigned, sim_release):
    station, priv = assigned
    with mock.patch(
        "apps.module_firmware.api_views.storage.open_stream",
        side_effect=_open_stream_for(sim_release),
    ):
        resp = _get(client, station, priv, sim_release.pk, "?artifact=sim")
        assert resp.status_code == 200
        assert b"".join(resp.streaming_content) == SIM
        assert resp["Content-Length"] == str(len(SIM))
        assert ".native_sim" in resp["Content-Disposition"]

        resp = _get(client, station, priv, sim_release.pk, "?artifact=sim", HTTP_RANGE="bytes=0-3")
        assert resp.status_code == 206
        assert resp["Content-Range"] == f"bytes 0-3/{len(SIM)}"
        assert b"".join(resp.streaming_content) == SIM[:4]


@pytest.mark.django_db
def test_download_sim_missing_404(client, assigned, fm):
    station, priv = assigned
    rel = ModuleFirmwareRelease.objects.create(
        module_type=fm,
        variant="vhf",
        version=TAG,
        storage_key="k",
        sha256="a" * 64,
        size_bytes=5,
        source_repo="r",
        source_tag=TAG,
    )
    with mock.patch("apps.module_firmware.api_views.storage.open_stream") as mock_open:
        resp = _get(client, station, priv, rel.pk, "?artifact=sim")
    assert resp.status_code == 404
    mock_open.assert_not_called()


@pytest.mark.django_db
def test_download_sim_still_requires_assignment(client, station_with_key, sim_release):
    station, priv = station_with_key
    with mock.patch("apps.module_firmware.api_views.storage.open_stream") as mock_open:
        resp = _get(client, station, priv, sim_release.pk, "?artifact=sim")
    assert resp.status_code == 403
    mock_open.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize("value", ["bogus", "", "SIM", "dfu,sim"])
def test_download_bad_artifact_400(client, assigned, sim_release, value):
    station, priv = assigned
    with mock.patch("apps.module_firmware.api_views.storage.open_stream") as mock_open:
        resp = _get(client, station, priv, sim_release.pk, f"?artifact={value}")
    assert resp.status_code == 400
    mock_open.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize("query", ["", "?artifact=dfu"])
def test_download_default_is_dfu(client, assigned, sim_release, query):
    station, priv = assigned
    with mock.patch(
        "apps.module_firmware.api_views.storage.open_stream",
        side_effect=_open_stream_for(sim_release),
    ):
        resp = _get(client, station, priv, sim_release.pk, query)
    assert resp.status_code == 200
    assert b"".join(resp.streaming_content) == b"hello"
    assert resp["Content-Length"] == "5"
    assert resp["Content-Disposition"].endswith('.signed.bin"')


# --- download size cap ----------------------------------------------------------


class _FakeResp(io.BytesIO):
    def __init__(self, data, content_length=None):
        super().__init__(data)
        self.headers = {} if content_length is None else {"Content-Length": str(content_length)}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def test_download_rejects_declared_oversize():
    resp = _FakeResp(b"x", content_length=101)
    with mock.patch("apps.module_firmware.releases.urllib.request.urlopen", return_value=resp):
        with pytest.raises(ValueError, match="exceeds cap"):
            releases._download("https://example.invalid/a", max_bytes=100)


def test_download_rejects_streamed_oversize_without_content_length():
    resp = _FakeResp(b"x" * 101)
    with (
        mock.patch("apps.module_firmware.releases.urllib.request.urlopen", return_value=resp),
        mock.patch("apps.module_firmware.releases._DOWNLOAD_CHUNK", 16),
    ):
        with pytest.raises(ValueError, match="exceeds cap"):
            releases._download("https://example.invalid/a", max_bytes=100)


def test_download_within_cap_returns_body():
    resp = _FakeResp(b"y" * 100, content_length=100)
    with (
        mock.patch("apps.module_firmware.releases.urllib.request.urlopen", return_value=resp),
        mock.patch("apps.module_firmware.releases._DOWNLOAD_CHUNK", 7),
    ):
        assert releases._download("https://example.invalid/a", max_bytes=100) == b"y" * 100


@pytest.mark.django_db
def test_import_oversized_native_sim_fails_job_and_cleans_uploads(fm):
    """An oversized sim ELF (beyond MAX_FIRMWARE_ASSET_BYTES) fails the job; nothing
    is published and every upload is cleaned."""
    good = _make_download()

    def urlopen(url, timeout=None):
        name = url.rsplit("/", 1)[-1]
        if name == "fm-sa818-vhf.native_sim":
            return _FakeResp(b"z" * 65, content_length=None)
        return _FakeResp(good(url))

    job = ModuleFirmwareImportJob.objects.create(
        module_type=fm, source_repo=fm.firmware_repo, tag=TAG
    )
    uploaded: list[str] = []
    deleted: list[str] = []
    with (
        mock.patch(
            "apps.module_firmware.releases.fetch_release_by_tag", return_value=_gh(_WITH_SIM)
        ),
        mock.patch("apps.module_firmware.releases.urllib.request.urlopen", side_effect=urlopen),
        mock.patch("apps.module_firmware.releases.MAX_FIRMWARE_ASSET_BYTES", 64),
        mock.patch("apps.module_firmware.releases.verify_blob_identity"),
        mock.patch(
            "apps.module_firmware.releases.storage.upload_bytes",
            side_effect=lambda k, d: uploaded.append(k),
        ),
        mock.patch(
            "apps.module_firmware.releases.storage.delete",
            side_effect=lambda k: deleted.append(k),
        ),
    ):
        releases.import_release_tag(job)
    job.refresh_from_db()
    assert job.status == ModuleFirmwareImportJob.Status.FAILED
    assert "exceeds cap" in job.error_message
    assert ModuleFirmwareRelease.all_objects.count() == 0
    assert set(uploaded) <= set(deleted)
