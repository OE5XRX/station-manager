"""Card templates accept a readonly=True flag that hides Add/Revoke
forms. Tests render the templates standalone and assert HTML markers.
"""

import pytest
from django.template.loader import render_to_string

from apps.accounts.models import User
from apps.stations.models import Region, RegionAssignment, Station


@pytest.fixture
def admin(db):
    return User.objects.create_user(
        username="OE5ADMIN",
        password="x",
        membership_level=User.MembershipLevel.ADMIN,
    )


@pytest.fixture
def member(db):
    return User.objects.create_user(
        username="OE5MEM1",
        password="x",
        membership_level=User.MembershipLevel.MEMBER,
    )


@pytest.fixture
def region(db):
    return Region.objects.create(name="Innviertel")


@pytest.fixture
def station(db, region):
    return Station.objects.create(name="OE5XRX-Test", callsign="OE5XRX", region=region)


@pytest.mark.django_db
class TestMembershipCardReadonly:
    def test_admin_mode_has_apply_button(self, member, admin):
        html = render_to_string(
            "accounts/_membership_card.html",
            {
                "object": member,
                "membership_level_choices": User.MembershipLevel.choices,
                "readonly": False,
                "request": _request(admin),
            },
        )
        assert "Apply" in html or "submit" in html

    def test_readonly_mode_has_no_form(self, member, admin):
        html = render_to_string(
            "accounts/_membership_card.html",
            {
                "object": member,
                "membership_level_choices": User.MembershipLevel.choices,
                "readonly": True,
                "request": _request(admin),
            },
        )
        # No POST form, no Apply button
        assert "<form" not in html
        assert "Apply" not in html


@pytest.mark.django_db
class TestRegionAssignmentsCardReadonly:
    def test_admin_mode_renders_add_form(self, member, region, admin):
        html = render_to_string(
            "accounts/_region_assignments_card.html",
            {
                "object": member,
                "existing_region_assignments": [],
                "available_regions": Region.objects.all(),
                "readonly": False,
                "request": _request(admin),
            },
        )
        assert "Add Region-Manager assignment" in html or "<form" in html

    def test_readonly_mode_omits_add_form(self, member, region, admin):
        html = render_to_string(
            "accounts/_region_assignments_card.html",
            {
                "object": member,
                "existing_region_assignments": [],
                "available_regions": Region.objects.all(),
                "readonly": True,
                "request": _request(admin),
            },
        )
        assert "<form" not in html

    def test_readonly_mode_keeps_existing_pills(self, member, region, admin):
        RegionAssignment.objects.create(
            user=member,
            region=region,
            role=RegionAssignment.Role.MANAGER,
            assigned_by=admin,
        )
        existing = list(member.region_assignments.select_related("region"))
        html = render_to_string(
            "accounts/_region_assignments_card.html",
            {
                "object": member,
                "existing_region_assignments": existing,
                "available_regions": Region.objects.none(),
                "readonly": True,
                "request": _request(admin),
            },
        )
        # Pill text present, but no revoke button
        assert "Innviertel" in html
        assert "✕" not in html


@pytest.mark.django_db
class TestStationAssignmentsCardReadonly:
    def test_admin_mode_renders_add_form(self, member, station, admin):
        html = render_to_string(
            "accounts/_station_assignments_card.html",
            {
                "object": member,
                "existing_station_assignments": [],
                "all_stations": Station.objects.all(),
                "readonly": False,
                "request": _request(admin),
            },
        )
        assert "Add Station assignment" in html or "<form" in html

    def test_readonly_mode_omits_forms(self, member, station, admin):
        html = render_to_string(
            "accounts/_station_assignments_card.html",
            {
                "object": member,
                "existing_station_assignments": [],
                "all_stations": Station.objects.all(),
                "readonly": True,
                "request": _request(admin),
            },
        )
        assert "<form" not in html


def _request(user):
    """Build a fake request object exposing the bits the templates read."""
    from django.test import RequestFactory

    req = RequestFactory().get("/")
    req.user = user
    req.csp_nonce = ""
    return req
