import pytest
from django.contrib import admin
from django.contrib.admin.sites import site
from django.test import RequestFactory

from apps.module_firmware.admin import ModuleFirmwareTargetForm
from apps.module_firmware.models import (
    ModuleFirmwareConvergenceState,
    ModuleFirmwareImportJob,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
    ModuleType,
)


def test_models_registered():
    assert ModuleFirmwareRelease in admin.site._registry
    assert ModuleFirmwareImportJob in admin.site._registry


def test_import_job_admin_readonly_includes_identity_fields():
    """module_type, source_repo, tag must be readonly — jobs are UI/worker-created."""
    job_admin = admin.site._registry[ModuleFirmwareImportJob]
    ro = job_admin.readonly_fields
    assert "module_type" in ro
    assert "source_repo" in ro
    assert "tag" in ro


def test_import_job_admin_no_add_change_delete():
    """Import jobs must not be creatable/editable/deletable via admin."""
    job_admin = admin.site._registry[ModuleFirmwareImportJob]
    request = RequestFactory().get("/")
    assert job_admin.has_add_permission(request) is False
    assert job_admin.has_change_permission(request) is False
    assert job_admin.has_delete_permission(request) is False


@pytest.mark.django_db
def test_target_and_convergence_registered_in_admin():
    assert ModuleFirmwareTarget in site._registry
    assert ModuleFirmwareConvergenceState in site._registry


@pytest.mark.django_db
def test_convergence_admin_is_readonly():
    admin_obj = site._registry[ModuleFirmwareConvergenceState]
    assert admin_obj.has_add_permission(request=None) is False


def _release(fm, version="26.09.15-01", variant="vhf"):
    return ModuleFirmwareRelease.objects.create(
        module_type=fm,
        variant=variant,
        version=version,
        storage_key="k",
        sha256="a" * 64,
        size_bytes=1,
        source_repo="r",
        source_tag="t",
    )


@pytest.mark.django_db
def test_target_admin_form_rejects_version_without_release():
    # F6: version is not free text — must match a non-archived release for the
    # module_type, else the form is invalid (no silent typo/unavailable version).
    fm = ModuleType.objects.create(key="fm", display_name="FM")
    _release(fm, version="26.09.15-01")
    form = ModuleFirmwareTargetForm(
        data={
            "module_type": fm.pk,
            "version": "26.09.99-99",  # no release for this version
            "scope": ModuleFirmwareTarget.Scope.FLEET,
        }
    )
    assert form.is_valid() is False
    assert "version" in form.errors


@pytest.mark.django_db
def test_target_admin_form_accepts_valid_version():
    # F6: a version with a matching release for the module_type passes.
    fm = ModuleType.objects.create(key="fm", display_name="FM")
    _release(fm, version="26.09.15-01")
    form = ModuleFirmwareTargetForm(
        data={
            "module_type": fm.pk,
            "version": "26.09.15-01",
            "scope": ModuleFirmwareTarget.Scope.FLEET,
        }
    )
    assert form.is_valid() is True, form.errors
