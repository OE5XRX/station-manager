"""Global Audit-Log filter: ?target_user=<pk> narrows AccountAuditLog
and SsoAuditLog entries to the given user (subject or actor).

The filter is consumed by the "Open in global audit log" link from
the per-user audit tab (UserDetailView).
"""

import pytest
from django.urls import reverse

from apps.accounts.models import AccountAuditLog, User
from apps.sso.models import SsoAuditLog


@pytest.fixture
def admin(db):
    return User.objects.create_user(
        username="OE5ADMIN",
        password="x",
        membership_level=User.MembershipLevel.ADMIN,
    )


@pytest.fixture
def member(db):
    return User.objects.create_user(
        username="OE5MEM1",
        password="x",
        membership_level=User.MembershipLevel.MEMBER,
    )


@pytest.fixture
def other_member(db):
    return User.objects.create_user(
        username="OE5MEM2",
        password="x",
        membership_level=User.MembershipLevel.MEMBER,
    )


@pytest.mark.django_db
class TestTargetUserFilter:
    """?target_user=<pk> narrows the merged feed."""

    def url(self, target_user):
        return reverse("audit:audit_list") + f"?category=account&target_user={target_user.pk}"

    def test_account_filter_matches_target(self, client, admin, member, other_member):
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.USER_CREATED,
            target_user=member,
            message="created member",
        )
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.USER_CREATED,
            target_user=other_member,
            message="created other",
        )

        client.force_login(admin)
        resp = client.get(self.url(member))
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "created member" in body
        assert "created other" not in body

    def test_sso_filter_matches_target(self, client, admin, member, other_member):
        SsoAuditLog.log(
            event_type=SsoAuditLog.EventType.LOGIN_SUCCESS,
            target_user=member,
            message="member login",
        )
        SsoAuditLog.log(
            event_type=SsoAuditLog.EventType.LOGIN_SUCCESS,
            target_user=other_member,
            message="other login",
        )

        client.force_login(admin)
        resp = client.get(reverse("audit:audit_list") + f"?category=sso&target_user={member.pk}")
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "member login" in body
        assert "other login" not in body

    def test_no_target_user_param_shows_all(self, client, admin, member, other_member):
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.USER_CREATED,
            target_user=member,
            message="entry-a",
        )
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.USER_CREATED,
            target_user=other_member,
            message="entry-b",
        )

        client.force_login(admin)
        resp = client.get(reverse("audit:audit_list") + "?category=account")
        body = resp.content.decode()
        assert "entry-a" in body
        assert "entry-b" in body


@pytest.mark.django_db
class TestAuditTableHideSubject:
    """_audit_table.html hides the Subject column when hide_subject=True."""

    def test_global_feed_shows_subject_header(self, client, admin, member):
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.USER_CREATED,
            target_user=member,
            message="x",
        )
        client.force_login(admin)
        resp = client.get(reverse("audit:audit_list") + "?category=account")
        # Global feed renders with hide_subject=False → "Subject" column header present
        assert "Subject" in resp.content.decode()


@pytest.mark.django_db
class TestTargetUserFilterSsoActorBranch:
    """SsoAuditLog stores the user as ``actor`` for events like LOGIN_SUCCESS.
    The ?target_user filter on the global feed must match both target_user
    AND actor so the per-user detail-audit-tab and the global link agree.
    """

    def test_sso_filter_matches_actor(self, client, admin, member, other_member):
        # LOGIN_SUCCESS typically fires with actor=user, target_user=None
        SsoAuditLog.log(
            event_type=SsoAuditLog.EventType.LOGIN_SUCCESS,
            actor=member,
            message="member-actor-event",
        )
        SsoAuditLog.log(
            event_type=SsoAuditLog.EventType.LOGIN_SUCCESS,
            actor=other_member,
            message="other-actor-event",
        )

        client.force_login(admin)
        resp = client.get(reverse("audit:audit_list") + f"?category=sso&target_user={member.pk}")
        body = resp.content.decode()
        # Actor-side match for member
        assert "member-actor-event" in body
        # Other-member event must NOT appear
        assert "other-actor-event" not in body


@pytest.mark.django_db
class TestTargetUserFilterStationFeed:
    """When ?target_user is set without an explicit ?user, the station feed
    narrows by user_id=<target_user> so the global feed doesn't leak
    unrelated station events into a per-user audit context.
    """

    def test_station_feed_respects_target_user(self, client, admin, member, other_member):
        from apps.stations.models import Region, Station, StationAuditLog

        region = Region.objects.create(name="Innviertel")
        station = Station.objects.create(name="OE5XRX-Test", callsign="OE5XRX", region=region)
        StationAuditLog.log(
            station=station,
            event_type=StationAuditLog.EventType.STATION_ASSIGNMENT_CREATED,
            user=member,
            message="member-station-event",
        )
        StationAuditLog.log(
            station=station,
            event_type=StationAuditLog.EventType.STATION_ASSIGNMENT_CREATED,
            user=other_member,
            message="other-station-event",
        )

        client.force_login(admin)
        resp = client.get(
            reverse("audit:audit_list") + f"?category=station&target_user={member.pk}"
        )
        body = resp.content.decode()
        assert "member-station-event" in body
        assert "other-station-event" not in body

    def test_explicit_user_param_wins_over_target_user(self, client, admin, member, other_member):
        """When both ?user and ?target_user are set on the station feed, the
        explicit ?user wins (it's the existing param)."""
        from apps.stations.models import Region, Station, StationAuditLog

        region = Region.objects.create(name="Innviertel")
        station = Station.objects.create(name="OE5XRX-Test", callsign="OE5XRX", region=region)
        StationAuditLog.log(
            station=station,
            event_type=StationAuditLog.EventType.STATION_ASSIGNMENT_CREATED,
            user=member,
            message="member-station-event",
        )
        StationAuditLog.log(
            station=station,
            event_type=StationAuditLog.EventType.STATION_ASSIGNMENT_CREATED,
            user=other_member,
            message="other-station-event",
        )

        client.force_login(admin)
        resp = client.get(
            reverse("audit:audit_list")
            + f"?category=station&user={other_member.pk}&target_user={member.pk}"
        )
        body = resp.content.decode()
        assert "other-station-event" in body
        assert "member-station-event" not in body
