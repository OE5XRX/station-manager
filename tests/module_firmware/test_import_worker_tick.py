from unittest import mock

import pytest

from apps.module_firmware.models import ModuleFirmwareImportJob, ModuleType
from apps.provisioning.management.commands.run_background_jobs import (
    process_pending_module_firmware_imports,
)


@pytest.mark.django_db
def test_tick_claims_and_runs_pending():
    t = ModuleType.objects.create(key="fm", display_name="FM", release_asset_prefix="fm-sa818")
    job = ModuleFirmwareImportJob.objects.create(module_type=t, source_repo="r", tag="26.07.04-01")
    with mock.patch(
        "apps.provisioning.management.commands.run_background_jobs.mfw_releases.import_release_tag"
    ) as imp:
        process_pending_module_firmware_imports()
    assert imp.call_count == 1
    assert imp.call_args.args[0].pk == job.pk
