import pytest

from apps.module_firmware.models import Module, ModuleType


@pytest.fixture
def fm_type(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


@pytest.mark.django_db
def test_module_defaults_and_orthogonal_status(fm_type):
    m = Module.objects.create(uid="ABC123", module_type=fm_type)
    assert m.uid_source == Module.UidSource.STM32_UID
    assert m.lifecycle_status == Module.Lifecycle.READY
    assert m.registration_status == Module.Registration.UNREGISTERED
    # orthogonal: can be deployed AND unregistered
    m.lifecycle_status = Module.Lifecycle.DEPLOYED
    m.save()
    assert m.registration_status == Module.Registration.UNREGISTERED


@pytest.mark.django_db
def test_module_uid_unique(fm_type):
    Module.objects.create(uid="DUP", module_type=fm_type)
    with pytest.raises(Exception):
        Module.objects.create(uid="DUP", module_type=fm_type)
