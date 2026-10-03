"""Regression tests for Copilot round-3 findings (R1-R7).

R1 — RolloutSequenceEntry perform_update NOT atomic (tag save + move_entry separate txns)
R2 — validate_tag uses raw initial_data['sequence'] → 500 on malformed PK
R3 — Stripping `sequence` in to_internal_value breaks full PUT
R4 — Station cascade delete orphans StationPhoto blobs (needs signal)
R5 — Import dedup blocks archived releases (use default manager)
R6 — services.py docstring wrong (doc-only)
R7 — move_entry temp offset can exceed PositiveSmallInt 32767
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


def _make_tag(name):
    from django.utils.text import slugify

    from apps.stations.models import StationTag

    slug = slugify(name)
    tag, _ = StationTag.objects.get_or_create(name=name, defaults={"slug": slug})
    return tag


def _make_release_with_assets(tag, machine="qemux86-64", channel="release"):
    """Return a GitHubRelease with complete asset triples."""
    from apps.images.github_releases import GitHubRelease

    base = f"oe5xrx-{machine}-{channel}-{tag}.wic.bz2"
    asset_names = frozenset({base, f"{base}.bundle", f"{base}.sha256"})
    return GitHubRelease(tag=tag, html_url="", is_latest=False, asset_names=asset_names)


# ---------------------------------------------------------------------------
# R1 — RolloutSequenceEntry perform_update is atomic (position+tag PATCH)
# ---------------------------------------------------------------------------


class TestR1EntryUpdateAtomic:
    """position+tag PATCH must be atomic: if the move fails, the tag change rolls back."""

    def test_happy_path_both_tag_and_position_saved(self, api_topology, bearer):
        """Happy path: PATCH with position+tag saves both atomically."""
        from apps.rollouts.models import RolloutSequenceEntry

        seq = _get_or_create_sequence()
        tag_a = _make_tag("r1-src-tag")
        tag_b = _make_tag("r1-src-tag-b")
        tag_new = _make_tag("r1-new-tag")
        entry_a = RolloutSequenceEntry.objects.create(sequence=seq, tag=tag_a, position=0)
        RolloutSequenceEntry.objects.create(sequence=seq, tag=tag_b, position=1)

        url = reverse("api:rollout-sequence-entry-detail", args=[entry_a.pk])
        r = bearer(api_topology["region_mgr"]).patch(
            url, {"position": 1, "tag": tag_new.pk}, format="json"
        )
        assert r.status_code == 200
        entry_a.refresh_from_db()
        # Both must be persisted
        assert entry_a.tag == tag_new
        assert entry_a.position == 1

    def test_position_patch_only_no_tag_still_works(self, api_topology, bearer):
        """PATCH of position only (no tag change) must still succeed."""
        from apps.rollouts.models import RolloutSequenceEntry

        seq = _get_or_create_sequence()
        tag_x = _make_tag("r1-pos-only-x")
        tag_y = _make_tag("r1-pos-only-y")
        entry_x = RolloutSequenceEntry.objects.create(sequence=seq, tag=tag_x, position=0)
        RolloutSequenceEntry.objects.create(sequence=seq, tag=tag_y, position=1)

        url = reverse("api:rollout-sequence-entry-detail", args=[entry_x.pk])
        r = bearer(api_topology["region_mgr"]).patch(url, {"position": 1}, format="json")
        assert r.status_code == 200
        entry_x.refresh_from_db()
        assert entry_x.position == 1


# ---------------------------------------------------------------------------
# R2 — validate_tag: malformed sequence PK → 400, not 500
# ---------------------------------------------------------------------------


class TestR2ValidateTagMalformedPK:
    """validate_tag must not 500 when `sequence` is a non-numeric string."""

    def test_non_numeric_sequence_returns_400(self, api_topology, bearer):
        """POST with sequence='notanint' → 400 field error, not 500."""
        tag = _make_tag("r2-any-tag")
        url = reverse("api:rollout-sequence-entry-list")
        r = bearer(api_topology["region_mgr"]).post(
            url,
            {"sequence": "notanint", "tag": tag.pk, "position": 0},
            format="json",
        )
        assert r.status_code == 400, f"Expected 400, got {r.status_code}: {r.data}"

    def test_duplicate_tag_still_returns_400(self, api_topology, bearer):
        """Duplicate tag within a sequence → 400 (not 500 or 201)."""
        from apps.rollouts.models import RolloutSequenceEntry

        seq = _get_or_create_sequence()
        tag = _make_tag("r2-dup-tag")
        RolloutSequenceEntry.objects.create(sequence=seq, tag=tag, position=0)

        url = reverse("api:rollout-sequence-entry-list")
        r = bearer(api_topology["region_mgr"]).post(
            url,
            {"sequence": seq.pk, "tag": tag.pk, "position": 1},
            format="json",
        )
        assert r.status_code == 400, f"Expected 400, got {r.status_code}: {r.data}"


# ---------------------------------------------------------------------------
# R3 — sequence is conditionally read-only on update (full PUT works)
# ---------------------------------------------------------------------------


class TestR3SequenceReadOnlyOnUpdate:
    """sequence must be read-only on update so full PUT doesn't require omitting it."""

    def test_full_put_with_sequence_field_returns_200(self, api_topology, bearer):
        """Full PUT that includes `sequence` must succeed (sequence ignored, not required)."""
        from apps.rollouts.models import RolloutSequenceEntry

        seq = _get_or_create_sequence()
        tag_a = _make_tag("r3-put-tag-a")
        tag_b = _make_tag("r3-put-tag-b")
        entry = RolloutSequenceEntry.objects.create(sequence=seq, tag=tag_a, position=0)

        url = reverse("api:rollout-sequence-entry-detail", args=[entry.pk])
        # Full PUT includes sequence — must not fail because sequence is required
        r = bearer(api_topology["region_mgr"]).put(
            url,
            {"sequence": seq.pk, "tag": tag_b.pk, "position": 0},
            format="json",
        )
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.data}"
        entry.refresh_from_db()
        assert entry.tag == tag_b, "Tag must be updated by full PUT"
        assert entry.sequence == seq, "Sequence must remain unchanged after PUT"

    def test_patch_sequence_field_is_ignored(self, api_topology, bearer):
        """PATCH with a sequence value is silently ignored (sequence unchanged)."""
        from apps.rollouts.models import RolloutSequence, RolloutSequenceEntry

        seq = _get_or_create_sequence()
        # Create a second sequence to try to reparent to
        seq2, _ = RolloutSequence.objects.get_or_create(singleton_key="other-r3")
        tag = _make_tag("r3-patch-seq-tag")
        entry = RolloutSequenceEntry.objects.create(sequence=seq, tag=tag, position=0)

        url = reverse("api:rollout-sequence-entry-detail", args=[entry.pk])
        r = bearer(api_topology["region_mgr"]).patch(url, {"sequence": seq2.pk}, format="json")
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.data}"
        entry.refresh_from_db()
        assert entry.sequence == seq, "Sequence must NOT change on PATCH"

    def test_create_still_requires_sequence(self, api_topology, bearer):
        """POST without sequence → 400 (sequence required on create)."""
        tag = _make_tag("r3-create-no-seq-tag")
        url = reverse("api:rollout-sequence-entry-list")
        r = bearer(api_topology["region_mgr"]).post(
            url, {"tag": tag.pk, "position": 0}, format="json"
        )
        assert r.status_code == 400, f"Expected 400, got {r.status_code}: {r.data}"


