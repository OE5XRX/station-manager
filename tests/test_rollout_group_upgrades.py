"""Upgrades, dashboard, station card and sequence editor on rollout groups."""

import pytest
from django.urls import reverse

from apps.deployments.models import Deployment, DeploymentResult
from apps.images.models import ImageRelease
from apps.rollouts.models import RolloutSequenceEntry, current_sequence
from apps.stations.models import RolloutGroup, Station

QEMU = ImageRelease.Machine.QEMU
RPI = ImageRelease.Machine.RPI


@pytest.fixture
def make_release(db):
    def _make(tag, machine=RPI, channel="release", latest=True, ota=True):
        extra = (
            {"rootfs_s3_key": f"r/{tag}", "rootfs_sha256": "b" * 64, "rootfs_size_bytes": 5}
            if ota
            else {}
        )
        return ImageRelease.objects.create(
            tag=tag,
            machine=machine,
            channel=channel,
            s3_key=f"i/{channel}/{tag}/{machine}",
            sha256="a" * 64,
            size_bytes=10,
            is_latest=latest,
            **extra,
        )

    return _make


@pytest.fixture
def empty_sequence(db):
    seq = current_sequence()
    seq.entries.all().delete()
    return seq


def _messages(response):
    return [str(m) for m in response.context["messages"]]


# --- single-station upgrade -------------------------------------------------


def test_upgrade_station_dev_group_queues_dev_image(
    client, admin_user, station, make_release, make_rollout_group
):
    dev = make_rollout_group("Dev", channel=RolloutGroup.Channel.DEV)
    cur = make_release("2026.10.01-01", channel="dev", latest=False)
    dev_latest = make_release("2026.10.10-01", channel="dev")
    make_release("2026.10.18-01", channel="release")  # newer, but wrong channel
    station.rollout_group = dev
    station.current_image_release = cur
    station.save()

    client.force_login(admin_user)
    r = client.post(reverse("rollouts:upgrade_station", args=[station.pk]))

    assert r.status_code == 302
    dep = Deployment.objects.get(target_station=station)
    assert dep.image_release == dev_latest


def test_upgrade_station_refuses_downgrade(client, admin_user, station, make_release):
    cur = make_release("2026.10.19-01", channel="dev", latest=False)
    make_release("2026.10.10-01", channel="release")
    station.current_image_release = cur
    station.save()

    client.force_login(admin_user)
    r = client.post(reverse("rollouts:upgrade_station", args=[station.pk]), follow=True)

    assert r.status_code == 200
    assert not Deployment.objects.exists()
    assert any("no downgrade" in m for m in _messages(r))


def test_upgrade_station_without_platform_message(client, admin_user, station, make_release):
    make_release("2026.10.10-01")
    client.force_login(admin_user)
    r = client.post(reverse("rollouts:upgrade_station", args=[station.pk]), follow=True)

    assert not Deployment.objects.exists()
    assert any("Platform unknown" in m for m in _messages(r))


# --- group upgrade ----------------------------------------------------------


def test_upgrade_group_creates_group_deployment(
    client, admin_user, make_release, make_rollout_group, empty_sequence
):
    group = make_rollout_group("Early")
    RolloutSequenceEntry.objects.create(sequence=empty_sequence, group=group, position=0)
    cur = make_release("2026.10.01-01", latest=False)
    new = make_release("2026.10.18-01")
    s1 = Station.objects.create(name="S1", rollout_group=group, current_image_release=cur)
    s2 = Station.objects.create(name="S2", rollout_group=group, current_image_release=cur)
    Station.objects.create(name="Other", current_image_release=cur)  # not in group

    client.force_login(admin_user)
    r = client.post(reverse("rollouts:upgrade_group", kwargs={"group_slug": group.slug}))

    assert r.status_code == 302
    dep = Deployment.objects.get()
    assert dep.target_type == "group"
    assert dep.target_group == group
    assert dep.image_release == new
    assert set(dep.results.values_list("station_id", flat=True)) == {s1.pk, s2.pk}


