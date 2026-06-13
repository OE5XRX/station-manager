"""Sub-Spec 2a — Welcome flow (UserCreationForm, UserCreateView, set-password).

Sub-Spec 2a §3.1.
"""

import pytest
from django.urls import reverse

from apps.accounts.forms import UserCreationForm
from apps.accounts.models import User


@pytest.fixture
def admin(db):
    return User.objects.create_user(
        username="OE5ADMIN",
        password="x",
        membership_level=User.MembershipLevel.ADMIN,
    )


@pytest.mark.django_db
class TestUserCreationFormFields:
    def test_no_password_fields(self):
        form = UserCreationForm()
        assert "password1" not in form.fields
        assert "password2" not in form.fields

    def test_identity_fields_present(self):
        form = UserCreationForm()
        for field in ("username", "email", "first_name", "last_name", "language"):
            assert field in form.fields


@pytest.mark.django_db
class TestUserCreationFormCleanEmail:
    def test_required(self):
        form = UserCreationForm(
            data={
                "username": "OE5NEW",
                "email": "",
                "first_name": "",
                "last_name": "",
                "language": "en",
            }
        )
        assert not form.is_valid()
        assert "email" in form.errors

    def test_unique_case_insensitive(self, db):
        User.objects.create_user(
            username="OE5OLD",
            email="A@Example.org",
            password="x",
            membership_level=User.MembershipLevel.MEMBER,
        )
        form = UserCreationForm(
            data={
                "username": "OE5NEW",
                "email": "a@example.org",
                "first_name": "",
                "last_name": "",
                "language": "en",
            }
        )
        assert not form.is_valid()
        assert "email" in form.errors

    def test_save_sets_unusable_password(self, db):
        form = UserCreationForm(
            data={
                "username": "OE5NEW",
                "email": "n@example.org",
                "first_name": "",
                "last_name": "",
                "language": "en",
            }
        )
        assert form.is_valid(), form.errors
        user = form.save()
        assert not user.has_usable_password()


@pytest.mark.django_db
class TestUserCreateViewWelcome:
    def test_create_user_sets_unusable_password(self, client, admin):
        client.force_login(admin)
        client.post(
            reverse("accounts:user_create"),
            {
                "username": "OE5NEW",
                "email": "n@example.org",
                "first_name": "",
                "last_name": "",
                "language": "en",
            },
        )
        new = User.objects.get(username="OE5NEW")
        assert not new.has_usable_password()

    def test_create_user_issues_welcome_token(self, client, admin):
        from apps.accounts.models import AccountToken

        client.force_login(admin)
        client.post(
            reverse("accounts:user_create"),
            {
                "username": "OE5NEW",
                "email": "n@example.org",
                "first_name": "",
                "last_name": "",
                "language": "en",
            },
        )
        new = User.objects.get(username="OE5NEW")
        assert AccountToken.objects.filter(
            user=new, token_type=AccountToken.TokenType.WELCOME, used_at__isnull=True
        ).exists()

    def test_create_user_sends_welcome_mail(self, client, admin):
        from django.core import mail

        client.force_login(admin)
        client.post(
            reverse("accounts:user_create"),
            {
                "username": "OE5NEW",
                "email": "n@example.org",
                "first_name": "Hans",
                "last_name": "",
                "language": "en",
            },
        )
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ["n@example.org"]
        assert "Welcome" in mail.outbox[0].subject

    def test_create_user_emits_audits(self, client, admin):
        from apps.accounts.models import AccountAuditLog

        client.force_login(admin)
        client.post(
            reverse("accounts:user_create"),
            {
                "username": "OE5NEW",
                "email": "n@example.org",
                "first_name": "",
                "last_name": "",
                "language": "en",
            },
        )
        new = User.objects.get(username="OE5NEW")
        events = set(
            AccountAuditLog.objects.filter(target_user=new).values_list("event_type", flat=True)
        )
        assert AccountAuditLog.EventType.USER_CREATED in events
        assert AccountAuditLog.EventType.WELCOME_TOKEN_SENT in events


