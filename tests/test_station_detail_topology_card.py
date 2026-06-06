"""Tests for the Station-Detail topology card rendering."""

import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.stations.models import Region, Station, StationAssignment


def _user(level, username):
    u = User.objects.create_user(username=username, password="x", email=f"{username}@x")
    u.membership_level = level
    u.save(update_fields=["membership_level"])
    return u


@pytest.mark.django_db
class TestStationDetailTopologyCard:
    def test_admin_sees_topology_card(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        Region.objects.create(name="Tirol", slug="tirol")
        client.force_login(admin)
        response = client.get(reverse("stations:station_detail", args=[s.pk]))
        body = response.content.decode()
        assert response.status_code == 200
        # Card header
        assert "Region" in body
        # Region option rendered in the picker
        assert "Tirol" in body
        # The set-region URL is rendered (admin-only marker)
        assert reverse("stations:station_set_region", args=[s.pk]) in body

    def test_member_does_not_see_topology_card(self, client):
        member = _user(User.MembershipLevel.MEMBER, "member")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        client.force_login(member)
        response = client.get(reverse("stations:station_detail", args=[s.pk]))
        body = response.content.decode()
        # The set-region form URL is admin-only — must not appear for member
        assert reverse("stations:station_set_region", args=[s.pk]) not in body

    def test_card_lists_existing_admin_and_maintainers(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        franz = _user(User.MembershipLevel.MEMBER, "franz")
        hans = _user(User.MembershipLevel.MEMBER, "hans")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        StationAssignment.objects.create(user=franz, station=s, role=StationAssignment.Role.ADMIN)
        StationAssignment.objects.create(
            user=hans, station=s, role=StationAssignment.Role.MAINTAINER
        )
        client.force_login(admin)
        response = client.get(reverse("stations:station_detail", args=[s.pk]))
        body = response.content.decode()
        assert "franz" in body
        assert "hans" in body
