import pytest
from django.db import IntegrityError

from apps.module_firmware.models import ModuleFirmwareTarget, ModuleType
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
