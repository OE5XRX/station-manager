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