def test_upgrade_group_mixed_machines_and_unprovisioned(
    client, admin_user, make_release, make_rollout_group
):
    group = make_rollout_group("Mixed", channel=RolloutGroup.Channel.DEV)
    rpi_cur = make_release("2026.10.01-01", machine=RPI, channel="dev", latest=False)
    qemu_cur = make_release("2026.10.01-01", machine=QEMU, channel="dev", latest=False)
    rpi_new = make_release("2026.10.10-01", machine=RPI, channel="dev")
    qemu_new = make_release("2026.10.11-01", machine=QEMU, channel="dev")
    Station.objects.create(name="rpi", rollout_group=group, current_image_release=rpi_cur)
    Station.objects.create(name="qemu", rollout_group=group, current_image_release=qemu_cur)
    Station.objects.create(name="unprov", rollout_group=group)  # no platform
    Station.objects.create(name="current", rollout_group=group, current_image_release=rpi_new)

    client.force_login(admin_user)
    r = client.post(reverse("rollouts:upgrade_group", args=[group.slug]), follow=True)

    deps = Deployment.objects.filter(target_group=group)
    assert {d.image_release for d in deps} == {rpi_new, qemu_new}
    for d in deps:
        assert d.results.count() == 1
        assert d.results.get().station.current_image_release.machine == d.image_release.machine
    assert any("Queued 2" in m and "2 skipped" in m for m in _messages(r))


def test_upgrade_group_works_for_group_outside_sequence(
    client, admin_user, make_release, make_rollout_group, empty_sequence
):
    group = make_rollout_group("Loose")
    cur = make_release("2026.10.01-01", latest=False)
    make_release("2026.10.18-01")
    s = Station.objects.create(name="S", rollout_group=group, current_image_release=cur)

    client.force_login(admin_user)
    client.post(reverse("rollouts:upgrade_group", args=[group.slug]))

    assert DeploymentResult.objects.filter(
        station=s, deployment__target_group=group, status=DeploymentResult.Status.PENDING
    ).exists()


# --- dashboard --------------------------------------------------------------


def test_dashboard_buckets_by_group_with_channel(
    client, admin_user, make_release, make_rollout_group, empty_sequence
):
    zulu = make_rollout_group("Zulu")
    alpha = make_rollout_group("Alpha", channel=RolloutGroup.Channel.DEV)
    in_seq_b = make_rollout_group("Seq B")
    in_seq_a = make_rollout_group("Seq A")
    RolloutSequenceEntry.objects.create(sequence=empty_sequence, group=in_seq_b, position=0)
    RolloutSequenceEntry.objects.create(sequence=empty_sequence, group=in_seq_a, position=1)
    Station.objects.create(name="in-alpha", rollout_group=alpha)
    Station.objects.create(name="no-group")

    client.force_login(admin_user)
    r = client.get(reverse("rollouts:upgrade_dashboard"))

    assert r.status_code == 200
    groups = r.context["groups"]
    assert [g["key"] for g in groups] == [
        in_seq_b.slug,
        in_seq_a.slug,
        alpha.slug,
        zulu.slug,
        "__unassigned__",
    ]
    by_key = {g["key"]: g for g in groups}
    assert by_key[alpha.slug]["channel"] == "dev"
    assert [row["station"].name for row in by_key[alpha.slug]["rows"]] == ["in-alpha"]
    assert by_key["__unassigned__"]["channel"] == "release"
    assert str(by_key["__unassigned__"]["name"]) == "No group"
    assert [row["station"].name for row in by_key["__unassigned__"]["rows"]] == ["no-group"]
    content = r.content.decode()
    assert "No group" in content
    assert f"upgrade/group/{alpha.slug}/" not in content  # nothing upgradable


