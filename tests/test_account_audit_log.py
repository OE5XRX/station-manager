"""Tests for AccountAuditLog model + log() helper."""

import pytest

from apps.accounts.models import AccountAuditLog, User


@pytest.mark.django_db
def test_event_type_choices():
    assert AccountAuditLog.EventType.MEMBERSHIP_PROMOTED == "membership_promoted"
    assert AccountAuditLog.EventType.MEMBERSHIP_DEMOTED == "membership_demoted"
    assert AccountAuditLog.EventType.REGION_ASSIGNMENT_CREATED == "region_assignment_created"
    assert AccountAuditLog.EventType.REGION_ASSIGNMENT_REVOKED == "region_assignment_revoked"
    assert AccountAuditLog.EventType.REGION_CREATED == "region_created"
    assert AccountAuditLog.EventType.REGION_UPDATED == "region_updated"
    assert AccountAuditLog.EventType.REGION_DELETED == "region_deleted"


@pytest.mark.django_db
def test_log_helper_creates_row():
    actor = User.objects.create_user(username="admin", password="x")
    target = User.objects.create_user(username="hans", password="x")
    entry = AccountAuditLog.log(
        event_type=AccountAuditLog.EventType.MEMBERSHIP_PROMOTED,
        actor=actor,
        target_user=target,
        message="applicant → member",
    )
    assert entry.pk is not None
    assert entry.actor == actor
    assert entry.target_user == target
    assert entry.created_at is not None


@pytest.mark.django_db
def test_str_format():
    entry = AccountAuditLog.log(
        event_type=AccountAuditLog.EventType.REGION_CREATED,
        message="created: Innviertel",
    )
    assert "Region Created" in str(entry)
