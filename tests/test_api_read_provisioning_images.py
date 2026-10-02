import pytest

from tests.test_api_read_fixtures import bearer, topology  # noqa: F401


@pytest.mark.django_db
def test_image_release_no_s3_keys_leak(topology):  # noqa: F811
    from apps.images.models import ImageRelease

    ImageRelease.objects.create(
        tag="v1",
        machine="qemux86-64",
        sha256="a" * 64,
        s3_key="secret/path",
        cosign_bundle_s3_key="c/k",
        size_bytes=0,
    )
    resp = bearer(topology["station_user"]).get("/api/v1/images/")
    assert resp.status_code == 200
    row = resp.data["results"][0]
    for forbidden in ("s3_key", "cosign_bundle_s3_key", "rootfs_s3_key"):
        assert forbidden not in row
    assert "is_ota_ready" in row


@pytest.mark.django_db
def test_provisioning_job_scope_and_no_s3(topology):  # noqa: F811
    from apps.images.models import ImageRelease
    from apps.provisioning.models import ProvisioningJob

    rel = ImageRelease.objects.create(
        tag="v1", machine="qemux86-64", sha256="a" * 64, s3_key="k", size_bytes=0
    )
    ProvisioningJob.objects.create(station=topology["station_out"], image_release=rel)
    j_in = ProvisioningJob.objects.create(station=topology["station_in"], image_release=rel)
    resp = bearer(topology["station_user"]).get("/api/v1/provisioning-jobs/")
    assert resp.status_code == 200
    assert {str(r["id"]) for r in resp.data["results"]} == {str(j_in.id)}
    assert "output_s3_key" not in resp.data["results"][0]


@pytest.mark.django_db
def test_image_import_jobs_internal_only(topology):  # noqa: F811
    from apps.images.models import ImageImportJob

    ImageImportJob.objects.create(tag="v1", machine="qemux86-64")
    assert bearer(topology["station_user"]).get("/api/v1/image-import-jobs/").data["count"] == 0
    assert bearer(topology["admin"]).get("/api/v1/image-import-jobs/").data["count"] == 1
