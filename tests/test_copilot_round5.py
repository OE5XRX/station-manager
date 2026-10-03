"""Regression tests for Copilot round-5 findings (R5a/R5b/R5c/R5d).

R5a — rollouts/signals.py: cascade-renumber signal doesn't lock the parent sequence
R5b — write_views.py: RegionAssignment create gate in perform_create (info-leak before 403)
R5c — write_views.py: ProvisioningJob create gate in perform_create (info-leak before 403)
R5d — write_views.py: rollout entry tag-uniqueness IntegrityError → 500 instead of 400/409
"""

import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_or_create_sequence(key="r5-test"):
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
# R5a — cascade-renumber signal acquires parent lock
# ---------------------------------------------------------------------------


class TestR5aSignalLock:
    """The post_delete renumber signal on RolloutSequenceEntry must:
    - acquire a SELECT FOR UPDATE lock on the parent sequence before renumbering
    - skip gracefully (no error) if the parent sequence is gone
    - still produce gap-free positions after a tag cascade-delete
    """

    def test_signal_renumbers_after_tag_delete_with_lock(self, api_topology, bearer):
        """StationTag delete cascades RolloutSequenceEntry → signal renumbers gap-free.

        This is the lock-guarded path (R5a extends R4b). The result is the same
        as R4b (gap-free positions, updated_at bumped) — this confirms the lock
        path is exercised without raising.
        """
        import datetime

        from apps.rollouts.models import RolloutSequenceEntry
        from apps.stations.models import StationTag

        seq = _get_or_create_sequence("r5a-lock-tag-delete")
        t0 = StationTag.objects.create(name="r5a-t0", slug="r5a-t0")
        t1 = StationTag.objects.create(name="r5a-t1", slug="r5a-t1")
        t2 = StationTag.objects.create(name="r5a-t2", slug="r5a-t2")

        RolloutSequenceEntry.objects.create(sequence=seq, tag=t0, position=0)
        e1 = RolloutSequenceEntry.objects.create(sequence=seq, tag=t1, position=1)
        RolloutSequenceEntry.objects.create(sequence=seq, tag=t2, position=2)

        before = datetime.datetime.now(datetime.UTC)

        # Delete the middle tag via the API (staff-only)
        url = reverse("api:station-tag-detail", args=[t1.pk])
        r = bearer(api_topology["staff"]).delete(url)
        assert r.status_code == 204, f"Expected 204, got {r.status_code}: {r.data}"

        # Entry must be gone (cascade)
        assert not RolloutSequenceEntry.objects.filter(pk=e1.pk).exists()

        # Remaining positions must be gap-free
        positions = list(seq.entries.order_by("position").values_list("position", flat=True))
        assert positions == list(range(len(positions))), (
            f"Positions must be gap-free after tag cascade-delete, got: {positions}"
        )
        assert len(positions) == 2

        # updated_at bumped
        seq.refresh_from_db()
        assert seq.updated_at >= before

    def test_signal_skips_gracefully_when_parent_gone(self):
        """If the parent sequence pk is None (detached instance), signal skips without error."""
        from apps.rollouts.models import RolloutSequenceEntry
        from apps.rollouts.signals import _on_sequence_entry_delete

        seq = _get_or_create_sequence("r5a-no-parent")
        t0 = _make_tag("r5a-no-parent-t0")
        entry = RolloutSequenceEntry.objects.create(sequence=seq, tag=t0, position=0)

        # Simulate a detached instance (as if parent was also being deleted in cascade)
        entry.sequence_id = None
        entry.sequence = None

        # Should not raise
        _on_sequence_entry_delete(sender=RolloutSequenceEntry, instance=entry)

    def test_signal_renumbers_via_orm_delete_gap_free(self):
        """ORM tag.delete() on the middle of 3 entries → gap-free [0,1] via locked signal."""
        from apps.rollouts.models import RolloutSequenceEntry
        from apps.stations.models import StationTag

        seq = _get_or_create_sequence("r5a-orm-gap")
        t0 = StationTag.objects.create(name="r5a-orm-t0", slug="r5a-orm-t0")
        t1 = StationTag.objects.create(name="r5a-orm-t1", slug="r5a-orm-t1")
        t2 = StationTag.objects.create(name="r5a-orm-t2", slug="r5a-orm-t2")
        RolloutSequenceEntry.objects.create(sequence=seq, tag=t0, position=0)
        RolloutSequenceEntry.objects.create(sequence=seq, tag=t1, position=1)
        RolloutSequenceEntry.objects.create(sequence=seq, tag=t2, position=2)

        t1.delete()

        positions = list(seq.entries.order_by("position").values_list("position", flat=True))
        assert positions == list(range(len(positions))), (
            f"ORM delete must produce gap-free positions via locked signal, got: {positions}"
        )

    def test_signal_lock_uses_select_for_update(self, monkeypatch):
        """The signal's renumber path calls select_for_update() on the parent sequence.

        We verify behaviorally by monkeypatching the QuerySet.select_for_update
        call path to confirm it is invoked during a signal-driven renumber.
        """
        from apps.rollouts.models import RolloutSequence, RolloutSequenceEntry
        from apps.stations.models import StationTag

        lock_called = []

        original_sfu = RolloutSequence.objects.select_for_update

        def tracking_sfu(*args, **kwargs):
            lock_called.append(True)
            return original_sfu(*args, **kwargs)

        monkeypatch.setattr(
            RolloutSequence.objects.__class__,
            "select_for_update",
            lambda self_, *a, **kw: tracking_sfu(*a, **kw),
        )

        seq = _get_or_create_sequence("r5a-sfu-track")
        t0 = StationTag.objects.create(name="r5a-sfu-t0", slug="r5a-sfu-t0")
        t1 = StationTag.objects.create(name="r5a-sfu-t1", slug="r5a-sfu-t1")
        RolloutSequenceEntry.objects.create(sequence=seq, tag=t0, position=0)
        entry = RolloutSequenceEntry.objects.create(sequence=seq, tag=t1, position=1)

        # Reset after setup DB writes
        lock_called.clear()

        # Trigger the signal via ORM delete of the entry (not the tag)
        entry.delete()

        # The signal must have called select_for_update on the parent
        assert lock_called, "Signal must call select_for_update() on the parent RolloutSequence"


