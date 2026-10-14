"""Capability write policy on the ``control`` area verbs (spec 2026-10-14 §3.4).

The policy maps a capability to ``control.use`` (default) or ``control.manage``
(calibration: filters, SA818 volume). ``can_write`` is station-independent —
station assignments no longer grant capability writes.
"""

import pytest
from django.contrib.auth.models import AnonymousUser

from apps.accounts.models import User
from apps.control import capability_policy as cp
from apps.stations.models import Region, RegionAssignment, Station, StationAssignment


def test_filter_is_manage_persisted():
    p = cp.policy_for("filter_hpf", "fm_transceiver")
    assert (p.verb, p.persist) == ("manage", True)


def test_unknown_module_type_takes_strictest():
    assert cp.policy_for("volume", None).verb == "manage"


def test_default_is_use():
    assert cp.policy_for("ptt", "fm_transceiver").verb == "use"


def test_invalid_verb_rejected():
    with pytest.raises(ValueError):
        cp.CapabilityPolicy(verb="admin")


def test_can_write(db, member_user, staff_user):
    assert cp.can_write(member_user, "ptt", "fm_transceiver") is True
    assert cp.can_write(member_user, "filter_hpf", None) is False
    assert cp.can_write(staff_user, "filter_hpf", None) is True


def test_unknown_caps_default_use_no_persist():
    p = cp.policy_for("frequency", "fm")
    assert p.verb == "use" and p.persist is False


@pytest.mark.parametrize("cap", ["filter_pre_emphasis", "filter_hpf", "filter_lpf"])
def test_filters_are_manage_and_persisted(cap):
    p = cp.policy_for(cap, "fm")
    assert p.verb == "manage" and p.persist is True


def test_exact_module_type_entry_wins(monkeypatch):
    monkeypatch.setitem(cp.POLICY, ("power", "fm"), cp.CapabilityPolicy("manage"))
    assert cp.policy_for("power", "fm").verb == "manage"
    assert cp.policy_for("power", "hf").verb == "use"


def test_every_policy_verb_is_a_control_verb():
    for p in cp.POLICY.values():
        assert p.verb in ("use", "manage")


@pytest.mark.parametrize("cap", [None, 42, ["filter_hpf"], {"a": 1}])
def test_policy_for_non_str_capability_is_default(cap):
    assert cp.policy_for(cap, "fm") == cp.CapabilityPolicy()
    assert cp.policy_for(cap, ["unhashable"]) == cp.CapabilityPolicy()


def test_unknown_module_type_uses_strictest_entry_for_name(monkeypatch):
    monkeypatch.setitem(cp.POLICY, ("power", "fm"), cp.CapabilityPolicy("manage"))
    monkeypatch.setitem(cp.POLICY, ("power", "hf"), cp.CapabilityPolicy("use", persist=True))
    p = cp.policy_for("power", None)
    assert p.verb == "manage" and p.persist is True
    # A known (str) type still resolves exactly / by name.
    assert cp.policy_for("power", "fm").verb == "manage"
    assert cp.policy_for("power", "vhf").verb == "use"
    # Non-str module type is treated as unknown → strictest.
    assert cp.policy_for("power", ["x"]).verb == "manage"
    # Name-only entries are unchanged.
    assert cp.policy_for("filter_hpf", None).verb == "manage"
    assert cp.policy_for("frequency", None) == cp.CapabilityPolicy()


def test_sa818_volume_is_manage_and_persisted():
    """SA818 AF-out volume sets the RX level into the ADC for every listener and resets to the
    SA818 boot default on a power cycle — calibration, not an operator knob: manage + persisted
    (re-applied by the server after the module comes back)."""
    p = cp.policy_for("volume", "fm_transceiver")
    assert p.verb == "manage" and p.persist is True


def test_volume_policy_is_scoped_to_fm_transceiver():
    """A ``volume`` capability on another known module type keeps the default policy."""
    p = cp.policy_for("volume", "audio_router")
    assert p.verb == "use" and p.persist is False


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
    "who,may_use,may_write_filter",
    [
        ("applicant", False, False),
        ("member", True, False),
        # Old semantics granted station managers nothing more for filters either; the
        # assignment is now irrelevant for capability writes entirely.
        ("region_manager", True, False),
        ("station_admin", True, False),
        ("staff", True, True),
        ("admin", True, True),
    ],
)
def test_can_write_matrix(who, may_use, may_write_filter):
    station = Station.objects.create(name="cp1", status="online")
    user = _user(who, station)
    assert cp.can_write(user, "filter_hpf", "fm") is may_write_filter
    assert cp.can_write(user, "frequency", "fm") is may_use


@pytest.mark.django_db
def test_can_write_fails_closed_anonymous_none_inactive():
    station = Station.objects.create(name="cp2", status="online")
    assert cp.can_write(AnonymousUser(), "frequency", "fm") is False
    assert cp.can_write(None, "frequency", "fm") is False
    user = _user("admin", station)
    user.is_active = False
    assert cp.can_write(user, "frequency", "fm") is False


def test_old_role_helpers_are_gone():
    for name in ("viewer_role", "role_allows", "ROLE_RANK", "ROLE_LABELS"):
        assert not hasattr(cp, name), name
