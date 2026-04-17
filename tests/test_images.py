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


class TestStorageKeys:
    def test_release_key_layout(self):
        from apps.images.storage import release_bundle_key, release_key

        assert release_key("v1-alpha", "qemux86-64") == "images/v1-alpha/qemux86-64.wic.bz2"
        assert (
            release_bundle_key("v1-alpha", "qemux86-64")
            == "images/v1-alpha/qemux86-64.wic.bz2.bundle"
        )


class TestGithubRelease:
    def test_fetch_parses_sha256_sidecar(self, tmp_path, monkeypatch):
        import hashlib

        from apps.images import github

        wic_body = b"fakewicbody"
        expected_sha = hashlib.sha256(wic_body).hexdigest()
        sidecar = (f"{expected_sha}  oe5xrx-qemux86-64-v1-alpha.wic.bz2\n").encode()

        captured = {}

        def fake_urlopen(url, *a, **kw):
            captured.setdefault("urls", []).append(url)
            body = {
                ".wic.bz2": wic_body,
                ".sha256": sidecar,
                ".bundle": b"fakebundle",
            }
            for suffix, payload in body.items():
                if url.endswith(suffix):
                    import io

                    return io.BytesIO(payload)
            raise AssertionError(f"unexpected url {url}")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

        asset = github.fetch_release_asset("OE5XRX/linux-image", "v1-alpha", "qemux86-64")
        assert asset.wic_bytes == wic_body
        assert asset.sha256 == expected_sha
        assert asset.bundle_bytes == b"fakebundle"


@pytest.mark.django_db
class TestImportView:
    def test_admin_can_create_import_job(self, client, admin_user):
        from django.urls import reverse

        from apps.images.models import ImageImportJob

        client.force_login(admin_user)
        response = client.post(
            reverse("images:import"),
            {"tag": "v1-alpha", "machine": "qemux86-64", "mark_as_latest": "on"},
        )
        assert response.status_code == 302  # redirect to list
        job = ImageImportJob.objects.get()
        assert job.tag == "v1-alpha"
        assert job.machine == "qemux86-64"
        assert job.mark_as_latest is True
        assert job.status == ImageImportJob.Status.PENDING
        assert job.requested_by == admin_user

    def test_operator_cannot_create_import_job(self, client, operator_user):
        from django.urls import reverse

        client.force_login(operator_user)
        response = client.post(
            reverse("images:import"),
            {"tag": "v1-alpha", "machine": "qemux86-64"},
        )
        # AdminRequiredMixin returns 403 for non-admin
        assert response.status_code == 403

    def test_anonymous_redirected_to_login(self, client):
        from django.urls import reverse

        response = client.post(
            reverse("images:import"),
            {"tag": "v1-alpha", "machine": "qemux86-64"},
        )
        assert response.status_code == 302
        assert "/accounts/login" in response["Location"]


class TestCosignVerify:
    def test_verify_invokes_cosign_binary(self, tmp_path, monkeypatch):
        from apps.images import cosign

        calls = []

        def fake_run(cmd, *a, **kw):
            calls.append(cmd)

            class Result:
                returncode = 0
                stdout = b""
                stderr = b""

            return Result()

        monkeypatch.setattr("subprocess.run", fake_run)
        cosign.verify_blob(
            blob_bytes=b"fake",
            bundle_bytes=b"also-fake",
            repo="OE5XRX/linux-image",
            tag="v1-alpha",
        )
        assert len(calls) == 1
        cmd = calls[0]
        assert cmd[0] == "cosign"
        assert "--bundle" in cmd
        # Identity should pin to this tag
        expected_regexp = (
            "https://github.com/OE5XRX/linux-image/.github/workflows/release.yml"
            "@refs/tags/v1-alpha"
        )
        assert expected_regexp in cmd

    def test_verify_raises_on_nonzero(self, monkeypatch):
        from apps.images import cosign

        def fake_run(cmd, *a, **kw):
            class Result:
                returncode = 1
                stdout = b""
                stderr = b"no valid signature"

            return Result()

        monkeypatch.setattr("subprocess.run", fake_run)
        with pytest.raises(cosign.CosignVerificationError):
            cosign.verify_blob(
                blob_bytes=b"fake",
                bundle_bytes=b"bad-bundle",
                repo="OE5XRX/linux-image",
                tag="v1-alpha",
            )
