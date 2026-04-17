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


@pytest.mark.django_db
class TestConfigRender:
    def test_render_produces_expected_fields(self, station):
        from apps.provisioning.config_render import render_config

        yaml_text = render_config(
            server_url="https://ham.oe5xrx.org",
            station_id=station.id,
        )
        assert f"station_id: {station.id}" in yaml_text
        assert "server_url: https://ham.oe5xrx.org" in yaml_text
        assert "ed25519_key_path: /etc/station-agent/device_key.pem" in yaml_text
        assert "terminal_enabled: true" in yaml_text


class TestGuestfishInject:
    def test_inject_files_into_data_partition(self, tmp_path):
        import bz2
        import subprocess
        from pathlib import Path

        from apps.provisioning.guestfish import inject_provisioning_files

        src = Path(__file__).parent / "fixtures" / "tiny.wic.bz2"
        wic_path = tmp_path / "tiny.wic"
        wic_path.write_bytes(bz2.decompress(src.read_bytes()))

        inject_provisioning_files(
            wic_path=wic_path,
            partition_device="/dev/sda1",
            config_yaml="server_url: https://x\n",
            private_key_pem=b"-----BEGIN PRIVATE KEY-----\nAAA\n-----END PRIVATE KEY-----\n",
        )

        # Read back via guestfish to verify
        result = subprocess.run(
            [
                "guestfish",
                "--ro",
                "-a",
                str(wic_path),
                "run",
                ":",
                "mount",
                "/dev/sda1",
                "/",
                ":",
                "cat",
                "/etc-overlay/station-agent/config.yml",
            ],
            capture_output=True,
            check=True,
        )
        assert b"server_url: https://x" in result.stdout
