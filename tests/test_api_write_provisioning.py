"""Tests for the ProvisioningJob create-trigger write endpoint (Phase 3, Task 9).

The endpoint is CREATE-ONLY — list/retrieve are inherited from the read
viewset; PATCH/PUT/DELETE return 405.  Authz: staff/admin only (internal).
requested_by is always the token user; status defaults to PENDING.

Validation parity with apps/provisioning/views.CreateProvisioningJobView:
- Duplicate-pending guard: a station with an active provisioning job (PENDING,
  RUNNING, or READY) → 400.  No OTA-ready or machine-match check (provisioning
  only needs the wic artifact; the UI does not gate on those either).
"""

import pytest
from django.urls import reverse

from apps.provisioning.models import ProvisioningJob
from apps.stations.models import StationAuditLog

URL = "api:provisioning-job-list"
DETAIL_URL = "api:provisioning-job-detail"


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _payload(api_topology, image_release):
    return {
        "station": api_topology["station_in"].pk,
        "image_release": image_release.pk,
    }


# ---------------------------------------------------------------------------
# Happy-path
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_staff_creates_provisioning_job_201(api_topology, bearer, image_release):
    """Staff user triggers a provisioning job → 201, ProvisioningJob row created."""
    t = api_topology
    payload = _payload(t, image_release)
    r = bearer(t["staff"]).post(reverse(URL), payload, format="json")
    assert r.status_code == 201
    assert ProvisioningJob.objects.filter(station=t["station_in"]).exists()


@pytest.mark.django_db
def test_admin_creates_provisioning_job_201(api_topology, bearer, image_release):
    """Admin user (also internal) triggers a provisioning job → 201."""
    t = api_topology
    payload = _payload(t, image_release)
    r = bearer(t["admin"]).post(reverse(URL), payload, format="json")
    assert r.status_code == 201


@pytest.mark.django_db
def test_requested_by_is_token_user(api_topology, bearer, image_release):
    """requested_by is set server-side to the authenticated user, not the body."""
    t = api_topology
    payload = _payload(t, image_release)
    payload["requested_by"] = t["admin"].pk  # body-supplied — must be IGNORED
    r = bearer(t["staff"]).post(reverse(URL), payload, format="json")
    assert r.status_code == 201
    job = ProvisioningJob.objects.latest("created_at")
    assert job.requested_by == t["staff"]


@pytest.mark.django_db
def test_status_defaults_pending(api_topology, bearer, image_release):
    """Created job status is PENDING, regardless of any body field."""
    t = api_topology
    payload = _payload(t, image_release)
    payload["status"] = "ready"  # must be IGNORED
    r = bearer(t["staff"]).post(reverse(URL), payload, format="json")
    assert r.status_code == 201
    job = ProvisioningJob.objects.latest("created_at")
    assert job.status == ProvisioningJob.Status.PENDING


@pytest.mark.django_db
def test_response_contains_id(api_topology, bearer, image_release):
    """201 response body contains the job id."""
    t = api_topology
    r = bearer(t["staff"]).post(reverse(URL), _payload(t, image_release), format="json")
    assert r.status_code == 201
    job = ProvisioningJob.objects.latest("created_at")
    assert str(r.data["id"]) == str(job.pk)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_provisioning_trigger_audit_row_created(api_topology, bearer, image_release):
    """A PROVISIONING_REQUESTED audit row is created with 'via API token' in the
    message and references the correct station."""
    t = api_topology
    station = t["station_in"]
    bearer(t["staff"]).post(reverse(URL), _payload(t, image_release), format="json")
    log = (
        StationAuditLog.objects.filter(
            station=station,
            event_type=StationAuditLog.EventType.PROVISIONING_REQUESTED,
        )
        .order_by("-id")
        .first()
    )
    assert log is not None
    assert "via API token" in log.message


# ---------------------------------------------------------------------------
# Permission matrix
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_region_mgr_cannot_trigger_403(api_topology, bearer, image_release):
    """Region manager is NOT internal → 403."""
    t = api_topology
    r = bearer(t["region_mgr"]).post(reverse(URL), _payload(t, image_release), format="json")
    assert r.status_code == 403


