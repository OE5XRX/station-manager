import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.control.models import StationModule
from apps.module_firmware.models import Module, ModuleType

User = get_user_model()


@pytest.mark.django_db
def test_station_control_panel_links_to_module_detail(client, station_factory):
    station = station_factory()
    t = ModuleType.objects.create(key="fm", display_name="FM")
    mod = Module.objects.create(uid="LINK1", module_type=t)
    StationModule.objects.create(
        station=station, slot="slot1", module_id="fm", type="fm",
        tracked_module=mod, online=True,
    )
    # can_use_station passes for any non-Applicant user.
    client.force_login(
        User.objects.create_user(
            username="s", password="x", membership_level=User.MembershipLevel.MEMBER
        )
    )
    resp = client.get(reverse("control:station_control", args=[station.pk]))
    assert resp.status_code == 200
    assert reverse("module_firmware:module_detail", args=["LINK1"]).encode() in resp.content
