import uuid

import pytest
from django.urls import reverse


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
        assert "ed25519_key_path: /etc/stationagent/device_key.pem" in yaml_text
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
                "/etc-overlay/stationagent/config.yml",
            ],
            capture_output=True,
            check=True,
        )
        assert b"server_url: https://x" in result.stdout


@pytest.mark.django_db
class TestProvisioningWorker:
    def test_pending_job_pipeline_goes_ready(
        self, station, image_release, admin_user, monkeypatch, settings
    ):
        from apps.api.models import DeviceKey
        from apps.provisioning.management.commands.run_background_jobs import (
            process_pending_provisioning_jobs,
        )
        from apps.provisioning.models import ProvisioningJob

        settings.SERVER_PUBLIC_URL = "https://ham.oe5xrx.org"

        # Stubs for IO-bound steps
        monkeypatch.setattr(
            "apps.images.storage.open_stream",
            lambda key: __import__("io").BytesIO(b"FAKEWICBZ2BYTES"),
        )
        monkeypatch.setattr(
            "apps.provisioning.management.commands.run_background_jobs._decompress_to",
            lambda src, dst: dst.write_bytes(b"FAKEWIC"),
        )
        monkeypatch.setattr(
            "apps.provisioning.guestfish.inject_provisioning_files",
            lambda **kw: None,
        )
        monkeypatch.setattr(
            "apps.provisioning.management.commands.run_background_jobs._compress_to_bytes",
            lambda path: b"FAKEWICBZ2",
        )
        uploaded = {}
        monkeypatch.setattr(
            "apps.images.storage.upload_bytes",
            lambda key, data: uploaded.setdefault(key, data),
        )

        job = ProvisioningJob.objects.create(
            station=station,
            image_release=image_release,
            requested_by=admin_user,
        )

        process_pending_provisioning_jobs()

        job.refresh_from_db()
        assert job.status == ProvisioningJob.Status.READY
        assert job.output_s3_key.startswith("provisioning/")
        assert job.output_s3_key.endswith(".wic.bz2")
        assert job.output_size_bytes == len(b"FAKEWICBZ2")
        assert job.expires_at is not None
        # DeviceKey was created (public half stored server-side)
        assert DeviceKey.objects.filter(station=station).exists()