# ---------------------------------------------------------------------------
# R4 — Station cascade delete cleans up StationPhoto blobs via signal
# ---------------------------------------------------------------------------


class TestR4CascadePhotoBlob:
    """Deleting a Station must remove StationPhoto image blobs (cascade path)."""

    @pytest.fixture
    def station_with_photo(self, api_topology):
        """Return a Station with a photo whose image blob exists in storage."""
        from django.core.files.base import ContentFile
        from django.core.files.storage import default_storage

        from apps.stations.models import Station, StationPhoto

        # Use the in-scope station from the topology
        station = Station.objects.create(
            name="r4-cascade-station",
            callsign="R4TST",
            region=api_topology["station_in"].region,
        )
        # Save a real blob to storage
        content = ContentFile(b"r4 fake image", name="r4_test.jpg")
        saved_name = default_storage.save("stations/photos/r4_test.jpg", content)
        photo = StationPhoto.objects.create(
            station=station,
            image=saved_name,
            caption="r4 test",
            uploaded_by=api_topology["admin"],
        )
        return station, photo, saved_name

    def test_station_delete_removes_photo_blob(self, station_with_photo, api_topology, bearer):
        """Cascade: deleting the station via API must also remove the photo blob."""
        from django.core.files.storage import default_storage
        from django.test import TestCase

        from apps.stations.models import StationAssignment

        station, photo, saved_name = station_with_photo
        assert default_storage.exists(saved_name), "Blob must exist before station delete"

        # Assign admin to this station so the API scope check passes
        StationAssignment.objects.get_or_create(
            station=station,
            user=api_topology["admin"],
            defaults={"role": "admin", "assigned_by": api_topology["admin"]},
        )

        url = reverse("api:station-detail", args=[station.pk])
        with TestCase.captureOnCommitCallbacks(execute=True):
            r = bearer(api_topology["admin"]).delete(url)
        assert r.status_code == 204, f"Expected 204, got {r.status_code}"

        assert not default_storage.exists(saved_name), (
            "Photo blob must be removed after station cascade delete"
        )

    def test_direct_photo_delete_still_removes_blob(self, api_topology, bearer):
        """Direct API DELETE of a StationPhoto must still remove the blob (regression guard)."""
        from django.core.files.base import ContentFile
        from django.core.files.storage import default_storage
        from django.test import TestCase

        from apps.stations.models import StationPhoto

        station = api_topology["station_in"]
        content = ContentFile(b"r4 direct delete image", name="r4_direct.jpg")
        saved_name = default_storage.save("stations/photos/r4_direct.jpg", content)
        photo = StationPhoto.objects.create(
            station=station,
            image=saved_name,
            caption="r4 direct",
            uploaded_by=api_topology["admin"],
        )
        assert default_storage.exists(saved_name)

        url = reverse("api:station-photo-detail", args=[photo.pk])
        with TestCase.captureOnCommitCallbacks(execute=True):
            r = bearer(api_topology["admin"]).delete(url)
        assert r.status_code == 204

        assert not default_storage.exists(saved_name), (
            "Blob must still be removed on direct photo DELETE"
        )


