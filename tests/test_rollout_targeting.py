import pytest

from apps.images.models import ImageRelease
from apps.rollouts import targeting as t
from apps.stations.models import RolloutGroup

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
def dev_group(db):
    return RolloutGroup.objects.create(name="Dev", slug="dev-g", channel=RolloutGroup.Channel.DEV)


def test_release_key_parses_date_tags():
    assert t.release_key("2026.10.19-04") == (2026, 10, 19, 4)
    assert t.release_key("v1.2.3") == (1, 2, 3)
    assert t.release_key("alpha") == ()


def test_dev_group_targets_latest_dev_same_machine(station, dev_group, make_release):
    cur = make_release("2026.10.01-01", channel="dev", latest=False)
    rpi_dev = make_release("2026.10.10-01", channel="dev")
    make_release("2026.10.10-01", machine=QEMU, channel="dev")
    make_release("2026.10.18-01", channel="release")
    station.rollout_group = dev_group
    station.current_image_release = cur
    station.save()
    res = t.resolve_upgrade(station)
    assert res.ok and res.target == rpi_dev


def test_no_group_targets_release(station, make_release):
    cur = make_release("2026.10.01-01", latest=False)
    new = make_release("2026.10.18-01")
    make_release("2026.10.19-01", channel="dev")
    station.current_image_release = cur
    station.save()
    assert t.station_channel(station) == "release"
    assert t.resolve_upgrade(station).target == new


def test_release_group_older_than_current_dev_is_not_newer(station, make_release):
    cur = make_release("2026.10.19-01", channel="dev", latest=False)
    make_release("2026.10.10-01", channel="release")
    station.current_image_release = cur
    station.save()
    res = t.resolve_upgrade(station)
    assert not res.ok and res.reason == t.UpgradeTarget.NOT_NEWER
    assert "2026.10.10-01" in t.reason_message(res, station)


def test_same_tag_other_channel_is_not_newer(station, make_release):
    cur = make_release("2026.10.19-01", channel="dev", latest=False)
    make_release("2026.10.19-01", channel="release")
    station.current_image_release = cur
    station.save()
    assert t.resolve_upgrade(station).reason == t.UpgradeTarget.NOT_NEWER


def test_no_platform(station):
    res = t.resolve_upgrade(station)
    assert res.reason == t.UpgradeTarget.NO_PLATFORM and res.target is None
    assert t.reason_message(res, station)


def test_no_release_in_channel(station, dev_group, make_release):
    cur = make_release("2026.10.01-01", latest=False)
    station.rollout_group = dev_group
    station.current_image_release = cur
    station.save()
    res = t.resolve_upgrade(station)
    assert res.reason == t.UpgradeTarget.NO_RELEASE
    assert t.reason_message(res, station)


def test_up_to_date(station, make_release):
    cur = make_release("2026.10.01-01")
    station.current_image_release = cur
    station.save()
    res = t.resolve_upgrade(station)
    assert res.reason == t.UpgradeTarget.UP_TO_DATE
    assert t.reason_message(res, station)


def test_not_ota_ready_only_when_newer(station, make_release):
    cur = make_release("2026.10.01-01", latest=False)
    new = make_release("2026.10.18-01", ota=False)
    station.current_image_release = cur
    station.save()
    res = t.resolve_upgrade(station)
    assert res.reason == t.UpgradeTarget.NOT_OTA_READY and res.target == new
    assert t.reason_message(res, station)
    # not newer wins over not-OTA-ready
    new.tag = "2026.09.01-01"
    new.save()
    assert t.resolve_upgrade(station).reason == t.UpgradeTarget.NOT_NEWER


def test_is_newer_falls_back_to_imported_at(make_release):
    old = make_release("alpha", latest=False)
    new = make_release("beta")
    assert t.is_newer(new, old)
    assert not t.is_newer(old, new)


def test_index_reused_no_extra_queries(station, make_release, django_assert_num_queries):
    cur = make_release("2026.10.01-01", latest=False)
    make_release("2026.10.18-01")
    station.current_image_release = cur
    station.save()
    index = t.latest_index()
    from apps.stations.models import Station

    st = Station.objects.select_related("rollout_group", "current_image_release").get(
        pk=station.pk
    )
    with django_assert_num_queries(0):
        assert t.resolve_upgrade(st, index).ok
