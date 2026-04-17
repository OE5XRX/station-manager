import pytest
from django.urls import reverse

from apps.accounts.models import User


@pytest.mark.django_db
class TestUserModel:
    def test_create_user(self):
        user = User.objects.create_user(username="test", password="pass123")
        assert user.role == "member"
        assert user.language == "en"

    def test_create_superuser(self):
        user = User.objects.create_superuser(username="super", password="pass123")
        assert user.role == "admin"
        assert user.is_superuser

    def test_is_admin_property(self):
        user = User(role="admin")
        assert user.is_admin is True
        assert user.is_operator is False

    def test_is_operator_property(self):
        user = User(role="operator")
        assert user.is_admin is False
        assert user.is_operator is True


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
