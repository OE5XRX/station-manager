import pytest
from django.db import IntegrityError

from apps.rollouts.models import RolloutSequence, RolloutSequenceEntry
from apps.stations.models import StationTag


@pytest.mark.django_db
class TestRolloutSequence:
    # RolloutSequence is a DB-enforced singleton (unique singleton_key),
    # so these tests reuse the migration-seeded row via current_sequence()
    # instead of creating their own — the old pattern would now hit the
    # unique index.

    def test_entries_are_ordered_by_position(self):
        from apps.rollouts.models import current_sequence

        seq = current_sequence()
        tag_a = StationTag.objects.create(name="alpha", slug="alpha")
        tag_b = StationTag.objects.create(name="beta", slug="beta")
        tag_c = StationTag.objects.create(name="gamma", slug="gamma")

        RolloutSequenceEntry.objects.create(sequence=seq, tag=tag_b, position=1)
        RolloutSequenceEntry.objects.create(sequence=seq, tag=tag_a, position=0)
        RolloutSequenceEntry.objects.create(sequence=seq, tag=tag_c, position=2)

        ordered = list(seq.entries.values_list("tag__name", flat=True))
        assert ordered == ["alpha", "beta", "gamma"]

    def test_tag_unique_per_sequence(self):
        from apps.rollouts.models import current_sequence

        seq = current_sequence()
        tag = StationTag.objects.create(name="t1", slug="t1")

        RolloutSequenceEntry.objects.create(sequence=seq, tag=tag, position=0)
        with pytest.raises(IntegrityError):
            RolloutSequenceEntry.objects.create(sequence=seq, tag=tag, position=1)

    def test_position_unique_per_sequence(self):
        from apps.rollouts.models import current_sequence

        seq = current_sequence()
        t1 = StationTag.objects.create(name="t1", slug="t1")
        t2 = StationTag.objects.create(name="t2", slug="t2")

        RolloutSequenceEntry.objects.create(sequence=seq, tag=t1, position=0)
        with pytest.raises(IntegrityError):
            RolloutSequenceEntry.objects.create(sequence=seq, tag=t2, position=0)


@pytest.mark.django_db
class TestSingletonSeed:
    # Plain django_db (savepoint rollback) — migration-seeded data is
    # set up once per test DB and restored between tests implicitly.
    # The earlier transaction=True flavour was flushing tables in some
    # pytest-django versions, which made the "exactly one sequence"
    # assertion brittle depending on test ordering.

    def test_exactly_one_sequence_exists_after_migrations(self):
        assert RolloutSequence.objects.count() == 1
        # No hard-coded pk — the migration deliberately doesn't force one
        # (explicit-pk inserts on Postgres leave the sequence un-advanced).

    def test_current_sequence_helper_returns_the_singleton(self):
        from apps.rollouts.models import current_sequence

        seq1 = current_sequence()
        seq2 = current_sequence()
        assert seq1 == seq2
        assert RolloutSequence.objects.count() == 1

    def test_singleton_key_prevents_a_second_row(self):
        """DB-level safety net: a second RolloutSequence row — whether
        from a concurrent current_sequence() race or an admin clicking
        Add — must be rejected by the singleton_key unique index, not
        silently accepted."""
        from django.db import IntegrityError, transaction

        with transaction.atomic(), pytest.raises(IntegrityError):
            RolloutSequence.objects.create()


@pytest.mark.django_db
class TestGrouping:
    def test_first_matching_tag_wins(self, make_station_tag):
        from apps.rollouts.grouping import group_stations_by_sequence
        from apps.rollouts.models import RolloutSequenceEntry, current_sequence
        from apps.stations.models import Station

        t_test = make_station_tag("test")
        t_easy = make_station_tag("easy")
        seq = current_sequence()
        seq.entries.all().delete()
        RolloutSequenceEntry.objects.create(sequence=seq, tag=t_test, position=0)
        RolloutSequenceEntry.objects.create(sequence=seq, tag=t_easy, position=1)

        s = Station.objects.create(name="S1")
        s.tags.add(t_test, t_easy)

        grouped = group_stations_by_sequence([s])
        # s must appear ONLY in the 'test' bucket, not in 'easy' too.
        assert grouped["test"] == [s]
        assert grouped["easy"] == []

    def test_unassigned_bucket(self):
        from apps.rollouts.grouping import group_stations_by_sequence
        from apps.rollouts.models import current_sequence
        from apps.stations.models import Station

        seq = current_sequence()
        seq.entries.all().delete()
        s = Station.objects.create(name="S-none")
        grouped = group_stations_by_sequence([s])
        assert grouped["__unassigned__"] == [s]


