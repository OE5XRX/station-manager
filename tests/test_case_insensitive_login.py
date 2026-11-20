"""Callsign login is case-insensitive: accounts are stored uppercase, users type lowercase."""

from datetime import timedelta

import pytest
from django.contrib.auth import authenticate, get_user_model
from django.urls import reverse
from django.utils import timezone
from oauth2_provider.models import Application

from apps.accounts.forms import UserChangeForm, UserCreationForm
from apps.accounts.models import User
from apps.sso.models import AppGrant
from apps.sso.permissions import SsoOAuth2Validator

UserModel = get_user_model()


@pytest.fixture
def oe5abc(db):
    return User.objects.create_user(username="OE5ABC", password="testpass123", email="a@x.test")


@pytest.mark.django_db
class TestWebLogin:
    @pytest.mark.parametrize("typed", ["OE5ABC", "oe5abc", "Oe5aBc"])
    def test_login_any_case(self, client, oe5abc, typed):
        response = client.post(
            reverse("accounts:login"), {"username": typed, "password": "testpass123"}
        )
        assert response.status_code == 302
        assert client.session["_auth_user_id"] == str(oe5abc.pk)

    def test_wrong_password_still_fails(self, client, oe5abc):
        response = client.post(
            reverse("accounts:login"), {"username": "oe5abc", "password": "nope"}
        )
        assert response.status_code == 200
        assert "_auth_user_id" not in client.session

    def test_soft_deleted_duplicate_with_other_case_does_not_break_login(self, rf, oe5abc):
        User.objects.create_user(
            username="oe5abc", password="old", deleted_at=timezone.now() - timedelta(days=1)
        )
        assert authenticate(rf.post("/"), username="oe5abc", password="testpass123") == oe5abc

    def test_soft_deleted_user_cannot_login(self, rf):
        User.objects.create_user(
            username="OE5DEL", password="pw", deleted_at=timezone.now() - timedelta(days=1)
        )
        assert authenticate(rf.post("/"), username="oe5del", password="pw") is None


@pytest.mark.django_db
class TestLockoutKey:
    def test_case_variants_share_axes_username(self, rf):
        from apps.accounts.axes import get_axes_username

        request = rf.post("/")
        assert get_axes_username(request, {"username": "OE5ABC"}) == "OE5ABC"
        assert get_axes_username(request, {"username": "oe5AbC"}) == "OE5ABC"
        # dotless i upper-cases to I, like the DB's UPPER()-based iexact match
        assert get_axes_username(request, {"username": "oe5abc".replace("a", "ı")}) == "OE5IBC"


@pytest.mark.django_db
class TestOidcPasswordGrantValidator:
    @pytest.fixture
    def application(self):
        return Application.objects.create(
            name="app",
            client_type=Application.CLIENT_CONFIDENTIAL,
            authorization_grant_type=Application.GRANT_PASSWORD,
            redirect_uris="https://example.org/cb/",
        )

    def test_validate_user_lowercase_with_soft_deleted_duplicate(self, oe5abc, application):
        User.objects.create_user(
            username="oe5abc", password="old", deleted_at=timezone.now() - timedelta(days=1)
        )
        AppGrant.objects.create(user=oe5abc, application=application)

        class Req:
            uri = "/sso/token/"
            http_method = "POST"
            decoded_body = ()
            headers = {}

        request = Req()
        ok = SsoOAuth2Validator().validate_user("oe5abc", "testpass123", application, request)
        assert ok is True
        assert request.user == oe5abc


@pytest.mark.django_db
class TestUsernameNormalisation:
    def _form(self, username):
        return UserCreationForm(
            {
                "username": username,
                "email": "n@x.test",
                "first_name": "",
                "last_name": "",
                "language": "en",
            }
        )

    def test_creation_form_uppercases(self, db):
        form = self._form("oe5new")
        assert form.is_valid(), form.errors
        assert form.save().username == "OE5NEW"

    def test_creation_form_rejects_case_variant_duplicate(self, oe5abc):
        form = self._form("oe5abc")
        assert not form.is_valid()
        assert "username" in form.errors

    def test_change_form_uppercases(self, oe5abc):
        form = UserChangeForm(
            {
                "username": "oe5xyz",
                "email": "a@x.test",
                "first_name": "",
                "last_name": "",
                "language": "en",
            },
            instance=oe5abc,
        )
        assert form.is_valid(), form.errors
        assert form.cleaned_data["username"] == "OE5XYZ"


@pytest.mark.django_db
class TestDuplicateGuards:
    def test_change_form_rejects_case_variant_duplicate(self, oe5abc):
        other = User.objects.create_user(username="OE5OTH", password="x", email="o@x.test")
        form = UserChangeForm(
            {
                "username": "oe5abc",
                "email": "o@x.test",
                "first_name": "",
                "last_name": "",
                "language": "en",
            },
            instance=other,
        )
        assert not form.is_valid()
        assert "username" in form.errors

    def test_change_form_allows_keeping_own_username(self, oe5abc):
        form = UserChangeForm(
            {
                "username": "oe5abc",
                "email": "a@x.test",
                "first_name": "",
                "last_name": "",
                "language": "en",
            },
            instance=oe5abc,
        )
        assert form.is_valid(), form.errors

    def test_legacy_case_variant_duplicates_fail_closed_to_exact_match(self, oe5abc):
        legacy = User.objects.create_user(username="oe5abc", password="y", email="l@x.test")
        assert User.objects.get_by_natural_key("oe5abc") == legacy
        assert User.objects.get_by_natural_key("OE5ABC") == oe5abc
        with pytest.raises(User.DoesNotExist):
            User.objects.get_by_natural_key("Oe5abc")


@pytest.mark.django_db
class TestAxesLockoutAcrossCase:
    def test_case_variants_share_lockout(self, client, oe5abc, settings):
        settings.AXES_FAILURE_LIMIT = 3
        url = reverse("accounts:login")
        for typed in ("OE5ABC", "oe5abc", "Oe5AbC"):
            client.post(url, {"username": typed, "password": "wrong"})
        response = client.post(url, {"username": "oE5aBc", "password": "testpass123"})
        assert response.status_code == 429
        assert "_auth_user_id" not in client.session
