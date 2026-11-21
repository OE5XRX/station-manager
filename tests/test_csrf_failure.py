"""Stale-tab CSRF failures on the auth forms redirect instead of showing a raw 403."""

import pytest
from django.contrib.messages import get_messages
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User


@pytest.fixture
def csrf_client():
    return Client(enforce_csrf_checks=True)


@pytest.fixture
def user(db):
    return User.objects.create_user(username="OE5ABC", password="testpass123", email="a@x.test")


BAD = {"username": "OE5ABC", "password": "testpass123", "csrfmiddlewaretoken": "stale"}


@pytest.mark.django_db
class TestCsrfFailure:
    def test_anonymous_login_redirects_to_login_with_message(self, csrf_client, user):
        response = csrf_client.post(reverse("accounts:login"), BAD)
        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:login")
        assert "_auth_user_id" not in csrf_client.session
        assert len(list(get_messages(response.wsgi_request))) == 1

    def test_authenticated_stale_login_redirects_to_dashboard(self, csrf_client, user):
        csrf_client.force_login(user)
        response = csrf_client.post(reverse("accounts:login"), BAD)
        assert response.status_code == 302
        assert response["Location"] == reverse("dashboard:index")

    def test_safe_next_is_kept(self, csrf_client, user):
        csrf_client.force_login(user)
        response = csrf_client.post(reverse("accounts:login"), {**BAD, "next": "/de/stations/"})
        assert response["Location"] == "/de/stations/"

    def test_open_redirect_next_is_ignored(self, csrf_client, user):
        csrf_client.force_login(user)
        response = csrf_client.post(
            reverse("accounts:login"), {**BAD, "next": "https://evil.example/"}
        )
        assert response["Location"] == reverse("dashboard:index")
        anon = Client(enforce_csrf_checks=True)
        response = anon.post(reverse("accounts:login"), {**BAD, "next": "//evil.example/"})
        assert response["Location"] == reverse("accounts:login")

    def test_anonymous_password_reset_redirects_to_same_page(self, csrf_client):
        url = reverse("accounts:password_reset_request")
        response = csrf_client.post(url, {"email": "a@x.test"})
        assert response.status_code == 302
        assert response["Location"] == url

    def test_non_auth_path_gets_styled_403(self, csrf_client, user):
        csrf_client.force_login(user)
        response = csrf_client.post(reverse("accounts:logout"), {})
        assert response.status_code == 403
        assert "Sitzung abgelaufen" in response.content.decode()

    def test_post_is_never_processed(self, csrf_client, user):
        # Even with valid credentials a bad token must not log in.
        csrf_client.post(reverse("accounts:login"), BAD)
        assert "_auth_user_id" not in csrf_client.session


@pytest.mark.django_db
class TestRedirectAuthenticatedLogin:
    def test_get_login_when_authenticated_redirects(self, client, user):
        client.force_login(user)
        response = client.get(reverse("accounts:login"))
        assert response.status_code == 302
        assert response["Location"] == reverse("dashboard:index")

    def test_get_login_honours_safe_next_only(self, client, user):
        client.force_login(user)
        response = client.get(reverse("accounts:login") + "?next=/de/stations/")
        assert response["Location"] == "/de/stations/"
        response = client.get(reverse("accounts:login") + "?next=https://evil.example/")
        assert response["Location"] == reverse("dashboard:index")

    def test_anonymous_still_sees_form(self, client):
        assert client.get(reverse("accounts:login")).status_code == 200