@pytest.mark.django_db
class TestUpgradeActions:
    def test_admin_can_upgrade_single_station(self, client, admin_user, station, image_release):
        from django.urls import reverse

        from apps.deployments.models import Deployment, DeploymentResult

        # Station must already be "on something" so UpgradeStationView can resolve a machine.
        station.current_image_release = image_release
        station.save(update_fields=["current_image_release"])

        # Introduce a newer release — same machine, is_latest flip.
        from apps.images.models import ImageRelease

        ImageRelease.objects.filter(is_latest=True, machine="qemux86-64").update(is_latest=False)
        newer = ImageRelease.objects.create(
            tag="v2",
            machine="qemux86-64",
            s3_key="images/v2/qemu.wic.bz2",
            sha256="d" * 64,
            size_bytes=2000,
            rootfs_s3_key="images/v2/qemu.rootfs.bz2",
            rootfs_sha256="e" * 64,
            rootfs_size_bytes=500,
            is_latest=True,
        )

        client.force_login(admin_user)
        response = client.post(
            reverse("rollouts:upgrade_station", args=[station.pk]),
        )
        assert response.status_code == 302
        dep = Deployment.objects.get(target_station=station)
        assert dep.image_release == newer
        assert DeploymentResult.objects.filter(deployment=dep, station=station).exists()

    def test_upgrade_station_refuses_release_without_rootfs(
        self, client, admin_user, station, image_release
    ):
        """If the latest release hasn't been extracted for OTA yet,
        UpgradeStationView must refuse at creation time instead of
        creating a Deployment the station can never install."""
        from django.urls import reverse

        from apps.deployments.models import Deployment
        from apps.images.models import ImageRelease

        station.current_image_release = image_release
        station.save(update_fields=["current_image_release"])

        # Pretend a newer release exists, but hasn't been processed
        # for OTA (rootfs_s3_key empty).
        ImageRelease.objects.filter(is_latest=True, machine="qemux86-64").update(is_latest=False)
        ImageRelease.objects.create(
            tag="v2-unprocessed",
            machine="qemux86-64",
            s3_key="images/v2/qemu.wic.bz2",
            sha256="z" * 64,
            size_bytes=1,
            is_latest=True,
            # rootfs_* deliberately left empty.
        )

        client.force_login(admin_user)
        response = client.post(
            reverse("rollouts:upgrade_station", kwargs={"station_pk": station.pk}),
            follow=True,
        )
        assert response.status_code == 200

        # No Deployment / DeploymentResult row was created.
        assert not Deployment.objects.filter(target_station=station).exists()

        # A flash error message names the tag and asks the operator
        # to re-import.
        msgs = [str(m) for m in response.context["messages"]]
        assert any("v2-unprocessed" in m and "re-import" in m.lower() for m in msgs)

    def test_operator_cannot_upgrade(self, client, operator_user, station):
        from django.urls import reverse

        client.force_login(operator_user)
        response = client.post(reverse("rollouts:upgrade_station", args=[station.pk]))
        assert response.status_code == 403

    def test_upgrade_group_creates_deployment(
        self, client, admin_user, image_release, make_station_tag
    ):
        from django.urls import reverse

        from apps.deployments.models import Deployment
        from apps.rollouts.models import RolloutSequenceEntry, current_sequence
        from apps.stations.models import Station

        # Two stations on the same machine, both on v1-alpha.
        tag = make_station_tag("test-stations")
        seq = current_sequence()
        seq.entries.all().delete()
        RolloutSequenceEntry.objects.create(sequence=seq, tag=tag, position=0)

        s1 = Station.objects.create(name="S1")
        s1.tags.add(tag)
        s1.current_image_release = image_release
        s1.save(update_fields=["current_image_release"])

        # Introduce a newer release for that machine.
        from apps.images.models import ImageRelease

        ImageRelease.objects.filter(is_latest=True, machine="qemux86-64").update(is_latest=False)
        ImageRelease.objects.create(
            tag="v2",
            machine="qemux86-64",
            s3_key="images/v2/qemu.wic.bz2",
            sha256="e" * 64,
            size_bytes=2000,
            rootfs_s3_key="images/v2/qemu.rootfs.bz2",
            rootfs_sha256="f" * 64,
            rootfs_size_bytes=500,
            is_latest=True,
        )

        client.force_login(admin_user)
        response = client.post(
            reverse("rollouts:upgrade_group", args=["test-stations"]),
        )
        assert response.status_code == 302
        dep = Deployment.objects.filter(target_tag=tag).first()
        assert dep is not None
        assert dep.image_release.tag == "v2"

    def test_upgrade_group_honors_first_match_wins(
        self, client, admin_user, image_release, make_station_tag
    ):
        """A station carrying both `test` (pos 0) and `easy` (pos 1) must
        only be upgraded when the `test` group button is pressed, never
        when the `easy` button is pressed — the dashboard and the action
        agree on which bucket the station lives in."""
        from django.urls import reverse

        from apps.deployments.models import Deployment
        from apps.rollouts.models import RolloutSequenceEntry, current_sequence
        from apps.stations.models import Station

        t_test = make_station_tag("test")
        t_easy = make_station_tag("easy")
        seq = current_sequence()
        seq.entries.all().delete()
        RolloutSequenceEntry.objects.create(sequence=seq, tag=t_test, position=0)
        RolloutSequenceEntry.objects.create(sequence=seq, tag=t_easy, position=1)

        s = Station.objects.create(name="Dual")
        s.tags.add(t_test, t_easy)
        s.current_image_release = image_release
        s.save(update_fields=["current_image_release"])

        from apps.images.models import ImageRelease

        ImageRelease.objects.filter(is_latest=True, machine="qemux86-64").update(is_latest=False)
        ImageRelease.objects.create(
            tag="v2",
            machine="qemux86-64",
            s3_key="images/v2/qemu.wic.bz2",
            sha256="f" * 64,
            size_bytes=2000,
            rootfs_s3_key="images/v2/qemu.rootfs.bz2",
            rootfs_sha256="g" * 64,
            rootfs_size_bytes=500,
            is_latest=True,
        )

        client.force_login(admin_user)
        # Press the "easy" group's button — station is in the test bucket,
        # so no deployment should be queued against the easy tag.
        response = client.post(reverse("rollouts:upgrade_group", args=["easy"]))
        assert response.status_code == 302
        assert not Deployment.objects.filter(target_tag=t_easy).exists()

        # Press the "test" group's button — the station's real bucket.
        response = client.post(reverse("rollouts:upgrade_group", args=["test"]))
        assert response.status_code == 302
        assert Deployment.objects.filter(target_tag=t_test).exists()

    def test_upgrade_group_skips_releases_without_rootfs(
        self, client, admin_user, make_station_tag, image_release
    ):
        """A group upgrade targeting two machines where one's latest
        release is OTA-ready and the other's isn't must create a
        Deployment for the ready one and tally the non-ready one into
        the 'skipped' summary.

        The qemu station is on `image_release` (older, OTA-ready via
        the fixture updated in Task 4), upgrading to qemu_ready. The
        rpi station is on rpi_not_ready, which is_latest but has
        empty rootfs_* → must be skipped.
        """
        from django.urls import reverse

        from apps.deployments.models import Deployment
        from apps.images.models import ImageRelease
        from apps.rollouts.models import RolloutSequenceEntry, current_sequence
        from apps.stations.models import Station

        tag = make_station_tag("group-a")
        seq = current_sequence()
        seq.entries.all().delete()
        RolloutSequenceEntry.objects.create(sequence=seq, tag=tag, position=0)

        ImageRelease.objects.filter(is_latest=True, machine="qemux86-64").update(is_latest=False)
        qemu_ready = ImageRelease.objects.create(
            tag="qemu-ready",
            machine="qemux86-64",
            s3_key="wic",
            sha256="a" * 64,
            size_bytes=1,
            rootfs_s3_key="rootfs",
            rootfs_sha256="b" * 64,
            rootfs_size_bytes=1,
            is_latest=True,
        )
        # An older rpi release the station is currently on (not is_latest).
        rpi_old = ImageRelease.objects.create(
            tag="rpi-old",
            machine="raspberrypi4-64",
            s3_key="wic-old",
            sha256="d" * 64,
            size_bytes=1,
            is_latest=False,
        )
        # The newest rpi release — is_latest, but missing rootfs_* → not OTA-ready.
        ImageRelease.objects.create(
            tag="rpi-not-ready",
            machine="raspberrypi4-64",
            s3_key="wic2",
            sha256="c" * 64,
            size_bytes=1,
            is_latest=True,
            # rootfs_* deliberately empty.
        )

        s_qemu = Station.objects.create(
            name="qemu-station",
            callsign="Q1TEST",
            current_image_release=image_release,  # older qemu release
        )
        s_qemu.tags.add(tag)
        s_rpi = Station.objects.create(
            name="rpi-station",
            callsign="R1TEST",
            current_image_release=rpi_old,  # older rpi release, target is rpi-not-ready
        )
        s_rpi.tags.add(tag)

        client.force_login(admin_user)
        response = client.post(
            reverse("rollouts:upgrade_group", kwargs={"tag_slug": tag.slug}),
            follow=True,
        )
        assert response.status_code == 200

        # Exactly one Deployment — for qemu, because rpi's release isn't OTA-ready.
        deployments = Deployment.objects.all()
        assert deployments.count() == 1
        assert deployments.first().image_release_id == qemu_ready.pk

        # Summary message: "Queued 1 upgrades (1 skipped)".
        msgs = [str(m) for m in response.context["messages"]]
        assert any("Queued 1" in m and "1 skipped" in m for m in msgs), msgs

    def test_upgrade_group_skips_unprovisioned_stations(
        self, client, admin_user, image_release, make_station_tag
    ):
        """Stations with no current_image_release have no known machine
        and must be counted as skipped, not silently dropped."""
        from django.urls import reverse

        from apps.deployments.models import Deployment
        from apps.rollouts.models import RolloutSequenceEntry, current_sequence
        from apps.stations.models import Station

        tag = make_station_tag("test")
        seq = current_sequence()
        seq.entries.all().delete()
        RolloutSequenceEntry.objects.create(sequence=seq, tag=tag, position=0)

        s = Station.objects.create(name="UnprovS")
        s.tags.add(tag)
        # current_image_release left None — station has no known machine.

        client.force_login(admin_user)
        response = client.post(reverse("rollouts:upgrade_group", args=["test"]))
        assert response.status_code == 302
        # No Deployment should be created since there's nothing to target.
        assert not Deployment.objects.filter(target_tag=tag).exists()


