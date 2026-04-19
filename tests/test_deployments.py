import json

import pytest
from django.urls import reverse

from apps.deployments.models import Deployment, DeploymentResult
from tests.conftest import device_auth_headers


@pytest.mark.django_db
class TestDeploymentCheck:
    def test_check_returns_pending_deployment(self, client, station_with_key, deployment_result):
        station, private_key = station_with_key
        result = deployment_result
        release = result.deployment.image_release

        body = json.dumps({"current_version": ""}).encode("utf-8")
        response = client.post(
            reverse("api:deployment_check"),
            data=body,
            content_type="application/json",
            **device_auth_headers(private_key, station.pk, body),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["deployment_result_id"] == result.pk
        assert data["deployment_id"] == result.deployment_id
        assert data["target_tag"] == release.tag
        assert data["checksum_sha256"] == release.sha256
        assert data["size_bytes"] == release.size_bytes
        assert data["download_url"].endswith(f"/deployments/{result.deployment_id}/download/")

    def test_check_no_pending(self, client, station_with_key):
        station, private_key = station_with_key
        body = json.dumps({"current_version": ""}).encode("utf-8")
        response = client.post(
            reverse("api:deployment_check"),
            data=body,
            content_type="application/json",
            **device_auth_headers(private_key, station.pk, body),
        )
        assert response.status_code == 204

    def test_check_requires_device_auth(self, client):
        body = json.dumps({"current_version": ""}).encode("utf-8")
        response = client.post(
            reverse("api:deployment_check"),
            data=body,
            content_type="application/json",
        )
        assert response.status_code == 401

    def test_check_accepts_current_version(self, client, station_with_key, deployment_result):
        """current_version is parsed but doesn't change behavior in MVP."""
        station, private_key = station_with_key
        body = json.dumps({"current_version": "v1-alpha"}).encode("utf-8")
        response = client.post(
            reverse("api:deployment_check"),
            data=body,
            content_type="application/json",
            **device_auth_headers(private_key, station.pk, body),
        )
        assert response.status_code == 200

    def test_check_returns_active_deployment_for_crash_recovery(
        self, client, station_with_key, deployment_result
    ):
        """An agent that crashed mid-download must be able to find its
        own deployment again on restart — so the check query must not
        ignore DOWNLOADING / INSTALLING / REBOOTING results."""
        station, private_key = station_with_key
        deployment_result.status = DeploymentResult.Status.DOWNLOADING
        deployment_result.save(update_fields=["status"])

        body = json.dumps({"current_version": ""}).encode("utf-8")
        response = client.post(
            reverse("api:deployment_check"),
            data=body,
            content_type="application/json",
            **device_auth_headers(private_key, station.pk, body),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["deployment_result_id"] == deployment_result.pk
        assert data["deployment_result_status"] == "downloading"

    def test_check_skips_terminal_results(self, client, station_with_key, deployment_result):
        """Terminal statuses (success/failed/cancelled/superseded/rolled_back)
        must not resurface in the check query — the agent has nothing to do
        with them."""
        station, private_key = station_with_key
        deployment_result.status = DeploymentResult.Status.SUCCESS
        deployment_result.save(update_fields=["status"])

        body = json.dumps({"current_version": ""}).encode("utf-8")
        response = client.post(
            reverse("api:deployment_check"),
            data=body,
            content_type="application/json",
            **device_auth_headers(private_key, station.pk, body),
        )
        assert response.status_code == 204


@pytest.mark.django_db
class TestDeploymentStatusUpdate:
    def test_status_update_downloading(self, client, station_with_key, deployment_result):
        """Status should transition to downloading."""
        station, private_key = station_with_key
        body = json.dumps({"status": "downloading"}).encode("utf-8")
        response = client.post(
            reverse("api:deployment_status_update", kwargs={"pk": deployment_result.pk}),
            data=body,
            content_type="application/json",
            **device_auth_headers(private_key, station.pk, body),
        )
        assert response.status_code == 200
        deployment_result.refresh_from_db()
        assert deployment_result.status == DeploymentResult.Status.DOWNLOADING

    def test_status_update_sets_started_at(self, client, station_with_key, deployment_result):
        """First non-pending status should set started_at."""
        station, private_key = station_with_key
        assert deployment_result.started_at is None

        body = json.dumps({"status": "downloading"}).encode("utf-8")
        client.post(
            reverse("api:deployment_status_update", kwargs={"pk": deployment_result.pk}),
            data=body,
            content_type="application/json",
            **device_auth_headers(private_key, station.pk, body),
        )
        deployment_result.refresh_from_db()
        assert deployment_result.started_at is not None

    def test_status_update_failed_sets_completed(
        self, client, station_with_key, deployment_result
    ):
        """Terminal status (failed) should set completed_at."""
        station, private_key = station_with_key
        body = json.dumps({"status": "failed", "error_message": "Checksum mismatch"}).encode(
            "utf-8"
        )
        client.post(
            reverse("api:deployment_status_update", kwargs={"pk": deployment_result.pk}),
            data=body,
            content_type="application/json",
            **device_auth_headers(private_key, station.pk, body),
        )
        deployment_result.refresh_from_db()
        assert deployment_result.status == DeploymentResult.Status.FAILED
        assert deployment_result.completed_at is not None
        assert deployment_result.error_message == "Checksum mismatch"


@pytest.mark.django_db
class TestDeploymentCommit:
    def test_commit_marks_success(self, client, station_with_key, deployment_result):
        """Agent commit should mark result as SUCCESS when version matches."""
        station, private_key = station_with_key
        # Move result to REBOOTING first (commit looks for rebooting/verifying/installing)
        deployment_result.status = DeploymentResult.Status.REBOOTING
        deployment_result.save(update_fields=["status"])

        # Version must match the deployment's image_release tag ("v1-alpha"
        # from the shared image_release fixture) — mismatches are
        # treated as bootloader rollbacks now.
        target_tag = deployment_result.deployment.image_release.tag
        body = json.dumps({"version": target_tag}).encode("utf-8")
        response = client.post(
            reverse("api:deployment_commit"),
            data=body,
            content_type="application/json",
            **device_auth_headers(private_key, station.pk, body),
        )
        assert response.status_code == 200
        deployment_result.refresh_from_db()
        assert deployment_result.status == DeploymentResult.Status.SUCCESS
        assert deployment_result.completed_at is not None
        assert deployment_result.new_version == target_tag

    def test_commit_rejects_version_mismatch(self, client, station_with_key, deployment_result):
        """A commit with a non-matching version means the bootloader
        rolled back — record as ROLLED_BACK, don't move the station's
        current_image_release, return 409."""
        station, private_key = station_with_key
        deployment_result.status = DeploymentResult.Status.REBOOTING
        deployment_result.save(update_fields=["status"])

        body = json.dumps({"version": "something-else"}).encode("utf-8")
        response = client.post(
            reverse("api:deployment_commit"),
            data=body,
            content_type="application/json",
            **device_auth_headers(private_key, station.pk, body),
        )
        assert response.status_code == 409
        deployment_result.refresh_from_db()
        assert deployment_result.status == DeploymentResult.Status.ROLLED_BACK
        assert "rollback" in deployment_result.error_message.lower()
        # Station's current_image_release must NOT have been bumped.
        station.refresh_from_db()
        assert station.current_image_release_id != deployment_result.deployment.image_release_id

    def test_commit_completes_deployment(self, client, station_with_key, deployment_result):
        """When all results are done, deployment status should update."""
        station, private_key = station_with_key
        deployment_result.status = DeploymentResult.Status.REBOOTING
        deployment_result.save(update_fields=["status"])

        target_tag = deployment_result.deployment.image_release.tag
        body = json.dumps({"version": target_tag}).encode("utf-8")
        client.post(
            reverse("api:deployment_commit"),
            data=body,
            content_type="application/json",
            **device_auth_headers(private_key, station.pk, body),
        )
        deployment = deployment_result.deployment
        deployment.refresh_from_db()
        assert deployment.status == Deployment.Status.COMPLETED


@pytest.mark.django_db
class TestDeploymentWebViews:
    def test_create_deployment_creates_results(
        self, client, operator_user, image_release, station
    ):
        """Creating a deployment should create DeploymentResult per target station."""
        client.force_login(operator_user)
        response = client.post(
            reverse("deployments:deployment_create"),
            data={
                "image_release": image_release.pk,
                "target_type": Deployment.TargetType.STATION,
                "target_station": station.pk,
                "strategy": Deployment.Strategy.IMMEDIATE,
                "phase_config": "{}",
            },
        )
        assert response.status_code == 302
        dep = Deployment.objects.latest("created_at")
        assert dep.results.count() == 1
        assert dep.results.first().station == station
        assert dep.status == Deployment.Status.IN_PROGRESS

    def test_cancel_deployment(self, client, operator_user, deployment, deployment_result):
        """Cancelling should set deployment and pending results to cancelled."""
        client.force_login(operator_user)
        response = client.post(
            reverse("deployments:deployment_cancel", kwargs={"pk": deployment.pk}),
        )
        assert response.status_code == 302
        deployment.refresh_from_db()
        assert deployment.status == Deployment.Status.CANCELLED
        deployment_result.refresh_from_db()
        assert deployment_result.status == DeploymentResult.Status.CANCELLED

    def test_deployment_list_requires_operator(self, client, member_user):
        """Member should get 403 on deployment list."""
        client.force_login(member_user)
        response = client.get(reverse("deployments:deployment_list"))
        assert response.status_code == 403

    def test_deployment_detail_shows_progress(
        self, client, operator_user, deployment, deployment_result
    ):
        """Detail page should render for operator."""
        client.force_login(operator_user)
        response = client.get(
            reverse("deployments:deployment_detail", kwargs={"pk": deployment.pk}),
        )
        assert response.status_code == 200

    def test_progress_excludes_cancelled_and_superseded_from_in_progress(
        self, deployment, station, image_release
    ):
        """Regression: in_progress used to be computed as
        total - completed - failed - pending, so CANCELLED and
        SUPERSEDED results were silently counted as 'in progress'
        — the dashboard and WebSocket payload both lied about the
        amount of live work left in a wave that had been cancelled.
        """
        from apps.deployments.models import DeploymentResult
        from apps.stations.models import Station

        s2 = Station.objects.create(name="s2", callsign="S2TEST")
        s3 = Station.objects.create(name="s3", callsign="S3TEST")
        s4 = Station.objects.create(name="s4", callsign="S4TEST")
        s5 = Station.objects.create(name="s5", callsign="S5TEST")

        DeploymentResult.objects.create(
            deployment=deployment, station=station, status=DeploymentResult.Status.SUCCESS
        )
        DeploymentResult.objects.create(
            deployment=deployment, station=s2, status=DeploymentResult.Status.DOWNLOADING
        )
        DeploymentResult.objects.create(
            deployment=deployment, station=s3, status=DeploymentResult.Status.PENDING
        )
        DeploymentResult.objects.create(
            deployment=deployment, station=s4, status=DeploymentResult.Status.CANCELLED
        )
        DeploymentResult.objects.create(
            deployment=deployment, station=s5, status=DeploymentResult.Status.SUPERSEDED
        )

        p = deployment.progress
        assert p["total"] == 5
        assert p["completed"] == 1
        assert p["in_progress"] == 1  # only DOWNLOADING
        assert p["pending"] == 1
        assert p["cancelled"] == 2  # CANCELLED + SUPERSEDED
        assert p["failed"] == 0
        # Categories must sum to total — no silent leakage.
        assert (
            p["completed"] + p["in_progress"] + p["pending"] + p["cancelled"] + p["failed"]
            == p["total"]
        )


@pytest.mark.django_db
class TestDeploymentImageReleaseFK:
    def test_deployment_uses_image_release(self, image_release, station, admin_user):
        from apps.deployments.models import Deployment

        dep = Deployment.objects.create(
            image_release=image_release,
            target_type=Deployment.TargetType.STATION,
            target_station=station,
            created_by=admin_user,
        )
        assert dep.image_release == image_release
        assert not hasattr(dep, "firmware_artifact")

    def test_superseded_status_exists(self):
        from apps.deployments.models import DeploymentResult

        assert DeploymentResult.Status.SUPERSEDED == "superseded"
        assert "superseded" in dict(DeploymentResult.Status.choices)

    def test_image_release_protect_on_delete(self, image_release, station, admin_user):
        from django.db.models.deletion import ProtectedError

        from apps.deployments.models import Deployment

        Deployment.objects.create(
            image_release=image_release,
            target_type=Deployment.TargetType.STATION,
            target_station=station,
            created_by=admin_user,
        )
        with pytest.raises(ProtectedError):
            image_release.delete()


@pytest.mark.django_db
class TestSupersession:
    def _second_release(self):
        from apps.images.models import ImageRelease

        # Unset the fixture's is_latest so we don't hit the partial-unique index.
        ImageRelease.objects.filter(is_latest=True, machine="qemux86-64").update(is_latest=False)
        return ImageRelease.objects.create(
            tag="v1-beta",
            machine="qemux86-64",
            s3_key="images/v1-beta/qemux86-64.wic.bz2",
            sha256="b" * 64,
            size_bytes=1000,
            is_latest=True,
        )

    def test_pending_result_gets_superseded(self, image_release, station, admin_user):
        from apps.deployments.models import Deployment, DeploymentResult
        from apps.deployments.supersession import supersede_pending_for_station

        dep1 = Deployment.objects.create(
            image_release=image_release,
            target_type=Deployment.TargetType.STATION,
            target_station=station,
            created_by=admin_user,
        )
        r1 = DeploymentResult.objects.create(deployment=dep1, station=station)

        newer = self._second_release()
        dep2 = Deployment.objects.create(
            image_release=newer,
            target_type=Deployment.TargetType.STATION,
            target_station=station,
            created_by=admin_user,
        )
        superseded = supersede_pending_for_station(station=station, new_deployment=dep2)
        assert superseded == [r1.pk]

        r1.refresh_from_db()
        assert r1.status == DeploymentResult.Status.SUPERSEDED

    def test_active_result_blocks_new_deployment(self, image_release, station, admin_user):
        from apps.deployments.models import Deployment, DeploymentResult
        from apps.deployments.supersession import (
            ActiveDeploymentConflictError,
            supersede_pending_for_station,
        )

        dep1 = Deployment.objects.create(
            image_release=image_release,
            target_type=Deployment.TargetType.STATION,
            target_station=station,
            created_by=admin_user,
        )
        DeploymentResult.objects.create(
            deployment=dep1,
            station=station,
            status=DeploymentResult.Status.INSTALLING,
        )

        newer = self._second_release()
        dep2 = Deployment.objects.create(
            image_release=newer,
            target_type=Deployment.TargetType.STATION,
            target_station=station,
            created_by=admin_user,
        )

        with pytest.raises(ActiveDeploymentConflictError):
            supersede_pending_for_station(station=station, new_deployment=dep2)


@pytest.mark.django_db
class TestDeploymentDownload:
    def test_full_download_streams_from_s3(
        self, client, station_with_key, image_release, admin_user, monkeypatch
    ):
        import io

        from apps.deployments.models import Deployment, DeploymentResult

        station, priv = station_with_key
        dep = Deployment.objects.create(
            image_release=image_release,
            target_type=Deployment.TargetType.STATION,
            target_station=station,
            status=Deployment.Status.IN_PROGRESS,
            created_by=admin_user,
        )
        DeploymentResult.objects.create(
            deployment=dep, station=station, status=DeploymentResult.Status.PENDING
        )

        monkeypatch.setattr(
            "apps.images.storage.open_stream", lambda key: io.BytesIO(b"IMAGE" * 10)
        )
        headers = device_auth_headers(priv, station.pk, b"")
        r = client.get(reverse("api:deployment_download", args=[dep.pk]), **headers)
        assert r.status_code == 200
        assert b"".join(r.streaming_content) == b"IMAGE" * 10

    def test_download_rejects_other_station(
        self, client, station_with_key, image_release, admin_user, db
    ):
        import base64
        import hashlib
        import time

        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            PublicFormat,
        )

        from apps.api.models import DeviceKey
        from apps.stations.models import Station

        station_a, _ = station_with_key
        dep = Deployment.objects.create(
            image_release=image_release,
            target_type=Deployment.TargetType.STATION,
            target_station=station_a,
            status=Deployment.Status.IN_PROGRESS,
            created_by=admin_user,
        )
        DeploymentResult.objects.create(
            deployment=dep,
            station=station_a,
            status=DeploymentResult.Status.PENDING,
        )

        # A different station with its own key.
        other = Station.objects.create(name="Other")
        priv_b = Ed25519PrivateKey.generate()
        pub = priv_b.public_key().public_bytes(encoding=Encoding.Raw, format=PublicFormat.Raw)
        DeviceKey.objects.create(
            station=other, current_public_key=base64.b64encode(pub).decode("ascii")
        )
        body_hash = hashlib.sha256(b"").hexdigest()
        ts = str(time.time())
        sig = base64.b64encode(priv_b.sign(f"{ts}:{body_hash}".encode())).decode("ascii")
        r = client.get(
            reverse("api:deployment_download", args=[dep.pk]),
            HTTP_AUTHORIZATION=f"DeviceKey {other.pk}",
            HTTP_X_DEVICE_SIGNATURE=sig,
            HTTP_X_DEVICE_TIMESTAMP=ts,
        )
        assert r.status_code == 403

    def test_range_returns_206(
        self, client, station_with_key, image_release, admin_user, monkeypatch
    ):
        import io

        from apps.deployments.models import Deployment, DeploymentResult

        station, priv = station_with_key
        dep = Deployment.objects.create(
            image_release=image_release,
            target_type=Deployment.TargetType.STATION,
            target_station=station,
            status=Deployment.Status.IN_PROGRESS,
            created_by=admin_user,
        )
        DeploymentResult.objects.create(
            deployment=dep,
            station=station,
            status=DeploymentResult.Status.PENDING,
        )
        # ImageRelease.size_bytes is 1000 (fixture); supply enough bytes so the Range
        # offset (e.g. 10) is valid.
        payload = b"X" * 1000
        monkeypatch.setattr("apps.images.storage.open_stream", lambda key: io.BytesIO(payload))
        headers = device_auth_headers(priv, station.pk, b"")
        r = client.get(
            reverse("api:deployment_download", args=[dep.pk]),
            HTTP_RANGE="bytes=10-19",
            **headers,
        )
        assert r.status_code == 206
        body = b"".join(r.streaming_content)
        assert body == b"X" * 10
        assert r["Content-Range"] == "bytes 10-19/1000"

    def test_range_end_before_start_returns_416(
        self, client, station_with_key, image_release, admin_user, monkeypatch
    ):
        import io

        from apps.deployments.models import Deployment, DeploymentResult

        station, priv = station_with_key
        dep = Deployment.objects.create(
            image_release=image_release,
            target_type=Deployment.TargetType.STATION,
            target_station=station,
            status=Deployment.Status.IN_PROGRESS,
            created_by=admin_user,
        )
        DeploymentResult.objects.create(
            deployment=dep, station=station, status=DeploymentResult.Status.PENDING
        )
        monkeypatch.setattr("apps.images.storage.open_stream", lambda key: io.BytesIO(b"X" * 1000))
        headers = device_auth_headers(priv, station.pk, b"")
        r = client.get(
            reverse("api:deployment_download", args=[dep.pk]),
            HTTP_RANGE="bytes=10-9",
            **headers,
        )
        assert r.status_code == 416
        assert r["Content-Range"] == "bytes */1000"

    def test_range_on_non_seekable_backend_returns_416(
        self, client, station_with_key, image_release, admin_user, monkeypatch
    ):
        """When the storage backend can't seek, Range must not fall back to
        read-and-discard — that's a bandwidth DoS vector. Return 416 so the
        agent restarts from 0."""
        import io

        from apps.deployments.models import Deployment, DeploymentResult

        class NoSeekStream:
            def __init__(self, data):
                self._data = data
                self._pos = 0
                self.closed = False

            def read(self, n):
                chunk = self._data[self._pos : self._pos + n]
                self._pos += len(chunk)
                return chunk

            def seek(self, _):
                raise io.UnsupportedOperation("seek")

            def close(self):
                self.closed = True

        station, priv = station_with_key
        dep = Deployment.objects.create(
            image_release=image_release,
            target_type=Deployment.TargetType.STATION,
            target_station=station,
            status=Deployment.Status.IN_PROGRESS,
            created_by=admin_user,
        )
        DeploymentResult.objects.create(
            deployment=dep, station=station, status=DeploymentResult.Status.PENDING
        )
        monkeypatch.setattr(
            "apps.images.storage.open_stream", lambda key: NoSeekStream(b"X" * 1000)
        )
        headers = device_auth_headers(priv, station.pk, b"")
        r = client.get(
            reverse("api:deployment_download", args=[dep.pk]),
            HTTP_RANGE="bytes=500-",
            **headers,
        )
        assert r.status_code == 416
        assert r["Content-Range"] == "bytes */1000"


@pytest.mark.django_db
class TestCommitSetsCurrentImage:
    def test_commit_updates_current_image_release(
        self, client, station_with_key, image_release, admin_user
    ):
        import json

        from apps.deployments.models import Deployment, DeploymentResult

        station, priv = station_with_key
        dep = Deployment.objects.create(
            image_release=image_release,
            target_type=Deployment.TargetType.STATION,
            target_station=station,
            status=Deployment.Status.IN_PROGRESS,
            created_by=admin_user,
        )
        DeploymentResult.objects.create(
            deployment=dep,
            station=station,
            status=DeploymentResult.Status.REBOOTING,
        )
        body = json.dumps({"version": image_release.tag}).encode("utf-8")
        response = client.post(
            reverse("api:deployment_commit"),
            data=body,
            content_type="application/json",
            **device_auth_headers(priv, station.pk, body),
        )
        assert response.status_code == 200
        station.refresh_from_db()
        assert station.current_image_release_id == image_release.pk


@pytest.mark.django_db
def test_broadcast_includes_machine_and_tag(station, image_release, admin_user, monkeypatch):
    from apps.deployments.consumers import broadcast_deployment_status
    from apps.deployments.models import Deployment, DeploymentResult

    captured = {}

    def fake_group_send(group, event):
        captured["event"] = event

    monkeypatch.setattr(
        "apps.deployments.consumers.async_to_sync",
        lambda fn: lambda *a, **k: fn(*a, **k),
    )
    monkeypatch.setattr(
        "apps.deployments.consumers.get_channel_layer",
        lambda: type("CL", (), {"group_send": staticmethod(fake_group_send)})(),
    )

    dep = Deployment.objects.create(
        image_release=image_release,
        target_type=Deployment.TargetType.STATION,
        target_station=station,
        status=Deployment.Status.IN_PROGRESS,
        created_by=admin_user,
    )
    result = DeploymentResult.objects.create(
        deployment=dep,
        station=station,
        status=DeploymentResult.Status.INSTALLING,
    )
    broadcast_deployment_status(dep, result=result)

    payload = captured["event"]["data"]
    assert payload["result"]["station_id"] == station.pk
    assert payload["result"]["tag"] == image_release.tag
    assert payload["result"]["machine"] == image_release.machine
