from django.contrib import admin

from apps.module_firmware.models import ModuleFirmwareImportJob, ModuleFirmwareRelease


def test_models_registered():
    assert ModuleFirmwareRelease in admin.site._registry
    assert ModuleFirmwareImportJob in admin.site._registry
