import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.module_firmware.models import Module, ModuleType

User = get_user_model()


@pytest.fixture
def module(db):
    t = ModuleType.objects.create(key="fm", display_name="FM")
    return Module.objects.create(uid="M1", module_type=t)


@pytest.mark.django_db
def test_non_staff_cannot_confirm(client, module):
    client.force_login(User.objects.create_user(username="u", password="x"))
    resp = client.post(reverse("module_firmware:module_confirm", args=[module.uid]))
    assert resp.status_code == 403
    module.refresh_from_db()
    assert module.registration_status == Module.Registration.UNREGISTERED


@pytest.mark.django_db
def test_staff_confirms(client, module):
    client.force_login(User.objects.create_user(username="s", password="x", is_staff=True))
    resp = client.post(reverse("module_firmware:module_confirm", args=[module.uid]))
    assert resp.status_code == 302
    module.refresh_from_db()
    assert module.registration_status == Module.Registration.REGISTERED


@pytest.mark.django_db
def test_staff_sets_lifecycle(client, module):
    client.force_login(User.objects.create_user(username="s", password="x", is_staff=True))
    resp = client.post(
        reverse("module_firmware:module_lifecycle", args=[module.uid]),
        {"lifecycle_status": "defect"},
    )
    assert resp.status_code == 302
    module.refresh_from_db()
    assert module.lifecycle_status == Module.Lifecycle.DEFECT


@pytest.mark.django_db
def test_deployed_is_not_an_operator_settable_lifecycle(client, module):
    """`deployed` is auto-derived from assignment; a manual set must be ignored."""
    client.force_login(User.objects.create_user(username="s", password="x", is_staff=True))
    resp = client.post(
        reverse("module_firmware:module_lifecycle", args=[module.uid]),
        {"lifecycle_status": "deployed"},
    )
    assert resp.status_code == 302
    module.refresh_from_db()
    assert module.lifecycle_status == Module.Lifecycle.READY  # unchanged


@pytest.mark.django_db
def test_staff_sets_notes(client, module):
    client.force_login(User.objects.create_user(username="s", password="x", is_staff=True))
    resp = client.post(
        reverse("module_firmware:module_notes", args=[module.uid]), {"notes": "bench unit"}
    )
    assert resp.status_code == 302
    module.refresh_from_db()
    assert module.notes == "bench unit"
