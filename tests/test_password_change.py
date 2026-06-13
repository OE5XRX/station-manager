"""ProfilePasswordChangeView — self-service password change.

Sub-Spec 1c Sektion 4.4.
"""

import pytest
from django.urls import reverse

from apps.accounts.models import AccountAuditLog, User


@pytest.fixture
def member(db):
    u = User.objects.create_user(
        username="OE5MEM1",
        membership_level=User.MembershipLevel.MEMBER,
    )
    u.set_password("oldsecret123!")
    u.save()
    return u


@pytest.mark.django_db
class TestProfilePasswordChange:
    def test_valid_change(self, client, member):
        client.force_login(member)
        resp = client.post(
            reverse("accounts:password_change"),
            {
                "old_password": "oldsecret123!",
                "new_password1": "newsecret456!",
                "new_password2": "newsecret456!",
            },
        )
        assert resp.status_code == 302
        member.refresh_from_db()
        # Old password no longer matches
        assert not member.check_password("oldsecret123!")
        # New one does
        assert member.check_password("newsecret456!")

    def test_session_stays_alive(self, client, member):
        """update_session_auth_hash must keep the session valid after change."""
        client.force_login(member)
        client.post(
            reverse("accounts:password_change"),
            {
                "old_password": "oldsecret123!",
                "new_password1": "newsecret456!",
                "new_password2": "newsecret456!",
            },
        )
        # Subsequent GET on profile still authenticated
        resp = client.get(reverse("accounts:profile"))
        assert resp.status_code == 200

    def test_emits_password_changed_audit(self, client, member):
        client.force_login(member)
        before = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.PASSWORD_CHANGED, target_user=member
        ).count()
        client.post(
            reverse("accounts:password_change"),
            {
                "old_password": "oldsecret123!",
                "new_password1": "newsecret456!",
                "new_password2": "newsecret456!",
            },
        )
        after = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.PASSWORD_CHANGED, target_user=member
        ).count()
        assert after == before + 1
        entry = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.PASSWORD_CHANGED, target_user=member
        ).latest("created_at")
        assert entry.message == "self-edit changed: password"
        assert entry.actor == member

    def test_wrong_old_password_no_change(self, client, member):
        client.force_login(member)
        resp = client.post(
            reverse("accounts:password_change"),
            {
                "old_password": "WRONG",
                "new_password1": "newsecret456!",
                "new_password2": "newsecret456!",
            },
        )
        assert resp.status_code == 302
        member.refresh_from_db()
        assert member.check_password("oldsecret123!")
        # No audit
        assert (
            AccountAuditLog.objects.filter(
                event_type=AccountAuditLog.EventType.PASSWORD_CHANGED, target_user=member
            ).count()
            == 0
        )

    def test_mismatched_new_passwords_no_change(self, client, member):
        client.force_login(member)
        client.post(
            reverse("accounts:password_change"),
            {
                "old_password": "oldsecret123!",
                "new_password1": "newsecret456!",
                "new_password2": "DIFFERENT",
            },
        )
        member.refresh_from_db()
        assert member.check_password("oldsecret123!")
        assert (
            AccountAuditLog.objects.filter(
                event_type=AccountAuditLog.EventType.PASSWORD_CHANGED, target_user=member
            ).count()
            == 0
        )
