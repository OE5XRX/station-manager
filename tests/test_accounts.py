import pytest
from django.contrib.auth.models import Group
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
def test_is_admin_true_when_user_in_admin_group():
    from apps.accounts.models import User

    admin_group, _ = Group.objects.get_or_create(name="admin")
    user = User.objects.create_user(username="a", password="x", email="a@x")
    user.groups.add(admin_group)
    assert user.is_admin is True
    assert user.is_operator is False
    assert user.is_staff_member is True


@pytest.mark.django_db
def test_is_operator_true_when_user_in_operator_group():
    from apps.accounts.models import User

    op_group, _ = Group.objects.get_or_create(name="operator")
    user = User.objects.create_user(username="o", password="x", email="o@x")
    user.groups.add(op_group)
    assert user.is_admin is False
    assert user.is_operator is True
    assert user.is_staff_member is True


@pytest.mark.django_db
def test_member_user_is_neither_admin_nor_operator():
    from apps.accounts.models import User

    member_group, _ = Group.objects.get_or_create(name="member")
    user = User.objects.create_user(username="m", password="x", email="m@x")
    user.groups.add(member_group)
    assert user.is_admin is False
    assert user.is_operator is False
    assert user.is_staff_member is False


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
def test_create_superuser_lands_in_admin_group():
    """createsuperuser must yield is_admin=True so the new account can
    actually access admin views, otherwise the chicken-and-egg
    bootstrap is broken on a fresh install."""
    user = User.objects.create_superuser(
        username="sup", password="x", email="s@x"
    )
    assert user.is_superuser is True
    assert user.is_admin is True


@pytest.mark.django_db
def test_audit_user_filter_finds_admins_via_group_not_role():
    """apps.audit.views.py admin-user-filter must work after Task 5;
    user added to admin group via Django admin (without setting .role)
    must still show up."""
    g, _ = Group.objects.get_or_create(name="admin")
    user = User.objects.create_user(username="byggroup", password="x", email="b@x")
    user.groups.add(g)
    admins = User.objects.filter(groups__name="admin").distinct()
    assert user in admins
