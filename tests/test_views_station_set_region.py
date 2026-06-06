"""Tests for StationSetRegionView (admin-only region setter)."""

import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.stations.models import Region, Station, StationAuditLog


def _user(level, username):
    u = User.objects.create_user(username=username, password="x", email=f"{username}@x")
    u.membership_level = level
    u.save(update_fields=["membership_level"])
    return u


@pytest.mark.django_db
class TestStationSetRegionView:
    def test_admin_can_set_region(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        r = Region.objects.create(name="Tirol", slug="tirol")
        client.force_login(admin)
        response = client.post(
            reverse("stations:station_set_region", args=[s.pk]),
            {"region": r.pk},
        )
        assert response.status_code == 200
        s.refresh_from_db()
        assert s.region == r
        # Signal should have emitted the audit entry
        assert StationAuditLog.objects.filter(
            event_type=StationAuditLog.EventType.STATION_REGION_CHANGED,
            station=s,
        ).exists()

    def test_admin_can_clear_region(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        r = Region.objects.create(name="Tirol", slug="tirol")
        s = Station.objects.create(name="OE5A", callsign="OE5A", region=r)
        client.force_login(admin)
        response = client.post(
            reverse("stations:station_set_region", args=[s.pk]),
            {"region": ""},
        )
        assert response.status_code == 200
        s.refresh_from_db()
        assert s.region is None

    def test_non_admin_forbidden(self, client):
        staff = _user(User.MembershipLevel.STAFF, "staff")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        r = Region.objects.create(name="Tirol", slug="tirol")
        client.force_login(staff)
        response = client.post(
            reverse("stations:station_set_region", args=[s.pk]),
            {"region": r.pk},
        )
        assert response.status_code in (302, 403)
        s.refresh_from_db()
        assert s.region is None

    def test_invalid_region_returns_404(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        client.force_login(admin)
        response = client.post(
            reverse("stations:station_set_region", args=[s.pk]),
            {"region": "99999"},
        )
        assert response.status_code == 404

    def test_missing_region_field_returns_400(self, client):
        # Omitting the field entirely must be a 400, not a silent
        # clear. Explicit empty string (region="") is the documented
        # way to clear — that's covered by test_admin_can_clear_region.
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        r = Region.objects.create(name="Tirol", slug="tirol")
        s = Station.objects.create(name="OE5A", callsign="OE5A", region=r)
        client.force_login(admin)
        response = client.post(
            reverse("stations:station_set_region", args=[s.pk]),
            {},  # no region key
        )
        assert response.status_code == 400
        s.refresh_from_db()
        # Region must NOT have been silently cleared.
        assert s.region == r

    def test_malformed_region_returns_404(self, client):
        # Non-integer region values must 404, not 500. Without the
        # explicit int() guard in the view this raised ValueError
        # during ORM PK coercion.
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        client.force_login(admin)
        response = client.post(
            reverse("stations:station_set_region", args=[s.pk]),
            {"region": "abc"},
        )
        assert response.status_code == 404

    def test_no_change_does_not_emit(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        r = Region.objects.create(name="Tirol", slug="tirol")
        s = Station.objects.create(name="OE5A", callsign="OE5A", region=r)
        client.force_login(admin)
        response = client.post(
            reverse("stations:station_set_region", args=[s.pk]),
            {"region": r.pk},
        )
        assert response.status_code == 200
        # No signal fired since region didn't change
        assert not StationAuditLog.objects.filter(
            event_type=StationAuditLog.EventType.STATION_REGION_CHANGED,
            station=s,
        ).exists()
