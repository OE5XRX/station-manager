import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

from apps.sso.oidc_claims import _build_groups, add_claims

User = get_user_model()


@pytest.mark.django_db
def test_add_claims_includes_username_email_name_groups():
    admin_group, _ = Group.objects.get_or_create(name="admin")
    techniker_group, _ = Group.objects.get_or_create(name="techniker")
    user = User.objects.create_user(
        username="peterb",
        password="x",
        email="peter@oe5xrx.org",
        first_name="Peter",
        last_name="Buchegger",
    )
    user.language = "de"
    user.save()
    user.groups.add(admin_group, techniker_group)

    claims = add_claims({}, user, request=None)

    assert claims["preferred_username"] == "peterb"
    assert claims["email"] == "peter@oe5xrx.org"
    assert claims["email_verified"] is True
    assert claims["name"] == "Peter Buchegger"
    assert claims["locale"] == "de"
    # groups are now synthesized: membership_level + "tag:<name>" for each
    # Django auth.Group. Default membership_level is "applicant" since this
    # user was created via create_user() without an explicit membership.
    assert set(claims["groups"]) == {"applicant", "tag:admin", "tag:techniker"}


@pytest.mark.django_db
def test_add_claims_falls_back_to_username_when_no_full_name():
    user = User.objects.create_user(username="anon", password="x", email="a@x.test")
    claims = add_claims({}, user, request=None)
    assert claims["name"] == "anon"


@pytest.mark.django_db
def test_add_claims_groups_is_always_a_list_even_if_empty():
    """RPs (InvenTree, Grafana) expect groups as a list; missing/scalar breaks them.

    Post-Task-2.2 every user has *at least* the membership_level group,
    so the minimum is ``["applicant"]`` for a fresh create_user().
    """
    user = User.objects.create_user(username="loner", password="x", email="l@x.test")
    claims = add_claims({}, user, request=None)
    assert claims["groups"] == ["applicant"]


@pytest.mark.django_db
def test_add_claims_email_verified_false_when_no_email():
    """If a user has no email (e.g. agent-bot account), email_verified must be False
    so RPs don't accidentally trust an empty string as a verified identifier."""
    user = User.objects.create_user(username="bot", password="x")
    claims = add_claims({}, user, request=None)
    assert claims["email"] == ""
    assert claims["email_verified"] is False


@pytest.mark.django_db
def test_add_claims_locale_defaults_to_en_when_user_has_no_language_attr():
    """Defense against a future User-model change that drops language."""
    user = User.objects.create_user(username="x", password="x", email="x@x.test")
    # User.language is "en" by default in this project
    claims = add_claims({}, user, request=None)
    assert claims["locale"] == "en"


@pytest.mark.django_db
def test_add_claims_preserves_existing_claim_keys():
    """If DOT pre-populates standard claims (sub, iss, exp...), we must not clobber them."""
    user = User.objects.create_user(username="x", password="x", email="x@x.test")
    existing = {"sub": "42", "iss": "https://example.org/sso", "exp": 999}
    claims = add_claims(existing, user, request=None)
    assert claims["sub"] == "42"
    assert claims["iss"] == "https://example.org/sso"
    assert claims["exp"] == 999
    # ...AND has our additions:
    assert claims["preferred_username"] == "x"


# ---------------------------------------------------------------------------
# Group synthesis tests (Task 2.2: _build_groups)
# ---------------------------------------------------------------------------

from apps.stations.models import Region, RegionAssignment, Station, StationAssignment  # noqa: E402


@pytest.fixture
def member(db):
    u = User.objects.create_user(username="peter", password="x", email="p@ex.org")
    u.membership_level = User.MembershipLevel.MEMBER
    u.save(update_fields=["membership_level"])
    User._invalidate_role_cache(u)
    return u


@pytest.fixture
def applicant(db):
    u = User.objects.create_user(username="anna", password="x")
    u.membership_level = User.MembershipLevel.APPLICANT
    u.save(update_fields=["membership_level"])
    User._invalidate_role_cache(u)
    return u


def test_build_groups_applicant_only_membership(db, applicant):
    assert _build_groups(applicant) == ["applicant"]


def test_build_groups_member_with_no_assignments(db, member):
    assert _build_groups(member) == ["member"]


def test_build_groups_includes_station_assignment(db, member):
    # Station has no slug field (verified in apps/stations/models.py), so the
    # implementation uses station.pk for the group string.
    s = Station.objects.create(name="OE5XRX-1", callsign="OE5XRX")
    StationAssignment.objects.create(user=member, station=s, role="admin")
    groups = _build_groups(member)
    assert "member" in groups
    assert f"station:{s.pk}:admin" in groups


def test_build_groups_includes_region_assignment(db, member):
    r = Region.objects.create(name="Wien", slug="wien")
    RegionAssignment.objects.create(user=member, region=r, role="manager")
    groups = _build_groups(member)
    assert "region:wien:manager" in groups


def test_build_groups_includes_tag_prefix_for_django_groups(db, member):
    g1 = Group.objects.create(name="kontakt-team")
    g2 = Group.objects.create(name="buehne-techniker")
    member.groups.add(g1, g2)
    groups = _build_groups(member)
    assert "tag:kontakt-team" in groups
    assert "tag:buehne-techniker" in groups


def test_build_groups_is_sorted_and_deduplicated(db, member):
    g = Group.objects.create(name="kontakt-team")
    member.groups.add(g)
    groups = _build_groups(member)
    assert groups == sorted(set(groups))


def test_add_claims_uses_synthesized_groups(db, member):
    g = Group.objects.create(name="kontakt-team")
    member.groups.add(g)
    claims = add_claims({}, member, request=None)
    assert "member" in claims["groups"]
    assert "tag:kontakt-team" in claims["groups"]
