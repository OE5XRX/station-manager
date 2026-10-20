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
    assert dep.results.get().image_release == dev_latest
    assert (dep.image_tag, dep.image_channel) == (dev_latest.tag, "dev")


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
    assert {r.image_release for r in dep.results.all()} == {new}
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
    assert {d.results.get().image_release for d in deps} == {rpi_new, qemu_new}
    for d in deps:
        assert d.results.count() == 1
        result = d.results.get()
        assert result.station.current_image_release.machine == result.image_release.machine
        assert d.image_tag == result.image_release.tag
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


def test_upgrade_group_skips_station_moved_out_concurrently(
    client, admin_user, make_release, make_rollout_group, monkeypatch
):
    from apps.rollouts import views as rollout_views

    group = make_rollout_group("Early")
    other = make_rollout_group("Elsewhere")
    cur = make_release("2026.10.01-01", latest=False)
    new = make_release("2026.10.18-01")
    s1 = Station.objects.create(name="S1", rollout_group=group, current_image_release=cur)
    s2 = Station.objects.create(name="S2", rollout_group=group, current_image_release=cur)

    real_index = rollout_views.latest_index

    def index_with_concurrent_move():
        # Between the member read and the locked write, S2 moves groups.
        Station.objects.filter(pk=s2.pk).update(rollout_group=other)
        return real_index()

    monkeypatch.setattr(rollout_views, "latest_index", index_with_concurrent_move)
    client.force_login(admin_user)
    client.post(reverse("rollouts:upgrade_group", args=[group.slug]))

    dep = Deployment.objects.get()
    assert dep.results.get().image_release == new
    assert list(dep.results.values_list("station_id", flat=True)) == [s1.pk]


def test_upgrade_group_same_tag_both_machines_is_one_deployment(
    client, admin_user, make_release, make_rollout_group
):
    group = make_rollout_group("SameTag")
    rpi_cur = make_release("2026.10.01-01", machine=RPI, latest=False)
    qemu_cur = make_release("2026.10.01-01", machine=QEMU, latest=False)
    rpi_new = make_release("2026.10.10-01", machine=RPI)
    qemu_new = make_release("2026.10.10-01", machine=QEMU)
    rpi = Station.objects.create(name="rpi", rollout_group=group, current_image_release=rpi_cur)
    qemu = Station.objects.create(name="qemu", rollout_group=group, current_image_release=qemu_cur)

    client.force_login(admin_user)
    client.post(reverse("rollouts:upgrade_group", args=[group.slug]))

    dep = Deployment.objects.get(target_group=group)
    assert (dep.image_tag, dep.image_channel) == ("2026.10.10-01", "release")
    assert {r.station_id: r.image_release for r in dep.results.all()} == {
        rpi.pk: rpi_new,
        qemu.pk: qemu_new,
    }


def _mid_flight(station, release):
    """A result of another deployment past PENDING → supersession conflict."""
    other = Deployment.objects.create(
        image_tag=release.tag,
        image_channel=release.channel,
        target_type=Deployment.TargetType.STATION,
        target_station=station,
        status=Deployment.Status.IN_PROGRESS,
    )
    DeploymentResult.objects.create(
        deployment=other,
        station=station,
        image_release=release,
        status=DeploymentResult.Status.DOWNLOADING,
    )
    return other


def test_upgrade_group_counts_active_conflict_as_skipped(
    client, admin_user, make_release, make_rollout_group
):
    group = make_rollout_group("Busy")
    cur = make_release("2026.10.01-01", latest=False)
    new = make_release("2026.10.18-01")
    free = Station.objects.create(name="free", rollout_group=group, current_image_release=cur)
    busy = Station.objects.create(name="busy", rollout_group=group, current_image_release=cur)
    _mid_flight(busy, cur)

    client.force_login(admin_user)
    r = client.post(reverse("rollouts:upgrade_group", args=[group.slug]), follow=True)

    dep = Deployment.objects.get(target_group=group)
    assert list(dep.results.values_list("station_id", flat=True)) == [free.pk]
    assert dep.results.get().image_release == new
    assert [(s["station_id"], s["reason"]) for s in dep.skipped_stations] == [
        (busy.pk, "active_deployment")
    ]
    assert any("Queued 1" in m and "1 skipped" in m for m in _messages(r))


