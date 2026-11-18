"""Privacy-policy acknowledgement: model fields, settings and the ack page."""

import pytest

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
