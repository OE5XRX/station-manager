"""Tests for StationAssignment HTMX endpoints."""

import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.stations.models import Station, StationAssignment


def _user(level, username):
    u = User.objects.create_user(username=username, password="x", email=f"{username}@x")
    u.membership_level = level
    u.save(update_fields=["membership_level"])
    return u


@pytest.mark.django_db
class TestStationAssignmentCreateView:
    def test_admin_can_add_station_admin(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        franz = _user(User.MembershipLevel.MEMBER, "franz")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        client.force_login(admin)
        response = client.post(
            reverse("accounts:station_assignment_create", args=[franz.pk]),
            {"station": s.pk, "role": "admin"},
        )
        assert response.status_code == 200
        assert StationAssignment.objects.filter(user=franz, station=s, role="admin").exists()

    def test_admin_can_add_station_maintainer(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        hans = _user(User.MembershipLevel.MEMBER, "hans")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        client.force_login(admin)
        response = client.post(
            reverse("accounts:station_assignment_create", args=[hans.pk]),
            {"station": s.pk, "role": "maintainer"},
        )
        assert response.status_code == 200
        assert StationAssignment.objects.filter(user=hans, station=s, role="maintainer").exists()

    def test_applicant_target_returns_400(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        a = _user(User.MembershipLevel.APPLICANT, "newbie")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        client.force_login(admin)
        response = client.post(
            reverse("accounts:station_assignment_create", args=[a.pk]),
            {"station": s.pk, "role": "admin"},
        )
        assert response.status_code == 400

    def test_invalid_role_returns_400(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        target = _user(User.MembershipLevel.MEMBER, "tgt")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        client.force_login(admin)
        response = client.post(
            reverse("accounts:station_assignment_create", args=[target.pk]),
            {"station": s.pk, "role": "warlord"},
        )
        assert response.status_code == 400

    def test_admin_conflict_without_takeover_returns_409(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        franz = _user(User.MembershipLevel.MEMBER, "franz")
        otto = _user(User.MembershipLevel.MEMBER, "otto")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        StationAssignment.objects.create(user=franz, station=s, role=StationAssignment.Role.ADMIN)
        client.force_login(admin)
        response = client.post(
            reverse("accounts:station_assignment_create", args=[otto.pk]),
            {"station": s.pk, "role": "admin"},
        )
        assert response.status_code == 409
        # franz still has the admin role
        assert StationAssignment.objects.filter(user=franz, station=s, role="admin").exists()

    def test_admin_takeover_replaces_existing(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        franz = _user(User.MembershipLevel.MEMBER, "franz")
        otto = _user(User.MembershipLevel.MEMBER, "otto")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        StationAssignment.objects.create(user=franz, station=s, role=StationAssignment.Role.ADMIN)
        client.force_login(admin)
        response = client.post(
            reverse("accounts:station_assignment_create", args=[otto.pk]),
            {"station": s.pk, "role": "admin", "takeover": "1"},
        )
        assert response.status_code == 200
        # franz lost the role, otto has it
        assert not StationAssignment.objects.filter(user=franz, station=s, role="admin").exists()
        assert StationAssignment.objects.filter(user=otto, station=s, role="admin").exists()

    def test_non_admin_forbidden(self, client):
        staff = _user(User.MembershipLevel.STAFF, "staff")
        target = _user(User.MembershipLevel.MEMBER, "tgt")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        client.force_login(staff)
        response = client.post(
            reverse("accounts:station_assignment_create", args=[target.pk]),
            {"station": s.pk, "role": "admin"},
        )
        assert response.status_code in (302, 403)


@pytest.mark.django_db
class TestStationAssignmentRevokeView:
    def test_admin_can_revoke(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        franz = _user(User.MembershipLevel.MEMBER, "franz")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        a = StationAssignment.objects.create(
            user=franz, station=s, role=StationAssignment.Role.MAINTAINER
        )
        client.force_login(admin)
        response = client.post(reverse("accounts:station_assignment_revoke", args=[a.pk]))
        assert response.status_code == 200
        assert not StationAssignment.objects.filter(pk=a.pk).exists()
