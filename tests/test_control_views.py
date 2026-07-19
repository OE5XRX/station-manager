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


# ---------------------------------------------------------------------------
# Task 2: generic descriptor renderer tests
# ---------------------------------------------------------------------------

GENERIC = [  # a second, fictitious module — NOT fm
    {
        "name": "azimuth",
        "kind": "setting",
        "type": "int",
        "ranges": [{"min": 0, "max": 359}],
        "unit": "deg",
    },
    {"name": "preset", "kind": "setting", "type": "enum", "values": ["park", "north", "zenith"]},
    {"name": "heater", "kind": "setting", "type": "bool"},
    {"name": "calibrate", "kind": "action", "type": "bool"},
    {"name": "temperature", "kind": "telemetry", "type": "int", "readonly": True},
]


def _render(client, station):
    return client.get(reverse("control:station_control", args=[station.pk])).content


def test_second_fictitious_module_renders_without_ui_code(client, station, operator):
    StationModule.objects.create(
        station=station,
        slot="slotX",
        module_id="rotator",
        type="rotator",
        capability_descriptor=GENERIC,
        last_state={"azimuth": 90, "preset": "north", "heater": True},
        online=True,
    )
    client.force_login(operator)
    html = _render(client, station)
    assert b'data-cap="azimuth"' in html and b'data-type="int"' in html
    assert b'data-cap="preset"' in html  # enum -> select
    assert b'data-cap="heater"' in html  # bool -> toggle
    assert b'data-cap="calibrate"' in html  # action -> button
    assert b'data-cap="temperature"' in html  # telemetry -> meter


def test_renderer_has_no_fm_or_frequency_hardcode():
    import pathlib

    root = pathlib.Path("apps/control/templates/control")
    for p in root.rglob("*.html"):
        txt = p.read_text().lower()
        assert "frequency" not in txt, f"{p} hardcodes 'frequency'"
        assert '"fm"' not in txt and ">fm<" not in txt, f"{p} hardcodes fm"


def test_offline_module_renders_from_last_state(client, station, operator):
    StationModule.objects.create(
        station=station,
        slot="slot0",
        module_id="fm",
        type="fm",
        capability_descriptor=FM,
        last_state={"frequency": 145.5},
        online=False,
    )
    client.force_login(operator)
    html = _render(client, station).decode()
    assert "145.5" in html  # value present in json_script/SSR
    # offline indicator present
    assert "cp-module" in html


def test_number_input_is_dot_decimal_locale_safe(client, station, operator):
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
    html = _render(client, station).decode()
    assert 'lang="en"' in html and 'inputmode="decimal"' in html
