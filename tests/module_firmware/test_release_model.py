import pytest
from django.db import IntegrityError
from apps.module_firmware.models import ModuleType, ModuleFirmwareRelease


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


def _mk(fm, variant="vhf", version="26.07.04-01"):
    return ModuleFirmwareRelease.objects.create(
        module_type=fm, variant=variant, version=version,
        storage_key=f"module_firmware/fm/{version}/{variant}.signed.bin",
        sha256="a"*64, size_bytes=1234,
        cosign_bundle_key=f"module_firmware/fm/{version}/{variant}.signed.bin.bundle",
        source_repo="OE5XRX/FW-RemoteStation", source_tag=version,
    )


@pytest.mark.django_db
def test_unique_type_variant_version(fm):
    _mk(fm)
    with pytest.raises(IntegrityError):
        _mk(fm)


@pytest.mark.django_db
def test_soft_delete_hides_from_objects(fm):
    r = _mk(fm)
    r.archive()
    assert ModuleFirmwareRelease.objects.filter(pk=r.pk).count() == 0
    assert ModuleFirmwareRelease.all_objects.filter(pk=r.pk).count() == 1
    r.restore()
    assert ModuleFirmwareRelease.objects.filter(pk=r.pk).count() == 1
