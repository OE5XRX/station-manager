import asyncio
import hashlib
from unittest import mock

import pytest
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from apps.images.github_releases import GitHubRelease
from apps.module_firmware import nudge, releases
from apps.module_firmware.models import (
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareImportJob,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
    ModuleType,
)
from apps.module_firmware.services import reconcile_module_type

FRAME = {"type": "control.to_agent", "frame": {"v": 1, "type": "firmware_check"}}


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(
        key="fm",
        display_name="FM",
        firmware_repo="OE5XRX/FW-RemoteStation",
        release_asset_prefix="fm-sa818",
    )


async def _receive(layer, channel):
    return await asyncio.wait_for(layer.receive(channel), timeout=0.05)


@pytest.fixture
def listener():
    """Subscribe a channel to a station's agent group; returns a drain fn."""
    layer = get_channel_layer()
    state = {}

    def subscribe(station_id):
        ch = async_to_sync(layer.new_channel)()
        async_to_sync(layer.group_add)(f"control_{station_id}_agent", ch)
        state[station_id] = ch

    def drain(station_id):
        out = []
        while True:
            try:
                msg = async_to_sync(_receive)(layer, state[station_id])
            except Exception:
                return out
            out.append(msg)

    subscribe.drain = drain
    return subscribe


def _release(fm, version="26.09.15-01"):
    return ModuleFirmwareRelease.objects.create(
        module_type=fm,
        variant="vhf",
        version=version,
        storage_key="k",
        sha256="a" * 64,
        size_bytes=1,
        source_repo="r",
        source_tag="t",
    )


def _module(fm, station, version="26.09.10-01"):
    m = Module.objects.create(
        uid="U1", module_type=fm, variant="vhf", last_reported_version=version
    )
    ModuleAssignmentHistory.objects.create(module=m, station=station, slot="slot0")
    return m


def _target(fm, version="26.09.15-01"):
    return ModuleFirmwareTarget.objects.create(
        module_type=fm, scope=ModuleFirmwareTarget.Scope.FLEET, version=version
    )


@pytest.mark.django_db
def test_target_create_nudges_affected_station(
    fm, station, listener, django_capture_on_commit_callbacks
):
    listener(station.pk)
    _release(fm)
    _module(fm, station)
    _target(fm)
    with django_capture_on_commit_callbacks(execute=True):
        reconcile_module_type(fm)
    assert listener.drain(station.pk) == [FRAME]


@pytest.mark.django_db
def test_no_nudge_for_converged_station(fm, station, listener, django_capture_on_commit_callbacks):
    """Converged (e.g. target deleted/moved to its version): no firmware_check nudge,
    but the open panels still get a fresh lock frame (the drain may have ended)."""
    listener(station.pk)
    viewers = _viewer(station.pk)
    _release(fm)
    _module(fm, station, version="26.09.15-01")
    _target(fm)
    with django_capture_on_commit_callbacks(execute=True):
        reconcile_module_type(fm)
    assert listener.drain(station.pk) == []
    locks = [m for m in viewers() if m["type"] == "control.lock"]
    assert len(locks) == 1
    assert locks[0]["lock"]["drain"] is None
    assert locks[0]["lock"]["state"] == "free"


@pytest.mark.django_db
def test_lock_refresh_for_every_reconciled_station_once(
    fm, station, listener, django_capture_on_commit_callbacks
):
    listener(station.pk)
    viewers = _viewer(station.pk)
    _release(fm)
    _module(fm, station)
    _target(fm)
    with django_capture_on_commit_callbacks(execute=True):
        reconcile_module_type(fm)
    assert listener.drain(station.pk) == [FRAME]
    assert [m["type"] for m in viewers()] == ["control.lock"]


@pytest.mark.django_db
def test_reconcile_module_type_only_loads_its_type(fm, station):
    """Open assignments of OTHER module types are neither loaded nor reconciled."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    other = ModuleType.objects.create(key="other", display_name="Other")
    for i in range(5):
        m = Module.objects.create(uid=f"O{i}", module_type=other, variant="vhf")
        ModuleAssignmentHistory.objects.create(module=m, station=station, slot=f"slot{i + 1}")
    with (
        mock.patch("apps.module_firmware.services.reconcile_module") as rec,
        CaptureQueriesContext(connection) as ctx,
    ):
        assert reconcile_module_type(fm) == 0
    rec.assert_not_called()
    history = ModuleAssignmentHistory._meta.db_table
    assignment_sql = [q["sql"] for q in ctx.captured_queries if history in q["sql"]][0]
    assert "module_type_id" in assignment_sql


def _viewer(station_id):
    """Subscribe a channel to the station's viewer group; returns a drain fn."""
    layer = get_channel_layer()
    ch = async_to_sync(layer.new_channel)()
    async_to_sync(layer.group_add)(f"control_{station_id}", ch)

    def drain():
        out = []
        while True:
            try:
                out.append(async_to_sync(_receive)(layer, ch))
            except Exception:
                return out

    return drain


@pytest.mark.django_db
def test_nudge_dedup(station, listener, django_capture_on_commit_callbacks):
    listener(station.pk)
    with django_capture_on_commit_callbacks(execute=True):
        nudge.nudge_stations([station.pk, station.pk])
    assert len(listener.drain(station.pk)) == 1


