import pytest
from django.contrib.auth import get_user_model

from apps.module_firmware import services
from apps.module_firmware.models import Module, ModuleType
from apps.stations.models import StationAuditLog

User = get_user_model()


@pytest.fixture
def module(db):
    t = ModuleType.objects.create(key="fm", display_name="FM")
    return Module.objects.create(uid="U1", module_type=t)


@pytest.mark.django_db
def test_confirm_registration(module):
    user = User.objects.create_user(username="admin", password="x", is_staff=True)
    services.confirm_registration(module, user=user)
    module.refresh_from_db()
    assert module.registration_status == Module.Registration.REGISTERED
    assert (
        StationAuditLog.objects.filter(
            module=module, event_type=StationAuditLog.EventType.MODULE_REGISTERED, user=user
        ).count()
        == 1
    )
    # idempotent
    services.confirm_registration(module, user=user)
    assert (
        StationAuditLog.objects.filter(
            module=module, event_type=StationAuditLog.EventType.MODULE_REGISTERED
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_set_lifecycle_defect(module):
    services.set_lifecycle(module, Module.Lifecycle.DEFECT, user=None)
    module.refresh_from_db()
    assert module.lifecycle_status == Module.Lifecycle.DEFECT