# ---------------------------------------------------------------------------
# R5b — RegionAssignment create gate pre-check (403 before validation)
# ---------------------------------------------------------------------------


class TestR5bRegionAssignmentCreatePreCheck:
    """Non-staff POST to region-assignments must return 403 BEFORE DRF validates
    the payload. This means a POST with a duplicate/invalid body still returns 403,
    not 400/422, when the caller lacks permission.

    FIX: create_requires = staticmethod(ws.can_write_region_assignment) on
    RegionAssignmentViewSet — mirrors RegionViewSet.create_requires.
    """

    def test_region_mgr_post_returns_403_not_400(self, api_topology, bearer):
        """region_mgr POST to region-assignments → 403 (before validation)."""
        t = api_topology
        # Duplicate/invalid body: same user+region that already has an assignment
        # If validation ran first, this would 400; with create_requires it's 403.
        payload = {"user": t["region_mgr"].pk, "region": t["region_in"].pk, "role": "manager"}
        r = bearer(t["region_mgr"]).post(
            reverse("api:region-assignment-list"), payload, format="json"
        )
        assert r.status_code == 403, (
            f"region_mgr must get 403 (not 400/422) on region-assignment create, "
            f"got {r.status_code}: {r.data}"
        )

    def test_station_user_post_returns_403_not_400(self, api_topology, bearer):
        """station_user POST to region-assignments → 403 (before validation)."""
        t = api_topology
        payload = {"user": t["station_user"].pk, "region": t["region_in"].pk, "role": "manager"}
        r = bearer(t["station_user"]).post(
            reverse("api:region-assignment-list"), payload, format="json"
        )
        assert r.status_code == 403, (
            f"station_user must get 403 on region-assignment create, got {r.status_code}: {r.data}"
        )

    def test_non_staff_with_invalid_body_gets_403_not_400(self, api_topology, bearer):
        """403 must come before serializer validation: send a body that would fail
        uniqueness checks — non-staff still gets 403, not 400."""
        t = api_topology
        # This assignment already exists (created in api_topology setup)
        # => a duplicate POST would normally 400; non-staff must 403 first
        payload = {
            "user": t["region_mgr"].pk,
            "region": t["region_in"].pk,
            "role": "manager",
        }
        r = bearer(t["region_mgr"]).post(
            reverse("api:region-assignment-list"), payload, format="json"
        )
        assert r.status_code == 403

    def test_staff_can_still_create_region_assignment(self, api_topology, bearer):
        """Staff POST to region-assignments still returns 201 (pre-check passes)."""
        t = api_topology
        payload = {"user": t["staff"].pk, "region": t["region_in"].pk, "role": "manager"}
        r = bearer(t["staff"]).post(reverse("api:region-assignment-list"), payload, format="json")
        assert r.status_code == 201, (
            f"Staff must still be able to create region-assignment, got {r.status_code}: {r.data}"
        )

    def test_admin_can_still_create_region_assignment(self, api_topology, bearer):
        """Admin POST to region-assignments still returns 201."""
        t = api_topology
        payload = {"user": t["region_mgr"].pk, "region": t["region_out"].pk, "role": "manager"}
        r = bearer(t["admin"]).post(reverse("api:region-assignment-list"), payload, format="json")
        assert r.status_code == 201, (
            f"Admin must still be able to create region-assignment, got {r.status_code}: {r.data}"
        )