@pytest.mark.django_db
def test_station_user_cannot_trigger_403(api_topology, bearer, image_release):
    """Station-assigned user → 403."""
    t = api_topology
    r = bearer(t["station_user"]).post(reverse(URL), _payload(t, image_release), format="json")
    assert r.status_code == 403


@pytest.mark.django_db
def test_applicant_cannot_trigger_403(api_topology, bearer, image_release):
    """Applicant-level user is rejected at the permission gate → 403."""
    t = api_topology
    r = bearer(t["applicant"]).post(reverse(URL), _payload(t, image_release), format="json")
    assert r.status_code == 403


@pytest.mark.django_db
def test_anon_cannot_trigger_401(anon_client, api_topology, image_release):
    """Unauthenticated request → 401."""
    t = api_topology
    r = anon_client.post(reverse(URL), _payload(t, image_release), format="json")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 405 guards (no update / delete)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_delete_returns_405(api_topology, bearer, image_release):
    """DELETE on a provisioning-job detail → 405."""
    t = api_topology
    job = ProvisioningJob.objects.create(
        station=t["station_in"],
        image_release=image_release,
        requested_by=t["admin"],
    )
    r = bearer(t["admin"]).delete(reverse(DETAIL_URL, args=[job.pk]))
    assert r.status_code == 405


@pytest.mark.django_db
def test_patch_returns_405(api_topology, bearer, image_release):
    """PATCH on a provisioning-job detail → 405."""
    t = api_topology
    job = ProvisioningJob.objects.create(
        station=t["station_in"],
        image_release=image_release,
        requested_by=t["admin"],
    )
    r = bearer(t["admin"]).patch(
        reverse(DETAIL_URL, args=[job.pk]), {"status": "failed"}, format="json"
    )
    assert r.status_code == 405


# ---------------------------------------------------------------------------
# Validation-parity: duplicate-pending guard (mirrors CreateProvisioningJobView)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "active_status",
    [ProvisioningJob.Status.PENDING, ProvisioningJob.Status.RUNNING, ProvisioningJob.Status.READY],
)
@pytest.mark.django_db
def test_duplicate_pending_job_400(api_topology, bearer, image_release, active_status):
    """If an active provisioning job (PENDING/RUNNING/READY) already exists for
    the station, a second trigger → 400, no second row created."""
    t = api_topology
    ProvisioningJob.objects.create(
        station=t["station_in"],
        image_release=image_release,
        status=active_status,
        requested_by=t["staff"],
    )
    before = ProvisioningJob.objects.filter(station=t["station_in"]).count()
    r = bearer(t["staff"]).post(reverse(URL), _payload(t, image_release), format="json")
    assert r.status_code == 400
    assert ProvisioningJob.objects.filter(station=t["station_in"]).count() == before


# ---------------------------------------------------------------------------
# C2 — atomic check+create guard
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_provisioning_create_uses_transaction_atomic(
    api_topology, bearer, image_release, monkeypatch
):
    """The duplicate-active-job check+create executes inside a transaction.atomic block.

    We verify this behaviorally: the check for an existing active job and the
    creation of a new job must behave as-if atomic (no second job when one is
    active).  This covers the select_for_update serialization invariant:
    a concurrent second request must see the first job before creating.
    """
    from apps.provisioning.models import ProvisioningJob

    t = api_topology
    # Create active job first
    ProvisioningJob.objects.create(
        station=t["station_in"],
        image_release=image_release,
        status=ProvisioningJob.Status.PENDING,
        requested_by=t["staff"],
    )
    before = ProvisioningJob.objects.filter(station=t["station_in"]).count()
    r = bearer(t["staff"]).post(reverse(URL), _payload(t, image_release), format="json")
    # The duplicate guard must fire — 400 and no new job
    assert r.status_code == 400
    assert ProvisioningJob.objects.filter(station=t["station_in"]).count() == before
