"""Tests for AccountAuditLog EventType additions (Sub-Spec 1a)."""

import pytest

from apps.accounts.models import AccountAuditLog


class TestNewEventTypes:
    """Eight new EventType members added for user-CRUD + station-assignment."""

    def test_user_created_present(self):
        assert AccountAuditLog.EventType.USER_CREATED == "user_created"

    def test_user_updated_present(self):
        assert AccountAuditLog.EventType.USER_UPDATED == "user_updated"

    def test_user_deleted_present(self):
        assert AccountAuditLog.EventType.USER_DELETED == "user_deleted"

    def test_user_activated_present(self):
        assert AccountAuditLog.EventType.USER_ACTIVATED == "user_activated"

    def test_user_deactivated_present(self):
        assert AccountAuditLog.EventType.USER_DEACTIVATED == "user_deactivated"

    def test_password_changed_present(self):
        assert AccountAuditLog.EventType.PASSWORD_CHANGED == "password_changed"

    def test_station_assignment_created_present(self):
        assert AccountAuditLog.EventType.STATION_ASSIGNMENT_CREATED == "station_assignment_created"

    def test_station_assignment_revoked_present(self):
        assert AccountAuditLog.EventType.STATION_ASSIGNMENT_REVOKED == "station_assignment_revoked"

    def test_all_existing_event_types_still_present(self):
        """Regression: existing EventTypes must not be removed."""
        assert AccountAuditLog.EventType.MEMBERSHIP_PROMOTED == "membership_promoted"
        assert AccountAuditLog.EventType.MEMBERSHIP_DEMOTED == "membership_demoted"
        assert AccountAuditLog.EventType.REGION_ASSIGNMENT_CREATED == "region_assignment_created"
        assert AccountAuditLog.EventType.REGION_ASSIGNMENT_REVOKED == "region_assignment_revoked"
        assert AccountAuditLog.EventType.REGION_CREATED == "region_created"
        assert AccountAuditLog.EventType.REGION_UPDATED == "region_updated"
        assert AccountAuditLog.EventType.REGION_DELETED == "region_deleted"


@pytest.mark.django_db
class TestEventTypeDBPersistence:
    """New EventTypes are saveable to AccountAuditLog."""

    def test_user_created_persists(self):
        entry = AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.USER_CREATED,
            message="OE5TEST <test@example.org>",
        )
        assert entry.pk is not None
        entry.refresh_from_db()
        assert entry.event_type == "user_created"

    def test_station_assignment_created_persists(self):
        entry = AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.STATION_ASSIGNMENT_CREATED,
            message="station=OE5XRX, role=admin",
        )
        assert entry.pk is not None
        entry.refresh_from_db()
        assert entry.event_type == "station_assignment_created"
