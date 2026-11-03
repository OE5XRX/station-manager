"""Shared fixtures-as-functions for module firmware update-status tests."""

from django.utils import timezone

from apps.module_firmware.models import (
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareConvergenceState,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
)

TARGET = "26.09.15-01"
OLD = "26.09.10-01"


def _online(station, backends=("dfu",)):
    station.last_seen = timezone.now()
    station.agent_capabilities = {"module_ota": {"backends": list(backends)}} if backends else {}
    station.save(update_fields=["last_seen", "agent_capabilities"])
    return station


def _target(fm, version=TARGET):
    return ModuleFirmwareTarget.objects.create(
        module_type=fm, scope=ModuleFirmwareTarget.Scope.FLEET, version=version
    )


def _release(fm, *, variant="vhf", version=TARGET, sim=False, storage_key="k", boot_key=""):
    return ModuleFirmwareRelease.objects.create(
        module_type=fm,
        variant=variant,
        version=version,
        storage_key=storage_key,
        sha256="a" * 64,
        size_bytes=1,
        sim_storage_key="s" if sim else "",
        source_repo="r",
        source_tag="t",
        boot_key_hash=boot_key,
    )


_uid = {"n": 0}


def _module(fm, station, *, slot="slot0", version=OLD, variant="vhf", synthetic=False, key=""):
    _uid["n"] += 1
    m = Module.objects.create(
        uid=f"U{_uid['n']}",
        module_type=fm,
        variant=variant,
        last_reported_version=version,
        reported_boot_key=key,
        uid_source=Module.UidSource.SYNTHETIC if synthetic else Module.UidSource.STM32_UID,
    )
    if station is not None:
        ModuleAssignmentHistory.objects.create(module=m, station=station, slot=slot)
    return m


def _cs(m, rel, **kw):
    return ModuleFirmwareConvergenceState.objects.create(module=m, target_release=rel, **kw)
