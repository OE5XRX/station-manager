"""Tests for recipients_for_station_alert.

Pins the routing contract: who gets an email for a station alert.
The spec (§4.7) says recipients are:
  - Vereins-Admins (membership_level=ADMIN), vereinsweit
  - Region-Manager of station.region (if set)
  - Station-Admin of this station
  - Station-Maintainer of this station

Excludes Vereins-Staff (operative role, not escalation inbox),
Applicants (defense-in-depth), inactive users, no-email users.
"""

import pytest

from apps.accounts.models import User
from apps.monitoring.recipients import recipients_for_station_alert
from apps.stations.models import (
    Region,
    RegionAssignment,
    Station,
    StationAssignment,
)


def _user(level, email="x@example.com", username=None):
    username = username or f"u{User.objects.count()}"
    u = User.objects.create_user(username=username, password="x", email=email)
    u.membership_level = level
    u.save(update_fields=["membership_level"])
    return u


@pytest.mark.django_db
class TestRecipientsForStationAlert:
    def test_admin_always_recipient(self):
        admin = _user(User.MembershipLevel.ADMIN)
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        assert admin in list(recipients_for_station_alert(s))

    def test_region_manager_in_set_for_own_region(self):
        mgr = _user(User.MembershipLevel.MEMBER)
        r = Region.objects.create(name="Tirol", slug="tirol")
        RegionAssignment.objects.create(user=mgr, region=r, role=RegionAssignment.Role.MANAGER)
        s = Station.objects.create(name="OE5A", callsign="OE5A", region=r)
        assert mgr in list(recipients_for_station_alert(s))

    def test_region_manager_not_in_set_for_other_region(self):
        mgr = _user(User.MembershipLevel.MEMBER)
        r1 = Region.objects.create(name="Tirol", slug="tirol")
        r2 = Region.objects.create(name="OOe", slug="ooe")
        RegionAssignment.objects.create(user=mgr, region=r1, role=RegionAssignment.Role.MANAGER)
        s = Station.objects.create(name="OE5A", callsign="OE5A", region=r2)
        assert mgr not in list(recipients_for_station_alert(s))

    def test_station_admin_in_set(self):
        u = _user(User.MembershipLevel.MEMBER)
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        StationAssignment.objects.create(user=u, station=s, role=StationAssignment.Role.ADMIN)
        assert u in list(recipients_for_station_alert(s))

    def test_station_admin_not_in_set_for_other_station(self):
        u = _user(User.MembershipLevel.MEMBER)
        s1 = Station.objects.create(name="OE5A", callsign="OE5A")
        s2 = Station.objects.create(name="OE5B", callsign="OE5B")
        StationAssignment.objects.create(user=u, station=s1, role=StationAssignment.Role.ADMIN)
        assert u not in list(recipients_for_station_alert(s2))

    def test_station_maintainer_in_set(self):
        u = _user(User.MembershipLevel.MEMBER)
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        StationAssignment.objects.create(user=u, station=s, role=StationAssignment.Role.MAINTAINER)
        assert u in list(recipients_for_station_alert(s))

    def test_staff_not_recipient_without_topology(self):
        staff = _user(User.MembershipLevel.STAFF)
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        assert staff not in list(recipients_for_station_alert(s))

    def test_member_without_assignments_not_recipient(self):
        m = _user(User.MembershipLevel.MEMBER)
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        assert m not in list(recipients_for_station_alert(s))

    def test_applicant_never_recipient(self):
        # Applicants cannot hold assignments by model-level invariant
        # (_ApplicantForbiddenMixin). The recipient query additionally
        # excludes them as defense-in-depth.
        a = _user(User.MembershipLevel.APPLICANT)
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        assert a not in list(recipients_for_station_alert(s))

    def test_dedup_same_user_multiple_roles(self):
        u = _user(User.MembershipLevel.ADMIN)
        r = Region.objects.create(name="Tirol", slug="tirol")
        s = Station.objects.create(name="OE5A", callsign="OE5A", region=r)
        RegionAssignment.objects.create(user=u, region=r, role=RegionAssignment.Role.MANAGER)
        # Vereins-Admin so the invariant doesn't block the assignment.
        StationAssignment.objects.create(user=u, station=s, role=StationAssignment.Role.ADMIN)
        recipients = list(recipients_for_station_alert(s))
        assert recipients.count(u) == 1

    def test_inactive_user_excluded(self):
        admin = _user(User.MembershipLevel.ADMIN)
        admin.is_active = False
        admin.save(update_fields=["is_active"])
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        assert admin not in list(recipients_for_station_alert(s))

    def test_user_without_email_excluded(self):
        admin = _user(User.MembershipLevel.ADMIN, email="")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        assert admin not in list(recipients_for_station_alert(s))

    def test_no_region_only_admin_and_station_assignments(self):
        admin = _user(User.MembershipLevel.ADMIN, username="admin")
        # An orphan region-manager exists but the station has no region
        mgr = _user(User.MembershipLevel.MEMBER, username="mgr")
        r = Region.objects.create(name="Tirol", slug="tirol")
        RegionAssignment.objects.create(user=mgr, region=r, role=RegionAssignment.Role.MANAGER)
        s = Station.objects.create(name="OE5A", callsign="OE5A", region=None)
        rcp = list(recipients_for_station_alert(s))
        assert admin in rcp
        assert mgr not in rcp
