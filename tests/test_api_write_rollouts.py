"""Permission-matrix + audit tests for RolloutSequence and RolloutSequenceEntry write endpoints.

Task 6: region-mgr/staff full CRUD.  Every mutation produces a CONFIG_CHANGED
AccountAuditLog row with "via API token" in the message.

Design notes:
- RolloutSequence is a singleton (singleton_key has a unique constraint) so
  we use get_or_create for the sequence itself and POST on entries to exercise
  the create permission matrix.
- updated_by is server-side: the request body is ignored; the authenticated
  user is set by perform_create/perform_update.
"""

import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_or_create_sequence():
    from apps.rollouts.models import RolloutSequence

    seq, _ = RolloutSequence.objects.get_or_create(singleton_key="current")
    return seq


def _make_tag(name=None):
    from django.utils.text import slugify

    from apps.stations.models import StationTag

    name = name or "test-tag"
    slug = slugify(name)
    tag, _ = StationTag.objects.get_or_create(name=name, defaults={"slug": slug})
    return tag


# ---------------------------------------------------------------------------
# RolloutSequence — PATCH (update) permission matrix
# (No meaningful POST: singleton constraint; use PATCH on the existing row.)
# ---------------------------------------------------------------------------


def test_sequence_update_region_mgr_allowed(api_topology, bearer):
    seq = _get_or_create_sequence()
    url = reverse("api:rollout-sequence-detail", args=[seq.pk])
    resp = bearer(api_topology["region_mgr"]).patch(url, {}, format="json")
    assert resp.status_code == 200


def test_sequence_update_staff_allowed(api_topology, bearer):
    seq = _get_or_create_sequence()
    url = reverse("api:rollout-sequence-detail", args=[seq.pk])
    resp = bearer(api_topology["staff"]).patch(url, {}, format="json")
    assert resp.status_code == 200


def test_sequence_update_admin_allowed(api_topology, bearer):
    seq = _get_or_create_sequence()
    url = reverse("api:rollout-sequence-detail", args=[seq.pk])
    resp = bearer(api_topology["admin"]).patch(url, {}, format="json")
    assert resp.status_code == 200


def test_sequence_update_station_user_forbidden(api_topology, bearer):
    seq = _get_or_create_sequence()
    url = reverse("api:rollout-sequence-detail", args=[seq.pk])
    resp = bearer(api_topology["station_user"]).patch(url, {}, format="json")
    assert resp.status_code == 403


def test_sequence_update_applicant_forbidden(api_topology, bearer):
    seq = _get_or_create_sequence()
    url = reverse("api:rollout-sequence-detail", args=[seq.pk])
    resp = bearer(api_topology["applicant"]).patch(url, {}, format="json")
    assert resp.status_code == 403


def test_sequence_update_anon_unauthorized(api_topology, anon_client):
    seq = _get_or_create_sequence()
    url = reverse("api:rollout-sequence-detail", args=[seq.pk])
    resp = anon_client.patch(url, {}, format="json")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# RolloutSequenceEntry — POST (create) permission matrix
# ---------------------------------------------------------------------------


def test_entry_create_region_mgr_allowed(api_topology, bearer):
    seq = _get_or_create_sequence()
    tag = _make_tag("alpha")
    url = reverse("api:rollout-sequence-entry-list")
    resp = bearer(api_topology["region_mgr"]).post(
        url, {"sequence": seq.pk, "tag": tag.pk, "position": 10}, format="json"
    )
    assert resp.status_code == 201


def test_entry_create_staff_allowed(api_topology, bearer):
    seq = _get_or_create_sequence()
    tag = _make_tag("beta")
    url = reverse("api:rollout-sequence-entry-list")
    resp = bearer(api_topology["staff"]).post(
        url, {"sequence": seq.pk, "tag": tag.pk, "position": 20}, format="json"
    )
    assert resp.status_code == 201


def test_entry_create_admin_allowed(api_topology, bearer):
    seq = _get_or_create_sequence()
    tag = _make_tag("gamma")
    url = reverse("api:rollout-sequence-entry-list")
    resp = bearer(api_topology["admin"]).post(
        url, {"sequence": seq.pk, "tag": tag.pk, "position": 30}, format="json"
    )
    assert resp.status_code == 201


def test_entry_create_station_user_forbidden(api_topology, bearer):
    seq = _get_or_create_sequence()
    tag = _make_tag("delta")
    url = reverse("api:rollout-sequence-entry-list")
    resp = bearer(api_topology["station_user"]).post(
        url, {"sequence": seq.pk, "tag": tag.pk, "position": 40}, format="json"
    )
    assert resp.status_code == 403


