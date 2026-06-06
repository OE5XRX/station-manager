"""Regression: deleting an ImageRelease that is referenced by a
Deployment or a ProvisioningJob must not 500.

Both FKs use ``on_delete=models.PROTECT`` because the release record is
audit-relevant — deployments and provisioning jobs are a historical
record of what was rolled out / shipped, and we don't want a "clean up
old images" UX to silently destroy that trail.

``ImageDeleteView`` therefore catches ``ProtectedError`` and surfaces
a flash error pointing at the blocking references. The release stays
on disk and in DB.
"""

import pytest
from django.contrib.messages import get_messages
from django.urls import reverse


def _release():
    from apps.images.models import ImageRelease

    return ImageRelease.objects.create(
        tag="v1-alpha",
        machine="qemux86-64",
        s3_key="images/v1-alpha/qemux86-64.wic.bz2",
        sha256="a" * 64,
        size_bytes=100,
    )


def _no_storage_calls(monkeypatch):
    """storage.delete must NOT fire when we abort the DB delete — the S3
    objects are still referenced by the protecting row's audit trail."""
    monkeypatch.setattr(
        "apps.images.storage.delete",
        lambda key: pytest.fail(f"storage.delete called for {key} despite PROTECT"),
    )


@pytest.mark.django_db
def test_delete_release_blocked_by_deployment(client, admin_user, monkeypatch):
    from apps.deployments.models import Deployment
    from apps.images.models import ImageRelease

    _no_storage_calls(monkeypatch)
    rel = _release()
    Deployment.objects.create(
        image_release=rel,
        target_type=Deployment.TargetType.ALL,
        strategy=Deployment.Strategy.IMMEDIATE,
        status=Deployment.Status.COMPLETED,
    )

    client.force_login(admin_user)
    resp = client.post(reverse("images:delete", args=[rel.pk]), follow=False)

    assert resp.status_code == 302
    assert resp["Location"] == reverse("images:list")
    assert ImageRelease.objects.filter(pk=rel.pk).exists()

    flashes = [str(m) for m in get_messages(resp.wsgi_request)]
    assert any("deployment" in f.lower() for f in flashes), flashes


@pytest.mark.django_db
def test_delete_release_blocked_by_provisioning_job(client, admin_user, monkeypatch):
    from apps.images.models import ImageRelease
    from apps.provisioning.models import ProvisioningJob
    from apps.stations.models import Station

    _no_storage_calls(monkeypatch)
    rel = _release()
    station = Station.objects.create(name="bench-station-1")
    ProvisioningJob.objects.create(station=station, image_release=rel)

    client.force_login(admin_user)
    resp = client.post(reverse("images:delete", args=[rel.pk]), follow=False)

    assert resp.status_code == 302
    assert resp["Location"] == reverse("images:list")
    assert ImageRelease.objects.filter(pk=rel.pk).exists()

    flashes = [str(m) for m in get_messages(resp.wsgi_request)]
    assert any("provisioning" in f.lower() for f in flashes), flashes
