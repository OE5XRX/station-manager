"""apps.accounts.emails.send_account_email — single dispatch point.

Sub-Spec 2a §6.1.
"""

import pytest
from django.conf import settings
from django.core import mail

from apps.accounts.models import User


@pytest.fixture
def member(db):
    return User.objects.create_user(
        username="OE5MEM1",
        email="m@example.org",
        first_name="Hans",
        password="x",
        membership_level=User.MembershipLevel.MEMBER,
    )


@pytest.mark.django_db
class TestSendAccountEmail:
    def test_welcome_renders_subject_body_and_uses_user_email(self, member):
        from apps.accounts.emails import send_account_email

        send_account_email(
            member,
            "welcome",
            {
                "raw_token": "ABC123XYZ",
                "actor": "OE5ADMIN",
            },
        )

        assert len(mail.outbox) == 1
        msg = mail.outbox[0]
        assert msg.to == ["m@example.org"]
        assert "Welcome" in msg.subject
        assert "Hans" in msg.body
        assert "OE5ADMIN" in msg.body
        assert "set-password/ABC123XYZ" in msg.body
        assert "7 days" in msg.body

    def test_reset_renders_with_user_email(self, member):
        from apps.accounts.emails import send_account_email

        send_account_email(member, "reset", {"raw_token": "RESETTOK"})

        assert len(mail.outbox) == 1
        msg = mail.outbox[0]
        assert msg.to == ["m@example.org"]
        assert "Password reset" in msg.subject
        assert "set-password/RESETTOK" in msg.body
        assert "24 hours" in msg.body

    def test_verify_uses_override_to(self, member):
        from apps.accounts.emails import send_account_email

        send_account_email(
            member,
            "verify",
            {
                "raw_token": "VERIFYTOK",
                "new_email": "neu@example.org",
                "old_email": "m@example.org",
                "override_to": "neu@example.org",
            },
        )

        assert len(mail.outbox) == 1
        msg = mail.outbox[0]
        # Goes to NEW address, NOT user.email
        assert msg.to == ["neu@example.org"]
        assert "verify-email/VERIFYTOK" in msg.body
        assert "neu@example.org" in msg.body
        assert "m@example.org" in msg.body

    def test_uses_default_from_email(self, member):
        from apps.accounts.emails import send_account_email

        send_account_email(member, "welcome", {"raw_token": "T"})

        assert mail.outbox[0].from_email == settings.DEFAULT_FROM_EMAIL
