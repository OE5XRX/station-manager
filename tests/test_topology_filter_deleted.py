"""Topology routing + visibility filtern soft-deleted user.

Sub-Spec 2b §3.1.
"""

import pytest
from django.utils import timezone

from apps.accounts.models import User


@pytest.fixture
def admin(db):
    return User.objects.create_user(
        username="OE5ADMIN",
        password="x",
        membership_level=User.MembershipLevel.ADMIN,
    )


@pytest.fixture
def deleted_member(db):
    u = User.objects.create_user(
        username="OE5DEAD",
        password="x",
        email="dead@example.org",
        membership_level=User.MembershipLevel.MEMBER,
    )
    u.deleted_at = timezone.now()
    u.is_active = False
    u.save()
    return u


@pytest.mark.django_db
class TestRecipientsExcludesDeleted:
    def test_recipients_for_station_alert_excludes_deleted_admin(
        self,
        db,
        deleted_member,
    ):
        """A soft-deleted Station-Admin is not in the alert-recipient set.

        In practice the soft-delete flow already revokes the assignment,
        but the recipients module should not depend on that — this
        directly verifies the deleted-user filter."""
        from apps.monitoring.recipients import recipients_for_station_alert
        from apps.stations.models import Region, Station, StationAssignment

        region = Region.objects.create(name="X")
        station = Station.objects.create(name="S", callsign="OE5S", region=region)
        # Force-create the assignment despite deleted_at being set on the user
        # so we can test the recipient-filter independently of the soft-delete
        # auto-revoke (defense in depth).
        StationAssignment.objects.create(
            user=deleted_member,
            station=station,
            role=StationAssignment.Role.ADMIN,
            assigned_by=deleted_member,
        )

        recipients = list(recipients_for_station_alert(station))
        assert deleted_member not in recipients


@pytest.mark.django_db
class TestDirectoryVisibility:
    def test_deleted_user_not_in_member_directory_list(self, client, admin, deleted_member):
        from django.urls import reverse

        client.force_login(admin)
        resp = client.get(reverse("accounts:user_list") + "?show=active")
        # OE5DEAD is soft-deleted → excluded from show=active default
        assert "OE5DEAD" not in resp.content.decode()

    def test_active_query_excludes_deleted_members(self, deleted_member):
        from apps.accounts.models import User as UserModel

        members_visible = list(
            UserModel.objects.active().filter(
                membership_level__in=[
                    UserModel.MembershipLevel.MEMBER,
                    UserModel.MembershipLevel.STAFF,
                    UserModel.MembershipLevel.ADMIN,
                ],
            )
        )
        assert deleted_member not in members_visible
