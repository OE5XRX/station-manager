"""Reusable role users + in/out-of-scope topology for API read tests."""

import pytest
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.api.models import PersonalAccessToken
from apps.stations.models import Region, RegionAssignment, Station, StationAssignment


def _user(username, level):
    return User.objects.create_user(username=username, password="x", membership_level=level)


@pytest.fixture
def topology(db):
    """Two regions/stations: 'in' (what the scoped user may see) and 'out'."""
    region_in = Region.objects.create(name="In", slug="in")
    region_out = Region.objects.create(name="Out", slug="out")
    station_in = Station.objects.create(name="S-in", callsign="OE1AAA", region=region_in)
    station_out = Station.objects.create(name="S-out", callsign="OE1BBB", region=region_out)

    admin = _user("admin", User.MembershipLevel.ADMIN)
    staff = _user("staff", User.MembershipLevel.STAFF)
    region_mgr = _user("rmgr", User.MembershipLevel.MEMBER)
    RegionAssignment.objects.create(user=region_mgr, region=region_in, role="manager")
    station_user = _user("suser", User.MembershipLevel.MEMBER)
    StationAssignment.objects.create(user=station_user, station=station_in, role="maintainer")
    applicant = _user("appl", User.MembershipLevel.APPLICANT)

    return {
        "region_in": region_in,
        "region_out": region_out,
        "station_in": station_in,
        "station_out": station_out,
        "admin": admin,
        "staff": staff,
        "region_mgr": region_mgr,
        "station_user": station_user,
        "applicant": applicant,
    }


def bearer(user):
    """Return an APIClient authenticated as `user` via a personal access token."""
    token, raw = PersonalAccessToken.issue(user=user, name="test")
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
    return client


def anon_client():
    return APIClient()
