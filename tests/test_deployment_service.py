"""apps.deployments.services: version-based plan + create_deployment."""

import pytest

from apps.deployments import services
from apps.deployments.models import Deployment, DeploymentResult
from apps.images.models import ImageRelease
from apps.stations.models import RolloutGroup, StationAuditLog

QEMU = ImageRelease.Machine.QEMU
RPI = ImageRelease.Machine.RPI


@pytest.fixture
def make_release(db):
    def _make(tag, machine=QEMU, channel="release", ota=True, **kw):
        extra = (
            {
                "rootfs_s3_key": f"r/{channel}/{tag}/{machine}",
                "rootfs_sha256": (machine[0] * 64),
                "rootfs_size_bytes": len(machine),
            }
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
            **extra,
            **kw,
        )

    return _make


@pytest.fixture
def on(station_factory):
    """Station currently running ``release`` (optionally in ``group``)."""

    def _make(release, group=None, **kw):
        return station_factory(current_image_release=release, rollout_group=group, **kw)

    return _make


def _plan_by_name(plans):
    return {p.station.name: p for p in plans}


# --- plan matrix ------------------------------------------------------------


def test_plan_ok_resolves_station_machine(make_release, on):
    q1, r1 = make_release("2026.10.01-01"), make_release("2026.10.01-01", machine=RPI)
    q2, r2 = make_release("2026.10.10-01"), make_release("2026.10.10-01", machine=RPI)
    sq, sr = on(q1), on(r1)

    plans = services.plan_deployment("2026.10.10-01", "release", [sq, sr])

    assert [(p.station, p.release, p.reason) for p in plans] == [
        (sq, q2, services.PLAN_OK),
        (sr, r2, services.PLAN_OK),
    ]
    assert not any(p.channel_switch or p.downgrade for p in plans)
    assert all(p.ok for p in plans)


def test_plan_no_platform(make_release, station_factory):
    make_release("v2")
    bare = station_factory()
    (plan,) = services.plan_deployment("v2", "release", [bare])
    assert plan.reason == services.SKIP_NO_PLATFORM
    assert plan.release is None
    assert "Platform unknown" in plan.message


def test_plan_no_image_for_machine(make_release, on):
    make_release("2026.10.10-01")  # qemu only
    sr = on(make_release("2026.10.01-01", machine=RPI))
    (plan,) = services.plan_deployment("2026.10.10-01", "release", [sr])
    assert plan.reason == services.SKIP_NO_IMAGE
    assert RPI in plan.message


def test_plan_wrong_channel_is_no_image(make_release, on):
    make_release("2026.10.10-01", channel="dev")
    s = on(make_release("2026.10.01-01"))
    (plan,) = services.plan_deployment("2026.10.10-01", "release", [s])
    assert plan.reason == services.SKIP_NO_IMAGE


def test_plan_archived_release_is_no_image(make_release, on):
    target = make_release("2026.10.10-01")
    target.archive()
    s = on(make_release("2026.10.01-01"))
    (plan,) = services.plan_deployment("2026.10.10-01", "release", [s])
    assert plan.reason == services.SKIP_NO_IMAGE


def test_plan_not_ota_ready(make_release, on):
    target = make_release("2026.10.10-01", ota=False)
    s = on(make_release("2026.10.01-01"))
    (plan,) = services.plan_deployment("2026.10.10-01", "release", [s])
    assert plan.reason == services.SKIP_NOT_OTA_READY
    assert plan.release == target
    assert "not OTA-ready" in plan.message


def test_plan_channel_switch_flag(make_release, on, make_rollout_group):
    dev = make_rollout_group("Dev", channel=RolloutGroup.Channel.DEV)
    make_release("2026.10.10-01")
    s_dev = on(make_release("2026.10.01-01"), group=dev)
    s_rel = on(make_release("2026.10.01-01", machine=RPI))
    make_release("2026.10.10-01", machine=RPI)

    plans = _plan_by_name(services.plan_deployment("2026.10.10-01", "release", [s_dev, s_rel]))

    assert plans[s_dev.name].channel_switch is True
    assert plans[s_rel.name].channel_switch is False
    assert plans[s_dev.name].ok


def test_plan_downgrade_flag(make_release, on):
    make_release("2026.10.01-01")
    newer = on(make_release("2026.10.10-01"))
    (plan,) = services.plan_deployment("2026.10.01-01", "release", [newer])
    assert plan.ok
    assert plan.downgrade is True


def test_plan_same_image_is_not_downgrade(make_release, on):
    cur = make_release("2026.10.10-01")
    s = on(cur)
    (plan,) = services.plan_deployment("2026.10.10-01", "release", [s])
    assert plan.ok
    assert plan.release == cur
    assert plan.downgrade is False


