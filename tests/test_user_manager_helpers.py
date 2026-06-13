"""User.objects.active() / deleted() Manager-Helper.

Sub-Spec 2b §2.3.
"""

import pytest
from django.utils import timezone

from apps.accounts.models import User


@pytest.mark.django_db
class TestUserManagerHelpers:
    def test_active_returns_non_deleted(self):
        User.objects.create_user(username="OE5ALICE", password="x")
        bob = User.objects.create_user(username="OE5BOB", password="x")
        bob.deleted_at = timezone.now()
        bob.save()

        active = list(User.objects.active().values_list("username", flat=True))
        assert "OE5ALICE" in active
        assert "OE5BOB" not in active

    def test_deleted_returns_soft_deleted_only(self):
        User.objects.create_user(username="OE5ALICE", password="x")
        bob = User.objects.create_user(username="OE5BOB", password="x")
        bob.deleted_at = timezone.now()
        bob.save()

        deleted = list(User.objects.deleted().values_list("username", flat=True))
        assert "OE5BOB" in deleted
        assert "OE5ALICE" not in deleted

    def test_all_still_returns_everyone(self):
        User.objects.create_user(username="OE5ALICE", password="x")
        bob = User.objects.create_user(username="OE5BOB", password="x")
        bob.deleted_at = timezone.now()
        bob.save()

        all_users = list(User.objects.all().values_list("username", flat=True))
        assert "OE5ALICE" in all_users
        assert "OE5BOB" in all_users


@pytest.mark.django_db
class TestUsernameReuseAfterSoftDelete:
    def test_can_create_new_user_with_soft_deleted_username(self):
        """A soft-deleted user's username can be reused by a fresh active user.

        Verifies the conditional UniqueConstraint is actually enforcing
        uniqueness only on non-deleted rows (no unconditional unique index
        left over from AbstractUser).
        """
        old = User.objects.create_user(username="OE5RECYC", password="x")
        old.deleted_at = timezone.now()
        old.is_active = False
        old.save()

        # Must not raise IntegrityError
        new = User.objects.create_user(username="OE5RECYC", password="x")
        assert new.pk != old.pk
        assert User.objects.filter(username="OE5RECYC").count() == 2

    def test_two_active_users_with_same_username_blocked(self):
        """The conditional constraint still blocks two ACTIVE users with same username."""
        from django.db import IntegrityError

        User.objects.create_user(username="OE5DUP", password="x")
        with pytest.raises(IntegrityError):
            User.objects.create_user(username="OE5DUP", password="x")


@pytest.mark.django_db
class TestLoginAfterCallsignReuse:
    def test_active_user_can_login_with_recycled_callsign(self, client):
        """After soft-delete + reuse, the active user must still log in.

        Without ``get_by_natural_key`` override, Django's ModelBackend
        calls ``get(username=...)`` and raises MultipleObjectsReturned
        because both the active and the soft-deleted row share the
        callsign. The override filters on ``deleted_at__isnull=True``
        so resolution is unambiguous.
        """
        from django.urls import reverse

        old = User.objects.create_user(username="OE5LOGIN", password="old_pw")
        old.deleted_at = timezone.now()
        old.is_active = False
        old.save()

        new = User.objects.create_user(username="OE5LOGIN", password="new_pw")
        assert new.pk != old.pk

        resp = client.post(
            reverse("accounts:login"),
            {"username": "OE5LOGIN", "password": "new_pw"},
            follow=False,
        )
        # Successful login redirects (302); failed login re-renders (200)
        assert resp.status_code == 302, (
            f"Login failed after callsign-reuse — possible MultipleObjectsReturned. "
            f"Got status {resp.status_code}."
        )

    def test_get_by_natural_key_resolves_active(self):
        """Direct test of the manager method, independent of the auth backend."""
        from apps.accounts.models import User as UserModel

        old = UserModel.objects.create_user(username="OE5NK", password="x")
        old.deleted_at = timezone.now()
        old.is_active = False
        old.save()

        new = UserModel.objects.create_user(username="OE5NK", password="x")

        resolved = UserModel.objects.get_by_natural_key("OE5NK")
        assert resolved.pk == new.pk
        assert resolved.deleted_at is None