@pytest.mark.django_db
class TestUpgradeDashboard:
    def test_admin_sees_groups(self, client, admin_user, station, image_release, make_station_tag):
        from django.urls import reverse

        from apps.rollouts.models import RolloutSequenceEntry, current_sequence

        tag = make_station_tag("test-stations")
        station.tags.add(tag)
        station.current_image_release = None
        station.save(update_fields=["current_image_release"])
        seq = current_sequence()
        seq.entries.all().delete()
        RolloutSequenceEntry.objects.create(sequence=seq, tag=tag, position=0)

        client.force_login(admin_user)
        response = client.get(reverse("rollouts:upgrade_dashboard"))
        assert response.status_code == 200
        assert b"test-stations" in response.content
        # The "Upgrade group" form action must render for this non-empty, non-unassigned group.
        assert b"upgrade/group/test-stations/" in response.content

    def test_operator_forbidden(self, client, operator_user):
        from django.urls import reverse

        client.force_login(operator_user)
        response = client.get(reverse("rollouts:upgrade_dashboard"))
        assert response.status_code == 403

    def test_dashboard_renders_active_deployment_status(
        self, client, admin_user, station, image_release, make_station_tag
    ):
        """The Status column must reflect the active DeploymentResult
        status on initial render — so a mid-flight deployment doesn't
        flash station connectivity for the split second before the first
        WebSocket event arrives."""
        from django.urls import reverse

        from apps.deployments.models import Deployment, DeploymentResult
        from apps.rollouts.models import RolloutSequenceEntry, current_sequence

        tag = make_station_tag("test-stations")
        station.tags.add(tag)
        station.current_image_release = image_release
        station.save(update_fields=["current_image_release"])
        seq = current_sequence()
        seq.entries.all().delete()
        RolloutSequenceEntry.objects.create(sequence=seq, tag=tag, position=0)

        # Introduce a newer release so the station has something pending.
        from apps.images.models import ImageRelease

        ImageRelease.objects.filter(is_latest=True, machine="qemux86-64").update(is_latest=False)
        newer = ImageRelease.objects.create(
            tag="v2",
            machine="qemux86-64",
            s3_key="images/v2/qemu.wic.bz2",
            sha256="f" * 64,
            size_bytes=2000,
            is_latest=True,
        )
        dep = Deployment.objects.create(
            image_release=newer,
            target_type=Deployment.TargetType.STATION,
            target_station=station,
            status=Deployment.Status.IN_PROGRESS,
            created_by=admin_user,
        )
        DeploymentResult.objects.create(
            deployment=dep,
            station=station,
            status=DeploymentResult.Status.DOWNLOADING,
        )

        client.force_login(admin_user)
        response = client.get(reverse("rollouts:upgrade_dashboard"))
        assert response.status_code == 200
        assert b"pill-downloading" in response.content
        assert b"DOWNLOADING" in response.content