@pytest.mark.django_db
class TestSetPasswordViewWelcome:
    def test_get_with_valid_welcome_token_renders_form(self, client, db):
        from apps.accounts.models import AccountToken
        from apps.accounts.tokens import issue_token

        user = User.objects.create_user(
            username="OE5NEW",
            email="n@example.org",
            membership_level=User.MembershipLevel.APPLICANT,
        )
        user.set_unusable_password()
        user.save()
        raw = issue_token(user, AccountToken.TokenType.WELCOME)

        resp = client.get(reverse("accounts:set_password", kwargs={"token": raw}))
        assert resp.status_code == 200
        assert b"new_password1" in resp.content

    def test_post_valid_sets_password_and_logs_in(self, client, db):
        from apps.accounts.models import AccountToken
        from apps.accounts.tokens import issue_token

        user = User.objects.create_user(
            username="OE5NEW",
            email="n@example.org",
            membership_level=User.MembershipLevel.APPLICANT,
        )
        user.set_unusable_password()
        user.save()
        raw = issue_token(user, AccountToken.TokenType.WELCOME)

        resp = client.post(
            reverse("accounts:set_password", kwargs={"token": raw}),
            {"new_password1": "VerySecret1!", "new_password2": "VerySecret1!"},
        )
        assert resp.status_code == 302
        user.refresh_from_db()
        assert user.has_usable_password()
        assert user.check_password("VerySecret1!")

    def test_post_valid_consumes_token(self, client, db):
        import hashlib

        from apps.accounts.models import AccountToken
        from apps.accounts.tokens import issue_token

        user = User.objects.create_user(
            username="OE5NEW",
            email="n@example.org",
            membership_level=User.MembershipLevel.APPLICANT,
        )
        user.set_unusable_password()
        user.save()
        raw = issue_token(user, AccountToken.TokenType.WELCOME)

        client.post(
            reverse("accounts:set_password", kwargs={"token": raw}),
            {"new_password1": "VerySecret1!", "new_password2": "VerySecret1!"},
        )
        h = hashlib.sha256(raw.encode()).hexdigest()
        assert AccountToken.objects.get(secret_hash=h).used_at is not None

    def test_get_with_invalid_token_redirects_to_login_with_error(self, client, db):
        resp = client.get(
            reverse("accounts:set_password", kwargs={"token": "garbage"}),
            follow=False,
        )
        assert resp.status_code == 302
        assert resp.url == reverse("accounts:login")

    def test_emits_password_set_from_token_audit(self, client, db):
        from apps.accounts.models import AccountAuditLog, AccountToken
        from apps.accounts.tokens import issue_token

        user = User.objects.create_user(
            username="OE5NEW",
            email="n@example.org",
            membership_level=User.MembershipLevel.APPLICANT,
        )
        user.set_unusable_password()
        user.save()
        raw = issue_token(user, AccountToken.TokenType.WELCOME)

        client.post(
            reverse("accounts:set_password", kwargs={"token": raw}),
            {"new_password1": "VerySecret1!", "new_password2": "VerySecret1!"},
        )
        entry = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.PASSWORD_SET_FROM_TOKEN,
            target_user=user,
        ).latest("created_at")
        assert "welcome" in entry.message
        assert entry.actor == user


@pytest.mark.django_db
class TestResendWelcomeView:
    def test_resend_invalidates_previous_and_issues_new(self, client, admin):
        import hashlib

        from apps.accounts.models import AccountToken
        from apps.accounts.tokens import issue_token

        user = User.objects.create_user(
            username="OE5NEW",
            email="n@example.org",
            membership_level=User.MembershipLevel.APPLICANT,
        )
        user.set_unusable_password()
        user.save()
        old_raw = issue_token(user, AccountToken.TokenType.WELCOME)

        client.force_login(admin)
        resp = client.post(reverse("accounts:resend_welcome", kwargs={"pk": user.pk}))
        assert resp.status_code == 302

        old_hash = hashlib.sha256(old_raw.encode()).hexdigest()
        assert AccountToken.objects.get(secret_hash=old_hash).used_at is not None
        # Exactly one fresh unused welcome token exists
        assert (
            AccountToken.objects.filter(
                user=user,
                token_type=AccountToken.TokenType.WELCOME,
                used_at__isnull=True,
            ).count()
            == 1
        )

    def test_resend_sends_mail_to_user(self, client, admin):
        from django.core import mail

        user = User.objects.create_user(
            username="OE5NEW",
            email="n@example.org",
            membership_level=User.MembershipLevel.APPLICANT,
        )
        user.set_unusable_password()
        user.save()
        client.force_login(admin)
        client.post(reverse("accounts:resend_welcome", kwargs={"pk": user.pk}))
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ["n@example.org"]

    def test_resend_rejects_user_with_usable_password(self, client, admin, db):
        from apps.accounts.models import AccountToken

        user = User.objects.create_user(
            username="OE5ACT",
            email="a@example.org",
            password="alreadySet",
            membership_level=User.MembershipLevel.MEMBER,
        )
        client.force_login(admin)
        resp = client.post(reverse("accounts:resend_welcome", kwargs={"pk": user.pk}))
        assert resp.status_code == 302
        assert not AccountToken.objects.filter(user=user).exists()

    def test_resend_requires_admin(self, client, db):
        from apps.accounts.models import AccountToken

        member = User.objects.create_user(
            username="OE5MEM",
            email="mem@example.org",
            password="x",
            membership_level=User.MembershipLevel.MEMBER,
        )
        target = User.objects.create_user(
            username="OE5NEW",
            email="n@example.org",
            membership_level=User.MembershipLevel.APPLICANT,
        )
        target.set_unusable_password()
        target.save()
        client.force_login(member)
        resp = client.post(reverse("accounts:resend_welcome", kwargs={"pk": target.pk}))
        # AdminRequiredMixin → 302 or 403; in any case no token
        assert resp.status_code in (302, 403)
        assert not AccountToken.objects.filter(user=target).exists()
