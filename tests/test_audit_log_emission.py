"""Tests that each topology mutation emits the right audit-log entry."""

import pytest

from apps.accounts.models import AccountAuditLog, User
from apps.stations.models import (
    Region,
    RegionAssignment,
    Station,
    StationAssignment,
    StationAuditLog,
)


def _admin():
    u = User.objects.create_user(username="admin", password="x", email="a@x")
    u.membership_level = User.MembershipLevel.ADMIN
    u.save(update_fields=["membership_level"])
    return u


def _member(name):
    u = User.objects.create_user(username=name, password="x", email=f"{name}@x")
    u.membership_level = User.MembershipLevel.MEMBER
    u.save(update_fields=["membership_level"])
    return u


@pytest.mark.django_db
def test_station_assignment_create_emits_audit_log():
    admin = _admin()
    franz = _member("franz")
    s = Station.objects.create(name="OE5A", callsign="OE5A")
    StationAssignment.objects.create(
        user=franz,
        station=s,
        role=StationAssignment.Role.ADMIN,
        assigned_by=admin,
    )
    entry = StationAuditLog.objects.filter(
        event_type=StationAuditLog.EventType.STATION_ASSIGNMENT_CREATED,
        station=s,
    ).first()
    assert entry is not None
    assert entry.user == admin  # actor is the assigned_by
    assert "franz" in entry.message.lower()
    assert "admin" in entry.message.lower()


@pytest.mark.django_db
def test_station_assignment_revoke_emits_audit_log():
    admin = _admin()
    franz = _member("franz")
    s = Station.objects.create(name="OE5A", callsign="OE5A")
    a = StationAssignment.objects.create(
        user=franz,
        station=s,
        role=StationAssignment.Role.MAINTAINER,
        assigned_by=admin,
    )
    a.delete()
    entry = StationAuditLog.objects.filter(
        event_type=StationAuditLog.EventType.STATION_ASSIGNMENT_REVOKED,
        station=s,
    ).first()
    assert entry is not None
    assert "franz" in entry.message.lower()


@pytest.mark.django_db
def test_station_region_change_emits_audit_log():
    s = Station.objects.create(name="OE5A", callsign="OE5A")
    r1 = Region.objects.create(name="Tirol", slug="tirol")
    r2 = Region.objects.create(name="OOe", slug="ooe")
    s.region = r1
    s.save()
    s.region = r2
    s.save()
    # We expect at least one CHANGED event with a meaningful message.
    entries = list(
        StationAuditLog.objects.filter(
            event_type=StationAuditLog.EventType.STATION_REGION_CHANGED,
            station=s,
        ).order_by("created_at")
    )
    assert len(entries) >= 1
    last = entries[-1]
    assert "tirol" in last.message.lower() or "ooe" in last.message.lower()


@pytest.mark.django_db
def test_station_region_unchanged_does_not_emit():
    s = Station.objects.create(name="OE5A", callsign="OE5A")
    r = Region.objects.create(name="Tirol", slug="tirol")
    s.region = r
    s.save()
    # Save again without changing region
    s.callsign = "OE5XYZ"
    s.save()
    entries = StationAuditLog.objects.filter(
        event_type=StationAuditLog.EventType.STATION_REGION_CHANGED,
        station=s,
    )
    # Exactly one entry (the first set), not two.
    assert entries.count() == 1


@pytest.mark.django_db
def test_region_assignment_create_emits_audit_log():
    admin = _admin()
    lisa = _member("lisa")
    r = Region.objects.create(name="Tirol", slug="tirol")
    RegionAssignment.objects.create(
        user=lisa,
        region=r,
        role=RegionAssignment.Role.MANAGER,
        assigned_by=admin,
    )
    entry = AccountAuditLog.objects.filter(
        event_type=AccountAuditLog.EventType.REGION_ASSIGNMENT_CREATED,
        target_user=lisa,
        region=r,
    ).first()
    assert entry is not None
    assert entry.actor == admin


@pytest.mark.django_db
def test_region_assignment_revoke_emits_audit_log():
    admin = _admin()
    lisa = _member("lisa")
    r = Region.objects.create(name="Tirol", slug="tirol")
    a = RegionAssignment.objects.create(
        user=lisa,
        region=r,
        role=RegionAssignment.Role.MANAGER,
        assigned_by=admin,
    )
    a.delete()
    entry = AccountAuditLog.objects.filter(
        event_type=AccountAuditLog.EventType.REGION_ASSIGNMENT_REVOKED,
        target_user=lisa,
    ).first()
    assert entry is not None


@pytest.mark.django_db
def test_region_create_update_delete_emits_audit_log():
    r = Region.objects.create(name="Innviertel", slug="innv")
    assert AccountAuditLog.objects.filter(
        event_type=AccountAuditLog.EventType.REGION_CREATED,
        region=r,
    ).exists()

    r.name = "Innviertel-West"
    r.save()
    assert AccountAuditLog.objects.filter(
        event_type=AccountAuditLog.EventType.REGION_UPDATED,
        region=r,
    ).exists()

    r.delete()
    # After delete, region FK becomes NULL; query by event_type only.
    assert AccountAuditLog.objects.filter(
        event_type=AccountAuditLog.EventType.REGION_DELETED,
    ).exists()
