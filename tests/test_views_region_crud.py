"""Tests for Region CRUD views (admin-only)."""

import pytest
from django.urls import reverse

from apps.accounts.models import AccountAuditLog, User
from apps.stations.models import Region, Station


def _user(level, username):
    u = User.objects.create_user(username=username, password="x", email=f"{username}@x")
    u.membership_level = level
    u.save(update_fields=["membership_level"])
    return u


@pytest.mark.django_db
class TestRegionListView:
    def test_admin_can_view(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        Region.objects.create(name="Tirol", slug="tirol")
        Region.objects.create(name="OOe", slug="ooe")
        client.force_login(admin)
        response = client.get(reverse("stations:region_list"))
        assert response.status_code == 200
        body = response.content.decode()
        assert "Tirol" in body
        assert "OOe" in body

    def test_non_admin_forbidden(self, client):
        staff = _user(User.MembershipLevel.STAFF, "staff")
        client.force_login(staff)
        response = client.get(reverse("stations:region_list"))
        assert response.status_code in (302, 403)


@pytest.mark.django_db
class TestRegionCreateView:
    def test_admin_can_create(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        client.force_login(admin)
        response = client.post(
            reverse("stations:region_create"),
            {
                "name": "Tirol",
                "slug": "tirol",
                "description": "Bezirk West",
            },
        )
        assert response.status_code in (200, 302)
        assert Region.objects.filter(name="Tirol", slug="tirol").exists()
        # Signal should have emitted REGION_CREATED audit
        assert AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.REGION_CREATED
        ).exists()

    def test_duplicate_slug_returns_form_error(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        Region.objects.create(name="Old", slug="tirol")
        client.force_login(admin)
        client.post(
            reverse("stations:region_create"),
            {"name": "New", "slug": "tirol", "description": ""},
        )
        # Form re-renders with errors (200) — Region not created twice
        assert Region.objects.filter(slug="tirol").count() == 1


@pytest.mark.django_db
class TestRegionUpdateView:
    def test_admin_can_update(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        r = Region.objects.create(name="Tirol", slug="tirol")
        client.force_login(admin)
        response = client.post(
            reverse("stations:region_update", args=[r.pk]),
            {
                "name": "Tirol-West",
                "slug": "tirol-west",
                "description": "",
            },
        )
        assert response.status_code in (200, 302)
        r.refresh_from_db()
        assert r.name == "Tirol-West"
        assert r.slug == "tirol-west"
        # REGION_UPDATED signal
        assert AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.REGION_UPDATED
        ).exists()


@pytest.mark.django_db
class TestRegionDeleteView:
    def test_admin_sees_confirmation_page(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        r = Region.objects.create(name="Tirol", slug="tirol")
        Station.objects.create(name="OE5A", callsign="OE5A", region=r)
        Station.objects.create(name="OE5B", callsign="OE5B", region=r)
        client.force_login(admin)
        response = client.get(reverse("stations:region_delete", args=[r.pk]))
        assert response.status_code == 200
        body = response.content.decode()
        # Confirmation shows the station count that will lose region
        assert "2" in body

    def test_admin_can_delete(self, client):
        admin = _user(User.MembershipLevel.ADMIN, "admin")
        r = Region.objects.create(name="Tirol", slug="tirol")
        s = Station.objects.create(name="OE5A", callsign="OE5A", region=r)
        client.force_login(admin)
        response = client.post(reverse("stations:region_delete", args=[r.pk]))
        assert response.status_code in (200, 302)
        assert not Region.objects.filter(pk=r.pk).exists()
        # Station's region FK is now NULL (SET_NULL)
        s.refresh_from_db()
        assert s.region is None
        # REGION_DELETED signal fired
        assert AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.REGION_DELETED
        ).exists()
