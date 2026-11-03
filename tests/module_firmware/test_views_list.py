import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.module_firmware.models import Module, ModuleType
from tests.module_firmware.update_status_helpers import (
    _module,
    _online,
    _release,
    _target,
)

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


# --- Task 9: firmware update status column ---------------------------------


@pytest.mark.django_db
def test_list_shows_firmware_column_with_reason_tooltip(client, applicant_user, station):
    fm = ModuleType.objects.create(key="fmx", display_name="FMX")
    _online(station, backends=())  # agent without module OTA -> "Image zu alt"
    _target(fm)
    _release(fm)
    m = _module(fm, station)
    client.force_login(applicant_user)
    body = client.get(reverse("module_firmware:module_list")).content.decode()
    assert "<th>Firmware</th>" in body
    assert m.uid in body
    assert "Wartet" in body
    assert 'title="Wartet: Station-Agent kann keine Module flashen (Image zu alt)"' in body
    # The list does not poll (a reload is enough).
    assert 'hx-trigger="every 5s"' not in body


@pytest.mark.django_db
def test_list_update_status_constant_queries(client, applicant_user, station, station_factory):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    fm = ModuleType.objects.create(key="fmq", display_name="FMQ")
    _online(station)
    _target(fm)
    _release(fm)
    _module(fm, station)
    client.force_login(applicant_user)
    url = reverse("module_firmware:module_list")
    client.get(url)  # warm session/caches
    with CaptureQueriesContext(connection) as one:
        client.get(url)
    for i in range(6):
        s = _online(station_factory())
        _module(fm, s, slot=f"slot{i}")
    with CaptureQueriesContext(connection) as many:
        client.get(url)
    assert len(many) == len(one)
