"""HTMX mutation endpoints must refuse soft-deleted users.

The detail-page UI marks membership/region/station cards as
``readonly=True`` for soft-deleted users, but the underlying POST
endpoints used to look up the User by pk without a ``deleted_at__isnull=True``
guard — so a hand-rolled POST could still mutate a deleted user's
membership level or reintroduce topology assignments.

Sub-Spec 2b §3.1: lifecycle invariants enforced both on the read side
and on every mutation endpoint.
"""

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User


@pytest.fixture
def admin(db):
    return User.objects.create_user(
        username="OE5ADMIN",
        password="x",
        membership_level=User.MembershipLevel.ADMIN,
    )


@pytest.fixture
def deleted_member(db):
    u = User.objects.create_user(
        username="OE5DEAD",
        email="dead@example.org",
        password="x",
        membership_level=User.MembershipLevel.MEMBER,
    )
    u.deleted_at = timezone.now()
    u.is_active = False
    u.save()
    return u


@pytest.mark.django_db
class TestMutationEndpointsRejectSoftDeleted:
    def test_membership_set_returns_404_on_soft_deleted_target(
        self, client, admin, deleted_member
    ):
        client.force_login(admin)
        resp = client.post(
            reverse("accounts:membership_set", kwargs={"pk": deleted_member.pk}),
            {"level": User.MembershipLevel.STAFF.value},
        )
        assert resp.status_code == 404
        deleted_member.refresh_from_db()
        # Membership untouched
        assert deleted_member.membership_level == User.MembershipLevel.MEMBER

    def test_region_assignment_create_returns_404_on_soft_deleted_target(
        self, client, admin, deleted_member
    ):
        from apps.stations.models import Region

        region = Region.objects.create(name="X")
        client.force_login(admin)
        resp = client.post(
            reverse(
                "accounts:region_assignment_create",
                kwargs={"user_pk": deleted_member.pk},
            ),
            {"region": region.pk},
        )
        assert resp.status_code == 404
        assert not deleted_member.region_assignments.exists()

    def test_station_assignment_create_returns_404_on_soft_deleted_target(
        self, client, admin, deleted_member
    ):
        from apps.stations.models import Region, Station

        region = Region.objects.create(name="X")
        station = Station.objects.create(name="S", callsign="OE5S", region=region)
        client.force_login(admin)
        resp = client.post(
            reverse(
                "accounts:station_assignment_create",
                kwargs={"user_pk": deleted_member.pk},
            ),
            {"station": station.pk, "role": "admin"},
        )
        assert resp.status_code == 404
        assert not deleted_member.station_assignments.exists()
