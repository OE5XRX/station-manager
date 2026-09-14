import pytest

from apps.module_firmware.models import ModuleFirmwareImportJob, ModuleType


@pytest.mark.django_db
def test_import_job_defaults():
    t = ModuleType.objects.create(key="fm", display_name="FM")
    j = ModuleFirmwareImportJob.objects.create(
        module_type=t, source_repo="OE5XRX/FW-RemoteStation", tag="26.07.04-01"
    )
    assert j.status == ModuleFirmwareImportJob.Status.PENDING
    assert j.error_message == "" and j.completed_at is None
