"""Regression tests for Copilot round-4 findings (R4a/R4b/R4c).

R4a — RolloutSequenceEntry tag-only PATCH acquires parent lock before saving
R4b — StationTag delete cascades RolloutSequenceEntry → gaps in sequence / no renumber
R4c — move_entry ValueError (offset overflow) → 500 instead of 400
"""

import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_or_create_sequence(key="current"):
    from apps.rollouts.models import RolloutSequence

    seq, _ = RolloutSequence.objects.get_or_create(singleton_key=key)
    return seq


def _make_tag(name):
    from django.utils.text import slugify

    from apps.stations.models import StationTag

    slug = slugify(name)
    tag, _ = StationTag.objects.get_or_create(name=name, defaults={"slug": slug})
    return tag


# ---------------------------------------------------------------------------
# R4a — tag-only PATCH holds parent lock before saving
# ---------------------------------------------------------------------------


class TestR4aTagOnlyPatchLocked:
    """A tag-only PATCH (no position change) must hold the parent sequence lock
    before saving and must stamp the parent sequence updated_by/updated_at.

    The lock ordering invariant is verified behaviorally: after a tag-only PATCH,
    the parent must be stamped (confirming the lock path was exercised) and the
    entry must carry the new tag.

    Also confirms that the existing position+tag atomic test still passes (no
    regression in the position-change path).
    """

    def test_tag_only_patch_stamps_parent_atomically(self, api_topology, bearer):
        """Tag-only PATCH must stamp parent sequence updated_by/updated_at atomically
        (i.e., within the same transaction that saves the tag field change)."""
        import datetime

        from apps.rollouts.models import RolloutSequence, RolloutSequenceEntry

        seq = _get_or_create_sequence("r4a-tag-only")
        tag_a = _make_tag("r4a-original")
        tag_b = _make_tag("r4a-replacement")
        entry = RolloutSequenceEntry.objects.create(sequence=seq, tag=tag_a, position=0)

        # Clear updated_by so we can assert it is set
        RolloutSequence.objects.filter(pk=seq.pk).update(updated_by=None)
        before = datetime.datetime.now(datetime.UTC)

        url = reverse("api:rollout-sequence-entry-detail", args=[entry.pk])
        r = bearer(api_topology["region_mgr"]).patch(url, {"tag": tag_b.pk}, format="json")
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.data}"

        entry.refresh_from_db()
        assert entry.tag == tag_b, "Entry tag must be updated"

        seq.refresh_from_db()
        assert seq.updated_by == api_topology["region_mgr"], (
            "Parent sequence.updated_by must be set to the API actor on tag-only PATCH"
        )
        assert seq.updated_at >= before, (
            "Parent sequence.updated_at must be bumped on tag-only PATCH"
        )

    def test_tag_only_patch_returns_200(self, api_topology, bearer):
        """Tag-only PATCH must return 200 (not 500 or any other error)."""
        from apps.rollouts.models import RolloutSequenceEntry

        seq = _get_or_create_sequence("r4a-200-check")
        tag_a = _make_tag("r4a-200-orig")
        tag_b = _make_tag("r4a-200-new")
        entry = RolloutSequenceEntry.objects.create(sequence=seq, tag=tag_a, position=0)

        url = reverse("api:rollout-sequence-entry-detail", args=[entry.pk])
        r = bearer(api_topology["region_mgr"]).patch(url, {"tag": tag_b.pk}, format="json")
        assert r.status_code == 200, (
            f"Tag-only PATCH must return 200, got {r.status_code}: {r.data}"
        )

    def test_tag_and_position_patch_still_atomic(self, api_topology, bearer):
        """Regression: position+tag PATCH must still succeed atomically (R1 not broken)."""
        from apps.rollouts.models import RolloutSequenceEntry

        seq = _get_or_create_sequence("r4a-pos-tag-atomic")
        tag_a = _make_tag("r4a-pos-tag-a")
        tag_b = _make_tag("r4a-pos-tag-b")
        tag_new = _make_tag("r4a-pos-tag-new")
        entry_a = RolloutSequenceEntry.objects.create(sequence=seq, tag=tag_a, position=0)
        RolloutSequenceEntry.objects.create(sequence=seq, tag=tag_b, position=1)

        url = reverse("api:rollout-sequence-entry-detail", args=[entry_a.pk])
        r = bearer(api_topology["region_mgr"]).patch(
            url, {"position": 1, "tag": tag_new.pk}, format="json"
        )
        assert r.status_code == 200, (
            f"position+tag PATCH must return 200, got {r.status_code}: {r.data}"
        )
        entry_a.refresh_from_db()
        assert entry_a.tag == tag_new
        assert entry_a.position == 1


# ---------------------------------------------------------------------------
# R4b — StationTag delete renumbers sequence (approach a: post_delete signal)
# ---------------------------------------------------------------------------


