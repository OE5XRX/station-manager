"""Privacy-policy acknowledgement: model fields, settings and the ack page."""

from urllib.parse import urlsplit

import pytest
from django.urls import reverse

from apps.accounts.models import AccountAuditLog


@pytest.mark.django_db
def test_new_user_has_not_acknowledged(member_user):
    assert member_user.privacy_ack_version == 0
    assert member_user.privacy_ack_at is None


def test_privacy_ack_event_type_exists():
    assert AccountAuditLog.EventType.PRIVACY_ACKNOWLEDGED == "privacy_acknowledged"


def test_retention_defaults(settings):
    assert settings.RETENTION_AUDIT_DAYS == 365
    assert settings.RETENTION_IP_DAYS == 90
    assert settings.RETENTION_BATCH_SIZE == 1000
    assert settings.RETENTION_ENFORCE is False
    assert settings.PRIVACY_POLICY_URL == "https://oe5xrx.org/docs/datenschutz.html"


def test_test_settings_disable_gate(settings):
    assert settings.PRIVACY_POLICY_VERSION == 0


# ---------------------------------------------------------------------------
# Acknowledgement page (GET/POST)
# ---------------------------------------------------------------------------

POLICY_URL = "https://oe5xrx.org/docs/datenschutz.html"


@pytest.fixture
def ack_client(client, member_user, settings):
    settings.PRIVACY_POLICY_VERSION = 1
    client.force_login(member_user)
    return client


def _url():
    return reverse("accounts:privacy_ack")


@pytest.mark.django_db
def test_get_renders_summary_and_link(ack_client):
    r = ack_client.get(_url() + "?next=/en/")
    assert r.status_code == 200
    text = r.text
    assert POLICY_URL in text
    assert "Zur Kenntnis genommen" in text
    assert 'name="next" value="/en/"' in text
    assert 'rel="noopener"' in text
    assert "info@oe5xrx.org" in text
    assert "Version 1" in text
    assert reverse("accounts:logout") in text


@pytest.mark.django_db
def test_get_drops_unsafe_next_from_form(ack_client):
    r = ack_client.get(_url() + "?next=https://evil.example/")
    assert "evil.example" not in r.text


@pytest.mark.django_db
def test_post_records_ack_and_audit(ack_client, member_user):
    r = ack_client.post(_url(), {"next": "/en/stations/"})
    member_user.refresh_from_db()
    assert member_user.privacy_ack_version == 1
    assert member_user.privacy_ack_at is not None
    row = AccountAuditLog.objects.get(event_type="privacy_acknowledged")
    assert row.actor == member_user
    assert row.target_user == member_user
    assert row.details == {"version": 1}
    assert "@" not in row.message
    assert r.status_code == 302
    assert r.url == "/en/stations/"


@pytest.mark.django_db
def test_double_post_single_audit_row(ack_client):
    ack_client.post(_url(), {"next": "/en/"})
    ack_client.post(_url(), {"next": "/en/"})
    assert AccountAuditLog.objects.filter(event_type="privacy_acknowledged").count() == 1


@pytest.mark.django_db
def test_post_with_version_zero_records_nothing(client, member_user, settings):
    settings.PRIVACY_POLICY_VERSION = 0
    client.force_login(member_user)
    r = client.post(_url(), {"next": "/en/"})
    member_user.refresh_from_db()
    assert member_user.privacy_ack_version == 0
    assert not AccountAuditLog.objects.filter(event_type="privacy_acknowledged").exists()
    assert r.status_code == 302


@pytest.mark.django_db
@pytest.mark.parametrize(
    "bad", ["//evil.example/", "https://evil.example/", "javascript:alert(1)", ""]
)
def test_unsafe_next_falls_back(ack_client, bad):
    r = ack_client.post(_url(), {"next": bad})
    assert r.status_code == 302
    assert r.url == reverse("dashboard:index")


@pytest.mark.django_db
def test_post_returns_to_sso_authorize_with_query(ack_client):
    target = "/sso/authorize/?client_id=abc&state=xyz"
    r = ack_client.post(_url(), {"next": target})
    assert r.url == target


@pytest.mark.django_db
def test_after_ack_dashboard_reachable(ack_client):
    assert ack_client.get(reverse("dashboard:index")).status_code == 302
    ack_client.post(_url(), {"next": "/en/"})
    assert ack_client.get(reverse("dashboard:index")).status_code == 200


@pytest.mark.django_db
def test_get_requires_login(client, settings):
    settings.PRIVACY_POLICY_VERSION = 1
    r = client.get(_url())
    assert r.status_code == 302
    assert urlsplit(r.url).path == reverse("accounts:login")
