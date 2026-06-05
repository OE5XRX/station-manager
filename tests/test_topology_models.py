"""Tests for the new topology models: Region, StationAssignment, RegionAssignment."""

import pytest
from django.db import IntegrityError, transaction

from apps.accounts.models import User
from apps.stations.models import Region, Station


@pytest.mark.django_db
class TestRegion:
    def test_str(self):
        r = Region.objects.create(name="Tirol", slug="tirol")
        assert str(r) == "Tirol"

    def test_unique_name(self):
        Region.objects.create(name="Tirol", slug="tirol-1")
        with pytest.raises(IntegrityError):
            Region.objects.create(name="Tirol", slug="tirol-2")

    def test_unique_slug(self):
        Region.objects.create(name="Tirol", slug="tirol")
        with pytest.raises(IntegrityError):
            Region.objects.create(name="Tirol Süd", slug="tirol")

    def test_description_optional(self):
        r = Region.objects.create(name="Salzburg", slug="sbg")
        assert r.description == ""


@pytest.mark.django_db
class TestStationRegionFK:
    def test_station_can_have_null_region(self):
        s = Station.objects.create(name="OE5XTR", callsign="OE5XTR")
        assert s.region is None

    def test_station_region_set_null_on_delete(self):
        r = Region.objects.create(name="Tirol", slug="tirol")
        s = Station.objects.create(name="OE5XTR", callsign="OE5XTR", region=r)
        r.delete()
        s.refresh_from_db()
        assert s.region is None

    def test_region_stations_reverse_relation(self):
        r = Region.objects.create(name="Tirol", slug="tirol")
        Station.objects.create(name="OE5A", callsign="OE5A", region=r)
        Station.objects.create(name="OE5B", callsign="OE5B", region=r)
        assert r.stations.count() == 2


@pytest.mark.django_db
class TestStationAssignment:
    def _member(self):
        u = User.objects.create_user(username="hans", password="x")
        u.membership_level = User.MembershipLevel.MEMBER
        u.save(update_fields=["membership_level"])
        return u

    def test_role_choices(self):
        from apps.stations.models import StationAssignment

        assert StationAssignment.Role.ADMIN == "admin"
        assert StationAssignment.Role.MAINTAINER == "maintainer"

    def test_create_admin_assignment(self):
        from apps.stations.models import StationAssignment

        s = Station.objects.create(name="OE5A", callsign="OE5A")
        a = StationAssignment.objects.create(
            user=self._member(),
            station=s,
            role=StationAssignment.Role.ADMIN,
        )
        assert a.assigned_at is not None

    def test_uniq_user_per_station(self):
        from apps.stations.models import StationAssignment

        s = Station.objects.create(name="OE5A", callsign="OE5A")
        u = self._member()
        StationAssignment.objects.create(
            user=u,
            station=s,
            role=StationAssignment.Role.MAINTAINER,
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            StationAssignment.objects.create(
                user=u,
                station=s,
                role=StationAssignment.Role.ADMIN,
            )

    def test_uniq_admin_per_station(self):
        from apps.stations.models import StationAssignment

        s = Station.objects.create(name="OE5A", callsign="OE5A")
        u1 = self._member()
        u2 = User.objects.create_user(username="franz", password="x")
        u2.membership_level = User.MembershipLevel.MEMBER
        u2.save(update_fields=["membership_level"])

        StationAssignment.objects.create(
            user=u1,
            station=s,
            role=StationAssignment.Role.ADMIN,
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            StationAssignment.objects.create(
                user=u2,
                station=s,
                role=StationAssignment.Role.ADMIN,
            )

    def test_multiple_maintainers_ok(self):
        from apps.stations.models import StationAssignment

        s = Station.objects.create(name="OE5A", callsign="OE5A")
        u1 = self._member()
        u2 = User.objects.create_user(username="franz", password="x")
        u2.membership_level = User.MembershipLevel.MEMBER
        u2.save(update_fields=["membership_level"])

        StationAssignment.objects.create(
            user=u1,
            station=s,
            role=StationAssignment.Role.MAINTAINER,
        )
        StationAssignment.objects.create(
            user=u2,
            station=s,
            role=StationAssignment.Role.MAINTAINER,
        )
        assert s.assignments.count() == 2


@pytest.mark.django_db
class TestRegionAssignment:
    def _member(self):
        u = User.objects.create_user(username="lisa", password="x")
        u.membership_level = User.MembershipLevel.MEMBER
        u.save(update_fields=["membership_level"])
        return u

    def test_role_choices(self):
        from apps.stations.models import RegionAssignment

        assert RegionAssignment.Role.MANAGER == "manager"

    def test_create_manager_assignment(self):
        from apps.stations.models import RegionAssignment

        r = Region.objects.create(name="Tirol", slug="tirol")
        a = RegionAssignment.objects.create(
            user=self._member(),
            region=r,
            role=RegionAssignment.Role.MANAGER,
        )
        assert a.assigned_at is not None

    def test_uniq_user_role_per_region(self):
        from apps.stations.models import RegionAssignment

        r = Region.objects.create(name="Tirol", slug="tirol")
        u = self._member()
        RegionAssignment.objects.create(
            user=u,
            region=r,
            role=RegionAssignment.Role.MANAGER,
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            RegionAssignment.objects.create(
                user=u,
                region=r,
                role=RegionAssignment.Role.MANAGER,
            )

    def test_multiple_managers_per_region_ok(self):
        from apps.stations.models import RegionAssignment

        r = Region.objects.create(name="Tirol", slug="tirol")
        u1 = self._member()
        u2 = User.objects.create_user(username="lisa2", password="x")
        u2.membership_level = User.MembershipLevel.MEMBER
        u2.save(update_fields=["membership_level"])

        RegionAssignment.objects.create(
            user=u1,
            region=r,
            role=RegionAssignment.Role.MANAGER,
        )
        RegionAssignment.objects.create(
            user=u2,
            region=r,
            role=RegionAssignment.Role.MANAGER,
        )
        assert r.assignments.count() == 2