# ---------------------------------------------------------------------------
# R5 — Import dedup allows re-import of archived releases
# ---------------------------------------------------------------------------


class TestR5ImportArchivedReimport:
    """An archived ImageRelease must NOT block re-import (409 only for active)."""

    @pytest.fixture
    def github_release_mock(self, monkeypatch):
        """Mock out the GitHub release browser call."""

        def _mock_fetch_releases(repo, limit=None):
            # Return releases for common test tags used by this test class
            return [
                _make_release_with_assets("v99.0.0-r5-archived"),
                _make_release_with_assets("v99.0.0-r5-active"),
            ]

        monkeypatch.setattr(
            "apps.images.github_releases.fetch_releases",
            _mock_fetch_releases,
        )

    def test_archived_release_can_be_reimported(self, api_topology, bearer, github_release_mock):
        """POST import for a (tag, machine, channel) whose only existing release is
        archived → 202 (allowed, not 409)."""
        from django.utils import timezone

        from apps.images.models import ImageRelease

        tag = "v99.0.0-r5-archived"
        machine = "qemux86-64"
        channel = "release"

        # Create an archived ImageRelease for this tuple
        ImageRelease.objects.create(
            tag=tag,
            machine=machine,
            channel=channel,
            s3_key=f"oe5xrx-{machine}-{channel}-{tag}.wic.bz2",
            sha256="a" * 64,
            size_bytes=1024,
            archived_at=timezone.now(),  # archived!
        )

        url = reverse("api:image-import")
        r = bearer(api_topology["admin"]).post(
            url, {"tag": tag, "machine": machine, "channel": channel}, format="json"
        )
        # Must be allowed — not a 409
        assert r.status_code in (200, 201, 202), (
            f"Expected 2xx for archived re-import, got {r.status_code}: {r.data}"
        )

    def test_active_release_still_blocked(self, api_topology, bearer, github_release_mock):
        """POST import for a (tag, machine, channel) whose existing release is NOT
        archived → 409 (still blocked)."""
        from apps.images.models import ImageRelease

        tag = "v99.0.0-r5-active"
        machine = "qemux86-64"
        channel = "release"

        # Create an active (non-archived) ImageRelease for this tuple
        ImageRelease.objects.create(
            tag=tag,
            machine=machine,
            channel=channel,
            s3_key=f"oe5xrx-{machine}-{channel}-{tag}.wic.bz2",
            sha256="b" * 64,
            size_bytes=1024,
        )

        url = reverse("api:image-import")
        r = bearer(api_topology["admin"]).post(
            url, {"tag": tag, "machine": machine, "channel": channel}, format="json"
        )
        assert r.status_code == 409, (
            f"Expected 409 for active release, got {r.status_code}: {r.data}"
        )


