"""Review-loop round 7: 405 vs 403, audit export of account rows, IP redaction."""

import csv
import io

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import AccountAuditLog, User

pytestmark = pytest.mark.django_db

DENIED = AccountAuditLog.EventType.PERMISSION_DENIED


def _mk(level, name=None):
    return User.objects.create_user(
        username=name or f"r7-{level}", password="x", membership_level=level
    )


@pytest.mark.parametrize("name", ["dashboard:index", "audit:audit_list"])
def test_admin_post_to_get_only_view_is_405_without_denial_row(client, name):
    client.force_login(_mk("admin"))
    assert client.post(reverse(name), {}).status_code == 405
    assert not AccountAuditLog.objects.filter(event_type=DENIED).exists()


def test_member_post_to_get_only_view_is_405_without_denial_row(client):
    client.force_login(_mk("member"))
    assert client.post(reverse("dashboard:index"), {}).status_code == 405
    assert not AccountAuditLog.objects.filter(event_type=DENIED).exists()


def test_drf_unsupported_method_is_405_without_denial_row():
    api = APIClient()
    api.force_authenticate(_mk("member"))
    # /api/v1/stations/ is list/create only: DELETE on the collection is unsupported.
    assert api.delete("/api/v1/stations/").status_code == 405
    assert not AccountAuditLog.objects.filter(event_type=DENIED).exists()


def test_export_includes_permission_denied_rows(client):
    AccountAuditLog.log(
        event_type=DENIED,
        actor=_mk("member", "denied-one"),
        message="images.manage denied (POST /x/)",
        ip_address="198.51.100.9",
        details={"area": "images", "verb": "manage", "count": 2},
    )
    client.force_login(_mk("admin"))
    url = reverse("audit:audit_export") + "?event_type=permission_denied"
    rows = list(csv.reader(io.StringIO(client.get(url).content.decode())))
    assert len(rows) == 2 and "images.manage denied" in rows[1][4]
    assert "denied-one" in rows[1] and "images" in rows[1][5]
    data = client.get(url + "&format=json").json()
    assert len(data) == 1 and data[0]["changes"]["verb"] == "manage"


def test_user_detail_audit_tab_hides_ip_from_non_auditor(client):
    me = _mk("member", "self-r7")
    admin = _mk("admin", "adm-r7")
    AccountAuditLog.log(
        event_type=AccountAuditLog.EventType.USER_UPDATED,
        actor=admin,
        target_user=me,
        message="changed",
        ip_address="203.0.113.99",
    )
    url = reverse("accounts:user_detail", args=[me.pk])
    client.force_login(me)
    html = client.get(url).content.decode()
    assert "changed" in html and "203.0.113.99" not in html
    client.force_login(admin)
    assert "203.0.113.99" in client.get(url).content.decode()
