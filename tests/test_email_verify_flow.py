"""Sub-Spec 2a — Email-Verify flow (form + view + audit).

Sub-Spec 2a §3.3.
"""

import pytest
from django.urls import reverse

from apps.accounts.forms import ProfileIdentityForm
from apps.accounts.models import AccountAuditLog, AccountToken, User


@pytest.fixture
def member(db):
    return User.objects.create_user(
        username="OE5MEM1",
        email="old@example.org",
        first_name="Hans",
        password="x",
        membership_level=User.MembershipLevel.MEMBER,
    )


@pytest.mark.django_db
class TestProfileIdentityFormEmailNonMutation:
    def test_save_with_email_changed_keeps_db_email(self, member):
        form = ProfileIdentityForm(
            data={
                "email": "new@example.org",
                "first_name": "Hans",
                "last_name": "",
                "language": "en",
            },
            instance=member,
        )
        assert form.is_valid(), form.errors
        form.save()
        member.refresh_from_db()
        # Email stays as OLD
        assert member.email == "old@example.org"

    def test_save_without_email_change_persists_other_fields(self, member):
        form = ProfileIdentityForm(
            data={
                "email": "old@example.org",
                "first_name": "Geänderter Name",
                "last_name": "",
                "language": "en",
            },
            instance=member,
        )
        assert form.is_valid(), form.errors
        form.save()
        member.refresh_from_db()
        assert member.first_name == "Geänderter Name"
        assert member.email == "old@example.org"

    def test_clean_email_rejects_other_user_email_iexact(self, db):
        User.objects.create_user(
            username="OTHER",
            email="Taken@Example.org",
            password="x",
            membership_level=User.MembershipLevel.MEMBER,
        )
        me = User.objects.create_user(
            username="ME",
            email="me@example.org",
            password="x",
            membership_level=User.MembershipLevel.MEMBER,
        )
        form = ProfileIdentityForm(
            data={
                "email": "taken@example.org",
                "first_name": "",
                "last_name": "",
                "language": "en",
            },
            instance=me,
        )
        assert not form.is_valid()
        assert "email" in form.errors

    def test_clean_email_allows_my_own_email_unchanged(self, member):
        form = ProfileIdentityForm(
            data={
                "email": "old@example.org",
                "first_name": "Hans",
                "last_name": "",
                "language": "en",
            },
            instance=member,
        )
        assert form.is_valid(), form.errors


@pytest.mark.django_db
class TestProfileEmailChangeTriggersVerify:
    def test_email_change_does_not_mutate_db_email(self, client, member):
        client.force_login(member)
        client.post(
            reverse("accounts:profile"),
            {
                "form_name": "identity",
                "identity-email": "new@example.org",
                "identity-first_name": "Hans",
                "identity-last_name": "",
                "identity-language": "en",
            },
        )
        member.refresh_from_db()
        assert member.email == "old@example.org"

    def test_email_change_issues_verify_token_with_payload(self, client, member):
        client.force_login(member)
        client.post(
            reverse("accounts:profile"),
            {
                "form_name": "identity",
                "identity-email": "new@example.org",
                "identity-first_name": "Hans",
                "identity-last_name": "",
                "identity-language": "en",
            },
        )
        t = AccountToken.objects.filter(
            user=member,
            token_type=AccountToken.TokenType.VERIFY,
            used_at__isnull=True,
        ).latest("created_at")
        assert t.payload == {"new_email": "new@example.org"}

    def test_email_change_sends_mail_to_new_address(self, client, member):
        from django.core import mail

        client.force_login(member)
        client.post(
            reverse("accounts:profile"),
            {
                "form_name": "identity",
                "identity-email": "new@example.org",
                "identity-first_name": "Hans",
                "identity-last_name": "",
                "identity-language": "en",
            },
        )
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ["new@example.org"]
        assert "Confirm" in mail.outbox[0].subject

    def test_email_change_emits_email_verify_requested_audit(self, client, member):
        client.force_login(member)
        client.post(
            reverse("accounts:profile"),
            {
                "form_name": "identity",
                "identity-email": "new@example.org",
                "identity-first_name": "Hans",
                "identity-last_name": "",
                "identity-language": "en",
            },
        )
        entry = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.EMAIL_VERIFY_REQUESTED,
            target_user=member,
        ).latest("created_at")
        assert entry.actor == member
        assert "old@example.org → new@example.org" in entry.message


@pytest.mark.django_db
class TestVerifyEmailClick:
    def test_verify_click_swaps_email(self, client, member):
        from apps.accounts.tokens import issue_token

        raw = issue_token(
            member,
            AccountToken.TokenType.VERIFY,
            payload={"new_email": "new@example.org"},
        )
        resp = client.get(reverse("accounts:verify_email", kwargs={"token": raw}))
        assert resp.status_code == 302
        member.refresh_from_db()
        assert member.email == "new@example.org"

    def test_verify_click_emits_email_verified_audit(self, client, member):
        from apps.accounts.tokens import issue_token

        raw = issue_token(
            member,
            AccountToken.TokenType.VERIFY,
            payload={"new_email": "new@example.org"},
        )
        client.get(reverse("accounts:verify_email", kwargs={"token": raw}))
        entry = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.EMAIL_VERIFIED,
            target_user=member,
        ).latest("created_at")
        assert "old@example.org → new@example.org" in entry.message

    def test_verify_click_consumed_token_redirects_with_error(self, client, member):
        from apps.accounts.tokens import consume_token, issue_token

        raw = issue_token(
            member,
            AccountToken.TokenType.VERIFY,
            payload={"new_email": "new@example.org"},
        )
        consume_token(raw, AccountToken.TokenType.VERIFY)
        resp = client.get(reverse("accounts:verify_email", kwargs={"token": raw}))
        assert resp.status_code == 302
        member.refresh_from_db()
        assert member.email == "old@example.org"

    def test_verify_blocked_if_new_email_grabbed_by_other_user(self, client, member, db):
        from apps.accounts.tokens import issue_token

        raw = issue_token(
            member,
            AccountToken.TokenType.VERIFY,
            payload={"new_email": "new@example.org"},
        )
        User.objects.create_user(
            username="OTHER",
            email="new@example.org",
            password="x",
            membership_level=User.MembershipLevel.MEMBER,
        )
        resp = client.get(reverse("accounts:verify_email", kwargs={"token": raw}))
        assert resp.status_code == 302
        member.refresh_from_db()
        assert member.email == "old@example.org"
        assert not AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.EMAIL_VERIFIED,
            target_user=member,
        ).exists()

    def test_new_verify_request_invalidates_previous(self, client, member):
        client.force_login(member)
        client.post(
            reverse("accounts:profile"),
            {
                "form_name": "identity",
                "identity-email": "new1@example.org",
                "identity-first_name": "Hans",
                "identity-last_name": "",
                "identity-language": "en",
            },
        )
        client.post(
            reverse("accounts:profile"),
            {
                "form_name": "identity",
                "identity-email": "new2@example.org",
                "identity-first_name": "Hans",
                "identity-last_name": "",
                "identity-language": "en",
            },
        )
        active = AccountToken.objects.filter(
            user=member,
            token_type=AccountToken.TokenType.VERIFY,
            used_at__isnull=True,
        )
        assert active.count() == 1
        assert active.first().payload == {"new_email": "new2@example.org"}
