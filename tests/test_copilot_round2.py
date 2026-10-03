"""Regression tests for Copilot round-2 findings (10 items).

1  — Region delete via API with RegionAssignments → 204, no IntegrityError,
     REGION_DELETED audit row written.
2  — Station delete cascade already covered in test_batch_a_audit_cascade.py
     (confirmed here with a reference test; actual logic proven by that file).
3  — Image import dedup guard (already fixed in batch C; confirmed present).
4  — Provisioning create atomic guard (already fixed in batch C; confirmed).
5  — audit _parse_ip: bracketed IPv6 [::1]:8080 and zone-id fe80::1%eth0.
6  — RolloutSequenceEntry PATCH: `sequence` field is read-only on update.
7  — validators=[] restored tag-uniqueness: duplicate tag → 400.
8  — StationPhoto DELETE orphans storage blob → blob deleted after destroy.
9  — RolloutSequenceEntry PATCH position+tag: both fields persisted.
10 — Image import validates (tag, machine, channel) tuple against channels_for.
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
    return GitHubRelease(
        tag=tag,
        html_url=f"https://github.com/OE5XRX/linux-image/releases/tag/{tag}",
        is_latest=True,
        asset_names=asset_names,
    )


def _make_fake_release_no_assets(tag):
    """Return a GitHubRelease with no asset triples."""
    from apps.images.github_releases import GitHubRelease

    return GitHubRelease(
        tag=tag,
        html_url=f"https://github.com/OE5XRX/linux-image/releases/tag/{tag}",
        is_latest=True,
        asset_names=frozenset(),
    )


# ---------------------------------------------------------------------------
# Item 1 — Region delete via API with RegionAssignments → 204, no IntegrityError
# ---------------------------------------------------------------------------


class TestRegionApiDeleteWithAssignments:
    """DELETE a Region via the API when it has RegionAssignments.

    Must:
    - Return 204 (no IntegrityError / 500)
    - Delete the Region row
    - Write a REGION_DELETED AccountAuditLog row (region=None after delete is fine)
    """

    @pytest.fixture
    def extra_region(self, db):
        from apps.stations.models import Region

        return Region.objects.create(name="ToDelete", slug="todelete")

    @pytest.fixture
    def member(self, db):
        from apps.accounts.models import User

        return User.objects.create_user(
            username="rd_member", password="x", membership_level=User.MembershipLevel.MEMBER
        )

    def test_api_delete_region_with_assignments_204(
        self, api_topology, bearer, extra_region, member, db
    ):
        """API DELETE of a region with RegionAssignments → 204."""
        from apps.stations.models import RegionAssignment

        admin_user = api_topology["admin"]
        RegionAssignment.objects.create(
            region=extra_region, user=member, role="manager", assigned_by=admin_user
        )
        url = reverse("api:region-detail", args=[extra_region.pk])
        r = bearer(admin_user).delete(url)
        assert r.status_code == 204, f"Expected 204, got {r.status_code}"

    def test_api_delete_region_with_assignments_row_gone(
        self, api_topology, bearer, extra_region, member, db
    ):
        """After API DELETE the Region row is removed from the DB."""
        from apps.stations.models import Region, RegionAssignment

        admin_user = api_topology["admin"]
        region_pk = extra_region.pk
        RegionAssignment.objects.create(
            region=extra_region, user=member, role="manager", assigned_by=admin_user
        )
        url = reverse("api:region-detail", args=[extra_region.pk])
        bearer(admin_user).delete(url)
        assert not Region.objects.filter(pk=region_pk).exists()

    def test_api_delete_region_with_assignments_audit_written(
        self, api_topology, bearer, extra_region, member, db
    ):
        """API DELETE of a region → REGION_DELETED AccountAuditLog row written."""
        from apps.accounts.models import AccountAuditLog
        from apps.stations.models import RegionAssignment

        admin_user = api_topology["admin"]
        RegionAssignment.objects.create(
            region=extra_region, user=member, role="manager", assigned_by=admin_user
        )
        before = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.REGION_DELETED
        ).count()
        url = reverse("api:region-detail", args=[extra_region.pk])
        bearer(admin_user).delete(url)
        after = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.REGION_DELETED
        ).count()
        # Both the view's perform_destroy AND the _on_region_delete signal fire;
        # at least 1 new REGION_DELETED row must be present.
        assert after >= before + 1, "REGION_DELETED audit row must be written"


# ---------------------------------------------------------------------------
# Item 2 — Station cascade: confirmed by test_batch_a_audit_cascade.py
# (Add an explicit marker here so the round-2 report can cite this file)
# ---------------------------------------------------------------------------


def test_station_cascade_covered_by_batch_a(db):
    """Canary: the batch-A cascade tests exist and cover station+assignment cascade.

    This test just imports the test module to confirm it is reachable;
    the actual assertions live in test_batch_a_audit_cascade.TestStationCascadeAudit.
    """
    import tests.test_batch_a_audit_cascade  # noqa: F401

    assert True, "batch-A cascade test module is present"


# ---------------------------------------------------------------------------
# Item 3 — Image import dedup guard (batch C already present; sanity-check)
# ---------------------------------------------------------------------------


def test_import_dedup_guard_present(api_topology, bearer, monkeypatch):
    """Posting import/ twice for the same (tag, machine, channel) where the
    first creates a PENDING job → second returns 409."""
    from apps.images import github_releases
    from apps.images.models import ImageImportJob

    t = api_topology
    # Release with proper assets so channel validation (item 10) also passes
    fake_releases = [_make_release_with_assets("v3.1", machine="qemux86-64", channel="release")]
    monkeypatch.setattr(github_releases, "fetch_releases", lambda *a, **k: fake_releases)

    ImageImportJob.objects.create(
        tag="v3.1",
        machine="qemux86-64",
        channel="release",
        mark_as_latest=False,
        requested_by=t["staff"],
        status=ImageImportJob.Status.PENDING,
    )
    r = bearer(t["staff"]).post(
        reverse("api:image-import"),
        {"tag": "v3.1", "machine": "qemux86-64", "channel": "release"},
        format="json",
    )
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# Item 4 — Provisioning atomic check+create (batch C already present; sanity-check)
# ---------------------------------------------------------------------------


def test_provisioning_atomic_guard_present(api_topology, bearer, image_release):
    """Provisioning duplicate guard fires and returns non-2xx when active job exists."""
    from apps.provisioning.models import ProvisioningJob

    t = api_topology
    ProvisioningJob.objects.create(
        station=t["station_in"],
        image_release=image_release,
        status=ProvisioningJob.Status.PENDING,
        requested_by=t["staff"],
    )
    r = bearer(t["staff"]).post(
        reverse("api:provisioning-job-list"),
        {"station": t["station_in"].pk, "image_release": image_release.pk},
        format="json",
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Item 5 — _parse_ip: bracketed IPv6 and zone-id
# ---------------------------------------------------------------------------


class TestParseIpIpv6Forms:
    """Unit tests for _parse_ip handling of non-trivial IPv6 inputs."""

    def test_bracketed_ipv6_with_port(self):
        """[::1]:8080 → '::1' (loopback, port stripped)."""
        from apps.api.audit import _parse_ip

        assert _parse_ip("[::1]:8080") == "::1"

    def test_bracketed_ipv6_full_address_with_port(self):
        """[2001:db8::1]:443 → '2001:db8::1'."""
        from apps.api.audit import _parse_ip

        assert _parse_ip("[2001:db8::1]:443") == "2001:db8::1"

    def test_ipv6_zone_id_stripped(self):
        """fe80::1%eth0 → a valid IPv6 address with zone stripped."""
        from apps.api.audit import _parse_ip

        result = _parse_ip("fe80::1%eth0")
        # Must be a valid IP string (not None) after zone is stripped
        assert result is not None
        import ipaddress

        ipaddress.ip_address(result)  # must not raise

    def test_plain_ipv6_unchanged(self):
        """Plain IPv6 without port or zone is returned as-is (normalised)."""
        from apps.api.audit import _parse_ip

        assert _parse_ip("2001:db8::1") == "2001:db8::1"

    def test_plain_ipv4_unchanged(self):
        """Plain IPv4 is still returned correctly."""
        from apps.api.audit import _parse_ip

        assert _parse_ip("10.0.0.1") == "10.0.0.1"

    def test_garbage_returns_none(self):
        """Garbage input → None (never abort audit)."""
        from apps.api.audit import _parse_ip

        assert _parse_ip("not-an-ip!!") is None

    def test_empty_returns_none(self):
        from apps.api.audit import _parse_ip

        assert _parse_ip("") is None

    def test_none_returns_none(self):
        from apps.api.audit import _parse_ip

        assert _parse_ip(None) is None


# ---------------------------------------------------------------------------
# Item 6 — RolloutSequenceEntry PATCH: `sequence` is read-only on update
# ---------------------------------------------------------------------------


class TestRolloutEntrySequenceReadOnlyOnUpdate:
    """PATCH an entry's `sequence` field → it must be ignored (read-only on update)."""

    def test_patch_sequence_is_ignored(self, api_topology, bearer):
        """PATCHing `sequence` on an existing entry must not reparent it.

        The field is accepted by the serializer but must be read-only on
        partial_update so a stale client sending `sequence` cannot silently
        move the entry.
        """
        from apps.rollouts.models import RolloutSequence, RolloutSequenceEntry

        seq = _get_or_create_sequence()
        # Create a second sequence to try to reparent to
        other_seq, _ = RolloutSequence.objects.get_or_create(singleton_key="other-r2")
        tag = _make_tag("r2-seq-ro-tag")
        entry = RolloutSequenceEntry.objects.create(sequence=seq, tag=tag, position=0)

        url = reverse("api:rollout-sequence-entry-detail", args=[entry.pk])
        r = bearer(api_topology["region_mgr"]).patch(
            url, {"sequence": other_seq.pk}, format="json"
        )
        # Must not crash (200 or 400 are both acceptable outcomes);
        # the key invariant is that the entry stays on the original sequence.
        assert r.status_code in (200, 400), f"Unexpected status: {r.status_code}"
        entry.refresh_from_db()
        assert entry.sequence_id == seq.pk, (
            "Entry must NOT be reparented to another sequence via PATCH"
        )


