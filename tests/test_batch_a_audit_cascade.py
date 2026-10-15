"""Regression tests for Copilot batch-A findings.

A1 — _client_ip IP validation.
A2 + A5 — origin-based cascade-delete guard (StationAssignment).
A3 — Region cascade-delete guard (RegionAssignment).
A4 — Station delete ProtectedError → 409, no false audit row.
"""

import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# A1 — _client_ip: validate the proxy-set IP, return None for garbage
# ---------------------------------------------------------------------------


class TestClientIp:
    """Unit tests for apps.api.audit._client_ip (trusted X-Real-IP, validated)."""

    def _make_request(self, real=None, remote=None, xff=None):
        """Build a minimal mock-request-like object."""

        class FakeReq:
            META = {}

        req = FakeReq()
        req.META = {}
        if real is not None:
            req.META["HTTP_X_REAL_IP"] = real
        if xff is not None:
            req.META["HTTP_X_FORWARDED_FOR"] = xff
        if remote is not None:
            req.META["REMOTE_ADDR"] = remote
        return req

    def test_valid_ipv4_returned(self):
        from apps.api.audit import _client_ip

        assert _client_ip(self._make_request(real="203.0.113.1")) == "203.0.113.1"

    def test_valid_ipv6_returned(self):
        from apps.api.audit import _client_ip

        assert _client_ip(self._make_request(real="2001:db8::1")) == "2001:db8::1"

    def test_garbage_real_ip_returns_none(self):
        from apps.api.audit import _client_ip

        assert _client_ip(self._make_request(real="not-an-ip; DROP TABLE audit--")) is None

    def test_garbage_real_ip_falls_back_to_remote(self):
        """When X-Real-IP is garbage, REMOTE_ADDR is used if valid."""
        from apps.api.audit import _client_ip

        assert _client_ip(self._make_request(real="garbage!!", remote="10.0.0.1")) == "10.0.0.1"

    def test_client_xff_is_ignored(self):
        """X-Forwarded-For is client-controlled behind nginx (it appends)."""
        from apps.api.audit import _client_ip

        req = self._make_request(xff="6.6.6.6, 10.0.0.1", remote="10.0.0.1")
        assert _client_ip(req) == "10.0.0.1"

    def test_no_header_returns_remote_addr(self):
        from apps.api.audit import _client_ip

        assert _client_ip(self._make_request(remote="172.16.0.1")) == "172.16.0.1"

    def test_no_header_garbage_remote_returns_none(self):
        from apps.api.audit import _client_ip

        assert _client_ip(self._make_request(remote="not-valid-either")) is None

    def test_ip_with_port_stripped(self):
        """An IPv4:port pair should be stripped to just the IP."""
        from apps.api.audit import _client_ip

        assert _client_ip(self._make_request(real="203.0.113.7:12345")) == "203.0.113.7"


class TestClientIpDoesNotSuppressAudit:
    """A garbage XFF must NOT suppress the audit row — it should write None."""

    def test_garbage_xff_still_produces_audit_row(self, api_topology, bearer):
        """Admin PATCH a region with garbage XFF → 200 AND audit row is written.

        The count may go up by more than 1 because both the view's
        audit_account_write AND the _on_region_save signal fire; >=+1 is the
        invariant.
        """
        from apps.accounts.models import AccountAuditLog

        t = api_topology
        before = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.REGION_UPDATED
        ).count()
        url = reverse("api:region-detail", args=[t["region_in"].pk])
        r = bearer(t["admin"]).patch(
            url,
            {"name": "Updated Name"},
            format="json",
            HTTP_X_REAL_IP="not-an-ip; DROP TABLE audit--",
        )
        assert r.status_code == 200
        after = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.REGION_UPDATED
        ).count()
        assert after >= before + 1, (
            "At least one REGION_UPDATED audit row must be written even with garbage XFF"
        )


# ---------------------------------------------------------------------------
# A2 + A5 — origin-based Station cascade: only StationAuditLog suppressed,
#            AccountAuditLog revocation still fires
# ---------------------------------------------------------------------------


