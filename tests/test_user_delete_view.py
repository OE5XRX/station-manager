"""UserDeleteView Impact-Anzeige + USER_DELETED-Audit + Self-Block.

Sub-Spec 1c Sektion 7.
"""

import pytest
from django.urls import reverse

from apps.accounts.models import AccountAuditLog, User
from apps.stations.models import Region, RegionAssignment, Station, StationAssignment


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
        email="m@example.org",
        membership_level=User.MembershipLevel.MEMBER,
    )


@pytest.fixture
def region(db):
    return Region.objects.create(name="Innviertel")


@pytest.fixture
def station(db, region):
    return Station.objects.create(name="OE5XRX-Test", callsign="OE5XRX", region=region)


@pytest.mark.django_db
class TestDeleteImpactContext:
    def test_no_assignments_zero_counts(self, client, admin, member):
        client.force_login(admin)
        resp = client.get(reverse("accounts:user_delete", kwargs={"pk": member.pk}))
        ctx = resp.context
        assert ctx["n_station_assignments"] == 0
        assert ctx["n_region_assignments"] == 0
        assert ctx["station_admin_assignments"] == []

    def test_counts_reflect_assignments(self, client, admin, member, region, station):
        RegionAssignment.objects.create(
            user=member, region=region, role=RegionAssignment.Role.MANAGER, assigned_by=admin
        )
        StationAssignment.objects.create(
            user=member,
            station=station,
            role=StationAssignment.Role.MAINTAINER,
            assigned_by=admin,
        )
        client.force_login(admin)
        resp = client.get(reverse("accounts:user_delete", kwargs={"pk": member.pk}))
        ctx = resp.context
        assert ctx["n_station_assignments"] == 1
        assert ctx["n_region_assignments"] == 1

    def test_station_admin_warning_list(self, client, admin, member, station):
        StationAssignment.objects.create(
            user=member, station=station, role=StationAssignment.Role.ADMIN, assigned_by=admin
        )
        client.force_login(admin)
        resp = client.get(reverse("accounts:user_delete", kwargs={"pk": member.pk}))
        ctx = resp.context
        admin_list = ctx["station_admin_assignments"]
        assert len(admin_list) == 1
        assert admin_list[0].station == station


@pytest.mark.django_db
class TestDeleteAuditAndCascade:
    def test_delete_emits_user_deleted_audit(self, client, admin, member):
        client.force_login(admin)
        before = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.USER_DELETED
        ).count()
        client.post(reverse("accounts:user_delete", kwargs={"pk": member.pk}))
        after = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.USER_DELETED
        ).count()
        assert after == before + 1
        # Username appears in message even though target_user gets SET_NULL
        # after cascade.
        entry = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.USER_DELETED
        ).latest("created_at")
        assert "OE5MEM1" in entry.message
        assert "m@example.org" in entry.message
        # actor stays admin (admin still exists)
        assert entry.actor == admin

    def test_self_delete_blocked(self, client, admin):
        client.force_login(admin)
        resp = client.post(reverse("accounts:user_delete", kwargs={"pk": admin.pk}))
        # Redirect, but user still exists
        assert resp.status_code == 302
        assert User.objects.filter(pk=admin.pk).exists()
        # No USER_DELETED audit for self
        assert (
            AccountAuditLog.objects.filter(
                event_type=AccountAuditLog.EventType.USER_DELETED, target_user=admin
            ).count()
            == 0
        )
