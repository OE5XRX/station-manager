"""UserSoftDeleteView — confirm-GET + POST mit Topology auto-revoke,
SSO-revoke, Token-invalidate.

Sub-Spec 2b §4.
"""

import pytest
from django.urls import reverse

from apps.accounts.models import AccountAuditLog, AccountToken, User
from apps.accounts.tokens import issue_token


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
        email="m@example.org",
        password="x",
        membership_level=User.MembershipLevel.MEMBER,
    )


@pytest.fixture
def region(db):
    from apps.stations.models import Region

    return Region.objects.create(name="Innviertel")


@pytest.fixture
def station(db, region):
    from apps.stations.models import Station

    return Station.objects.create(name="OE5XRX-Test", callsign="OE5XRX", region=region)


@pytest.mark.django_db
class TestSoftDeleteConfirmGET:
    def test_get_shows_counts(self, client, admin, member, region, station):
        from apps.stations.models import RegionAssignment, StationAssignment

        RegionAssignment.objects.create(
            user=member,
            region=region,
            role=RegionAssignment.Role.MANAGER,
            assigned_by=admin,
        )
        StationAssignment.objects.create(
            user=member,
            station=station,
            role=StationAssignment.Role.MAINTAINER,
            assigned_by=admin,
        )

        client.force_login(admin)
        resp = client.get(reverse("accounts:user_soft_delete", kwargs={"pk": member.pk}))
        assert resp.status_code == 200
        assert resp.context["n_station_assignments"] == 1
        assert resp.context["n_region_assignments"] == 1

    def test_get_shows_station_admin_warning_list(self, client, admin, member, station):
        from apps.stations.models import StationAssignment

        StationAssignment.objects.create(
            user=member,
            station=station,
            role=StationAssignment.Role.ADMIN,
            assigned_by=admin,
        )
        client.force_login(admin)
        resp = client.get(reverse("accounts:user_soft_delete", kwargs={"pk": member.pk}))
        assert len(resp.context["station_admin_assignments"]) == 1

    def test_active_user_returns_200(self, client, admin, member):
        client.force_login(admin)
        resp = client.get(reverse("accounts:user_soft_delete", kwargs={"pk": member.pk}))
        assert resp.status_code == 200

    def test_soft_deleted_user_returns_404(self, client, admin, member):
        from django.utils import timezone

        member.deleted_at = timezone.now()
        member.is_active = False
        member.save()

        client.force_login(admin)
        resp = client.get(reverse("accounts:user_soft_delete", kwargs={"pk": member.pk}))
        assert resp.status_code == 404


@pytest.mark.django_db
class TestSoftDeletePOST:
    def test_post_sets_deleted_at_and_deleted_by_and_is_active_false(
        self,
        client,
        admin,
        member,
    ):
        client.force_login(admin)
        client.post(reverse("accounts:user_soft_delete", kwargs={"pk": member.pk}))
        member.refresh_from_db()
        assert member.deleted_at is not None
        assert member.deleted_by == admin
        assert member.is_active is False

    def test_self_soft_delete_blocked(self, client, admin):
        client.force_login(admin)
        resp = client.post(reverse("accounts:user_soft_delete", kwargs={"pk": admin.pk}))
        assert resp.status_code == 302
        admin.refresh_from_db()
        assert admin.deleted_at is None

    def test_topology_auto_revoked_with_per_assignment_audit(
        self,
        client,
        admin,
        member,
        region,
        station,
    ):
        from apps.stations.models import RegionAssignment, StationAssignment

        RegionAssignment.objects.create(
            user=member,
            region=region,
            role=RegionAssignment.Role.MANAGER,
            assigned_by=admin,
        )
        StationAssignment.objects.create(
            user=member,
            station=station,
            role=StationAssignment.Role.ADMIN,
            assigned_by=admin,
        )

        client.force_login(admin)
        client.post(reverse("accounts:user_soft_delete", kwargs={"pk": member.pk}))

        assert not member.station_assignments.exists()
        assert not member.region_assignments.exists()

        region_audits = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.REGION_ASSIGNMENT_REVOKED,
            target_user=member,
        )
        station_audits = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.STATION_ASSIGNMENT_REVOKED,
            target_user=member,
        )
        assert region_audits.count() == 1
        assert station_audits.count() == 1
        assert "reason=user_soft_deleted" in region_audits.first().message
        assert "reason=user_soft_deleted" in station_audits.first().message

    def test_account_tokens_invalidated(self, client, admin, member):
        for ttype in [
            AccountToken.TokenType.WELCOME,
            AccountToken.TokenType.RESET,
            AccountToken.TokenType.VERIFY,
        ]:
            issue_token(member, ttype)

        assert member.account_tokens.filter(used_at__isnull=True).count() == 3

        client.force_login(admin)
        client.post(reverse("accounts:user_soft_delete", kwargs={"pk": member.pk}))

        assert member.account_tokens.filter(used_at__isnull=True).count() == 0
        assert member.account_tokens.filter(used_at__isnull=False).count() == 3

    def test_emits_user_soft_deleted_audit_with_email_in_message(
        self,
        client,
        admin,
        member,
    ):
        client.force_login(admin)
        client.post(reverse("accounts:user_soft_delete", kwargs={"pk": member.pk}))

        audit = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.USER_SOFT_DELETED,
            target_user=member,
        ).first()
        assert audit is not None
        assert audit.actor == admin
        assert "OE5MEM1" in audit.message
        assert "m@example.org" in audit.message
