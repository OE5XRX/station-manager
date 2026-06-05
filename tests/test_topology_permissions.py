"""Tests for User permission-helpers: is_internal + topology lookups."""

import pytest

from apps.accounts.models import User
from apps.stations.models import (
    Region,
    RegionAssignment,
    Station,
    StationAssignment,
)


def _user(level=User.MembershipLevel.MEMBER):
    u = User.objects.create_user(username=f"u{User.objects.count()}", password="x")
    u.membership_level = level
    u.save(update_fields=["membership_level"])
    return u


@pytest.mark.django_db
class TestIsInternal:
    def test_admin_is_internal(self):
        assert _user(User.MembershipLevel.ADMIN).is_internal is True

    def test_staff_is_internal(self):
        assert _user(User.MembershipLevel.STAFF).is_internal is True

    def test_member_is_not_internal(self):
        assert _user(User.MembershipLevel.MEMBER).is_internal is False

    def test_applicant_is_not_internal(self):
        assert _user(User.MembershipLevel.APPLICANT).is_internal is False


@pytest.mark.django_db
class TestIsStationAdmin:
    def test_returns_true_when_assignment_exists(self):
        u = _user()
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        StationAssignment.objects.create(
            user=u,
            station=s,
            role=StationAssignment.Role.ADMIN,
        )
        assert u.is_station_admin(s) is True

    def test_returns_false_when_only_maintainer(self):
        u = _user()
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        StationAssignment.objects.create(
            user=u,
            station=s,
            role=StationAssignment.Role.MAINTAINER,
        )
        assert u.is_station_admin(s) is False

    def test_returns_false_for_other_station(self):
        u = _user()
        s1 = Station.objects.create(name="OE5A", callsign="OE5A")
        s2 = Station.objects.create(name="OE5B", callsign="OE5B")
        StationAssignment.objects.create(
            user=u,
            station=s1,
            role=StationAssignment.Role.ADMIN,
        )
        assert u.is_station_admin(s2) is False


@pytest.mark.django_db
class TestIsStationMaintainer:
    def test_returns_true_when_assignment_exists(self):
        u = _user()
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        StationAssignment.objects.create(
            user=u,
            station=s,
            role=StationAssignment.Role.MAINTAINER,
        )
        assert u.is_station_maintainer(s) is True

    def test_returns_false_when_only_admin(self):
        u = _user()
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        StationAssignment.objects.create(
            user=u,
            station=s,
            role=StationAssignment.Role.ADMIN,
        )
        # is_station_admin only — not maintainer
        assert u.is_station_maintainer(s) is False


@pytest.mark.django_db
class TestIsRegionManager:
    def test_returns_true_when_assignment_exists(self):
        u = _user()
        r = Region.objects.create(name="Tirol", slug="tirol")
        RegionAssignment.objects.create(
            user=u,
            region=r,
            role=RegionAssignment.Role.MANAGER,
        )
        assert u.is_region_manager(r) is True

    def test_returns_false_for_other_region(self):
        u = _user()
        r1 = Region.objects.create(name="Tirol", slug="tirol")
        r2 = Region.objects.create(name="OÖ", slug="ooe")
        RegionAssignment.objects.create(
            user=u,
            region=r1,
            role=RegionAssignment.Role.MANAGER,
        )
        assert u.is_region_manager(r2) is False

    def test_returns_false_for_none_region(self):
        u = _user()
        assert u.is_region_manager(None) is False


@pytest.mark.django_db
class TestCanAdministerStation:
    def test_admin_can_administer_any_station(self):
        u = _user(User.MembershipLevel.ADMIN)
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        assert u.can_administer_station(s) is True

    def test_staff_can_administer_any_station(self):
        u = _user(User.MembershipLevel.STAFF)
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        assert u.can_administer_station(s) is True

    def test_station_admin_can_administer_own_station(self):
        u = _user()
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        StationAssignment.objects.create(
            user=u,
            station=s,
            role=StationAssignment.Role.ADMIN,
        )
        assert u.can_administer_station(s) is True

    def test_station_admin_cannot_administer_other_station(self):
        u = _user()
        s1 = Station.objects.create(name="OE5A", callsign="OE5A")
        s2 = Station.objects.create(name="OE5B", callsign="OE5B")
        StationAssignment.objects.create(
            user=u,
            station=s1,
            role=StationAssignment.Role.ADMIN,
        )
        assert u.can_administer_station(s2) is False

    def test_region_manager_can_administer_stations_in_region(self):
        u = _user()
        r = Region.objects.create(name="Tirol", slug="tirol")
        s = Station.objects.create(name="OE5A", callsign="OE5A", region=r)
        RegionAssignment.objects.create(
            user=u,
            region=r,
            role=RegionAssignment.Role.MANAGER,
        )
        assert u.can_administer_station(s) is True

    def test_region_manager_cannot_administer_stations_in_other_region(self):
        u = _user()
        r1 = Region.objects.create(name="Tirol", slug="tirol")
        r2 = Region.objects.create(name="OÖ", slug="ooe")
        s = Station.objects.create(name="OE5A", callsign="OE5A", region=r2)
        RegionAssignment.objects.create(
            user=u,
            region=r1,
            role=RegionAssignment.Role.MANAGER,
        )
        assert u.can_administer_station(s) is False

    def test_member_without_assignment_cannot_administer(self):
        u = _user(User.MembershipLevel.MEMBER)
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        assert u.can_administer_station(s) is False

    def test_applicant_cannot_administer(self):
        u = _user(User.MembershipLevel.APPLICANT)
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        assert u.can_administer_station(s) is False


@pytest.mark.django_db
class TestCanMaintainStation:
    def test_includes_can_administer(self):
        u = _user(User.MembershipLevel.STAFF)
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        assert u.can_maintain_station(s) is True

    def test_includes_station_maintainer(self):
        u = _user()
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        StationAssignment.objects.create(
            user=u,
            station=s,
            role=StationAssignment.Role.MAINTAINER,
        )
        assert u.can_maintain_station(s) is True

    def test_member_without_assignment_cannot_maintain(self):
        u = _user(User.MembershipLevel.MEMBER)
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        assert u.can_maintain_station(s) is False


@pytest.mark.django_db
class TestCanUseStation:
    def test_applicant_cannot_use(self):
        u = _user(User.MembershipLevel.APPLICANT)
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        assert u.can_use_station(s) is False

    def test_member_can_use(self):
        u = _user(User.MembershipLevel.MEMBER)
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        assert u.can_use_station(s) is True

    def test_staff_can_use(self):
        u = _user(User.MembershipLevel.STAFF)
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        assert u.can_use_station(s) is True

    def test_admin_can_use(self):
        u = _user(User.MembershipLevel.ADMIN)
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        assert u.can_use_station(s) is True
