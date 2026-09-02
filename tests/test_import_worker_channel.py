"""Tests that channel is threaded through the image import worker."""
from __future__ import annotations

import pytest
from unittest import mock

from apps.images.models import ImageImportJob, ImageRelease
from apps.provisioning.management.commands import run_background_jobs as rbj


def _assert_channel(channel):
    assert channel == "dev"


@pytest.mark.django_db
def test_import_sets_channel_and_keys(monkeypatch):
    job = ImageImportJob.objects.create(tag="v1", machine="qemux86-64", channel="dev")
    job.status = ImageImportJob.Status.RUNNING
    job.save(update_fields=["status"])

    fake_asset = mock.Mock(wic_bytes=b"\x00" * 10, bundle_bytes=b"b", sha256="a" * 64)
    monkeypatch.setattr(
        rbj.github,
        "fetch_release_asset",
        lambda repo, tag, machine, channel: (_assert_channel(channel), fake_asset)[1],
    )
    monkeypatch.setattr(rbj.cosign, "verify_blob", lambda **kw: None)
    monkeypatch.setattr(rbj.image_storage, "upload_bytes", lambda k, d: None)
    monkeypatch.setattr(rbj, "_decompress_to", lambda s, d: d.write_bytes(b"x"))

    def fake_extract(dec, out):
        out.write_bytes(b"rootfs")
        return 6, "b" * 64

    monkeypatch.setattr(rbj.extraction, "extract_rootfs", fake_extract)

    rbj._run_import_job(job)

    rel = ImageRelease.all_objects.get(tag="v1", machine="qemux86-64", channel="dev")
    assert rel.channel == "dev"
    assert rel.s3_key == "images/v1/dev/qemux86-64.wic.bz2"


@pytest.mark.django_db
def test_release_channel_job_uses_release_partitioned_keys(monkeypatch):
    """A job with default channel='release' must still produce /release/ paths."""
    job = ImageImportJob.objects.create(tag="v2", machine="qemux86-64", channel="release")
    job.status = ImageImportJob.Status.RUNNING
    job.save(update_fields=["status"])

    fake_asset = mock.Mock(wic_bytes=b"\x00" * 10, bundle_bytes=b"b", sha256="c" * 64)
    captured_channel = []
    monkeypatch.setattr(
        rbj.github,
        "fetch_release_asset",
        lambda repo, tag, machine, channel: (captured_channel.append(channel), fake_asset)[1],
    )
    monkeypatch.setattr(rbj.cosign, "verify_blob", lambda **kw: None)
    monkeypatch.setattr(rbj.image_storage, "upload_bytes", lambda k, d: None)
    monkeypatch.setattr(rbj, "_decompress_to", lambda s, d: d.write_bytes(b"x"))

    def fake_extract(dec, out):
        out.write_bytes(b"rootfs")
        return 6, "d" * 64

    monkeypatch.setattr(rbj.extraction, "extract_rootfs", fake_extract)

    rbj._run_import_job(job)

    assert captured_channel == ["release"]
    rel = ImageRelease.all_objects.get(tag="v2", machine="qemux86-64", channel="release")
    assert rel.s3_key == "images/v2/release/qemux86-64.wic.bz2"
