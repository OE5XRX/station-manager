import pytest
from django.db import IntegrityError

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