# ---------------------------------------------------------------------------
# R5c — ProvisioningJob create gate pre-check (403 before validation)
# ---------------------------------------------------------------------------


class TestR5cProvisioningJobCreatePreCheck:
    """Non-staff POST to provisioning-jobs must return 403 BEFORE DRF validates
    the payload (info-leak fix: same class as R5b but for ProvisioningJobViewSet).

    FIX: add can_trigger_provisioning_role(user) 1-arg helper in write_scoping +
    create_requires = staticmethod(ws.can_trigger_provisioning_role) on
    ProvisioningJobViewSet.
    """

    def test_region_mgr_post_returns_403_not_400(self, api_topology, bearer, image_release):
        """region_mgr POST to provisioning-jobs → 403 (before validation)."""
        t = api_topology
        payload = {"station": t["station_in"].pk, "image_release": image_release.pk}
        r = bearer(t["region_mgr"]).post(
            reverse("api:provisioning-job-list"), payload, format="json"
        )
        assert r.status_code == 403, (
            f"region_mgr must get 403 on provisioning-job create, got {r.status_code}: {r.data}"
        )

    def test_station_user_post_returns_403_not_400(self, api_topology, bearer, image_release):
        """station_user POST to provisioning-jobs → 403 (before validation)."""
        t = api_topology
        payload = {"station": t["station_in"].pk, "image_release": image_release.pk}
        r = bearer(t["station_user"]).post(
            reverse("api:provisioning-job-list"), payload, format="json"
        )
        assert r.status_code == 403, (
            f"station_user must get 403 on provisioning-job create, got {r.status_code}: {r.data}"
        )

    def test_non_staff_with_invalid_station_gets_403_not_400(
        self, api_topology, bearer, image_release
    ):
        """403 before validation: non-staff with invalid body still gets 403 not 400."""
        t = api_topology
        # station_out is out of scope for station_user — serializer validation would
        # fail on station FK visibility, but the pre-check must fire first.
        payload = {"station": t["station_out"].pk, "image_release": image_release.pk}
        r = bearer(t["station_user"]).post(
            reverse("api:provisioning-job-list"), payload, format="json"
        )
        assert r.status_code == 403

    def test_staff_can_still_trigger_provisioning(self, api_topology, bearer, image_release):
        """Staff POST to provisioning-jobs still returns 201."""
        from apps.provisioning.models import ProvisioningJob

        t = api_topology
        payload = {"station": t["station_in"].pk, "image_release": image_release.pk}
        r = bearer(t["staff"]).post(reverse("api:provisioning-job-list"), payload, format="json")
        assert r.status_code == 201, (
            f"Staff must still trigger provisioning-job 201, got {r.status_code}: {r.data}"
        )
        assert ProvisioningJob.objects.filter(station=t["station_in"]).exists()

    def test_admin_can_still_trigger_provisioning(self, api_topology, bearer, image_release):
        """Admin POST to provisioning-jobs still returns 201."""
        t = api_topology
        payload = {"station": t["station_in"].pk, "image_release": image_release.pk}
        r = bearer(t["admin"]).post(reverse("api:provisioning-job-list"), payload, format="json")
        assert r.status_code == 201, (
            f"Admin must still trigger provisioning-job 201, got {r.status_code}: {r.data}"
        )


# ---------------------------------------------------------------------------
# R5d — rollout entry tag-uniqueness IntegrityError → 400/409 (not 500)
# ---------------------------------------------------------------------------


