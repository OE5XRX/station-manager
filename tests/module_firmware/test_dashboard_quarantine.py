import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.module_firmware.models import Module, ModuleType

User = get_user_model()


@pytest.mark.django_db
def test_dashboard_reports_quarantined(client):
    t = ModuleType.objects.create(key="fm", display_name="FM")
    Module.objects.create(uid="A", module_type=t)  # unknown
    Module.objects.create(
        uid="B", module_type=t, firmware_convergence=Module.Convergence.QUARANTINED
    )
    client.force_login(User.objects.create_user(username="u", password="x"))
    resp = client.get(reverse("dashboard:index"))
    assert resp.status_code == 200
    assert resp.context["module_stats"]["quarantined"] == 1
    assert list(resp.context["quarantined_modules"].values_list("uid", flat=True)) == ["B"]