def test_upgrade_group_supersedes_queued_result_elsewhere(
    client, admin_user, make_release, make_rollout_group
):
    group = make_rollout_group("Queued")
    cur = make_release("2026.10.01-01", latest=False)
    new = make_release("2026.10.18-01")
    queued_st = Station.objects.create(name="q", rollout_group=group, current_image_release=cur)
    free = Station.objects.create(name="f", rollout_group=group, current_image_release=cur)
    other = _mid_flight(queued_st, cur)
    other.results.update(status=DeploymentResult.Status.PENDING)

    client.force_login(admin_user)
    r = client.post(reverse("rollouts:upgrade_group", args=[group.slug]), follow=True)

    dep = Deployment.objects.get(target_group=group)
    assert {res.station_id: res.image_release for res in dep.results.all()} == {
        queued_st.pk: new,
        free.pk: new,
    }
    assert dep.skipped_stations == []
    old = other.results.get()
    assert old.status == DeploymentResult.Status.SUPERSEDED
    other.refresh_from_db()
    assert other.status != Deployment.Status.IN_PROGRESS
    assert any("Queued 2" in m for m in _messages(r))


def test_upgrade_group_all_conflicting_creates_nothing(
    client, admin_user, make_release, make_rollout_group
):
    group = make_rollout_group("AllBusy")
    cur = make_release("2026.10.01-01", latest=False)
    make_release("2026.10.18-01")
    for name in ("a", "b"):
        s = Station.objects.create(name=name, rollout_group=group, current_image_release=cur)
        _mid_flight(s, cur)

    client.force_login(admin_user)
    r = client.post(reverse("rollouts:upgrade_group", args=[group.slug]), follow=True)

    assert not Deployment.objects.filter(target_group=group).exists()
    assert any("Queued 0" in m and "2 skipped" in m for m in _messages(r))


def test_upgrade_station_active_conflict_shows_reason(client, admin_user, station, make_release):
    cur = make_release("2026.10.01-01", latest=False)
    make_release("2026.10.18-01")
    station.current_image_release = cur
    station.save()
    blocking = _mid_flight(station, cur)

    client.force_login(admin_user)
    r = client.post(reverse("rollouts:upgrade_station", args=[station.pk]), follow=True)

    assert list(Deployment.objects.values_list("pk", flat=True)) == [blocking.pk]
    assert any(m for m in _messages(r))


# --- deferred audit (on_commit) ---------------------------------------------


def _fw_logs():
    from apps.stations.models import StationAuditLog

    return StationAuditLog.objects.filter(event_type=StationAuditLog.EventType.FIRMWARE_UPDATE)


def test_upgrade_station_writes_one_audit_after_commit(
    client, admin_user, station, make_release, django_capture_on_commit_callbacks
):
    cur = make_release("2026.10.01-01", latest=False)
    make_release("2026.10.18-01")
    station.current_image_release = cur
    station.current_os_version = "2026.10.01-01"
    station.save()

    client.force_login(admin_user)
    with django_capture_on_commit_callbacks(execute=True):
        client.post(reverse("rollouts:upgrade_station", args=[station.pk]))

    dep = Deployment.objects.get(target_station=station)
    (log,) = _fw_logs()
    assert log.station_id == station.pk
    assert log.user == admin_user
    assert "2026.10.18-01" in log.message
    assert f"deployment #{dep.pk}" in log.message
    assert admin_user.username in log.message


def test_upgrade_station_conflict_writes_no_audit(
    client, admin_user, station, make_release, django_capture_on_commit_callbacks
):
    cur = make_release("2026.10.01-01", latest=False)
    make_release("2026.10.18-01")
    station.current_image_release = cur
    station.save()
    _mid_flight(station, cur)

    client.force_login(admin_user)
    with django_capture_on_commit_callbacks(execute=True):
        client.post(reverse("rollouts:upgrade_station", args=[station.pk]))

    assert not _fw_logs().exists()


def test_upgrade_group_audits_served_stations_only(
    client, admin_user, make_release, make_rollout_group, django_capture_on_commit_callbacks
):
    group = make_rollout_group("Audit")
    cur = make_release("2026.10.01-01", latest=False)
    make_release("2026.10.18-01")
    a = Station.objects.create(name="a", rollout_group=group, current_image_release=cur)
    b = Station.objects.create(name="b", rollout_group=group, current_image_release=cur)
    busy = Station.objects.create(name="busy", rollout_group=group, current_image_release=cur)
    unprov = Station.objects.create(name="unprov", rollout_group=group)  # skipped
    _mid_flight(busy, cur)

    client.force_login(admin_user)
    with django_capture_on_commit_callbacks(execute=True):
        client.post(reverse("rollouts:upgrade_group", args=[group.slug]))

    dep = Deployment.objects.get(target_group=group)
    logs = _fw_logs()
    assert sorted(logs.values_list("station_id", flat=True)) == sorted([a.pk, b.pk])
    assert not logs.filter(station__in=[busy, unprov]).exists()
    for log in logs:
        assert "2026.10.18-01" in log.message
        assert f"deployment #{dep.pk}" in log.message
        assert admin_user.username in log.message
        assert "group 'Audit'" in log.message
