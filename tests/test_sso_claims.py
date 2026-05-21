import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

from apps.sso.oidc_claims import add_claims

User = get_user_model()


@pytest.mark.django_db
def test_add_claims_includes_username_email_name_groups():
    admin_group, _ = Group.objects.get_or_create(name="admin")
    techniker_group, _ = Group.objects.get_or_create(name="techniker")
    user = User.objects.create_user(
        username="peterb",
        password="x",
        email="peter@oe5xrx.org",
        first_name="Peter",
        last_name="Buchegger",
    )
    user.language = "de"
    user.save()
    user.groups.add(admin_group, techniker_group)

    claims = add_claims({}, user, request=None)

    assert claims["preferred_username"] == "peterb"
    assert claims["email"] == "peter@oe5xrx.org"
    assert claims["email_verified"] is True
    assert claims["name"] == "Peter Buchegger"
    assert claims["locale"] == "de"
    assert set(claims["groups"]) == {"admin", "techniker"}


@pytest.mark.django_db
def test_add_claims_falls_back_to_username_when_no_full_name():
    user = User.objects.create_user(username="anon", password="x", email="a@x.test")
    claims = add_claims({}, user, request=None)
    assert claims["name"] == "anon"


@pytest.mark.django_db
def test_add_claims_groups_is_always_a_list_even_if_empty():
    """RPs (InvenTree, Grafana) expect groups as a list; missing/scalar breaks them."""
    user = User.objects.create_user(username="loner", password="x", email="l@x.test")
    claims = add_claims({}, user, request=None)
    assert claims["groups"] == []


@pytest.mark.django_db
def test_add_claims_email_verified_false_when_no_email():
    """If a user has no email (e.g. agent-bot account), email_verified must be False
    so RPs don't accidentally trust an empty string as a verified identifier."""
    user = User.objects.create_user(username="bot", password="x")
    claims = add_claims({}, user, request=None)
    assert claims["email"] == ""
    assert claims["email_verified"] is False


@pytest.mark.django_db
def test_add_claims_locale_defaults_to_en_when_user_has_no_language_attr():
    """Defense against a future User-model change that drops language."""
    user = User.objects.create_user(username="x", password="x", email="x@x.test")
    # User.language is "en" by default in this project
    claims = add_claims({}, user, request=None)
    assert claims["locale"] == "en"


@pytest.mark.django_db
def test_add_claims_preserves_existing_claim_keys():
    """If DOT pre-populates standard claims (sub, iss, exp...), we must not clobber them."""
    user = User.objects.create_user(username="x", password="x", email="x@x.test")
    existing = {"sub": "42", "iss": "https://example.org/sso", "exp": 999}
    claims = add_claims(existing, user, request=None)
    assert claims["sub"] == "42"
    assert claims["iss"] == "https://example.org/sso"
    assert claims["exp"] == 999
    # ...AND has our additions:
    assert claims["preferred_username"] == "x"