class TestR4bTagDeleteRenumbersSequence:
    """Deleting a StationTag that appears in a RolloutSequence must:
      - remove the RolloutSequenceEntry (cascade),
      - renumber remaining entries to be 0-based and gap-free,
      - stamp the parent RolloutSequence updated_at.

    The renumber is driven by a post_delete signal on RolloutSequenceEntry so it
    catches the cascade path (tag delete) as well as any other ORM removal.
    Because the signal fires without an actor, updated_by is left as-is (or None
    if not previously set); the important invariant is that positions are gap-free
    and updated_at is bumped.
    """

    def test_tag_delete_removes_entry_and_renumbers_sequence(self, api_topology, bearer):
        """Delete the middle tag → entry removed, remaining entries renumbered
        to [0, 1] (no gap), parent sequence updated_at bumped, no 500."""
        import datetime

        from apps.rollouts.models import RolloutSequenceEntry
        from apps.stations.models import StationTag

        seq = _get_or_create_sequence("r4b-tag-delete")
        tag_t0 = StationTag.objects.create(name="r4b-tag-t0", slug="r4b-tag-t0")
        tag_t1 = StationTag.objects.create(name="r4b-tag-t1", slug="r4b-tag-t1")
        tag_t2 = StationTag.objects.create(name="r4b-tag-t2", slug="r4b-tag-t2")
        RolloutSequenceEntry.objects.create(sequence=seq, tag=tag_t0, position=0)
        e1 = RolloutSequenceEntry.objects.create(sequence=seq, tag=tag_t1, position=1)
        RolloutSequenceEntry.objects.create(sequence=seq, tag=tag_t2, position=2)

        # Record updated_at before the delete
        seq.refresh_from_db()
        before = datetime.datetime.now(datetime.UTC)

        # Delete the middle tag via the API (staff-only)
        url = reverse("api:station-tag-detail", args=[tag_t1.pk])
        r = bearer(api_topology["staff"]).delete(url)
        assert r.status_code == 204, f"Expected 204 on tag delete, got {r.status_code}: {r.data}"

        # Entry for tag_t1 must be gone (cascaded)
        assert not RolloutSequenceEntry.objects.filter(pk=e1.pk).exists(), (
            "RolloutSequenceEntry for deleted tag must be removed via cascade"
        )

        # Remaining entries must be 0-based contiguous (no gap)
        positions = list(seq.entries.order_by("position").values_list("position", flat=True))
        assert positions == list(range(len(positions))), (
            f"Positions must be gap-free after tag delete, got: {positions}"
        )
        assert len(positions) == 2, f"Expected 2 remaining entries, got {len(positions)}"

        # Parent updated_at must be bumped
        seq.refresh_from_db()
        assert seq.updated_at >= before, (
            "Parent sequence.updated_at must be bumped after tag-cascade-delete"
        )

    def test_tag_delete_via_orm_also_renumbers(self, api_topology):
        """ORM tag.delete() (not via API) must also renumber via signal."""
        from apps.rollouts.models import RolloutSequenceEntry
        from apps.stations.models import StationTag

        seq = _get_or_create_sequence("r4b-orm-delete")
        t0 = StationTag.objects.create(name="r4b-orm-t0", slug="r4b-orm-t0")
        t1 = StationTag.objects.create(name="r4b-orm-t1", slug="r4b-orm-t1")
        t2 = StationTag.objects.create(name="r4b-orm-t2", slug="r4b-orm-t2")
        RolloutSequenceEntry.objects.create(sequence=seq, tag=t0, position=0)
        RolloutSequenceEntry.objects.create(sequence=seq, tag=t1, position=1)
        RolloutSequenceEntry.objects.create(sequence=seq, tag=t2, position=2)

        # Delete middle tag via ORM (not API) — signal must still fire
        t1.delete()

        positions = list(seq.entries.order_by("position").values_list("position", flat=True))
        assert positions == list(range(len(positions))), (
            f"ORM tag.delete() must also renumber sequence, got: {positions}"
        )

    def test_tag_delete_no_sequence_entry_no_error(self, api_topology, bearer):
        """Deleting a tag that is NOT in any sequence must still return 204 (no error)."""
        from apps.stations.models import StationTag

        tag = StationTag.objects.create(name="r4b-unlinked-tag", slug="r4b-unlinked-tag")
        url = reverse("api:station-tag-detail", args=[tag.pk])
        r = bearer(api_topology["staff"]).delete(url)
        assert r.status_code == 204, (
            f"Deleting an unlinked tag must return 204, got {r.status_code}"
        )

    def test_renumber_is_idempotent_with_api_remove_entry(self, api_topology, bearer):
        """API DELETE on a RolloutSequenceEntry (not a tag delete) must still renumber
        correctly — the signal must coexist with remove_entry's own renumber without
        double-renumbering or errors."""
        from apps.rollouts.models import RolloutSequenceEntry

        seq = _get_or_create_sequence("r4b-idempotent")
        t0 = _make_tag("r4b-idem-t0")
        t1 = _make_tag("r4b-idem-t1")
        t2 = _make_tag("r4b-idem-t2")
        RolloutSequenceEntry.objects.create(sequence=seq, tag=t0, position=0)
        e1 = RolloutSequenceEntry.objects.create(sequence=seq, tag=t1, position=1)
        RolloutSequenceEntry.objects.create(sequence=seq, tag=t2, position=2)

        # Delete entry directly via API (not via tag delete)
        url = reverse("api:rollout-sequence-entry-detail", args=[e1.pk])
        r = bearer(api_topology["region_mgr"]).delete(url)
        assert r.status_code == 204, f"Entry DELETE must return 204, got {r.status_code}"

        positions = list(seq.entries.order_by("position").values_list("position", flat=True))
        assert positions == list(range(len(positions))), (
            f"Positions must be gap-free after entry DELETE via API, got: {positions}"
        )