class TestR5dEntryTagIntegrityError400:
    """A concurrent duplicate tag-entry write that bypasses serializer validation
    and hits the DB unique constraint must return 400 (or 409), never 500.

    FIX: wrap add_entry and the perform_update save path in IntegrityError catch →
    DRFValidationError 400 (field "tag", "already in this sequence").
    """

    def test_forced_integrity_error_on_create_returns_400_not_500(
        self, api_topology, bearer, monkeypatch
    ):
        """Force IntegrityError from add_entry → API must return 400, not 500."""
        from django.db import IntegrityError

        from apps.rollouts import services as rollout_services

        def _raising_add_entry(*args, **kwargs):
            raise IntegrityError(
                "UNIQUE constraint failed: "
                "rollouts_rolloutsequenceentry.sequence_id, "
                "rollouts_rolloutsequenceentry.tag_id"
            )

        monkeypatch.setattr(rollout_services, "add_entry", _raising_add_entry)

        seq = _get_or_create_sequence("r5d-create-ie")
        tag = _make_tag("r5d-create-tag")
        url = reverse("api:rollout-sequence-entry-list")
        # position is required by the serializer (the service ignores it, but DRF validates it)
        r = bearer(api_topology["region_mgr"]).post(
            url, {"sequence": seq.pk, "tag": tag.pk, "position": 0}, format="json"
        )
        assert r.status_code in (400, 409), (
            f"IntegrityError on create must produce 400/409, got {r.status_code}: "
            f"{r.data if hasattr(r, 'data') else '(no data)'}"
        )

    def test_forced_integrity_error_on_update_returns_400_not_500(
        self, api_topology, bearer, monkeypatch
    ):
        """Force IntegrityError during entry PATCH (tag-only) → API must return 400/409, not 500.

        We monkeypatch the serializer's update path to raise IntegrityError
        before the DB write, simulating the race where two concurrent PATCHes
        both pass validate_tag but one hits the DB unique constraint first.
        """
        from django.db import IntegrityError

        from apps.rollouts.models import RolloutSequenceEntry

        seq = _get_or_create_sequence("r5d-update-ie")
        tag_a = _make_tag("r5d-upd-tag-a")
        tag_new = _make_tag("r5d-upd-tag-new")  # not in sequence — passes validate_tag

        entry_a = RolloutSequenceEntry.objects.create(sequence=seq, tag=tag_a, position=0)

        # Monkeypatch ModelSerializer.update (parent) to raise IntegrityError
        # to simulate the race: validate_tag passes, but the DB write collides.
        from rest_framework.serializers import ModelSerializer

        def _raising_update(self_, instance, validated_data):
            raise IntegrityError(
                "UNIQUE constraint failed: "
                "rollouts_rolloutsequenceentry.sequence_id, "
                "rollouts_rolloutsequenceentry.tag_id"
            )

        monkeypatch.setattr(ModelSerializer, "update", _raising_update)

        url = reverse("api:rollout-sequence-entry-detail", args=[entry_a.pk])
        r = bearer(api_topology["region_mgr"]).patch(url, {"tag": tag_new.pk}, format="json")
        assert r.status_code in (400, 409), (
            f"IntegrityError on update must produce 400/409, got {r.status_code}: "
            f"{r.data if hasattr(r, 'data') else '(no data)'}"
        )

    def test_normal_duplicate_tag_still_returns_400_via_serializer(self, api_topology, bearer):
        """Normal duplicate tag (not a race) still returns 400 via serializer fast-path."""
        from apps.rollouts.models import RolloutSequenceEntry

        seq = _get_or_create_sequence("r5d-dup-400")
        tag = _make_tag("r5d-dup-tag")
        RolloutSequenceEntry.objects.create(sequence=seq, tag=tag, position=0)

        url = reverse("api:rollout-sequence-entry-list")
        # position is required by the serializer
        r = bearer(api_topology["region_mgr"]).post(
            url, {"sequence": seq.pk, "tag": tag.pk, "position": 0}, format="json"
        )
        # The serializer (validate_tag) or add_entry's None-return path should 400
        assert r.status_code == 400, (
            f"Normal duplicate tag must still return 400, got {r.status_code}: {r.data}"
        )

    def test_integrity_error_response_has_tag_field_or_detail(
        self, api_topology, bearer, monkeypatch
    ):
        """The 400/409 response body from IntegrityError must contain 'tag' or 'detail'."""
        from django.db import IntegrityError

        from apps.rollouts import services as rollout_services

        def _raising_add_entry(*args, **kwargs):
            raise IntegrityError("UNIQUE constraint failed: sequence_id, tag_id")

        monkeypatch.setattr(rollout_services, "add_entry", _raising_add_entry)

        seq = _get_or_create_sequence("r5d-ie-body")
        tag = _make_tag("r5d-ie-body-tag")
        url = reverse("api:rollout-sequence-entry-list")
        # position is required by the serializer
        r = bearer(api_topology["region_mgr"]).post(
            url, {"sequence": seq.pk, "tag": tag.pk, "position": 0}, format="json"
        )
        assert r.status_code in (400, 409)
        # Must have a field-level 'tag' error or a 'detail'
        assert "tag" in r.data or "detail" in r.data or "non_field_errors" in r.data, (
            f"Response must contain 'tag', 'detail', or 'non_field_errors', got: {r.data}"
        )
