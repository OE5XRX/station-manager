"""Tests for AccountAuditLog doppel-emit on StationAssignment save/delete.

Sub-Spec 1a Foundation Sektion 3.2. Bestehender StationAuditLog-Emit
bleibt unverändert — wir prüfen den ZUSÄTZLICHEN AccountAuditLog-Eintrag.
"""

import pytest

from apps.accounts.models import AccountAuditLog, User
from apps.stations.models import Region, Station, StationAssignment, StationAuditLog


@pytest.fixture
def region(db):
    return Region.objects.create(name="Innviertel")


@pytest.fixture
def station(db, region):
    return Station.objects.create(name="OE5XRX-Test", callsign="OE5XRX", region=region)


@pytest.fixture
def assigner(db):
    # Wer das Assignment vergibt (z.B. ein Admin).
    return User.objects.create_user(
        username="OE5ADMIN",
        password="x",
        membership_level=User.MembershipLevel.ADMIN,
    )


@pytest.fixture
def member(db):
    return User.objects.create_user(
        username="OE5MEMBER",
        password="x",
        membership_level=User.MembershipLevel.MEMBER,
    )


@pytest.mark.django_db
class TestStationAssignmentDoppelEmit:
    """Pro StationAssignment.save schreibt das Signal sowohl
    StationAuditLog als auch AccountAuditLog."""

    def test_create_emits_account_audit_log(self, station, member, assigner):
        before = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.STATION_ASSIGNMENT_CREATED
        ).count()
        StationAssignment.objects.create(
            station=station,
            user=member,
            role=StationAssignment.Role.MAINTAINER,
            assigned_by=assigner,
        )
        after = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.STATION_ASSIGNMENT_CREATED
        ).count()
        assert after == before + 1

    def test_create_emits_with_target_user(self, station, member, assigner):
        StationAssignment.objects.create(
            station=station,
            user=member,
            role=StationAssignment.Role.MAINTAINER,
            assigned_by=assigner,
        )
        entry = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.STATION_ASSIGNMENT_CREATED,
            target_user=member,
        ).latest("created_at")
        assert entry.target_user == member
        assert entry.actor == assigner

    def test_create_message_contains_station_and_role(self, station, member, assigner):
        StationAssignment.objects.create(
            station=station,
            user=member,
            role=StationAssignment.Role.MAINTAINER,
            assigned_by=assigner,
        )
        entry = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.STATION_ASSIGNMENT_CREATED,
            target_user=member,
        ).latest("created_at")
        # message format: "station=<callsign or name>, role=<role display>"
        assert "OE5XRX" in entry.message
        assert "Station-Maintainer" in entry.message or "maintainer" in entry.message.lower()

    def test_create_also_emits_station_audit_log(self, station, member, assigner):
        """Regression: bestehender StationAuditLog-Emit bleibt unverändert."""
        before = StationAuditLog.objects.filter(
            station=station,
            event_type=StationAuditLog.EventType.STATION_ASSIGNMENT_CREATED,
        ).count()
        StationAssignment.objects.create(
            station=station,
            user=member,
            role=StationAssignment.Role.MAINTAINER,
            assigned_by=assigner,
        )
        after = StationAuditLog.objects.filter(
            station=station,
            event_type=StationAuditLog.EventType.STATION_ASSIGNMENT_CREATED,
        ).count()
        assert after == before + 1

    def test_delete_emits_account_audit_log_revoked(self, station, member, assigner):
        assignment = StationAssignment.objects.create(
            station=station,
            user=member,
            role=StationAssignment.Role.MAINTAINER,
            assigned_by=assigner,
        )
        before = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.STATION_ASSIGNMENT_REVOKED
        ).count()
        assignment.delete()
        after = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.STATION_ASSIGNMENT_REVOKED
        ).count()
        assert after == before + 1

    def test_delete_emits_with_target_user(self, station, member, assigner):
        assignment = StationAssignment.objects.create(
            station=station,
            user=member,
            role=StationAssignment.Role.MAINTAINER,
            assigned_by=assigner,
        )
        assignment.delete()
        entry = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.STATION_ASSIGNMENT_REVOKED,
            target_user=member,
        ).latest("created_at")
        assert entry.target_user == member
