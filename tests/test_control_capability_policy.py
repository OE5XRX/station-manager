import pytest
from django.contrib.auth.models import AnonymousUser

from apps.accounts.models import User
from apps.control import capability_policy as cp
from apps.stations.models import Region, RegionAssignment, Station, StationAssignment


def test_unknown_caps_default_operator_no_persist():
    p = cp.policy_for("frequency", "fm")
    assert p.write_role == "operator" and p.persist is False


@pytest.mark.parametrize("cap", ["filter_pre_emphasis", "filter_hpf", "filter_lpf"])
def test_filters_are_staff_and_persisted(cap):
    p = cp.policy_for(cap, "fm")
    assert p.write_role == "staff" and p.persist is True


def test_exact_module_type_entry_wins(monkeypatch):
    monkeypatch.setitem(cp.POLICY, ("power", "fm"), cp.CapabilityPolicy("admin"))
    assert cp.policy_for("power", "fm").write_role == "admin"
    assert cp.policy_for("power", "hf").write_role == "operator"


def test_every_policy_role_is_ranked():
    for p in cp.POLICY.values():
        assert p.write_role in cp.ROLE_RANK


def _user(kind, station):
    level = {
        "applicant": User.MembershipLevel.APPLICANT,
        "staff": User.MembershipLevel.STAFF,
        "admin": User.MembershipLevel.ADMIN,
    }.get(kind, User.MembershipLevel.MEMBER)
    user = User.objects.create(username=f"u-{kind}", membership_level=level)
    if kind == "region_manager":
        region = Region.objects.create(name="R", slug="r")
        station.region = region
        station.save()
        RegionAssignment.objects.create(user=user, region=region, role="manager")
    elif kind == "station_admin":
        StationAssignment.objects.create(user=user, station=station, role="admin")
    return user


@pytest.mark.django_db
@pytest.mark.parametrize(
    "who,expected_role,may_write_filter",
    [
        ("applicant", None, False),
        ("member", "operator", False),
        ("region_manager", "station_manager", False),
        ("station_admin", "station_manager", False),
        ("staff", "staff", True),
        ("admin", "admin", True),
    ],
)
def test_viewer_role_matrix(who, expected_role, may_write_filter):
    station = Station.objects.create(name="cp1", status="online")
    user = _user(who, station)
    assert cp.viewer_role(user, station) == expected_role
    assert cp.can_write(user, station, "filter_hpf", "fm") is may_write_filter
    assert cp.can_write(user, station, "frequency", "fm") is (expected_role is not None)


@pytest.mark.django_db
def test_viewer_role_fail_closed_anonymous_none_inactive():
    station = Station.objects.create(name="cp2", status="online")
    assert cp.viewer_role(AnonymousUser(), station) is None
    assert cp.viewer_role(None, station) is None
    user = _user("admin", station)
    user.is_active = False
    assert cp.viewer_role(user, station) is None
    assert cp.can_write(user, station, "frequency", "fm") is False


@pytest.mark.django_db
def test_applicant_never_outranks_can_use_station(monkeypatch):
    station = Station.objects.create(name="cp3", status="online")
    user = _user("applicant", station)
    monkeypatch.setattr(User, "can_administer_station", lambda self, s: True)
    assert cp.viewer_role(user, station) is None
