import pytest
from django.urls import reverse

from apps.accounts.models import User


@pytest.mark.django_db
class TestUserModel:
    def test_create_user(self):
        user = User.objects.create_user(username="test", password="pass123")
        assert user.language == "en"

    def test_create_superuser(self):
        user = User.objects.create_superuser(username="super", password="pass123")
        assert user.is_superuser


@pytest.mark.django_db
def test_is_admin_true_when_membership_level_admin():
    """``is_admin`` is driven by ``membership_level=ADMIN`` (Task 9)."""
    user = User.objects.create_user(username="a", password="x", email="a@x")
    user.membership_level = User.MembershipLevel.ADMIN
    user.save(update_fields=["membership_level"])
    User._invalidate_role_cache(user)
    assert user.is_admin is True
    assert user.is_internal is True


@pytest.mark.django_db
def test_is_internal_true_when_membership_level_staff():
    """``is_internal`` covers STAFF and ADMIN; STAFF must not be admin."""
    user = User.objects.create_user(username="o", password="x", email="o@x")
    user.membership_level = User.MembershipLevel.STAFF
    user.save(update_fields=["membership_level"])
    User._invalidate_role_cache(user)
    assert user.is_admin is False
    assert user.is_internal is True


@pytest.mark.django_db
def test_member_user_is_neither_admin_nor_internal():
    user = User.objects.create_user(username="m", password="x", email="m@x")
    user.membership_level = User.MembershipLevel.MEMBER
    user.save(update_fields=["membership_level"])
    User._invalidate_role_cache(user)
    assert user.is_admin is False
    assert user.is_internal is False


@pytest.mark.django_db
class TestLoginView:
    def test_login_page_renders(self, client):
        response = client.get(reverse("accounts:login"))
        assert response.status_code == 200

    def test_login_success(self, client, admin_user):
        response = client.post(
            reverse("accounts:login"),
            {"username": "admin", "password": "testpass123"},
        )
        assert response.status_code == 302

    def test_login_failure(self, client):
        response = client.post(
            reverse("accounts:login"),
            {"username": "wrong", "password": "wrong"},
        )
        assert response.status_code == 200


@pytest.mark.django_db
class TestUserManagement:
    def test_user_list_requires_admin(self, client, member_user):
        client.force_login(member_user)
        response = client.get(reverse("accounts:user_list"))
        assert response.status_code == 403

    def test_user_list_admin_access(self, client, admin_user):
        client.force_login(admin_user)
        response = client.get(reverse("accounts:user_list"))
        assert response.status_code == 200


@pytest.mark.django_db
def test_create_superuser_sets_admin_membership_level():
    """createsuperuser must yield is_admin=True so the new account can
    actually access admin views, otherwise the chicken-and-egg
    bootstrap is broken on a fresh install. After Task 10, the manager
    sets ``membership_level=ADMIN`` directly (formerly: added user to
    the legacy ``admin`` Django Group)."""
    user = User.objects.create_superuser(username="sup", password="x", email="s@x")
    assert user.is_superuser is True
    assert user.membership_level == User.MembershipLevel.ADMIN
    assert user.is_admin is True


@pytest.mark.django_db
def test_audit_user_filter_finds_admins_by_membership_level():
    """apps.audit.views admin-user-filter (Task 10) selects on
    ``membership_level=ADMIN``."""
    user = User.objects.create_user(username="byglvl", password="x", email="b@x")
    user.membership_level = User.MembershipLevel.ADMIN
    user.save(update_fields=["membership_level"])
    admins = User.objects.filter(membership_level=User.MembershipLevel.ADMIN)
    assert user in admins
