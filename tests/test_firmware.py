import hashlib

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.firmware.models import FirmwareArtifact


@pytest.mark.django_db
class TestFirmwareUpload:
    def test_upload_computes_checksum(self, client, operator_user):
        """Uploading a firmware file should compute sha256 checksum."""
        client.force_login(operator_user)
        file_content = b"firmware binary data for checksum test"
        expected_hash = hashlib.sha256(file_content).hexdigest()
        dummy_file = SimpleUploadedFile(
            "firmware.bin",
            file_content,
            content_type="application/octet-stream",
        )
        response = client.post(
            reverse("firmware:firmware_upload"),
            data={
                "name": "checksum-test",
                "version": "2.0.0",
                "artifact_type": "os_image",
                "file": dummy_file,
                "release_notes": "Test upload",
            },
        )
        assert response.status_code == 302
        artifact = FirmwareArtifact.objects.get(name="checksum-test", version="2.0.0")
        assert artifact.checksum_sha256 == expected_hash

    def test_upload_computes_file_size(self, client, operator_user):
        """Uploading should compute file_size field."""
        client.force_login(operator_user)
        file_content = b"\x00" * 512
        dummy_file = SimpleUploadedFile(
            "firmware-size.bin",
            file_content,
            content_type="application/octet-stream",
        )
        response = client.post(
            reverse("firmware:firmware_upload"),
            data={
                "name": "size-test",
                "version": "1.0.0",
                "artifact_type": "os_image",
                "file": dummy_file,
                "release_notes": "",
            },
        )
        assert response.status_code == 302
        artifact = FirmwareArtifact.objects.get(name="size-test", version="1.0.0")
        assert artifact.file_size == 512

    def test_upload_requires_operator(self, client, member_user):
        """Members should get 403 on firmware upload."""
        client.force_login(member_user)
        dummy_file = SimpleUploadedFile(
            "firmware-bad.bin",
            b"bad data",
            content_type="application/octet-stream",
        )
        response = client.post(
            reverse("firmware:firmware_upload"),
            data={
                "name": "not-allowed",
                "version": "1.0.0",
                "artifact_type": "os_image",
                "file": dummy_file,
            },
        )
        assert response.status_code == 403


@pytest.mark.django_db
class TestFirmwareDownload:
    def test_download_requires_operator(self, client, member_user, firmware_artifact):
        """Members should get 403 on firmware download."""
        client.force_login(member_user)
        response = client.get(
            reverse("firmware:firmware_download", kwargs={"pk": firmware_artifact.pk}),
        )
        assert response.status_code == 403

    def test_download_invalid_pk_returns_404(self, client, operator_user):
        """Non-existent pk should return 404."""
        client.force_login(operator_user)
        response = client.get(
            reverse("firmware:firmware_download", kwargs={"pk": 99999}),
        )
        assert response.status_code == 404

    def test_download_serves_file(self, client, operator_user, firmware_artifact):
        """Download should return FileResponse with correct headers."""
        client.force_login(operator_user)
        response = client.get(
            reverse("firmware:firmware_download", kwargs={"pk": firmware_artifact.pk}),
        )
        assert response.status_code == 200
        assert response["Content-Type"] == "application/octet-stream"
        assert "Content-Disposition" in response
        assert firmware_artifact.name in response["Content-Disposition"]


@pytest.mark.django_db
class TestFirmwareListPermissions:
    def test_firmware_list_requires_login(self, client):
        """Unauthenticated user should be redirected."""
        response = client.get(reverse("firmware:firmware_list"))
        assert response.status_code == 302

    def test_member_can_view_firmware_list(self, client, member_user):
        """Members should be able to view the firmware list."""
        client.force_login(member_user)
        response = client.get(reverse("firmware:firmware_list"))
        assert response.status_code == 200

    def test_member_cannot_delete_firmware(self, client, member_user, firmware_artifact):
        """Members should get 403 on firmware delete."""
        client.force_login(member_user)
        response = client.post(
            reverse("firmware:firmware_delete", kwargs={"pk": firmware_artifact.pk}),
        )
        assert response.status_code == 403
