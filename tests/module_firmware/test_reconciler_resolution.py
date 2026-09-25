# tests/module_firmware/test_reconciler_resolution.py
import pytest

from apps.module_firmware.models import (
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
    ModuleType,
)
from apps.module_firmware.reconciler import desired_release_for_module, effective_target
from apps.stations.models import StationTag


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


def _target(fm, scope, version, **kw):
    return ModuleFirmwareTarget.objects.create(module_type=fm, scope=scope, version=version, **kw)


@pytest.mark.django_db
def test_precedence_station_over_tag_over_fleet(fm, station_factory):
    st = station_factory()
    tag = StationTag.objects.create(name="t", slug="t")
    st.tags.add(tag)
    _target(fm, ModuleFirmwareTarget.Scope.FLEET, "fleet-v")
    _target(fm, ModuleFirmwareTarget.Scope.TAG, "tag-v", tag=tag)
    _target(fm, ModuleFirmwareTarget.Scope.STATION, "station-v", station=st)
    assert effective_target(st, fm).version == "station-v"


@pytest.mark.django_db
def test_tag_over_fleet_when_no_station_target(fm, station_factory):
    st = station_factory()
    tag = StationTag.objects.create(name="t", slug="t")
    st.tags.add(tag)
    _target(fm, ModuleFirmwareTarget.Scope.FLEET, "fleet-v")
    _target(fm, ModuleFirmwareTarget.Scope.TAG, "tag-v", tag=tag)
    assert effective_target(st, fm).version == "tag-v"


@pytest.mark.django_db
def test_multiple_tag_targets_newest_updated_at_wins(fm, station_factory):
    st = station_factory()
    a = StationTag.objects.create(name="a", slug="a")
    b = StationTag.objects.create(name="b", slug="b")
    st.tags.add(a, b)
    _target(fm, ModuleFirmwareTarget.Scope.TAG, "a-v", tag=a)
    tb = _target(fm, ModuleFirmwareTarget.Scope.TAG, "b-v", tag=b)
    # Force tb to be the newer updated_at.
    tb.version = "b-v2"
    tb.save()
    assert effective_target(st, fm).pk == tb.pk


@pytest.mark.django_db
def test_canary_gate_excludes_non_canary_station(fm, station_factory):
    # Review Focus #2: fleet target gated on canary_tag, station not in tag.
    st = station_factory()
    canary = StationTag.objects.create(name="canary", slug="canary")
    _target(fm, ModuleFirmwareTarget.Scope.FLEET, "fleet-v", canary_tag=canary)
    assert effective_target(st, fm) is None


@pytest.mark.django_db
def test_canary_gate_includes_canary_station(fm, station_factory):
    st = station_factory()
    canary = StationTag.objects.create(name="canary", slug="canary")
    st.tags.add(canary)
    _target(fm, ModuleFirmwareTarget.Scope.FLEET, "fleet-v", canary_tag=canary)
    assert effective_target(st, fm).version == "fleet-v"


@pytest.mark.django_db
def test_desired_release_resolves_type_and_variant(fm, station_factory):
    st = station_factory()
    m = Module.objects.create(uid="U1", module_type=fm, variant="vhf")
    ModuleAssignmentHistory.objects.create(module=m, station=st, slot="slot0")
    _target(fm, ModuleFirmwareTarget.Scope.FLEET, "26.09.15-01")
    rel = ModuleFirmwareRelease.objects.create(
        module_type=fm,
        variant="vhf",
        version="26.09.15-01",
        storage_key="k",
        sha256="a" * 64,
        size_bytes=1,
        source_repo="r",
        source_tag="t",
    )
    assert desired_release_for_module(m).pk == rel.pk


@pytest.mark.django_db
def test_no_release_for_variant_returns_none(fm, station_factory):
    # Review Focus #3: type matches but no release for the module's variant.
    st = station_factory()
    m = Module.objects.create(uid="U2", module_type=fm, variant="uhf")
    ModuleAssignmentHistory.objects.create(module=m, station=st, slot="slot0")
    _target(fm, ModuleFirmwareTarget.Scope.FLEET, "26.09.15-01")
    ModuleFirmwareRelease.objects.create(
        module_type=fm,
        variant="vhf",
        version="26.09.15-01",
        storage_key="k",
        sha256="a" * 64,
        size_bytes=1,
        source_repo="r",
        source_tag="t",
    )
    assert desired_release_for_module(m) is None


@pytest.mark.django_db
def test_no_open_assignment_returns_none(fm):
    m = Module.objects.create(uid="U3", module_type=fm, variant="vhf")
    assert desired_release_for_module(m) is None


@pytest.mark.django_db
def test_archived_release_is_not_desired(fm, station_factory):
    st = station_factory()
    m = Module.objects.create(uid="U4", module_type=fm, variant="vhf")
    ModuleAssignmentHistory.objects.create(module=m, station=st, slot="slot0")
    _target(fm, ModuleFirmwareTarget.Scope.FLEET, "26.09.15-01")
    rel = ModuleFirmwareRelease.objects.create(
        module_type=fm,
        variant="vhf",
        version="26.09.15-01",
        storage_key="k",
        sha256="a" * 64,
        size_bytes=1,
        source_repo="r",
        source_tag="t",
    )
    rel.archive()
    assert desired_release_for_module(m) is None