class TestStationCascadeAudit:
    """Deleting a Station that has StationAssignments must:
    - succeed without IntegrityError / dangling FK
    - still emit AccountAuditLog STATION_ASSIGNMENT_REVOKED for each assignment
    - NOT emit per-assignment StationAuditLog STATION_ASSIGNMENT_REVOKED rows
      (those would reference a deleted station → FK violation)
    """

    @pytest.fixture
    def region(self, db):
        from apps.stations.models import Region

        return Region.objects.create(name="Cascade Region", slug="cascade")

    @pytest.fixture
    def station(self, region):
        from apps.stations.models import Station

        return Station.objects.create(name="Delete Me", callsign="OE9DEL", region=region)

    @pytest.fixture
    def member(self, db):
        from apps.accounts.models import User

        return User.objects.create_user(
            username="cascade_member", password="x", membership_level=User.MembershipLevel.MEMBER
        )

    @pytest.fixture
    def assigner(self, db):
        from apps.accounts.models import User

        return User.objects.create_user(
            username="cascade_admin", password="x", membership_level=User.MembershipLevel.ADMIN
        )

    def test_cascade_delete_account_audit_preserved(self, station, member, assigner):
        """Cascade-delete the station → AccountAuditLog REVOKED still written."""
        from apps.accounts.models import AccountAuditLog
        from apps.stations.models import StationAssignment

        StationAssignment.objects.create(
            station=station, user=member, role="maintainer", assigned_by=assigner
        )
        before = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.STATION_ASSIGNMENT_REVOKED,
            target_user=member,
        ).count()
        # This must not raise IntegrityError
        station.delete()
        after = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.STATION_ASSIGNMENT_REVOKED,
            target_user=member,
        ).count()
        assert after == before + 1, "AccountAuditLog revocation must fire even on cascade-delete"

    def test_cascade_delete_no_dangling_station_audit(self, station, member, assigner):
        """Cascade-delete the station → no per-assignment StationAuditLog REVOKED written
        (which would dangle)."""
        from apps.stations.models import StationAssignment, StationAuditLog

        assignment = StationAssignment.objects.create(
            station=station, user=member, role="maintainer", assigned_by=assigner
        )
        before_revoke = StationAuditLog.objects.filter(
            event_type=StationAuditLog.EventType.STATION_ASSIGNMENT_REVOKED,
        ).count()
        station.delete()
        after_revoke = StationAuditLog.objects.filter(
            event_type=StationAuditLog.EventType.STATION_ASSIGNMENT_REVOKED,
        ).count()
        # No new REVOKED rows with the now-missing station FK
        assert after_revoke == before_revoke, (
            "StationAuditLog ASSIGNMENT_REVOKED must NOT be written during cascade-delete "
            "(would leave dangling FK)"
        )
        _ = assignment  # referenced only for fixture setup; already deleted by cascade

    def test_standalone_delete_still_emits_both_audit_rows(self, station, member, assigner):
        """A direct (non-cascade) assignment delete still emits BOTH audit rows."""
        from apps.accounts.models import AccountAuditLog
        from apps.stations.models import StationAssignment, StationAuditLog

        assignment = StationAssignment.objects.create(
            station=station, user=member, role="maintainer", assigned_by=assigner
        )
        before_account = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.STATION_ASSIGNMENT_REVOKED
        ).count()
        before_station = StationAuditLog.objects.filter(
            event_type=StationAuditLog.EventType.STATION_ASSIGNMENT_REVOKED
        ).count()
        assignment.delete()
        after_account = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.STATION_ASSIGNMENT_REVOKED
        ).count()
        after_station = StationAuditLog.objects.filter(
            event_type=StationAuditLog.EventType.STATION_ASSIGNMENT_REVOKED
        ).count()
        assert after_account == before_account + 1, "AccountAuditLog must fire on direct delete"
        assert after_station == before_station + 1, "StationAuditLog must fire on direct delete"

    def test_api_station_delete_succeeds(self, api_topology, bearer):
        """API DELETE of a station with assignments → 204, no crash."""
        t = api_topology
        url = reverse("api:station-detail", args=[t["station_in"].pk])
        r = bearer(t["admin"]).delete(url)
        assert r.status_code == 204


# ---------------------------------------------------------------------------
# A3 — Region cascade: RegionAssignment delete does not leave dangling FK
# ---------------------------------------------------------------------------


class TestRegionCascadeAudit:
    """Deleting a Region that has RegionAssignments must:
    - succeed without IntegrityError
    - handle AccountAuditLog safely (region FK is SET_NULL, but the origin-based
      guard must prevent a NEW row referencing the deleted region)
    """

    @pytest.fixture
    def region(self, db):
        from apps.stations.models import Region

        return Region.objects.create(name="Region Cascade", slug="rcascade")

    @pytest.fixture
    def member(self, db):
        from apps.accounts.models import User

        return User.objects.create_user(
            username="rcascade_member", password="x", membership_level=User.MembershipLevel.MEMBER
        )

    @pytest.fixture
    def admin_user(self, db):
        from apps.accounts.models import User

        return User.objects.create_user(
            username="rcascade_admin", password="x", membership_level=User.MembershipLevel.ADMIN
        )

    def test_region_cascade_no_integrity_error(self, region, member, admin_user):
        """Delete a Region with RegionAssignments → no IntegrityError."""
        from apps.stations.models import RegionAssignment

        RegionAssignment.objects.create(
            region=region, user=member, role="manager", assigned_by=admin_user
        )
        # Must not raise IntegrityError
        region.delete()

    def test_region_cascade_audit_row_written(self, region, member, admin_user):
        """Delete a Region with RegionAssignments → AccountAuditLog REGION_DELETED written."""
        from apps.accounts.models import AccountAuditLog
        from apps.stations.models import RegionAssignment

        RegionAssignment.objects.create(
            region=region, user=member, role="manager", assigned_by=admin_user
        )
        before = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.REGION_DELETED
        ).count()
        region.delete()
        after = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.REGION_DELETED
        ).count()
        assert after == before + 1

    def test_region_standalone_assignment_delete_emits_audit(self, region, member, admin_user):
        """A direct RegionAssignment delete (not cascade) still emits its audit row."""
        from apps.accounts.models import AccountAuditLog
        from apps.stations.models import RegionAssignment

        assignment = RegionAssignment.objects.create(
            region=region, user=member, role="manager", assigned_by=admin_user
        )
        before = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.REGION_ASSIGNMENT_REVOKED,
            target_user=member,
        ).count()
        assignment.delete()
        after = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.REGION_ASSIGNMENT_REVOKED,
            target_user=member,
        ).count()
        assert after == before + 1


