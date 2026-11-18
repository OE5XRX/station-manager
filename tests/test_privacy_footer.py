"""The "Datenschutz" footer link appears on every page (when the gate is on)."""

import pytest
from django.urls import reverse

POLICY_HREF = 'href="https://oe5xrx.org/docs/datenschutz.html"'


@pytest.fixture
def acked_client(client, member_user, settings):
    settings.PRIVACY_POLICY_VERSION = 1
    member_user.privacy_ack_version = 1
    member_user.save(update_fields=["privacy_ack_version"])
    client.force_login(member_user)
    return client


@pytest.mark.django_db
@pytest.mark.parametrize("name", ["accounts:login", "accounts:password_reset_request"])
def test_footer_on_anonymous_pages(client, settings, name):
    settings.PRIVACY_POLICY_VERSION = 1
    r = client.get(reverse(name))
    assert r.status_code == 200
    assert POLICY_HREF in r.text
    assert "Datenschutz" in r.text


@pytest.mark.django_db
def test_footer_on_dashboard(acked_client):
    r = acked_client.get(reverse("dashboard:index"))
    assert r.status_code == 200
    assert POLICY_HREF in r.text


@pytest.mark.django_db
def test_footer_on_privacy_ack_page(acked_client):
    r = acked_client.get(reverse("accounts:privacy_ack"))
    assert r.text.count(POLICY_HREF) >= 2  # page link + footer link


@pytest.mark.django_db
def test_footer_hidden_when_version_zero(client, settings):
    settings.PRIVACY_POLICY_VERSION = 0
    r = client.get(reverse("accounts:login"))
    assert "datenschutz.html" not in r.text


def test_context_processor_registered(settings):
    procs = settings.TEMPLATES[0]["OPTIONS"]["context_processors"]
    assert "apps.accounts.context_processors.privacy" in procs
