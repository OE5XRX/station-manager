import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.control.models import StationModule
from apps.stations.models import Station

FM = [
    {
        "name": "frequency",
        "kind": "setting",
        "type": "float",
        "ranges": [{"name": "vhf", "min": 134.0, "max": 174.0}],
    },
    {"name": "ptt", "kind": "action", "type": "bool"},
    {"name": "rssi", "kind": "telemetry", "type": "int", "readonly": True, "min_interval_ms": 250},
]


@pytest.fixture
def station(db):
    return Station.objects.create(name="s1", status="online")


@pytest.fixture
def operator(db, station):
    u = User.objects.create_user(username="op", password="x")
    # Grant can_use_station via the project's assignment mechanism.
    # can_use_station requires membership_level != APPLICANT; role is required
    # for StationAssignment.
    u.membership_level = User.MembershipLevel.MEMBER
    u.save(update_fields=["membership_level"])
    from apps.stations.models import StationAssignment

    StationAssignment.objects.create(
        user=u, station=station, role=StationAssignment.Role.MAINTAINER
    )
    return u


def test_anonymous_redirected(client, station):
    r = client.get(reverse("control:station_control", args=[station.pk]))
    assert r.status_code in (301, 302)


def test_permitted_user_gets_panel(client, station, operator):
    StationModule.objects.create(
        station=station,
        slot="slot0",
        module_id="fm",
        type="fm",
        capability_descriptor=FM,
        last_state={"frequency": 145.5},
        online=True,
    )
    client.force_login(operator)
    r = client.get(reverse("control:station_control", args=[station.pk]))
    assert r.status_code == 200
    assert b'data-cap="frequency"' in r.content
    assert b'id="control-panel"' in r.content


def test_forbidden_user_gets_403(client, station):
    other = User.objects.create_user(username="no", password="x")
    client.force_login(other)
    r = client.get(reverse("control:station_control", args=[station.pk]))
    assert r.status_code == 403
