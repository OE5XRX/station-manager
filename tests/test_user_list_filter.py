"""UserListView ?show=active|inactive|deleted|all filter.

Sub-Spec 2b §3.1 + §7.1.
"""

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User


@pytest.fixture
def admin(db):
    return User.objects.create_user(
        username="OE5ADMIN",
        password="x",
        membership_level=User.MembershipLevel.ADMIN,
    )


@pytest.fixture
def population(db):
    active = User.objects.create_user(username="OE5ACTV", password="x")
    inactive = User.objects.create_user(username="OE5INAC", password="x")
    inactive.is_active = False
    inactive.save()
    deleted = User.objects.create_user(username="OE5DEAD", password="x")
    deleted.deleted_at = timezone.now()
    deleted.is_active = False
    deleted.save()
    return {"active": active, "inactive": inactive, "deleted": deleted}


@pytest.mark.django_db
class TestUserListFilter:
    def test_default_shows_active_only(self, client, admin, population):
        client.force_login(admin)
        resp = client.get(reverse("accounts:user_list"))
        usernames = {u.username for u in resp.context["users"]}
        assert "OE5ACTV" in usernames
        assert "OE5ADMIN" in usernames
        assert "OE5INAC" not in usernames
        assert "OE5DEAD" not in usernames

    def test_show_inactive_shows_inactive_only(self, client, admin, population):
        client.force_login(admin)
        resp = client.get(reverse("accounts:user_list") + "?show=inactive")
        usernames = {u.username for u in resp.context["users"]}
        assert "OE5INAC" in usernames
        assert "OE5ACTV" not in usernames
        assert "OE5DEAD" not in usernames

    def test_show_deleted_shows_deleted_only(self, client, admin, population):
        client.force_login(admin)
        resp = client.get(reverse("accounts:user_list") + "?show=deleted")
        usernames = {u.username for u in resp.context["users"]}
        assert "OE5DEAD" in usernames
        assert "OE5ACTV" not in usernames
        assert "OE5INAC" not in usernames

    def test_show_all_shows_everyone(self, client, admin, population):
        client.force_login(admin)
        resp = client.get(reverse("accounts:user_list") + "?show=all")
        usernames = {u.username for u in resp.context["users"]}
        assert "OE5ACTV" in usernames
        assert "OE5INAC" in usernames
        assert "OE5DEAD" in usernames
