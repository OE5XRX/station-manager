"""Regression: provisioning bakes settings.SERVER_PUBLIC_URL into
the agent config inside the rootfs.

Before this fix the worker used getattr(settings, "SERVER_PUBLIC_URL",
"https://ham.oe5xrx.org") and settings.SERVER_PUBLIC_URL did not exist
— so every station provisioned after the 2026-05 CAX21→prod-01
migration ended up pointing at the legacy CAX21 hostname.
"""

from unittest.mock import patch

import pytest
from django.test import override_settings

from apps.images.models import ImageRelease
from apps.provisioning.management.commands.run_background_jobs import (
    _run_provisioning_job,
)
from apps.provisioning.models import ProvisioningJob


def _release_and_job(station):
    release = ImageRelease.objects.create(
        tag="v1-alpha",
        machine="qemux86-64",
        s3_key="images/v1-alpha/qemux86-64.wic.bz2",
        sha256="a" * 64,
        size_bytes=100,
    )
    return ProvisioningJob.objects.create(
        station=station,
        image_release=release,
    )


@pytest.fixture
def provisioning_stubs():
    """Stub out the heavy paths in _run_provisioning_job so the test
    only exercises the URL wire-up, not the wic-injection / S3 round-
    trip. ``inject_provisioning_files`` is captured so the test can
    assert what config_yaml it received."""
    with (
        patch(
            "apps.provisioning.management.commands.run_background_jobs.image_storage.open_stream"
        ) as open_stream,
        patch(
            "apps.provisioning.management.commands.run_background_jobs._decompress_to"
        ) as decompress,
        patch(
            "apps.provisioning.management.commands.run_background_jobs._compress_to_bytes"
        ) as compress,
        patch(
            "apps.provisioning.management.commands.run_background_jobs.guestfish.inject_provisioning_files"
        ) as inject,
        patch(
            "apps.provisioning.management.commands.run_background_jobs.image_storage.upload_bytes"
        ),
    ):
        open_stream.return_value.__enter__.return_value.read.side_effect = [b"", b""]
        decompress.return_value = None
        compress.return_value = b""
        yield inject


@pytest.mark.django_db
@override_settings(SERVER_PUBLIC_URL="")
def test_provisioning_fails_loud_without_server_public_url(station, provisioning_stubs):
    """An empty SERVER_PUBLIC_URL must mark the ProvisioningJob FAILED
    with a clear error_message rather than producing a config.yml with
    an empty server_url field that the agent would silently fail against."""
    job = _release_and_job(station)

    _run_provisioning_job(job)
    job.refresh_from_db()

    assert job.status == ProvisioningJob.Status.FAILED
    assert "SERVER_PUBLIC_URL" in job.error_message

    # inject_provisioning_files MUST NOT have been called — we abort
    # before any wic mutation.
    assert provisioning_stubs.call_count == 0


@pytest.mark.django_db
@override_settings(SERVER_PUBLIC_URL="https://remote.oe5xrx.org")
def test_provisioning_bakes_server_public_url_from_settings(station, provisioning_stubs):
    """Happy path: the configured URL ends up in the rendered config.yml.

    Note: this test passes both before and after the fix because
    override_settings injects the attribute regardless. It's kept as a
    regression test for the next time someone touches render_config
    or _run_provisioning_job — it pins the contract that the worker
    must round-trip settings.SERVER_PUBLIC_URL into the YAML."""
    job = _release_and_job(station)

    _run_provisioning_job(job)

    config_yaml = provisioning_stubs.call_args.kwargs["config_yaml"]
    assert "server_url: https://remote.oe5xrx.org" in config_yaml
    assert "ham.oe5xrx.org" not in config_yaml
