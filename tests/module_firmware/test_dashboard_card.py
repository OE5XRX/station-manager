import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.module_firmware.models import Module, ModuleType

User = get_user_model()


@pytest.mark.django_db
def test_dashboard_shows_module_counts(client):
    t = ModuleType.objects.create(key="fm", display_name="FM")
    Module.objects.create(uid="A", module_type=t)  # unregistered
    Module.objects.create(uid="B", module_type=t, lifecycle_status=Module.Lifecycle.DEFECT)
    client.force_login(User.objects.create_user(username="u", password="x"))
    resp = client.get(reverse("dashboard:index"))
    assert resp.status_code == 200
    assert resp.context["module_stats"]["total"] == 2
    assert resp.context["module_stats"]["unregistered"] == 2
    assert resp.context["module_stats"]["attention"] == 1
