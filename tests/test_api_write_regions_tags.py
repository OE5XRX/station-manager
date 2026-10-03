"""Permission-matrix + audit tests for the Region and StationTag write endpoints.

Task 3: Region (staff/admin-only CRUD) + StationTag (staff/admin-only CRUD).
Every mutation must produce a DB audit row containing "via API token".

Batch B regression tests are appended below the original suite.
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


# ---------------------------------------------------------------------------
# Batch B regression tests
# ---------------------------------------------------------------------------


def test_station_tag_reserved_slug_returns_400(api_topology, bearer):
    """B1: POST a StationTag with slug='__unassigned__' → 400 (field error on
    slug), no row created, no 500.  DRF ModelSerializer does NOT call
    model.clean(), so the reserved-slug check must live in the serializer.
    """
    from apps.stations.models import StationTag

    t = api_topology
    url = reverse("api:station-tag-list")
    resp = bearer(t["staff"]).post(
        url,
        {"name": "Rollout Sentinel", "slug": "__unassigned__"},
        format="json",
    )
    assert resp.status_code == 400, resp.data
    assert "slug" in resp.data, f"Expected field error on 'slug', got: {resp.data}"
    assert not StationTag.objects.filter(slug="__unassigned__").exists()


def test_station_tag_create_nonstaff_gets_403_before_validation(api_topology, bearer):
    """B5: non-staff POSTing to StationTag gets 403 from has_permission,
    BEFORE any serializer/uniqueness validation runs.  A duplicate-name POST
    by a non-staff caller must return 403 not 400.
    """
    t = api_topology
    # Create the tag as staff first so a 2nd POST would normally trigger
    # a uniqueness 400 if validation ran before the authz check.
    from apps.stations.models import StationTag

    StationTag.objects.create(name="Existing", slug="existing")
    url = reverse("api:station-tag-list")
    # region_mgr is non-staff; duplicate slug would 400 if validation ran first
    resp = bearer(t["region_mgr"]).post(
        url, {"name": "Existing", "slug": "existing"}, format="json"
    )
    assert resp.status_code == 403, resp.data
    # station_user is also non-staff
    resp = bearer(t["station_user"]).post(url, {"name": "New", "slug": "new"}, format="json")
    assert resp.status_code == 403, resp.data


def test_region_create_nonstaff_gets_403_before_validation(api_topology, bearer):
    """B5: non-staff POSTing to Region gets 403 from has_permission,
    BEFORE any serializer/uniqueness validation runs.
    """

    t = api_topology
    # 'in' and 'out' already exist in api_topology; a duplicate would 400 if
    # validation ran before authz.
    url = reverse("api:region-list")
    resp = bearer(t["region_mgr"]).post(url, {"name": "In", "slug": "in"}, format="json")
    assert resp.status_code == 403, resp.data
    resp = bearer(t["station_user"]).post(
        url, {"name": "NewRegion", "slug": "newregion"}, format="json"
    )
    assert resp.status_code == 403, resp.data