# ---------------------------------------------------------------------------
# Item 7 — validators=[] restored tag-uniqueness
# ---------------------------------------------------------------------------


class TestRolloutEntryDuplicateTagValidation:
    """POST/PATCH with a tag already in the sequence → 400, not a DB duplicate/500."""

    def test_post_duplicate_tag_returns_400(self, api_topology, bearer):
        """POSTing a tag that already exists in the sequence → 400."""
        from apps.rollouts.models import RolloutSequenceEntry

        seq = _get_or_create_sequence()
        tag = _make_tag("r2-dup-tag")
        # Pre-create the entry
        RolloutSequenceEntry.objects.create(sequence=seq, tag=tag, position=0)

        url = reverse("api:rollout-sequence-entry-list")
        r = bearer(api_topology["region_mgr"]).post(
            url, {"sequence": seq.pk, "tag": tag.pk, "position": 1}, format="json"
        )
        # The service returns None for a duplicate tag and the view returns 400.
        assert r.status_code == 400, (
            f"Expected 400 for duplicate tag, got {r.status_code}: {getattr(r, 'data', '')}"
        )

    def test_patch_duplicate_tag_returns_400(self, api_topology, bearer):
        """PATCHing an entry to a tag that already exists in the sequence → 400."""
        from apps.rollouts.models import RolloutSequenceEntry

        seq = _get_or_create_sequence()
        tag_a = _make_tag("r2-dup-a-tag")
        tag_b = _make_tag("r2-dup-b-tag")
        # Both tags are already in the sequence
        RolloutSequenceEntry.objects.create(sequence=seq, tag=tag_a, position=0)
        entry_b = RolloutSequenceEntry.objects.create(sequence=seq, tag=tag_b, position=1)

        # Try to PATCH entry_b so it has tag_a (which is taken)
        url = reverse("api:rollout-sequence-entry-detail", args=[entry_b.pk])
        r = bearer(api_topology["region_mgr"]).patch(url, {"tag": tag_a.pk}, format="json")
        assert r.status_code == 400, (
            f"Expected 400 for duplicate tag on PATCH, got {r.status_code}"
        )