@pytest.mark.django_db
class TestSequenceEdit:
    def test_add_entry(self, client, admin_user, make_station_tag):
        from django.urls import reverse

        from apps.rollouts.models import current_sequence

        current_sequence().entries.all().delete()
        tag = make_station_tag("test")
        client.force_login(admin_user)
        response = client.post(
            reverse("rollouts:sequence_add"),
            {"tag": tag.pk},
        )
        assert response.status_code == 302
        seq = current_sequence()
        assert seq.entries.count() == 1

    def test_remove_entry(self, client, admin_user, make_station_tag):
        from django.urls import reverse

        from apps.rollouts.models import RolloutSequenceEntry, current_sequence

        tag = make_station_tag("test")
        seq = current_sequence()
        seq.entries.all().delete()
        entry = RolloutSequenceEntry.objects.create(sequence=seq, tag=tag, position=0)
        client.force_login(admin_user)
        response = client.post(reverse("rollouts:sequence_remove", args=[entry.pk]))
        assert response.status_code == 302
        assert not RolloutSequenceEntry.objects.filter(pk=entry.pk).exists()

    def test_reorder(self, client, admin_user, make_station_tag):
        from django.urls import reverse

        from apps.rollouts.models import RolloutSequenceEntry, current_sequence

        seq = current_sequence()
        seq.entries.all().delete()
        t1 = make_station_tag("t1")
        t2 = make_station_tag("t2")
        e1 = RolloutSequenceEntry.objects.create(sequence=seq, tag=t1, position=0)
        e2 = RolloutSequenceEntry.objects.create(sequence=seq, tag=t2, position=1)

        client.force_login(admin_user)
        response = client.post(
            reverse("rollouts:sequence_reorder"),
            {"order": f"{e2.pk},{e1.pk}"},
        )
        assert response.status_code == 200
        e1.refresh_from_db()
        e2.refresh_from_db()
        assert e1.position == 1
        assert e2.position == 0

    def test_reorder_rejects_duplicate_ids(self, client, admin_user, make_station_tag):
        """`1,1,2` used to pass the set-equality check and leave the
        sequence non-normalized — one entry double-assigned, one
        position skipped. Now it must be rejected up front."""
        from django.urls import reverse

        from apps.rollouts.models import RolloutSequenceEntry, current_sequence

        seq = current_sequence()
        seq.entries.all().delete()
        t1 = make_station_tag("t1")
        t2 = make_station_tag("t2")
        e1 = RolloutSequenceEntry.objects.create(sequence=seq, tag=t1, position=0)
        e2 = RolloutSequenceEntry.objects.create(sequence=seq, tag=t2, position=1)

        client.force_login(admin_user)
        response = client.post(
            reverse("rollouts:sequence_reorder"),
            {"order": f"{e1.pk},{e1.pk},{e2.pk}"},
        )
        assert response.status_code == 400
        e1.refresh_from_db()
        e2.refresh_from_db()
        # Nothing moved.
        assert e1.position == 0
        assert e2.position == 1