def test_plan_single_release_query(make_release, on, django_assert_num_queries):
    make_release("v2")
    make_release("v2", machine=RPI)
    stations = [on(make_release("v1")), on(make_release("v1", machine=RPI))]
    stations = list(
        type(stations[0])
        .objects.filter(pk__in=[s.pk for s in stations])
        .select_related("current_image_release", "rollout_group")
    )
    with django_assert_num_queries(1):
        services.plan_deployment("v2", "release", stations)


# --- available_versions -----------------------------------------------------


def test_available_versions_groups_machines_and_orders(make_release):
    make_release("2026.10.01-01")
    make_release("2026.10.10-01")
    make_release("2026.10.10-01", machine=RPI)
    make_release("2026.10.19-01", channel="dev")
    make_release("2026.10.20-01", ota=False)  # not deployable
    make_release("2026.10.21-01").archive()  # archived

    assert services.available_versions() == [
        ("dev", "2026.10.19-01", [QEMU]),
        ("release", "2026.10.10-01", sorted([QEMU, RPI])),
        ("release", "2026.10.01-01", [QEMU]),
    ]


# --- create_deployment ------------------------------------------------------


def test_create_fleet_mixed_machines(make_release, on, station_factory, staff_user):
    q1, r1 = make_release("v1"), make_release("v1", machine=RPI)
    q2, r2 = make_release("v2"), make_release("v2", machine=RPI)
    sq, sr = on(q1, current_os_version="v1"), on(r1)
    bare = station_factory()

    dep = services.create_deployment(
        tag="v2", channel="release", target_type=Deployment.TargetType.ALL, user=staff_user
    )

    assert dep.status == Deployment.Status.IN_PROGRESS
    assert (dep.image_tag, dep.image_channel, dep.created_by) == ("v2", "release", staff_user)
    results = {r.station_id: r for r in dep.results.all()}
    assert set(results) == {sq.pk, sr.pk}
    assert results[sq.pk].image_release == q2
    assert results[sr.pk].image_release == r2
    assert results[sq.pk].previous_version == "v1"
    assert results[sr.pk].previous_version == ""
    assert all(r.status == DeploymentResult.Status.PENDING for r in results.values())
    dep.refresh_from_db()
    assert dep.skipped_stations == [
        {
            "station_id": bare.pk,
            "station_name": bare.name,
            "reason": services.SKIP_NO_PLATFORM,
            "message": "Platform unknown — station was not provisioned via station-manager.",
        }
    ]


def test_create_group_target_defaults_to_group_members(
    make_release, on, make_rollout_group, staff_user
):
    group = make_rollout_group("Beta")
    cur = make_release("v1")
    make_release("v2")
    member = on(cur, group=group)
    on(cur)  # not in the group

    dep = services.create_deployment(
        tag="v2",
        channel="release",
        target_type=Deployment.TargetType.GROUP,
        target_group=group,
        user=staff_user,
        strategy=Deployment.Strategy.PHASED,
        phase_config={"batch_size": 2},
    )

    assert list(dep.results.values_list("station_id", flat=True)) == [member.pk]
    assert dep.target_group == group
    assert dep.target_group_name == "Beta"
    assert (dep.strategy, dep.phase_config) == (Deployment.Strategy.PHASED, {"batch_size": 2})


def test_create_explicit_stations_restricts_target(make_release, on, staff_user):
    cur = make_release("v1")
    make_release("v2")
    a, _b = on(cur), on(cur)
    dep = services.create_deployment(
        tag="v2",
        channel="release",
        target_type=Deployment.TargetType.ALL,
        stations=[a],
        user=staff_user,
    )
    assert list(dep.results.values_list("station_id", flat=True)) == [a.pk]
    assert dep.skipped_stations == []


def test_create_supersedes_pending(make_release, on, staff_user):
    cur = make_release("v1")
    make_release("v2")
    make_release("v3")
    s = on(cur)
    first = services.create_deployment(
        tag="v2", channel="release", target_type="station", target_station=s, user=staff_user
    )
    second = services.create_deployment(
        tag="v3", channel="release", target_type="station", target_station=s, user=staff_user
    )
    first.refresh_from_db()
    assert first.results.get().status == DeploymentResult.Status.SUPERSEDED
    assert first.status == Deployment.Status.CANCELLED
    assert second.results.get().status == DeploymentResult.Status.PENDING


def test_create_active_conflict_skips_station(make_release, on, staff_user):
    cur = make_release("v1")
    make_release("v2")
    make_release("v3")
    busy, free = on(cur), on(cur)
    first = services.create_deployment(
        tag="v2",
        channel="release",
        target_type="station",
        target_station=busy,
        user=staff_user,
    )
    first.results.update(status=DeploymentResult.Status.INSTALLING)

    dep = services.create_deployment(
        tag="v3", channel="release", target_type="all", user=staff_user
    )

    assert list(dep.results.values_list("station_id", flat=True)) == [free.pk]
    (skip,) = dep.skipped_stations
    assert skip["station_id"] == busy.pk
    assert skip["reason"] == services.SKIP_ACTIVE
    assert "mid-deployment" in skip["message"]
    # The in-flight result is untouched.
    assert first.results.get().status == DeploymentResult.Status.INSTALLING


