"""Regression tests for Copilot round-4 findings (R4a/R4b/R4c).

R4a — RolloutSequenceEntry tag-only PATCH acquires parent lock before saving
R4b — RolloutGroup delete cascades RolloutSequenceEntry → gaps in sequence / no renumber
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


def _make_group(name):
    from django.utils.text import slugify

    from apps.stations.models import RolloutGroup

    slug = slugify(name)
    group, _ = RolloutGroup.objects.get_or_create(name=name, defaults={"slug": slug})
    return group


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
        tag_a = _make_group("r4a-original")
        tag_b = _make_group("r4a-replacement")
        entry = RolloutSequenceEntry.objects.create(sequence=seq, group=tag_a, position=0)

        # Clear updated_by so we can assert it is set
        RolloutSequence.objects.filter(pk=seq.pk).update(updated_by=None)
        before = datetime.datetime.now(datetime.UTC)

        url = reverse("api:rollout-sequence-entry-detail", args=[entry.pk])
        r = bearer(api_topology["admin"]).patch(url, {"group": tag_b.pk}, format="json")
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.data}"

        entry.refresh_from_db()
        assert entry.group == tag_b, "Entry tag must be updated"

        seq.refresh_from_db()
        assert seq.updated_by == api_topology["admin"], (
            "Parent sequence.updated_by must be set to the API actor on tag-only PATCH"
        )
        assert seq.updated_at >= before, (
            "Parent sequence.updated_at must be bumped on tag-only PATCH"
        )

    def test_tag_only_patch_returns_200(self, api_topology, bearer):
        """Tag-only PATCH must return 200 (not 500 or any other error)."""
        from apps.rollouts.models import RolloutSequenceEntry

        seq = _get_or_create_sequence("r4a-200-check")
        tag_a = _make_group("r4a-200-orig")
        tag_b = _make_group("r4a-200-new")
        entry = RolloutSequenceEntry.objects.create(sequence=seq, group=tag_a, position=0)

        url = reverse("api:rollout-sequence-entry-detail", args=[entry.pk])
        r = bearer(api_topology["admin"]).patch(url, {"group": tag_b.pk}, format="json")
        assert r.status_code == 200, (
            f"Tag-only PATCH must return 200, got {r.status_code}: {r.data}"
        )

    def test_tag_and_position_patch_still_atomic(self, api_topology, bearer):
        """Regression: position+tag PATCH must still succeed atomically (R1 not broken)."""
        from apps.rollouts.models import RolloutSequenceEntry

        seq = _get_or_create_sequence("r4a-pos-tag-atomic")
        tag_a = _make_group("r4a-pos-tag-a")
        tag_b = _make_group("r4a-pos-tag-b")
        tag_new = _make_group("r4a-pos-tag-new")
        entry_a = RolloutSequenceEntry.objects.create(sequence=seq, group=tag_a, position=0)
        RolloutSequenceEntry.objects.create(sequence=seq, group=tag_b, position=1)

        url = reverse("api:rollout-sequence-entry-detail", args=[entry_a.pk])
        r = bearer(api_topology["admin"]).patch(
            url, {"position": 1, "group": tag_new.pk}, format="json"
        )
        assert r.status_code == 200, (
            f"position+tag PATCH must return 200, got {r.status_code}: {r.data}"
        )
        entry_a.refresh_from_db()
        assert entry_a.group == tag_new
        assert entry_a.position == 1


# ---------------------------------------------------------------------------
# R4b — sequence entries stay gap-free (post_delete signal); a group in the
# sequence can't be deleted (PROTECT)
# ---------------------------------------------------------------------------


class TestR4bTagDeleteRenumbersSequence:
    """Removing a RolloutSequenceEntry by any ORM path renumbers the remaining
    entries 0-based and gap-free via the post_delete signal. Deleting a group
    that still has an entry is blocked (PROTECT) — the user decision is that a
    group must be removed from the sequence first.
    """

    def test_group_delete_blocked_while_in_sequence(self, api_topology, bearer):
        """A group in the sequence is PROTECTed: API delete → 409, entries untouched."""
        from apps.rollouts.models import RolloutSequenceEntry
        from apps.stations.models import RolloutGroup

        seq = _get_or_create_sequence("r4b-tag-delete")
        groups = [
            RolloutGroup.objects.create(name=f"r4b-tag-t{i}", slug=f"r4b-tag-t{i}")
            for i in range(3)
        ]
        for i, g in enumerate(groups):
            RolloutSequenceEntry.objects.create(sequence=seq, group=g, position=i)

        url = reverse("api:rollout-group-detail", args=[groups[1].pk])
        r = bearer(api_topology["staff"]).delete(url)
        assert r.status_code == 409, r.content
        assert "sequence_entries=1" in r.data["detail"]
        assert RolloutGroup.objects.filter(pk=groups[1].pk).exists()
        assert list(seq.entries.order_by("position").values_list("position", flat=True)) == [
            0,
            1,
            2,
        ]

    def test_entry_delete_via_orm_also_renumbers(self, api_topology):
        """ORM entry.delete() (not via the service) must also renumber via signal."""
        from apps.rollouts.models import RolloutSequenceEntry
        from apps.stations.models import RolloutGroup

        seq = _get_or_create_sequence("r4b-orm-delete")
        t0 = RolloutGroup.objects.create(name="r4b-orm-t0", slug="r4b-orm-t0")
        t1 = RolloutGroup.objects.create(name="r4b-orm-t1", slug="r4b-orm-t1")
        t2 = RolloutGroup.objects.create(name="r4b-orm-t2", slug="r4b-orm-t2")
        RolloutSequenceEntry.objects.create(sequence=seq, group=t0, position=0)
        e1 = RolloutSequenceEntry.objects.create(sequence=seq, group=t1, position=1)
        RolloutSequenceEntry.objects.create(sequence=seq, group=t2, position=2)

        # Delete the middle entry via ORM (not the service) — signal must still fire
        e1.delete()

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
        t0 = _make_group("r4b-idem-t0")
        t1 = _make_group("r4b-idem-t1")
        t2 = _make_group("r4b-idem-t2")
        RolloutSequenceEntry.objects.create(sequence=seq, group=t0, position=0)
        e1 = RolloutSequenceEntry.objects.create(sequence=seq, group=t1, position=1)
        RolloutSequenceEntry.objects.create(sequence=seq, group=t2, position=2)

        # Delete entry directly via API (not via tag delete)
        url = reverse("api:rollout-sequence-entry-detail", args=[e1.pk])
        r = bearer(api_topology["admin"]).delete(url)
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
        tag_a = _make_group("r4c-overflow-a")
        tag_b = _make_group("r4c-overflow-b")
        entry_a = RolloutSequenceEntry.objects.create(sequence=seq, group=tag_a, position=0)
        RolloutSequenceEntry.objects.create(sequence=seq, group=tag_b, position=1)

        url = reverse("api:rollout-sequence-entry-detail", args=[entry_a.pk])
        r = bearer(api_topology["admin"]).patch(url, {"position": 1}, format="json")
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
        tag_a = _make_group("r4c-msg-a")
        tag_b = _make_group("r4c-msg-b")
        entry_a = RolloutSequenceEntry.objects.create(sequence=seq, group=tag_a, position=0)
        RolloutSequenceEntry.objects.create(sequence=seq, group=tag_b, position=1)

        url = reverse("api:rollout-sequence-entry-detail", args=[entry_a.pk])
        r = bearer(api_topology["admin"]).patch(url, {"position": 1}, format="json")
        assert r.status_code == 400

        # Flatten all error strings from the response
        resp_text = json.dumps(r.data)
        assert len(resp_text) > 10, f"Error response body must be non-trivial, got: {resp_text}"

    def test_normal_reorder_still_works_when_no_overflow(self, api_topology, bearer):
        """Regression: normal reorder (no overflow) must still return 200."""
        from apps.rollouts.models import RolloutSequenceEntry

        seq = _get_or_create_sequence("r4c-normal")
        tag_a = _make_group("r4c-normal-a")
        tag_b = _make_group("r4c-normal-b")
        entry_a = RolloutSequenceEntry.objects.create(sequence=seq, group=tag_a, position=0)
        RolloutSequenceEntry.objects.create(sequence=seq, group=tag_b, position=1)

        url = reverse("api:rollout-sequence-entry-detail", args=[entry_a.pk])
        r = bearer(api_topology["admin"]).patch(url, {"position": 1}, format="json")
        assert r.status_code == 200, (
            f"Normal reorder (no overflow) must return 200, got {r.status_code}: {r.data}"
        )
