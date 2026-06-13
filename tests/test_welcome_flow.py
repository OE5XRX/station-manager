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