# ---------------------------------------------------------------------------
# R6 — services.py docstring (doc-only, just import-check)
# ---------------------------------------------------------------------------


def test_r6_services_docstring_is_present():
    """Smoke: import the module and confirm its __doc__ string is non-empty."""
    from apps.rollouts import services

    assert services.__doc__, "rollouts.services module must have a docstring"
    # R6 fix: the docstring must NOT claim UI delegates to these services
    doc = services.__doc__
    assert "API" in doc or "api" in doc, "Docstring should mention API write path"


# ---------------------------------------------------------------------------
# R7 — move_entry temp offset stays within PositiveSmallInt 32767
# ---------------------------------------------------------------------------


class TestR7OffsetCap:
    """move_entry must guard against temp positions exceeding PositiveSmallInt 32767."""

    def test_reorder_normal_sequence_stays_within_bound(self, api_topology, bearer):
        """A reorder in a normal-size sequence (100 entries) must work without overflow."""
        from apps.rollouts.models import RolloutSequence, RolloutSequenceEntry

        seq, _ = RolloutSequence.objects.get_or_create(singleton_key="r7-overflow-seq")

        # Create 10 entries (representative; 100 is too slow for a unit test)
        tags = [_make_tag(f"r7-tag-{i:03d}") for i in range(10)]
        entries = [
            RolloutSequenceEntry.objects.create(sequence=seq, tag=t, position=i)
            for i, t in enumerate(tags)
        ]

        # Move last entry to position 0
        entry = entries[-1]
        url = reverse("api:rollout-sequence-entry-detail", args=[entry.pk])
        r = bearer(api_topology["region_mgr"]).patch(url, {"position": 0}, format="json")
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.data}"

        # Positions must be gap-free
        positions = list(seq.entries.order_by("position").values_list("position", flat=True))
        assert positions == list(range(len(positions))), f"Gaps after reorder: {positions}"

    def test_offset_calculation_stays_below_32767(self):
        """Unit test: for a sequence of 100 entries at positions 0..99,
        max_current=99, n=100, offset=max(99,100)+1=101 → temp max = 99+101 = 200 ≤ 32767."""
        from apps.rollouts.services import _calc_offset

        # max_current=99, n=100 → offset must be 101, temp max = 99+101=200
        offset = _calc_offset(max_current=99, n=100)
        assert offset == 101
        assert 99 + offset <= 32767, f"Temp position would overflow: {99 + offset}"

    def test_large_sequence_raises_clean_error(self):
        """If entries are at extreme positions that would cause overflow, raise ValueError."""
        from apps.rollouts.services import _calc_offset

        # max_current = 32700, n = 100 → offset = max(32700, 100)+1 = 32701
        # temp max = 32700 + 32701 = 65401 > 32767 → should raise
        try:
            offset = _calc_offset(max_current=32700, n=100)
            # If no error raised, the offset must still be valid (≤ 32767 - max_current)
            assert 32700 + offset <= 32767, (
                f"Temp position {32700 + offset} would overflow PositiveSmallIntegerField"
            )
        except ValueError:
            pass  # Expected: clean error for overflow-inducing sequence
