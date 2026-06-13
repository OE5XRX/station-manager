"""UserHardPurgeView — irreversible delete of an already-soft-deleted user.

Sub-Spec 2b §6.
"""

import io
import os

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import AccountAuditLog, AccountToken, User


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
class TestHardPurge:
    def test_active_user_returns_404(self, client, admin):
        active = User.objects.create_user(username="OE5LIVE", password="x")
        client.force_login(admin)
        resp = client.get(reverse("accounts:user_hard_purge", kwargs={"pk": active.pk}))
        assert resp.status_code == 404

    def test_post_cascades_account_tokens(self, client, admin, deleted_member):
        from apps.accounts.tokens import issue_token

        issue_token(deleted_member, AccountToken.TokenType.WELCOME)
        assert AccountToken.objects.filter(user=deleted_member).exists()

        client.force_login(admin)
        client.post(reverse("accounts:user_hard_purge", kwargs={"pk": deleted_member.pk}))

        assert not AccountToken.objects.filter(user=deleted_member).exists()

    def test_post_sets_audit_actor_and_target_to_null_but_message_preserves_strings(
        self,
        client,
        admin,
        deleted_member,
    ):
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.USER_SOFT_DELETED,
            actor=admin,
            target_user=deleted_member,
            message=f"{deleted_member.username} <{deleted_member.email}>",
        )

        client.force_login(admin)
        client.post(reverse("accounts:user_hard_purge", kwargs={"pk": deleted_member.pk}))

        assert not User.objects.filter(pk=deleted_member.pk).exists()

        audit = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.USER_SOFT_DELETED,
        ).first()
        assert audit is not None
        assert audit.target_user is None  # SET_NULL after purge
        assert "OE5DEAD" in audit.message

    def test_post_deletes_avatar_file(self, client, admin, deleted_member, tmp_path, settings):
        from django.test import TestCase
        from PIL import Image

        settings.MEDIA_ROOT = str(tmp_path)
        settings.STORAGES = {
            **settings.STORAGES,
            "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        }

        img = Image.new("RGB", (50, 50), color=(255, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        buf.seek(0)
        f = SimpleUploadedFile("a.jpg", buf.read(), content_type="image/jpeg")
        deleted_member.avatar = f
        deleted_member.save()
        avatar_path = deleted_member.avatar.path
        assert os.path.exists(avatar_path)

        client.force_login(admin)
        # Avatar cleanup is deferred via transaction.on_commit() so it
        # only runs after a successful commit. In pytest-django the
        # outer transaction is rolled back, so callbacks must be flushed
        # explicitly via captureOnCommitCallbacks(execute=True).
        with TestCase.captureOnCommitCallbacks(execute=True):
            client.post(reverse("accounts:user_hard_purge", kwargs={"pk": deleted_member.pk}))

        assert not os.path.exists(avatar_path)

    def test_emits_user_hard_purged_audit_with_soft_delete_date_in_message(
        self,
        client,
        admin,
        deleted_member,
    ):
        soft_date = deleted_member.deleted_at.strftime("%Y-%m-%d")

        client.force_login(admin)
        client.post(reverse("accounts:user_hard_purge", kwargs={"pk": deleted_member.pk}))

        audit = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.USER_HARD_PURGED,
        ).first()
        assert audit is not None
        assert audit.actor == admin
        assert "OE5DEAD" in audit.message
        assert soft_date in audit.message
