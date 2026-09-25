import pytest
from django.db import IntegrityError

from apps.module_firmware.models import (
    Module,
    ModuleFirmwareConvergenceState,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
    ModuleType,
    QUARANTINE_ATTEMPT_LIMIT,
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
def test_quarantine_limit_is_three():
    assert QUARANTINE_ATTEMPT_LIMIT == 3


@pytest.mark.django_db
def test_convergence_state_unique_per_module_release(fm):
    m = Module.objects.create(uid="C1", module_type=fm, variant="vhf")
    rel = ModuleFirmwareRelease.objects.create(
        module_type=fm, variant="vhf", version="1",
        storage_key="k", sha256="a" * 64, size_bytes=1, source_repo="r", source_tag="t",
    )
    ModuleFirmwareConvergenceState.objects.create(module=m, target_release=rel)
    with pytest.raises(IntegrityError):
        ModuleFirmwareConvergenceState.objects.create(module=m, target_release=rel)


@pytest.mark.django_db
def test_convergence_state_defaults(fm):
    m = Module.objects.create(uid="C2", module_type=fm, variant="vhf")
    rel = ModuleFirmwareRelease.objects.create(
        module_type=fm, variant="vhf", version="1",
        storage_key="k", sha256="b" * 64, size_bytes=1, source_repo="r", source_tag="t",
    )
    cs = ModuleFirmwareConvergenceState.objects.create(module=m, target_release=rel)
    assert cs.state == ModuleFirmwareConvergenceState.State.UPDATING
    assert cs.attempts == 0
    assert cs.last_error_mode == ""
    assert cs.last_attempt_at is None
