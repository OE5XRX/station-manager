import pytest

from apps.stations.models import Region, RegionAssignment, StationAssignment
from apps.stations.scoping import accessible_regions, accessible_stations


@pytest.fixture
def region(db):
    return Region.objects.create(name="Alpen")


@pytest.mark.django_db
def test_admin_sees_all_stations(admin_user, station_factory):
    station_factory()
    station_factory()
    assert accessible_stations(admin_user).count() == 2


@pytest.mark.django_db
def test_staff_sees_all_stations(operator_user, station_factory):
    station_factory()
    assert accessible_stations(operator_user).count() == 1


@pytest.mark.django_db
def test_region_manager_sees_region_stations_only(member_user, region, station_factory):
    in_region = station_factory(region=region)
    station_factory()  # outside
    RegionAssignment.objects.create(
        user=member_user, region=region, role="manager", assigned_by=member_user
    )
    result = list(accessible_stations(member_user))
    assert result == [in_region]
    assert list(accessible_regions(member_user)) == [region]


@pytest.mark.django_db
def test_station_assigned_member_sees_assigned_only(member_user, station_factory):
    assigned = station_factory()
    station_factory()  # not assigned
    StationAssignment.objects.create(
        user=member_user, station=assigned, role="maintainer", assigned_by=member_user
    )
    assert list(accessible_stations(member_user)) == [assigned]


@pytest.mark.django_db
def test_applicant_sees_nothing(applicant_user, station_factory):
    station_factory()
    assert accessible_stations(applicant_user).count() == 0