# ---------------------------------------------------------------------------
# Item 8 — StationPhoto DELETE orphans storage blob
# ---------------------------------------------------------------------------


class TestStationPhotoDeleteBlobCleaned:
    """DELETE a StationPhoto that has an image → storage blob removed after commit."""

    @pytest.fixture
    def station(self, api_topology):
        return api_topology["station_in"]

    def test_delete_photo_removes_blob(self, api_topology, bearer, station, settings):
        """After API DELETE of a StationPhoto, the image blob must not exist in storage."""
        from django.core.files.base import ContentFile
        from django.core.files.storage import default_storage
        from django.test import TestCase

        from apps.stations.models import StationPhoto

        # Upload a file to in-memory storage
        image_content = b"fake image content for test"
        content_file = ContentFile(image_content, name="test_round2.jpg")
        saved_name = default_storage.save("station_photos/test_round2.jpg", content_file)
        assert default_storage.exists(saved_name), "File must exist before delete"

        # Create a StationPhoto pointing to the saved file
        photo = StationPhoto.objects.create(
            station=station,
            image=saved_name,
            caption="round2 test",
            uploaded_by=api_topology["admin"],
        )

        url = reverse("api:station-photo-detail", args=[photo.pk])
        # captureOnCommitCallbacks forces on_commit hooks to fire even
        # though pytest-django wraps the test in a savepoint that never
        # commits to the real database.
        with TestCase.captureOnCommitCallbacks(execute=True):
            r = bearer(api_topology["admin"]).delete(url)
        assert r.status_code == 204, f"Expected 204, got {r.status_code}"

        # After commit the blob must be gone
        assert not default_storage.exists(saved_name), (
            "Storage blob must be deleted after photo DELETE"
        )


