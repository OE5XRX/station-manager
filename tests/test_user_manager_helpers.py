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
