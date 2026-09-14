import pytest

from apps.module_firmware.models import ModuleType


@pytest.mark.django_db
def test_moduletype_fw_source_fields():
    t = ModuleType.objects.create(
        key="fm",
        display_name="FM",
        firmware_repo="OE5XRX/FW-RemoteStation",
        release_asset_prefix="fm-sa818",
    )
    assert t.firmware_repo == "OE5XRX/FW-RemoteStation"
    assert t.release_asset_prefix == "fm-sa818"


@pytest.mark.django_db
def test_moduletype_fw_fields_optional():
    t = ModuleType.objects.create(key="power", display_name="Power")
    assert t.firmware_repo == "" and t.release_asset_prefix == ""