@pytest.mark.django_db
class TestProvisioningViews:
    def test_admin_creates_provisioning_job(self, client, admin_user, station, image_release):
        from apps.provisioning.models import ProvisioningJob

        client.force_login(admin_user)
        response = client.post(
            reverse("provisioning:new", kwargs={"station_pk": station.pk}),
            {"image_release": image_release.pk},
        )
        assert response.status_code == 302
        assert ProvisioningJob.objects.filter(station=station).count() == 1
        job = ProvisioningJob.objects.get()
        assert job.status == ProvisioningJob.Status.PENDING
        assert job.requested_by == admin_user
        assert job.image_release == image_release

    def test_create_rejects_if_active_job_exists(self, client, admin_user, station, image_release):
        from apps.provisioning.models import ProvisioningJob

        ProvisioningJob.objects.create(
            station=station,
            image_release=image_release,
            requested_by=admin_user,
            status=ProvisioningJob.Status.RUNNING,
        )
        client.force_login(admin_user)
        response = client.post(
            reverse("provisioning:new", args=[station.pk]),
            {"image_release": image_release.pk},
        )
        assert response.status_code == 302
        assert ProvisioningJob.objects.count() == 1

    def test_create_rejects_mismatched_machine(self, client, admin_user, station, image_release):
        """If the posted machine doesn't match the image's machine, reject."""
        from apps.provisioning.models import ProvisioningJob

        # image_release fixture is qemux86-64; submit "raspberrypi4-64" as machine
        client.force_login(admin_user)
        response = client.post(
            reverse("provisioning:new", args=[station.pk]),
            {"machine": "raspberrypi4-64", "image_release": image_release.pk},
        )
        assert response.status_code == 302
        assert ProvisioningJob.objects.count() == 0

    def test_operator_cannot_create_job(self, client, operator_user, station, image_release):
        from apps.provisioning.models import ProvisioningJob

        client.force_login(operator_user)
        response = client.post(
            reverse("provisioning:new", kwargs={"station_pk": station.pk}),
            {"image_release": image_release.pk},
        )
        assert response.status_code == 403
        assert ProvisioningJob.objects.count() == 0

    def test_status_endpoint_returns_partial(self, client, admin_user, station, image_release):
        from apps.provisioning.models import ProvisioningJob

        job = ProvisioningJob.objects.create(
            station=station,
            image_release=image_release,
            requested_by=admin_user,
        )
        client.force_login(admin_user)
        response = client.get(reverse("provisioning:status", kwargs={"pk": job.id}))
        assert response.status_code == 200
        body = response.content.lower()
        assert b"pending" in body or b"running" in body

    def test_download_ready_job_streams_and_marks_downloaded(
        self, client, admin_user, station, image_release, monkeypatch
    ):
        from datetime import timedelta

        from django.utils import timezone

        from apps.provisioning.models import ProvisioningJob

        job = ProvisioningJob.objects.create(
            station=station,
            image_release=image_release,
            requested_by=admin_user,
            status=ProvisioningJob.Status.READY,
            output_s3_key="provisioning/abc/test.wic.bz2",
            output_size_bytes=10,
            ready_at=timezone.now(),
            expires_at=timezone.now() + timedelta(hours=1),
        )
        monkeypatch.setattr(
            "apps.images.storage.open_stream",
            lambda key: __import__("io").BytesIO(b"0123456789"),
        )
        client.force_login(admin_user)
        response = client.get(reverse("provisioning:download", kwargs={"pk": job.id}))
        assert response.status_code == 200
        assert b"".join(response.streaming_content) == b"0123456789"
        job.refresh_from_db()
        assert job.status == ProvisioningJob.Status.DOWNLOADED
        assert job.downloaded_at is not None

    def test_download_aborted_stays_ready(
        self, client, admin_user, station, image_release, monkeypatch
    ):
        import io
        from datetime import timedelta

        from django.utils import timezone

        from apps.provisioning.models import ProvisioningJob

        job = ProvisioningJob.objects.create(
            station=station,
            image_release=image_release,
            requested_by=admin_user,
            status=ProvisioningJob.Status.READY,
            output_s3_key="provisioning/abc/test.wic.bz2",
            output_size_bytes=100,
            ready_at=timezone.now(),
            expires_at=timezone.now() + timedelta(hours=1),
        )
        monkeypatch.setattr(
            "apps.images.storage.open_stream",
            lambda key: io.BytesIO(b"0" * 100),
        )
        client.force_login(admin_user)
        response = client.get(reverse("provisioning:download", args=[job.id]))
        assert response.status_code == 200
        # Pull one chunk, then close the underlying generator — this is what
        # WSGI servers do when the client disconnects mid-response. Django's
        # streaming_content wraps the raw generator in a map(); the raw one
        # is accessible as response._iterator.
        iterator = iter(response.streaming_content)
        next(iterator)
        response._iterator.close()
        job.refresh_from_db()
        assert job.status == ProvisioningJob.Status.READY
        assert job.downloaded_at is None

    def test_download_expired_job_returns_410(self, client, admin_user, station, image_release):
        from datetime import timedelta

        from django.utils import timezone

        from apps.provisioning.models import ProvisioningJob

        job = ProvisioningJob.objects.create(
            station=station,
            image_release=image_release,
            requested_by=admin_user,
            status=ProvisioningJob.Status.READY,
            output_s3_key="provisioning/abc/test.wic.bz2",
            output_size_bytes=10,
            ready_at=timezone.now() - timedelta(hours=2),
            expires_at=timezone.now() - timedelta(hours=1),
        )
        client.force_login(admin_user)
        response = client.get(reverse("provisioning:download", kwargs={"pk": job.id}))
        assert response.status_code == 410

    def test_download_sanitizes_content_disposition_filename(
        self, client, admin_user, station, image_release, monkeypatch
    ):
        """A nasty s3 key must not inject CRLF or stray quotes into the header."""
        import io
        from datetime import timedelta

        from django.utils import timezone

        from apps.provisioning.models import ProvisioningJob

        job = ProvisioningJob.objects.create(
            station=station,
            image_release=image_release,
            requested_by=admin_user,
            status=ProvisioningJob.Status.READY,
            output_s3_key='provisioning/abc/evil"\r\ninjected: yes".wic.bz2',
            output_size_bytes=4,
            ready_at=timezone.now(),
            expires_at=timezone.now() + timedelta(hours=1),
        )
        monkeypatch.setattr(
            "apps.images.storage.open_stream",
            lambda key: io.BytesIO(b"xxxx"),
        )
        client.force_login(admin_user)
        response = client.get(reverse("provisioning:download", kwargs={"pk": job.id}))
        assert response.status_code == 200
        disposition = response["Content-Disposition"]
        # No CR/LF anywhere in the rendered header value.
        assert "\r" not in disposition
        assert "\n" not in disposition
        # The only quotes allowed are the two wrapping filename="...".
        assert disposition.count('"') == 2
        # Drain the response so the generator closes cleanly in the fixture.
        b"".join(response.streaming_content)


@pytest.mark.django_db
class TestStationDetailIntegration:
    def test_admin_sees_provisioning_section(self, client, admin_user, station, image_release):
        client.force_login(admin_user)
        response = client.get(
            reverse("stations:station_detail", kwargs={"pk": station.pk}),
        )
        assert response.status_code == 200
        assert b"Provisioning" in response.content
        assert b"Generate provisioning bundle" in response.content
        # Machine dropdown must render so the version select can be scoped
        # to a single machine (is_latest is unique per machine, not globally).
        assert b'name="machine"' in response.content
        # CSP compliance: the provisioning section's <script> must carry a
        # nonce and must not use the old inline onchange="_filterVersions..."
        # handler, which the project's nonce-based CSP would block.
        assert b"nonce=" in response.content
        assert b'onchange="_filterVersions' not in response.content

    def test_operator_does_not_see_provisioning_section(
        self, client, operator_user, station, image_release
    ):
        client.force_login(operator_user)
        response = client.get(
            reverse("stations:station_detail", kwargs={"pk": station.pk}),
        )
        assert response.status_code == 200
        assert b"Generate provisioning bundle" not in response.content
