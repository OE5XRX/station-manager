import pytest
from django.db import IntegrityError, transaction

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

    def test_promoting_existing_record_demotes_previous_latest(self):
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
            is_latest=False,
        )
        new.is_latest = True
        new.save()
        old.refresh_from_db()
        assert old.is_latest is False
        assert new.is_latest is True

    def test_db_constraint_blocks_two_latest_per_machine(self):
        ImageRelease.objects.create(
            tag="v0.9.0",
            machine=ImageRelease.Machine.QEMU,
            s3_key="images/v0.9.0/qemu.wic.bz2",
            sha256="a" * 64,
            size_bytes=1000,
            is_latest=True,
        )
        # Bypass save() to simulate a concurrent UPDATE that didn't go through the flip logic.
        # bulk_create skips Model.save(), and a second is_latest=True row for the same machine
        # must then be rejected by the partial unique index at the DB layer.
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                ImageRelease.objects.filter(tag="v0.9.0").update(is_latest=True)
                ImageRelease.objects.bulk_create(
                    [
                        ImageRelease(
                            tag="v1-alpha",
                            machine=ImageRelease.Machine.QEMU,
                            s3_key="images/v1-alpha/qemu.wic.bz2",
                            sha256="b" * 64,
                            size_bytes=2000,
                            is_latest=True,
                        )
                    ]
                )


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

        # READY branch
        ok_job = ImageImportJob.objects.create(
            tag="v1-alpha",
            machine="qemux86-64",
            requested_by=admin_user,
        )
        ok_job.status = ImageImportJob.Status.READY
        ok_job.save()
        ok_job.refresh_from_db()
        assert ok_job.status == ImageImportJob.Status.READY

        # FAILED branch with error_message
        bad_job = ImageImportJob.objects.create(
            tag="v9",
            machine="qemux86-64",
            requested_by=admin_user,
        )
        bad_job.status = ImageImportJob.Status.FAILED
        bad_job.error_message = "cosign verification failed"
        bad_job.save()
        bad_job.refresh_from_db()
        assert bad_job.status == ImageImportJob.Status.FAILED
        assert "cosign" in bad_job.error_message