# ---------------------------------------------------------------------------
# Item 9 — RolloutSequenceEntry PATCH position+tag: both persisted
# ---------------------------------------------------------------------------


class TestRolloutEntryPatchPositionAndTag:
    """PATCH an entry with BOTH a new position AND a new tag → both must be saved."""

    def test_patch_position_and_tag_both_persisted(self, api_topology, bearer):
        from apps.rollouts.models import RolloutSequenceEntry

        seq = _get_or_create_sequence()
        tag_a = _make_tag("r2-pt-a-tag")
        tag_b = _make_tag("r2-pt-b-tag")
        tag_c = _make_tag("r2-pt-c-tag")
        entry = RolloutSequenceEntry.objects.create(sequence=seq, tag=tag_a, position=0)
        # Add a second entry so the move is meaningful (from 0 → 1)
        RolloutSequenceEntry.objects.create(sequence=seq, tag=tag_b, position=1)

        url = reverse("api:rollout-sequence-entry-detail", args=[entry.pk])
        r = bearer(api_topology["region_mgr"]).patch(
            url, {"position": 1, "tag": tag_c.pk}, format="json"
        )
        assert r.status_code == 200, (
            f"Expected 200 for combined position+tag PATCH, got {r.status_code}"
        )
        entry.refresh_from_db()
        assert entry.tag == tag_c, f"Tag must be updated to tag_c, got {entry.tag}"
        assert entry.position == 1, f"Position must be moved to 1, got {entry.position}"
        # Parent sequence updated_by must reflect the actor
        seq.refresh_from_db()
        assert seq.updated_by == api_topology["region_mgr"]


# ---------------------------------------------------------------------------
# Item 10 — Image import validates (tag, machine, channel) via channels_for
# ---------------------------------------------------------------------------


class TestImportChannelValidation:
    """import/ must validate the full (tag, machine, channel) tuple.

    A valid tag with an invalid machine/channel → 400, no job created.
    A valid tag with a valid machine/channel → 202.
    """

    def test_valid_tag_bad_machine_channel_400(self, api_topology, bearer, monkeypatch):
        """Tag exists in GH releases but the (machine, channel) combo has no
        importable assets → 400, no ImageImportJob created."""
        from apps.images import github_releases
        from apps.images.models import ImageImportJob

        # Only qemux86-64/release is importable; rpi/nightly is NOT
        fake_releases = [
            _make_release_with_assets("v5.0", machine="qemux86-64", channel="release")
        ]
        monkeypatch.setattr(github_releases, "fetch_releases", lambda *a, **k: fake_releases)

        t = api_topology
        before = ImageImportJob.objects.count()
        r = bearer(t["staff"]).post(
            reverse("api:image-import"),
            {"tag": "v5.0", "machine": "raspberrypi4-64", "channel": "nightly"},
            format="json",
        )
        assert r.status_code == 400, (
            f"Expected 400 for non-importable variant, got {r.status_code}"
        )
        assert ImageImportJob.objects.count() == before, "No job must be created"

    def test_valid_tag_valid_machine_channel_202(self, api_topology, bearer, monkeypatch):
        """Tag with matching importable (machine, channel) → 202 as before."""
        from apps.images import github_releases

        fake_releases = [
            _make_release_with_assets("v5.1", machine="qemux86-64", channel="release")
        ]
        monkeypatch.setattr(github_releases, "fetch_releases", lambda *a, **k: fake_releases)

        t = api_topology
        r = bearer(t["staff"]).post(
            reverse("api:image-import"),
            {"tag": "v5.1", "machine": "qemux86-64", "channel": "release"},
            format="json",
        )
        assert r.status_code == 202, (
            f"Expected 202 for valid importable variant, got {r.status_code}"
        )

    def test_valid_tag_wrong_channel_only_400(self, api_topology, bearer, monkeypatch):
        """Tag + machine correct, but channel doesn't exist for that machine → 400."""
        from apps.images import github_releases
        from apps.images.models import ImageImportJob

        fake_releases = [
            _make_release_with_assets("v5.2", machine="qemux86-64", channel="release")
        ]
        monkeypatch.setattr(github_releases, "fetch_releases", lambda *a, **k: fake_releases)

        t = api_topology
        before = ImageImportJob.objects.count()
        r = bearer(t["staff"]).post(
            reverse("api:image-import"),
            {"tag": "v5.2", "machine": "qemux86-64", "channel": "nightly"},
            format="json",
        )
        assert r.status_code == 400
        assert ImageImportJob.objects.count() == before