# ---------------------------------------------------------------------------
# A4 — Station delete with PROTECT FK → 409, no false audit row
# ---------------------------------------------------------------------------


class TestStationDeleteProtect:
    """DELETE a station that has a DeploymentResult (on_delete=PROTECT) must:
    - return 409 Conflict (not 500)
    - leave the station in the DB
    - NOT write a false DELETED StationAuditLog row
    """

    def test_station_with_deployment_result_returns_409(self, api_topology, bearer, image_release):
        from apps.deployments.models import Deployment, DeploymentResult

        t = api_topology
        dep = Deployment.objects.create(
            image_release=image_release,
            target_type=Deployment.TargetType.STATION,
            target_station=t["station_in"],
            status=Deployment.Status.IN_PROGRESS,
            created_by=t["admin"],
        )
        DeploymentResult.objects.create(
            deployment=dep,
            station=t["station_in"],
            status=DeploymentResult.Status.PENDING,
            previous_version="0.9.0",
        )
        url = reverse("api:station-detail", args=[t["station_in"].pk])
        r = bearer(t["admin"]).delete(url)
        assert r.status_code == 409

    def test_station_with_deployment_result_still_exists_after_409(
        self, api_topology, bearer, image_release
    ):
        from apps.deployments.models import Deployment, DeploymentResult
        from apps.stations.models import Station

        t = api_topology
        dep = Deployment.objects.create(
            image_release=image_release,
            target_type=Deployment.TargetType.STATION,
            target_station=t["station_in"],
            status=Deployment.Status.IN_PROGRESS,
            created_by=t["admin"],
        )
        DeploymentResult.objects.create(
            deployment=dep,
            station=t["station_in"],
            status=DeploymentResult.Status.PENDING,
            previous_version="0.9.0",
        )
        url = reverse("api:station-detail", args=[t["station_in"].pk])
        bearer(t["admin"]).delete(url)
        assert Station.objects.filter(pk=t["station_in"].pk).exists()

    def test_station_with_deployment_result_no_false_audit_row(
        self, api_topology, bearer, image_release
    ):
        from apps.deployments.models import Deployment, DeploymentResult
        from apps.stations.models import StationAuditLog

        t = api_topology
        dep = Deployment.objects.create(
            image_release=image_release,
            target_type=Deployment.TargetType.STATION,
            target_station=t["station_in"],
            status=Deployment.Status.IN_PROGRESS,
            created_by=t["admin"],
        )
        DeploymentResult.objects.create(
            deployment=dep,
            station=t["station_in"],
            status=DeploymentResult.Status.PENDING,
            previous_version="0.9.0",
        )
        before = StationAuditLog.objects.filter(
            station=t["station_in"],
            event_type=StationAuditLog.EventType.DELETED,
        ).count()
        url = reverse("api:station-detail", args=[t["station_in"].pk])
        bearer(t["admin"]).delete(url)
        after = StationAuditLog.objects.filter(
            station=t["station_in"],
            event_type=StationAuditLog.EventType.DELETED,
        ).count()
        assert after == before, "No false DELETED audit row must be written when delete fails"

    def test_station_without_protect_refs_deletes_204_and_audit(self, api_topology, bearer):
        """Normal station delete: 204 + audit row written."""
        from apps.stations.models import StationAuditLog

        t = api_topology
        station = t["station_in"]
        before = StationAuditLog.objects.filter(
            event_type=StationAuditLog.EventType.DELETED,
        ).count()
        url = reverse("api:station-detail", args=[station.pk])
        r = bearer(t["admin"]).delete(url)
        assert r.status_code == 204
        after = StationAuditLog.objects.filter(
            event_type=StationAuditLog.EventType.DELETED,
        ).count()
        assert after == before + 1
