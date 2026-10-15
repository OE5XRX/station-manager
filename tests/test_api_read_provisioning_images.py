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
def test_provisioning_job_admin_only_and_no_s3(topology):  # noqa: F811
    from apps.images.models import ImageRelease
    from apps.provisioning.models import ProvisioningJob

    rel = ImageRelease.objects.create(
        tag="v1", machine="qemux86-64", sha256="a" * 64, s3_key="k", size_bytes=0
    )
    j_out = ProvisioningJob.objects.create(station=topology["station_out"], image_release=rel)
    j_in = ProvisioningJob.objects.create(station=topology["station_in"], image_release=rel)
    # provisioning has no own view level: view resolves to manage (Admin).
    for role in ("station_user", "staff"):
        resp = bearer(topology[role]).get("/api/v1/provisioning-jobs/")
        assert resp.status_code == 403
        assert resp.json()["required_level"] == "admin"
    resp = bearer(topology["admin"]).get("/api/v1/provisioning-jobs/")
    assert resp.status_code == 200
    assert {str(r["id"]) for r in resp.data["results"]} == {str(j_in.id), str(j_out.id)}
    assert "output_s3_key" not in resp.data["results"][0]


@pytest.mark.django_db
def test_image_import_jobs_visible_with_images_view(topology):  # noqa: F811
    # images.view = Applicant; the images page lists recent import jobs to
    # every viewer, so the API does too.
    from apps.images.models import ImageImportJob

    ImageImportJob.objects.create(tag="v1", machine="qemux86-64")
    assert bearer(topology["station_user"]).get("/api/v1/image-import-jobs/").data["count"] == 1
    assert bearer(topology["admin"]).get("/api/v1/image-import-jobs/").data["count"] == 1