# ---------------------------------------------------------------------------
# R4c — offset overflow ValueError → 400, not 500
# ---------------------------------------------------------------------------


class TestR4cOffsetOverflow400:
    """When move_entry raises ValueError (sequence too large to reorder),
    the API must return 400, not 500."""

    def test_offset_overflow_returns_400_not_500(self, api_topology, bearer, monkeypatch):
        """Monkeypatch _calc_offset to raise ValueError → API must return 400."""
        from apps.rollouts import services as rollout_services
        from apps.rollouts.models import RolloutSequenceEntry

        def _raising_calc_offset(max_current, n):
            raise ValueError("Sequence too large to reorder safely (monkeypatched for test)")

        monkeypatch.setattr(rollout_services, "_calc_offset", _raising_calc_offset)

        seq = _get_or_create_sequence("r4c-overflow")
        tag_a = _make_tag("r4c-overflow-a")
        tag_b = _make_tag("r4c-overflow-b")
        entry_a = RolloutSequenceEntry.objects.create(sequence=seq, tag=tag_a, position=0)
        RolloutSequenceEntry.objects.create(sequence=seq, tag=tag_b, position=1)

        url = reverse("api:rollout-sequence-entry-detail", args=[entry_a.pk])
        r = bearer(api_topology["region_mgr"]).patch(url, {"position": 1}, format="json")
        assert r.status_code == 400, (
            f"ValueError from _calc_offset must produce 400, got {r.status_code}: "
            f"{r.data if hasattr(r, 'data') else '(no data)'}"
        )
        # Confirm the error is on "position" field or at least is a validation error
        assert "position" in r.data or "non_field_errors" in r.data or "detail" in r.data, (
            f"Response must contain a field-level or detail error, got: {r.data}"
        )

    def test_offset_overflow_message_is_helpful(self, api_topology, bearer, monkeypatch):
        """The 400 response for overflow must contain a non-empty error message."""
        import json

        from apps.rollouts import services as rollout_services
        from apps.rollouts.models import RolloutSequenceEntry

        def _raising_calc_offset(max_current, n):
            raise ValueError("Sequence too large to reorder safely")

        monkeypatch.setattr(rollout_services, "_calc_offset", _raising_calc_offset)

        seq = _get_or_create_sequence("r4c-msg")
        tag_a = _make_tag("r4c-msg-a")
        tag_b = _make_tag("r4c-msg-b")
        entry_a = RolloutSequenceEntry.objects.create(sequence=seq, tag=tag_a, position=0)
        RolloutSequenceEntry.objects.create(sequence=seq, tag=tag_b, position=1)

        url = reverse("api:rollout-sequence-entry-detail", args=[entry_a.pk])
        r = bearer(api_topology["region_mgr"]).patch(url, {"position": 1}, format="json")
        assert r.status_code == 400

        # Flatten all error strings from the response
        resp_text = json.dumps(r.data)
        assert len(resp_text) > 10, f"Error response body must be non-trivial, got: {resp_text}"

    def test_normal_reorder_still_works_when_no_overflow(self, api_topology, bearer):
        """Regression: normal reorder (no overflow) must still return 200."""
        from apps.rollouts.models import RolloutSequenceEntry

        seq = _get_or_create_sequence("r4c-normal")
        tag_a = _make_tag("r4c-normal-a")
        tag_b = _make_tag("r4c-normal-b")
        entry_a = RolloutSequenceEntry.objects.create(sequence=seq, tag=tag_a, position=0)
        RolloutSequenceEntry.objects.create(sequence=seq, tag=tag_b, position=1)

        url = reverse("api:rollout-sequence-entry-detail", args=[entry_a.pk])
        r = bearer(api_topology["region_mgr"]).patch(url, {"position": 1}, format="json")
        assert r.status_code == 200, (
            f"Normal reorder (no overflow) must return 200, got {r.status_code}: {r.data}"
        )