def test_entry_create_applicant_forbidden(api_topology, bearer):
    seq = _get_or_create_sequence()
    tag = _make_tag("epsilon")
    url = reverse("api:rollout-sequence-entry-list")
    resp = bearer(api_topology["applicant"]).post(
        url, {"sequence": seq.pk, "tag": tag.pk, "position": 50}, format="json"
    )
    assert resp.status_code == 403


def test_entry_create_anon_unauthorized(api_topology, anon_client):
    seq = _get_or_create_sequence()
    tag = _make_tag("zeta")
    url = reverse("api:rollout-sequence-entry-list")
    resp = anon_client.post(
        url, {"sequence": seq.pk, "tag": tag.pk, "position": 60}, format="json"
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# RolloutSequenceEntry — DELETE permission matrix
# ---------------------------------------------------------------------------


def test_entry_delete_region_mgr_allowed(api_topology, bearer):
    from apps.rollouts.models import RolloutSequenceEntry

    seq = _get_or_create_sequence()
    tag = _make_tag("to-delete-rmgr")
    entry = RolloutSequenceEntry.objects.create(sequence=seq, tag=tag, position=100)
    url = reverse("api:rollout-sequence-entry-detail", args=[entry.pk])
    resp = bearer(api_topology["region_mgr"]).delete(url)
    assert resp.status_code == 204


def test_entry_delete_station_user_forbidden(api_topology, bearer):
    from apps.rollouts.models import RolloutSequenceEntry

    seq = _get_or_create_sequence()
    tag = _make_tag("to-delete-suser")
    entry = RolloutSequenceEntry.objects.create(sequence=seq, tag=tag, position=200)
    url = reverse("api:rollout-sequence-entry-detail", args=[entry.pk])
    resp = bearer(api_topology["station_user"]).delete(url)
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# updated_by is server-side (body-supplied value is ignored)
# ---------------------------------------------------------------------------


def test_sequence_updated_by_is_server_side(api_topology, bearer):
    """Even if the client sends an updated_by value, the server must set it
    to the authenticated user."""
    from apps.accounts.models import User

    seq = _get_or_create_sequence()
    url = reverse("api:rollout-sequence-detail", args=[seq.pk])
    other_user = User.objects.create_user(username="other_rollout", password="x")
    resp = bearer(api_topology["region_mgr"]).patch(
        url, {"updated_by": other_user.pk}, format="json"
    )
    assert resp.status_code == 200
    seq.refresh_from_db()
    assert seq.updated_by == api_topology["region_mgr"]


# ---------------------------------------------------------------------------
# Audit: write → CONFIG_CHANGED AccountAuditLog row with "via API token"
# ---------------------------------------------------------------------------


def test_sequence_update_produces_audit_row(api_topology, bearer):
    from apps.accounts.models import AccountAuditLog

    seq = _get_or_create_sequence()
    url = reverse("api:rollout-sequence-detail", args=[seq.pk])
    bearer(api_topology["region_mgr"]).patch(url, {}, format="json")
    log = AccountAuditLog.objects.filter(
        event_type=AccountAuditLog.EventType.CONFIG_CHANGED,
        message__contains="via API token",
    ).latest("created_at")
    assert "RolloutSequence" in log.message


def test_entry_create_produces_audit_row(api_topology, bearer):
    from apps.accounts.models import AccountAuditLog

    seq = _get_or_create_sequence()
    tag = _make_tag("audit-test-tag")
    url = reverse("api:rollout-sequence-entry-list")
    bearer(api_topology["staff"]).post(
        url, {"sequence": seq.pk, "tag": tag.pk, "position": 5}, format="json"
    )
    log = AccountAuditLog.objects.filter(
        event_type=AccountAuditLog.EventType.CONFIG_CHANGED,
        message__contains="via API token",
    ).latest("created_at")
    assert "RolloutSequenceEntry" in log.message


def test_entry_delete_produces_audit_row(api_topology, bearer):
    from apps.accounts.models import AccountAuditLog
    from apps.rollouts.models import RolloutSequenceEntry

    seq = _get_or_create_sequence()
    tag = _make_tag("audit-delete-tag")
    entry = RolloutSequenceEntry.objects.create(sequence=seq, tag=tag, position=300)
    url = reverse("api:rollout-sequence-entry-detail", args=[entry.pk])
    bearer(api_topology["region_mgr"]).delete(url)
    log = AccountAuditLog.objects.filter(
        event_type=AccountAuditLog.EventType.CONFIG_CHANGED,
        message__contains="via API token",
    ).latest("created_at")
    assert "RolloutSequenceEntry" in log.message


# ---------------------------------------------------------------------------
# C1 — locked sequence protocol invariants
# ---------------------------------------------------------------------------


def test_api_create_entry_at_occupied_position_renumbers(api_topology, bearer):
    """API create at an occupied position must shift rather than 500.

    The service uses add_entry() which appends at max+1 regardless of the
    client-supplied position (position is ignored/server-assigned). The
    result must be gap-free.
    """
    from apps.rollouts.models import RolloutSequenceEntry

    seq = _get_or_create_sequence()
    tag_a = _make_tag("c1-occupied-a")
    tag_b = _make_tag("c1-occupied-b")
    # Pre-populate position 0
    RolloutSequenceEntry.objects.create(sequence=seq, tag=tag_a, position=0)

    url = reverse("api:rollout-sequence-entry-list")
    # POST a new tag — position=0 is occupied; service must not crash
    r = bearer(api_topology["region_mgr"]).post(
        url, {"sequence": seq.pk, "tag": tag_b.pk, "position": 0}, format="json"
    )
    assert r.status_code == 201
    # Positions must be gap-free after create
    positions = list(seq.entries.order_by("position").values_list("position", flat=True))
    assert positions == list(range(len(positions))), f"Gaps in positions: {positions}"


def test_api_delete_entry_renumbers_and_bumps_parent(api_topology, bearer):
    """API delete must renumber remaining entries (no gap) and bump parent
    updated_by / updated_at."""
    from apps.rollouts.models import RolloutSequenceEntry

    seq = _get_or_create_sequence()
    tag_a = _make_tag("c1-del-a")
    tag_b = _make_tag("c1-del-b")
    tag_c = _make_tag("c1-del-c")
    entry_a = RolloutSequenceEntry.objects.create(sequence=seq, tag=tag_a, position=0)
    entry_b = RolloutSequenceEntry.objects.create(sequence=seq, tag=tag_b, position=1)
    _entry_c = RolloutSequenceEntry.objects.create(sequence=seq, tag=tag_c, position=2)

    url = reverse("api:rollout-sequence-entry-detail", args=[entry_b.pk])
    r = bearer(api_topology["region_mgr"]).delete(url)
    assert r.status_code == 204

    # Remaining entries must be 0-based contiguous
    positions = list(seq.entries.order_by("position").values_list("position", flat=True))
    assert positions == list(range(len(positions))), f"Gap after delete: {positions}"

    # Parent updated_by must be the API actor
    seq.refresh_from_db()
    assert seq.updated_by == api_topology["region_mgr"]

    # entry_a is still present (position may have shifted)
    assert seq.entries.filter(pk=entry_a.pk).exists()


def test_api_delete_entry_parent_updated_by_reflects_actor(api_topology, bearer):
    """updated_by on the parent RolloutSequence is set to the API actor on delete."""
    from apps.rollouts.models import RolloutSequenceEntry

    seq = _get_or_create_sequence()
    tag = _make_tag("c1-actor-tag")
    entry = RolloutSequenceEntry.objects.create(sequence=seq, tag=tag, position=0)
    url = reverse("api:rollout-sequence-entry-detail", args=[entry.pk])
    bearer(api_topology["staff"]).delete(url)
    seq.refresh_from_db()
    assert seq.updated_by == api_topology["staff"]


def test_api_create_entry_bumps_parent_updated_by(api_topology, bearer):
    """API create must set parent updated_by to the API actor."""
    seq = _get_or_create_sequence()
    tag = _make_tag("c1-create-bump-tag")
    url = reverse("api:rollout-sequence-entry-list")
    r = bearer(api_topology["region_mgr"]).post(
        url, {"sequence": seq.pk, "tag": tag.pk, "position": 0}, format="json"
    )
    assert r.status_code == 201
    seq.refresh_from_db()
    assert seq.updated_by == api_topology["region_mgr"]


def test_api_reorder_entry_leaves_positions_gap_free(api_topology, bearer):
    """API PATCH (reorder) to an occupied position must resolve via two-phase
    move and leave the sequence gap-free."""
    from apps.rollouts.models import RolloutSequenceEntry

    seq = _get_or_create_sequence()
    tag_a = _make_tag("c1-reorder-a")
    tag_b = _make_tag("c1-reorder-b")
    entry_a = RolloutSequenceEntry.objects.create(sequence=seq, tag=tag_a, position=0)
    entry_b = RolloutSequenceEntry.objects.create(sequence=seq, tag=tag_b, position=1)

    # PATCH entry_b to position=0 (occupied by entry_a) → two-phase move
    url = reverse("api:rollout-sequence-entry-detail", args=[entry_b.pk])
    r = bearer(api_topology["region_mgr"]).patch(url, {"position": 0}, format="json")
    assert r.status_code == 200

    positions = list(seq.entries.order_by("position").values_list("position", flat=True))
    assert positions == list(range(len(positions))), f"Gap after reorder: {positions}"

    # entry_b should now be at position 0
    entry_b.refresh_from_db()
    assert entry_b.position == 0
    # entry_a should have been pushed to position 1
    entry_a.refresh_from_db()
    assert entry_a.position == 1