@pytest.mark.django_db
def test_channel_layer_failure_swallowed(station, django_capture_on_commit_callbacks):
    layer = mock.Mock()
    layer.group_send = mock.AsyncMock(side_effect=RuntimeError("redis down"))
    with mock.patch("apps.module_firmware.nudge.get_channel_layer", return_value=layer):
        with django_capture_on_commit_callbacks(execute=True):
            nudge.nudge_stations([station.pk])
    layer.group_send.assert_awaited_once()


def _blob(variant):
    return f"signed-{variant}".encode()


def _fake_download(url):
    name = url.rsplit("/", 1)[-1]
    if name == "SHA256SUMS":
        return f"{hashlib.sha256(_blob('vhf')).hexdigest()}  fm-sa818-vhf.signed.bin\n".encode()
    if name.endswith(".signed.bin"):
        return _blob("vhf")
    return b"bundle"


@pytest.mark.django_db
def test_import_publish_triggers_reconcile_and_nudge(
    fm, station, listener, django_capture_on_commit_callbacks
):
    listener(station.pk)
    _module(fm, station)
    _target(fm, "26.09.20-01")  # target exists, release not yet imported
    job = ModuleFirmwareImportJob.objects.create(
        module_type=fm, source_repo=fm.firmware_repo, tag="26.09.20-01"
    )
    rel = GitHubRelease(
        tag="26.09.20-01",
        html_url="",
        is_latest=True,
        asset_names=frozenset(
            {"fm-sa818-vhf.signed.bin", "fm-sa818-vhf.signed.bin.bundle", "SHA256SUMS"}
        ),
    )
    with (
        mock.patch("apps.module_firmware.releases.fetch_release_by_tag", return_value=rel),
        mock.patch("apps.module_firmware.releases._download", side_effect=_fake_download),
        mock.patch("apps.module_firmware.releases.verify_blob_identity"),
        mock.patch("apps.module_firmware.releases.storage.upload_bytes"),
        django_capture_on_commit_callbacks(execute=True),
    ):
        releases.import_release_tag(job)
    job.refresh_from_db()
    assert job.status == ModuleFirmwareImportJob.Status.READY
    assert listener.drain(station.pk) == [FRAME]


@pytest.mark.django_db
def test_release_quarantine_refreshes_lock_and_nudges(
    fm, station, listener, django_capture_on_commit_callbacks
):
    """Releasing a quarantine restarts the drain: open panels get a fresh lock frame
    (drain banner) and the agent a firmware_check nudge, both after commit."""
    from django.contrib.auth import get_user_model

    from apps.module_firmware import services
    from apps.module_firmware.models import ModuleFirmwareConvergenceState

    user = get_user_model().objects.create_user(
        username="staff", password="x", membership_level="staff"
    )
    from django.utils import timezone

    station.last_seen = timezone.now()
    station.agent_capabilities = {"module_ota": {"backends": ["dfu"]}}
    station.save(update_fields=["last_seen", "agent_capabilities"])
    listener(station.pk)
    viewers = _viewer(station.pk)
    rel = _release(fm)
    m = _module(fm, station)
    Module.objects.filter(pk=m.pk).update(firmware_convergence=Module.Convergence.QUARANTINED)
    m.refresh_from_db()
    _target(fm)
    ModuleFirmwareConvergenceState.objects.create(
        module=m,
        target_release=rel,
        state=ModuleFirmwareConvergenceState.State.QUARANTINED,
        attempts=3,
        last_error_mode="rolled_back",
    )
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        assert services.release_quarantine(m, user=user) == [rel.version]
        assert listener.drain(station.pk) == []  # nothing before commit
    assert callbacks
    assert listener.drain(station.pk) == [FRAME]
    locks = [msg for msg in viewers() if msg["type"] == "control.lock"]
    assert len(locks) == 1
    assert locks[0]["lock"]["drain"] is not None


@pytest.mark.django_db
def test_release_quarantine_resets_transient_failures_so_drain_restarts(
    fm, station, listener, django_capture_on_commit_callbacks
):
    """A row quarantined with transient_failures >= DRAIN_TRANSIENT_LIMIT must drain
    again after the release (the streak is reset with the attempts)."""
    from django.contrib.auth import get_user_model
    from django.utils import timezone

    from apps.module_firmware import services
    from apps.module_firmware.drain import drain_status
    from apps.module_firmware.models import DRAIN_TRANSIENT_LIMIT, ModuleFirmwareConvergenceState

    user = get_user_model().objects.create_user(
        username="staff", password="x", membership_level="staff"
    )
    station.last_seen = timezone.now()
    station.agent_capabilities = {"module_ota": {"backends": ["dfu"]}}
    station.save(update_fields=["last_seen", "agent_capabilities"])
    listener(station.pk)
    viewers = _viewer(station.pk)
    rel = _release(fm)
    m = _module(fm, station)
    Module.objects.filter(pk=m.pk).update(firmware_convergence=Module.Convergence.QUARANTINED)
    m.refresh_from_db()
    _target(fm)
    row = ModuleFirmwareConvergenceState.objects.create(
        module=m,
        target_release=rel,
        state=ModuleFirmwareConvergenceState.State.QUARANTINED,
        attempts=1,
        transient_failures=DRAIN_TRANSIENT_LIMIT,
        last_error_mode="rejected",
    )
    with django_capture_on_commit_callbacks(execute=True):
        assert services.release_quarantine(m, user=user) == [rel.version]
    row.refresh_from_db()
    assert row.transient_failures == 0
    assert drain_status(station).active
    locks = [msg for msg in viewers() if msg["type"] == "control.lock"]
    assert len(locks) == 1
    assert locks[0]["lock"]["drain"] is not None
