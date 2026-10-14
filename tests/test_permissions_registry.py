import pytest
from django.core.exceptions import ImproperlyConfigured

from apps.accounts.models import User
from apps.accounts.permissions import (
    AREAS,
    Area,
    Level,
    _validate_areas,
    can,
    can_override_station,
    level_of,
    matrix,
    required_level,
    users_with,
)

SPEC_MATRIX = {
    # area: (view, use, manage) as declared in spec §1.3 (None = "–")
    "dashboard": ("A", None, None),
    "profile": (None, "A", None),
    "stations": ("A", None, "S"),
    "control": ("A", "M", "S"),
    "terminal": (None, "Ad", None),
    "topology": ("A", None, "Ad"),
    "deployments": ("A", None, "S"),
    "rollouts": ("A", None, "Ad"),
    "images": ("A", None, "Ad"),
    "provisioning": (None, None, "Ad"),
    "alerts": ("A", None, "S"),
    "alert_rules": ("A", None, "Ad"),
    "modules": ("A", None, "S"),
    "users": ("M", None, "Ad"),
    "api_tokens": (None, "M", None),
    "audit": ("Ad", None, None),
    "sso": (None, None, "Ad"),
}
_L = {"A": Level.APPLICANT, "M": Level.MEMBER, "S": Level.STAFF, "Ad": Level.ADMIN, None: None}


def test_registry_matches_spec_matrix_exactly():
    assert set(AREAS) == set(SPEC_MATRIX)
    for name, (v, u, m) in SPEC_MATRIX.items():
        area = AREAS[name]
        assert (area.view, area.use, area.manage) == (_L[v], _L[u], _L[m]), name


def test_lower_verb_inherits_next_higher_defined():
    assert required_level("terminal", "view") == Level.ADMIN
    assert required_level("provisioning", "view") == Level.ADMIN
    assert required_level("provisioning", "use") == Level.ADMIN
    assert required_level("profile", "view") == Level.APPLICANT
    assert required_level("api_tokens", "view") == Level.MEMBER
    assert required_level("stations", "use") == Level.STAFF


def test_higher_undefined_verb_is_nobody():
    assert required_level("audit", "manage") is None
    assert required_level("audit", "use") is None
    assert required_level("profile", "manage") is None
    assert required_level("dashboard", "manage") is None


def test_unknown_area_or_verb_raises_keyerror():
    with pytest.raises(KeyError):
        required_level("image", "view")
    with pytest.raises(KeyError):
        required_level("images", "edit")


def test_monotonic_violation_rejected():
    with pytest.raises(ImproperlyConfigured):
        _validate_areas({"x": Area("x", view=Level.ADMIN, manage=Level.MEMBER)})


def test_area_without_any_verb_rejected():
    with pytest.raises(ImproperlyConfigured):
        _validate_areas({"x": Area("x")})


def test_verb_label_for_undefined_verb_rejected():
    with pytest.raises(ImproperlyConfigured):
        _validate_areas({"x": Area("x", view=Level.MEMBER, verb_labels={"manage": "boom"})})


@pytest.mark.parametrize(
    "slug,expected",
    [
        ("applicant", Level.APPLICANT),
        ("member", Level.MEMBER),
        ("staff", Level.STAFF),
        ("admin", Level.ADMIN),
    ],
)
def test_level_of_active_user(db, slug, expected):
    u = User.objects.create_user(username=f"u-{slug}", password="x", membership_level=slug)
    assert level_of(u) == expected


def test_level_of_anonymous_inactive_deleted(db):
    from django.contrib.auth.models import AnonymousUser
    from django.utils import timezone

    assert level_of(None) is None
    assert level_of(AnonymousUser()) is None
    u = User.objects.create_user(
        username="ina", password="x", membership_level="admin", is_active=False
    )
    assert level_of(u) is None
    d = User.objects.create_user(username="del", password="x", membership_level="admin")
    d.deleted_at = timezone.now()
    assert level_of(d) is None


def test_can_and_user_can(db):
    m = User.objects.create_user(username="m", password="x", membership_level="member")
    assert can(m, "control", "use") is True
    assert can(m, "control", "manage") is False
    assert m.can("users", "view") is True
    assert m.can("audit", "view") is False


def test_can_anonymous_is_false():
    from django.contrib.auth.models import AnonymousUser

    assert can(AnonymousUser(), "dashboard", "view") is False


def test_can_unknown_area_raises(db):
    m = User.objects.create_user(username="m2", password="x", membership_level="admin")
    with pytest.raises(KeyError):
        can(m, "imagez", "view")


def test_users_with_excludes_inactive_and_deleted(db):
    from django.utils import timezone

    a = User.objects.create_user(username="a", password="x", membership_level="admin")
    s = User.objects.create_user(username="s", password="x", membership_level="staff")
    User.objects.create_user(username="m", password="x", membership_level="member")
    User.objects.create_user(username="i", password="x", membership_level="admin", is_active=False)
    d = User.objects.create_user(username="d", password="x", membership_level="admin")
    d.deleted_at = timezone.now()
    d.save(update_fields=["deleted_at"])
    assert set(users_with("alerts", "manage")) == {a, s}
    assert set(users_with("audit", "manage")) == set()


def test_can_override_station(db, station):
    from apps.stations.models import Region, RegionAssignment, StationAssignment

    staff = User.objects.create_user(username="st", password="x", membership_level="staff")
    member = User.objects.create_user(username="mm", password="x", membership_level="member")
    sadm = User.objects.create_user(username="sa", password="x", membership_level="member")
    rmgr = User.objects.create_user(username="rm", password="x", membership_level="member")
    region = Region.objects.create(name="R1", slug="r1")
    station.region = region
    station.save(update_fields=["region"])
    StationAssignment.objects.create(user=sadm, station=station, role="admin")
    RegionAssignment.objects.create(user=rmgr, region=region, role="manager")
    assert can_override_station(staff, station) is True
    assert can_override_station(sadm, station) is True
    assert can_override_station(rmgr, station) is True
    assert can_override_station(member, station) is False


def test_matrix_rows_resolved():
    rows = {r.area: r for r in matrix()}
    assert rows["terminal"].view == Level.ADMIN
    assert rows["audit"].manage is None
    assert list(rows) == list(AREAS)
