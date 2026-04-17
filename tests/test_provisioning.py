import uuid

import pytest


@pytest.fixture
def image_release(db):
    from apps.images.models import ImageRelease

    return ImageRelease.objects.create(
        tag="v1-alpha",
        machine="qemux86-64",
        s3_key="images/v1-alpha/qemux86-64.wic.bz2",
        sha256="a" * 64,
        size_bytes=1000,
        is_latest=True,
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
        assert job.ready_at is None
        assert job.downloaded_at is None
        assert job.output_size_bytes is None
        assert job.created_at is not None
        assert job.id is not None

    def test_uuid_primary_key(self, station, image_release, admin_user):
        from apps.provisioning.models import ProvisioningJob

        job = ProvisioningJob.objects.create(
            station=station,
            image_release=image_release,
            requested_by=admin_user,
        )
        assert isinstance(job.id, uuid.UUID)
        # Ensure the UUID default produces distinct values across instances.
        other = ProvisioningJob.objects.create(
            station=station,
            image_release=image_release,
            requested_by=admin_user,
        )
        assert other.id != job.id
