from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command

from apps.module_firmware import storage
from apps.module_firmware.mcuboot import DEV_KEY_HASH_PREFIX
from apps.module_firmware.models import ModuleFirmwareRelease, ModuleType

DEV_BLOB = (Path(__file__).parent.parent / "fixtures" / "mcuboot" / "dev-signed.bin").read_bytes()


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(
        key="fm",
        display_name="FM",
        firmware_repo="OE5XRX/FW-RemoteStation",
        release_asset_prefix="fm-sa818",
    )


def _release(fm, version, *, boot_key_hash="", storage_key="x"):
    return ModuleFirmwareRelease.objects.create(
        module_type=fm,
        variant="vhf",
        version=version,
        storage_key=storage_key,
        sha256="0" * 64,
        size_bytes=1,
        boot_key_hash=boot_key_hash,
    )


def test_backfill_fills_empty_only(fm):
    key_a = storage.release_key("fm", "1.0.0-00", "vhf")
    key_b = storage.release_key("fm", "1.0.1-00", "vhf")
    storage.upload_bytes(key_a, DEV_BLOB)
    storage.upload_bytes(key_b, DEV_BLOB)
    a = _release(fm, "1.0.0-00", storage_key=key_a)
    b = _release(fm, "1.0.1-00", boot_key_hash="f" * 16, storage_key=key_b)

    out = StringIO()
    call_command("backfill_release_boot_keys", stdout=out)
    a.refresh_from_db()
    b.refresh_from_db()
    assert a.boot_key_hash == DEV_KEY_HASH_PREFIX
    assert b.boot_key_hash == "f" * 16
    assert "updated 1 of 1" in out.getvalue()

    out = StringIO()
    call_command("backfill_release_boot_keys", stdout=out)
    a.refresh_from_db()
    assert a.boot_key_hash == DEV_KEY_HASH_PREFIX
    assert "updated 0 of 0" in out.getvalue()


def test_backfill_missing_object_is_skipped(fm):
    r = _release(fm, "1.0.0-00", storage_key="module_firmware/fm/absent.signed.bin")
    err = StringIO()
    call_command("backfill_release_boot_keys", stdout=StringIO(), stderr=err)
    r.refresh_from_db()
    assert r.boot_key_hash == ""
    assert "absent.signed.bin" in err.getvalue()