def test_dashboard_dev_station_up_to_date_on_dev_latest(
    client, admin_user, make_release, make_rollout_group
):
    dev = make_rollout_group("Dev", channel=RolloutGroup.Channel.DEV)
    dev_latest = make_release("2026.10.10-01", channel="dev")
    make_release("2026.10.18-01", channel="release")  # must not count as target
    s = Station.objects.create(name="dev-st", rollout_group=dev, current_image_release=dev_latest)

    client.force_login(admin_user)
    r = client.get(reverse("rollouts:upgrade_dashboard"))

    assert (s, dev_latest) in r.context["up_to_date"]
    by_key = {g["key"]: g for g in r.context["groups"]}
    assert by_key[dev.slug]["rows"] == []
    assert b"All stations in this group are up to date." in r.content


def test_dashboard_empty_group_says_no_stations(client, admin_user, make_rollout_group):
    group = make_rollout_group("Lonely")
    client.force_login(admin_user)
    r = client.get(reverse("rollouts:upgrade_dashboard"))

    by_key = {g["key"]: g for g in r.context["groups"]}
    assert by_key[group.slug]["is_empty"]
    assert b"No stations in this group." in r.content


# --- station card -----------------------------------------------------------


def test_station_card_shows_not_newer_reason(client, admin_user, station, make_release):
    cur = make_release("2026.10.19-01", channel="dev", latest=False)
    make_release("2026.10.10-01", channel="release")
    station.current_image_release = cur
    station.save()

    client.force_login(admin_user)
    r = client.get(reverse("stations:station_detail", kwargs={"pk": station.pk}))

    assert r.status_code == 200
    assert r.context["upgrade"].reason == "not_newer"
    content = r.content.decode()
    assert "no downgrade" in content
    assert "Upgrade this station" not in content


# --- sequence editor --------------------------------------------------------


def test_sequence_add_remove_reorder_groups(
    client, admin_user, make_rollout_group, empty_sequence
):
    client.raise_request_exception = True
    a, b, c = (make_rollout_group(n) for n in ("ga", "gb", "gc"))
    client.force_login(admin_user)

    for g in (a, b, c):
        assert client.post(reverse("rollouts:sequence_add"), {"group": g.pk}).status_code == 302
    entries = list(empty_sequence.entries.order_by("position"))
    assert [(e.group, e.position) for e in entries] == [(a, 0), (b, 1), (c, 2)]

    r = client.post(reverse("rollouts:sequence_remove", args=[entries[1].pk]))
    assert r.status_code == 302
    assert list(empty_sequence.entries.values_list("group", "position")) == [
        (a.pk, 0),
        (c.pk, 1),
    ]

    e_a, e_c = entries[0], entries[2]
    r = client.post(reverse("rollouts:sequence_reorder"), {"order": f"{e_c.pk},{e_a.pk}"})
    assert r.status_code == 200
    assert list(empty_sequence.entries.values_list("group", "position")) == [
        (c.pk, 0),
        (a.pk, 1),
    ]

    page = client.get(reverse("rollouts:sequence_edit"))
    assert page.status_code == 200
    assert "gc" in page.content.decode()


def test_group_in_sequence_cannot_be_deleted(make_rollout_group, empty_sequence):
    from django.db.models.deletion import ProtectedError

    groups = [make_rollout_group(f"g{i}") for i in range(3)]
    for i, g in enumerate(groups):
        RolloutSequenceEntry.objects.create(sequence=empty_sequence, group=g, position=i)

    with pytest.raises(ProtectedError):
        groups[0].delete()

    assert empty_sequence.entries.count() == 3


def test_group_name_not_double_escaped_in_confirm(
    client, admin_user, station, make_release, make_rollout_group
):
    group = make_rollout_group("A&B")
    station.rollout_group = group
    station.current_image_release = make_release("2026.10.01-01", latest=False)
    station.save()
    make_release("2026.10.18-01")
    client.force_login(admin_user)
    html = client.get(reverse("rollouts:upgrade_dashboard")).content.decode()
    assert "group A\\u0026B to the latest" in html
    assert "&amp;amp;" not in html and "A\\u0026amp;B" not in html
