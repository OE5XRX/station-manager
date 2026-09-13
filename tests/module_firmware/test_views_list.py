import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.module_firmware.models import Module, ModuleType

User = get_user_model()


@pytest.fixture
def data(db):
    t = ModuleType.objects.create(key="fm", display_name="FM")
    Module.objects.create(uid="REAL1", module_type=t)
    Module.objects.create(uid="SIM1", module_type=t, uid_source=Module.UidSource.SYNTHETIC)


@pytest.mark.django_db
def test_list_requires_login(client, data):
    resp = client.get(reverse("module_firmware:module_list"))
    assert resp.status_code in (302, 403)


@pytest.mark.django_db
def test_list_visible_to_any_user_and_filters(client, data):
    user = User.objects.create_user(username="u", password="x")
    client.force_login(user)
    resp = client.get(reverse("module_firmware:module_list"))
    assert resp.status_code == 200
    assert b"REAL1" in resp.content and b"SIM1" in resp.content
    resp = client.get(reverse("module_firmware:module_list") + "?uid_source=stm32_uid")
    assert b"REAL1" in resp.content and b"SIM1" not in resp.content
