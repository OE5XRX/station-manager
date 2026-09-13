import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.module_firmware.models import Module, ModuleAssignmentHistory, ModuleType
from apps.stations.models import StationAuditLog

User = get_user_model()


@pytest.fixture
def module(db, station_factory):
    t = ModuleType.objects.create(key="fm", display_name="FM")
    m = Module.objects.create(uid="LIFE1", module_type=t)
    ModuleAssignmentHistory.objects.create(module=m, station=station_factory(), slot="slot1")
    StationAuditLog.log(
        module=m, event_type=StationAuditLog.EventType.MODULE_DISCOVERED, message="x"
    )
    return m


@pytest.mark.django_db
def test_detail_shows_history_and_audit(client, module):
    client.force_login(User.objects.create_user(username="u", password="x"))
    resp = client.get(reverse("module_firmware:module_detail", args=[module.uid]))
    assert resp.status_code == 200
    assert b"LIFE1" in resp.content
    assert b"slot1" in resp.content


@pytest.mark.django_db
def test_detail_edit_controls_only_for_staff(client, module):
    client.force_login(User.objects.create_user(username="u", password="x"))
    resp = client.get(reverse("module_firmware:module_detail", args=[module.uid]))
    assert resp.context["can_edit"] is False
    client.force_login(User.objects.create_user(username="s", password="x", is_staff=True))
    resp = client.get(reverse("module_firmware:module_detail", args=[module.uid]))
    assert resp.context["can_edit"] is True
