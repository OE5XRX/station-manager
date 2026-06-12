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
