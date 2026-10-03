"""Permission-matrix + audit tests for the Region and StationTag write endpoints.

Task 3: Region (staff/admin-only CRUD) + StationTag (staff/admin-only CRUD).
Every mutation must produce a DB audit row containing "via API token".
"""

import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


def test_region_crud_staff_only(api_topology, bearer):
    t = api_topology
    url = reverse("api:region-list")
    resp = bearer(t["staff"]).post(url, {"name": "R3", "slug": "r3"}, format="json")
    assert resp.status_code == 201
    resp = bearer(t["region_mgr"]).post(url, {"name": "R4", "slug": "r4"}, format="json")
    assert resp.status_code == 403
    detail = reverse("api:region-detail", args=[t["region_in"].pk])
    resp = bearer(t["region_mgr"]).patch(detail, {"description": "x"}, format="json")
    assert resp.status_code == 403
    resp = bearer(t["admin"]).patch(detail, {"description": "ok"}, format="json")
    assert resp.status_code == 200


def test_station_tag_crud_staff_only(api_topology, bearer):
    t = api_topology
    url = reverse("api:station-tag-list")
    resp = bearer(t["staff"]).post(url, {"name": "VHF", "slug": "vhf"}, format="json")
    assert resp.status_code == 201
    resp = bearer(t["station_user"]).post(url, {"name": "HF", "slug": "hf"}, format="json")
    assert resp.status_code == 403


def test_region_create_audits(api_topology, bearer):
    from apps.accounts.models import AccountAuditLog

    t = api_topology
    bearer(t["staff"]).post(
        reverse("api:region-list"), {"name": "R9", "slug": "r9"}, format="json"
    )
    log = AccountAuditLog.objects.filter(
        event_type=AccountAuditLog.EventType.REGION_CREATED
    ).latest("created_at")
    assert "via API token" in log.message


def test_station_tag_create_audits(api_topology, bearer):
    """StationTag writes audit to AccountAuditLog with CONFIG_CHANGED event type."""
    from apps.accounts.models import AccountAuditLog

    t = api_topology
    bearer(t["staff"]).post(
        reverse("api:station-tag-list"), {"name": "DMR", "slug": "dmr"}, format="json"
    )
    log = AccountAuditLog.objects.filter(
        event_type=AccountAuditLog.EventType.CONFIG_CHANGED
    ).latest("created_at")
    assert "via API token" in log.message


def test_region_delete_audits(api_topology, bearer):
    """Staff delete of a region records an API REGION_DELETED audit row with token origin.

    NB: a post_delete signal on Region also emits a REGION_DELETED row
    ("deleted: <name>"), so filter on the API message ("via API token")
    to isolate the row this write path produced rather than the newest.
    """
    from apps.accounts.models import AccountAuditLog
    from apps.stations.models import Region

    t = api_topology
    victim = Region.objects.create(name="Doomed", slug="doomed")
    detail = reverse("api:region-detail", args=[victim.pk])
    resp = bearer(t["staff"]).delete(detail)
    assert resp.status_code == 204
    log = AccountAuditLog.objects.filter(
        event_type=AccountAuditLog.EventType.REGION_DELETED,
        message__contains="via API token",
    ).latest("created_at")
    assert "Region doomed deleted" in log.message


def test_station_tag_delete_staff_only(api_topology, bearer, make_station_tag):
    """Non-internal users get 403 on StationTag DELETE; staff gets 204."""
    tag = make_station_tag("UHF")
    detail = reverse("api:station-tag-detail", args=[tag.pk])
    t = api_topology
    assert bearer(t["region_mgr"]).delete(detail).status_code == 403
    assert bearer(t["station_user"]).delete(detail).status_code == 403
    assert bearer(t["staff"]).delete(detail).status_code == 204