@pytest.mark.django_db
class TestStationUpgradeCard:
    def test_admin_sees_upgrade_button(self, client, admin_user, station, image_release):
        from django.urls import reverse

        from apps.images.models import ImageRelease

        station.current_image_release = image_release
        station.save(update_fields=["current_image_release"])
        # Flip to a newer release so the card offers an upgrade target.
        ImageRelease.objects.filter(is_latest=True, machine="qemux86-64").update(is_latest=False)
        ImageRelease.objects.create(
            tag="v2",
            machine="qemux86-64",
            s3_key="images/v2/qemu.wic.bz2",
            sha256="z" * 64,
            size_bytes=1,
            is_latest=True,
        )
        client.force_login(admin_user)
        r = client.get(reverse("stations:station_detail", kwargs={"pk": station.pk}))
        assert r.status_code == 200
        assert b"Upgrade this station" in r.content
        assert b"v2" in r.content

    def test_already_on_latest_disables_button(self, client, admin_user, station, image_release):
        from django.urls import reverse

        station.current_image_release = image_release  # image_release is is_latest=True
        station.save(update_fields=["current_image_release"])
        client.force_login(admin_user)
        r = client.get(reverse("stations:station_detail", kwargs={"pk": station.pk}))
        assert r.status_code == 200
        assert b"Already on latest" in r.content
