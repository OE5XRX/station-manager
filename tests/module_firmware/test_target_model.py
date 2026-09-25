import pytest
from django.db import IntegrityError, transaction

from apps.module_firmware.models import (
    QUARANTINE_ATTEMPT_LIMIT,
    Module,
    ModuleFirmwareConvergenceState,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
    ModuleType,
)
from apps.stations.models import StationTag


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


@pytest.mark.django_db
def test_scope_choices():
    assert set(ModuleFirmwareTarget.Scope.values) == {"fleet", "tag", "station"}


@pytest.mark.django_db
def test_single_fleet_target_per_module_type(fm):
    ModuleFirmwareTarget.objects.create(
        module_type=fm, version="26.09.15-01", scope=ModuleFirmwareTarget.Scope.FLEET
    )
    with pytest.raises(IntegrityError):
        ModuleFirmwareTarget.objects.create(
            module_type=fm, version="26.09.16-01", scope=ModuleFirmwareTarget.Scope.FLEET
        )


@pytest.mark.django_db
def test_single_tag_target_per_type_tag(fm):
    tag = StationTag.objects.create(name="canary", slug="canary")
    ModuleFirmwareTarget.objects.create(
        module_type=fm, version="1", scope=ModuleFirmwareTarget.Scope.TAG, tag=tag
    )
    with pytest.raises(IntegrityError):
        ModuleFirmwareTarget.objects.create(
            module_type=fm, version="2", scope=ModuleFirmwareTarget.Scope.TAG, tag=tag
        )


@pytest.mark.django_db
def test_fleet_target_with_tag_violates_constraint(fm):
    # F8: a fleet target must not carry a tag/station ref.
    tag = StationTag.objects.create(name="t", slug="t")
    with pytest.raises(IntegrityError), transaction.atomic():
        ModuleFirmwareTarget.objects.create(
            module_type=fm, version="1", scope=ModuleFirmwareTarget.Scope.FLEET, tag=tag
        )


@pytest.mark.django_db
def test_tag_target_without_tag_violates_constraint(fm):
    # F8: a tag target must set a tag.
    with pytest.raises(IntegrityError), transaction.atomic():
        ModuleFirmwareTarget.objects.create(
            module_type=fm, version="1", scope=ModuleFirmwareTarget.Scope.TAG
        )


@pytest.mark.django_db
def test_station_target_without_station_violates_constraint(fm):
    # F8: a station target must set a station.
    with pytest.raises(IntegrityError), transaction.atomic():
        ModuleFirmwareTarget.objects.create(
            module_type=fm, version="1", scope=ModuleFirmwareTarget.Scope.STATION
        )


@pytest.mark.django_db
def test_station_target_with_tag_violates_constraint(fm, station_factory):
    # F8: a station target must not also carry a tag.
    st = station_factory()
    tag = StationTag.objects.create(name="t", slug="t")
    with pytest.raises(IntegrityError), transaction.atomic():
        ModuleFirmwareTarget.objects.create(
            module_type=fm,
            version="1",
            scope=ModuleFirmwareTarget.Scope.STATION,
            station=st,
            tag=tag,
        )


@pytest.mark.django_db
def test_valid_scoped_targets_satisfy_constraints(fm, station_factory):
    # F8: the coherent shapes (fleet/no-ref, tag/tag-only, station/station-only)
    # all save cleanly.
    st = station_factory()
    tag = StationTag.objects.create(name="t", slug="t")
    ModuleFirmwareTarget.objects.create(
        module_type=fm, version="1", scope=ModuleFirmwareTarget.Scope.FLEET
    )
    ModuleFirmwareTarget.objects.create(
        module_type=fm, version="1", scope=ModuleFirmwareTarget.Scope.TAG, tag=tag
    )
    ModuleFirmwareTarget.objects.create(
        module_type=fm, version="1", scope=ModuleFirmwareTarget.Scope.STATION, station=st
    )
    assert ModuleFirmwareTarget.objects.count() == 3


@pytest.mark.django_db
def test_fleet_target_with_canary_tag_satisfies_constraint(fm):
    # F8 watch: canary_tag is separate from tag — a fleet target with a canary_tag
    # (and no tag/station) still satisfies the fleet constraint.
    canary = StationTag.objects.create(name="canary", slug="canary")
    ModuleFirmwareTarget.objects.create(
        module_type=fm,
        version="1",
        scope=ModuleFirmwareTarget.Scope.FLEET,
        canary_tag=canary,
    )
    assert ModuleFirmwareTarget.objects.filter(canary_tag=canary).count() == 1


@pytest.mark.django_db
def test_tag_target_with_canary_tag_violates_constraint(fm):
    # R2-3: canary_tag is a fleet-only gate — a tag target must not carry one.
    tag = StationTag.objects.create(name="t", slug="t")
    canary = StationTag.objects.create(name="canary", slug="canary")
    with pytest.raises(IntegrityError), transaction.atomic():
        ModuleFirmwareTarget.objects.create(
            module_type=fm,
            version="1",
            scope=ModuleFirmwareTarget.Scope.TAG,
            tag=tag,
            canary_tag=canary,
        )


@pytest.mark.django_db
def test_station_target_with_canary_tag_violates_constraint(fm, station_factory):
    # R2-3: a station target must not carry a canary_tag either.
    st = station_factory()
    canary = StationTag.objects.create(name="canary", slug="canary")
    with pytest.raises(IntegrityError), transaction.atomic():
        ModuleFirmwareTarget.objects.create(
            module_type=fm,
            version="1",
            scope=ModuleFirmwareTarget.Scope.STATION,
            station=st,
            canary_tag=canary,
        )


@pytest.mark.django_db
def test_quarantine_limit_is_three():
    assert QUARANTINE_ATTEMPT_LIMIT == 3


@pytest.mark.django_db
def test_convergence_state_unique_per_module_release(fm):
    m = Module.objects.create(uid="C1", module_type=fm, variant="vhf")
    rel = ModuleFirmwareRelease.objects.create(
        module_type=fm,
        variant="vhf",
        version="1",
        storage_key="k",
        sha256="a" * 64,
        size_bytes=1,
        source_repo="r",
        source_tag="t",
    )
    ModuleFirmwareConvergenceState.objects.create(module=m, target_release=rel)
    with pytest.raises(IntegrityError):
        ModuleFirmwareConvergenceState.objects.create(module=m, target_release=rel)


@pytest.mark.django_db
def test_convergence_state_defaults(fm):
    m = Module.objects.create(uid="C2", module_type=fm, variant="vhf")
    rel = ModuleFirmwareRelease.objects.create(
        module_type=fm,
        variant="vhf",
        version="1",
        storage_key="k",
        sha256="b" * 64,
        size_bytes=1,
        source_repo="r",
        source_tag="t",
    )
    cs = ModuleFirmwareConvergenceState.objects.create(module=m, target_release=rel)
    assert cs.state == ModuleFirmwareConvergenceState.State.UPDATING
    assert cs.attempts == 0
    assert cs.last_error_mode == ""
    assert cs.last_attempt_at is None