def test_create_nothing_servable_raises_and_rolls_back(make_release, on, staff_user):
    cur = make_release("v1")
    make_release("v2")
    s = on(cur)
    first = services.create_deployment(
        tag="v2", channel="release", target_type="station", target_station=s, user=staff_user
    )
    first.results.update(status=DeploymentResult.Status.DOWNLOADING)
    n_deployments = Deployment.objects.count()
    n_audits = StationAuditLog.objects.count()

    with pytest.raises(services.NoEligibleStationsError) as excinfo:
        services.create_deployment(
            tag="v2", channel="release", target_type="station", target_station=s, user=staff_user
        )

    assert Deployment.objects.count() == n_deployments
    assert StationAuditLog.objects.count() == n_audits
    (plan,) = excinfo.value.plans
    assert plan.reason == services.SKIP_ACTIVE


def test_create_no_eligible_before_write(make_release, station_factory, staff_user):
    make_release("v2")
    station_factory()
    with pytest.raises(services.NoEligibleStationsError) as excinfo:
        services.create_deployment(tag="v2", channel="release", target_type="all", user=staff_user)
    assert not Deployment.objects.exists()
    assert [p.reason for p in excinfo.value.plans] == [services.SKIP_NO_PLATFORM]


def test_create_empty_target_raises(make_release, staff_user):
    make_release("v2")
    with pytest.raises(services.NoEligibleStationsError):
        services.create_deployment(tag="v2", channel="release", target_type="all", user=staff_user)
    assert not Deployment.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_create_audit_on_commit_per_served_station(make_release, on, station_factory, staff_user):
    cur = make_release("v1")
    make_release("v2")
    a, b = on(cur), on(cur)
    bare = station_factory()

    dep = services.create_deployment(
        tag="v2", channel="release", target_type="all", user=staff_user, ip_address="10.0.0.1"
    )

    logs = StationAuditLog.objects.filter(event_type=StationAuditLog.EventType.FIRMWARE_UPDATE)
    assert sorted(logs.values_list("station_id", flat=True)) == sorted([a.pk, b.pk])
    assert not logs.filter(station=bare).exists()
    log = logs.get(station=a)
    assert log.user == staff_user
    assert log.ip_address == "10.0.0.1"
    assert log.message == f"Deployment #{dep.pk} created: v2 (qemux86-64) targeting {a.name}."


def test_create_audit_deferred_until_commit(make_release, on, staff_user):
    """Inside the test transaction nothing commits → no audit rows yet."""
    s = on(make_release("v1"))
    make_release("v2")
    services.create_deployment(tag="v2", channel="release", target_type="all", user=staff_user)
    assert not StationAuditLog.objects.filter(station=s).exists()


def test_create_custom_audit_message_and_opt_out(
    make_release, on, staff_user, django_capture_on_commit_callbacks
):
    s = on(make_release("v1"), current_os_version="v1")
    make_release("v2")
    make_release("v3")
    with django_capture_on_commit_callbacks(execute=True):
        dep = services.create_deployment(
            tag="v2",
            channel="release",
            target_type="all",
            user=staff_user,
            audit_message=lambda st, d, rel: f"custom {st.current_os_version}->{rel.tag} #{d.pk}",
        )
    assert StationAuditLog.objects.get(station=s).message == f"custom v1->v2 #{dep.pk}"

    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        services.create_deployment(
            tag="v3", channel="release", target_type="all", user=staff_user, audit=False
        )
    assert callbacks == []
    assert StationAuditLog.objects.filter(station=s).count() == 1


def test_deployment_str_uses_version(make_release, on, staff_user):
    on(make_release("v1"))
    make_release("v2", channel="release")
    dep = services.create_deployment(
        tag="v2", channel="release", target_type="all", user=staff_user
    )
    assert str(dep) == f"Deployment #{dep.pk} - v2 [release] (In Progress)"


def test_create_skips_not_ota_ready_but_serves_rest(make_release, on, staff_user):
    make_release("v2", ota=False)
    good = make_release("v2", machine=RPI)
    q = on(make_release("v1"))
    r = on(make_release("v1", machine=RPI))
    dep = services.create_deployment(
        tag="v2", channel="release", target_type="all", user=staff_user
    )
    assert [(x.station_id, x.image_release_id) for x in dep.results.all()] == [(r.pk, good.pk)]
    assert [(s["station_id"], s["reason"]) for s in dep.skipped_stations] == [
        (q.pk, services.SKIP_NOT_OTA_READY)
    ]
