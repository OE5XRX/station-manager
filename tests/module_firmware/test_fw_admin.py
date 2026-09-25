import pytest
from django.contrib import admin
from django.contrib.admin.sites import site
from django.test import RequestFactory

from apps.module_firmware.models import (
    ModuleFirmwareConvergenceState,
    ModuleFirmwareImportJob,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
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
