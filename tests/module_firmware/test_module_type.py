import pytest

from apps.module_firmware.models import ModuleType


@pytest.mark.django_db
def test_module_type_str_and_unique_key():
    t = ModuleType.objects.create(key="fm", display_name="FM Transceiver")
    assert str(t) == "FM Transceiver"
    with pytest.raises(Exception):
        ModuleType.objects.create(key="fm", display_name="Duplicate")
