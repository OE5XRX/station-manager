"""UserRestoreView — restore soft-deleted user.

Sub-Spec 2b §5.
"""

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import AccountAuditLog, User


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
    )
    u.deleted_at = timezone.now()
    u.is_active = False
    u.save()
    return u


@pytest.mark.django_db
class TestRestore:
    def test_restore_sets_deleted_at_null_and_is_active_true(
        self,
        client,
        admin,
        deleted_member,
    ):
        client.force_login(admin)
        client.post(reverse("accounts:user_restore", kwargs={"pk": deleted_member.pk}))
        deleted_member.refresh_from_db()
        assert deleted_member.deleted_at is None
        assert deleted_member.deleted_by is None
        assert deleted_member.is_active is True

    def test_active_user_returns_404(self, client, admin):
        active = User.objects.create_user(username="OE5LIVE", password="x")
        client.force_login(admin)
        resp = client.post(reverse("accounts:user_restore", kwargs={"pk": active.pk}))
        assert resp.status_code == 404

    def test_restore_blocked_when_email_conflicts_with_active_user(
        self,
        client,
        admin,
        deleted_member,
    ):
        User.objects.create_user(
            username="OE5NEW",
            email="dead@example.org",
            password="x",
        )
        client.force_login(admin)
        client.post(reverse("accounts:user_restore", kwargs={"pk": deleted_member.pk}))
        deleted_member.refresh_from_db()
        assert deleted_member.deleted_at is not None  # still soft-deleted

    def test_emits_user_restored_audit(self, client, admin, deleted_member):
        client.force_login(admin)
        client.post(reverse("accounts:user_restore", kwargs={"pk": deleted_member.pk}))
        audit = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.USER_RESTORED,
            target_user=deleted_member,
        ).first()
        assert audit is not None
        assert audit.actor == admin
        assert "OE5DEAD" in audit.message
