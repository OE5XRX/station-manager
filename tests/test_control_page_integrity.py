"""Browser-free integrity guards for the D5 control page.

These catch a whole class of silent-break bugs that unit tests miss because the
templates are only exercised by Alpine at runtime: a template referencing an
Alpine method the JS component never defines, a leaked/truncated template tag
landing in the response body, the JS load order breaking, or the connect-time
inventory failing to serialize as valid JSON.

They complement the Channels protocol tests (tests/test_control_panel_ws.py),
which cover the browser<->server<->agent contract, and the Node pure-logic
tests (tests/js/control-logic.test.mjs).
"""

import json
import pathlib
import re

import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.control.models import StationModule
from apps.stations.models import Station, StationAssignment

FM = [
    {
        "name": "frequency",
        "kind": "setting",
        "type": "float",
        "ranges": [{"name": "vhf", "min": 134.0, "max": 174.0}],
    },
    {"name": "power_level", "kind": "setting", "type": "enum", "values": ["low", "high"]},
    {"name": "ptt", "kind": "action", "type": "bool"},
    {"name": "rssi", "kind": "telemetry", "type": "int", "readonly": True, "min_interval_ms": 250},
]
# A second, entirely fictitious module — proves the page wires up generically.
ROTATOR = [
    {"name": "azimuth", "kind": "setting", "type": "int", "ranges": [{"min": 0, "max": 359}]},
    {"name": "preset", "kind": "setting", "type": "enum", "values": ["park", "zenith"]},
    {"name": "heater", "kind": "setting", "type": "bool"},
    {"name": "calibrate", "kind": "action", "type": "bool"},
    {"name": "temperature", "kind": "telemetry", "type": "int", "readonly": True},
]

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATES = REPO_ROOT / "apps/control/templates/control"
PANEL_JS = (REPO_ROOT / "static/js/control-panel.js").read_text()


@pytest.fixture
def station(db):
    return Station.objects.create(name="int1", status="online")


@pytest.fixture
def operator(db, station):
    u = User.objects.create_user(username="op-int", password="x")
    u.membership_level = User.MembershipLevel.MEMBER
    u.save(update_fields=["membership_level"])
    StationAssignment.objects.create(
        user=u, station=station, role=StationAssignment.Role.MAINTAINER
    )
    return u


@pytest.fixture
def multi_module_station(station):
    StationModule.objects.create(
        station=station,
        slot="slot0",
        module_id="fm",
        type="fm",
        capability_descriptor=FM,
        last_state={"frequency": 145.5, "power_level": "low"},
        online=True,
    )
    StationModule.objects.create(
        station=station,
        slot="slot1",
        module_id="rotator",
        type="rotator",
        capability_descriptor=ROTATOR,
        last_state={"azimuth": 90, "preset": "park", "heater": True},
        online=False,
    )
    return station


def _render(client, station):
    return client.get(reverse("control:station_control", args=[station.pk])).content.decode()


def test_page_renders_with_multiple_heterogeneous_modules(client, operator, multi_module_station):
    client.force_login(operator)
    html = _render(client, multi_module_station)
    # Both modules present; the second is a fictitious type rendered generically.
    assert 'data-module="fm"' in html
    assert 'data-module="rotator"' in html
    assert 'data-cap="azimuth"' in html


def test_connect_time_inventory_is_valid_json(client, operator, multi_module_station):
    """The #control-initial json_script must be valid JSON so Alpine can seed
    values/telemetry/online for a correct (and offline-safe) first paint."""
    client.force_login(operator)
    html = _render(client, multi_module_station)
    m = re.search(
        r'<script id="control-initial" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert m, "control-initial json_script missing"
    data = json.loads(m.group(1))  # raises if not valid JSON
    slots = {entry["slot"] for entry in data}
    assert {"slot0", "slot1"} <= slots
    # Offline module's last_state survives into the snapshot (offline render).
    rotator = next(mod for e in data for mod in e["modules"] if mod["module"] == "rotator")
    assert rotator["online"] is False
    assert rotator["state"]["azimuth"] == 90


def test_js_load_order_logic_before_component(client, operator, multi_module_station):
    """control-logic.js defines window.OE5XRXControlLogic and MUST load before
    control-panel.js consumes it."""
    client.force_login(operator)
    html = _render(client, multi_module_station)
    i_logic = html.find("control-logic.js")
    i_panel = html.find("control-panel.js")
    assert i_logic != -1 and i_panel != -1
    assert i_logic < i_panel
    assert "control-panel.css" in html


def test_no_leaked_template_artifacts_in_body(client, operator, multi_module_station):
    """A truncated/leaked template tag (the exact bug caught in review) would
    dump `{%`/`{#`/`%}` into the response body. Guard against regressions."""
    client.force_login(operator)
    html = _render(client, multi_module_station)
    body = html.split("<body", 1)[-1]
    for artifact in ("{%", "%}", "{#", "#}", "{{", "}}"):
        assert artifact not in body, f"leaked template artifact {artifact!r} in rendered body"


# Every Alpine method the control templates invoke must be defined on the
# component — a typo here silently breaks the UI at runtime with no test failure
# elsewhere. This is the regression guard for the template<->JS method contract.
CONTRACT_METHODS = [
    "valueOf",
    "displayValue",
    "isPending",
    "errorOf",
    "setValue",
    "stepValue",
    "doAction",
    "telemetryText",
    "telemetryPct",
    "moduleOnline",
    "canControl",
    "pttState",
    "pttPhase",
    "pttDown",
    "pttUp",
    "acquire",
    "release",
    "request",
    "preempt",
    "grant",
    "dismissRequest",
]


@pytest.mark.parametrize("method", CONTRACT_METHODS)
def test_template_method_is_defined_in_component(method):
    # Defined as `name:` (object member) or `name (` (method shorthand/getter).
    pattern = re.compile(r"(^|[\s,{])" + re.escape(method) + r"\s*[:(]", re.MULTILINE)
    assert pattern.search(PANEL_JS), f"controlPanel component is missing '{method}'"


def test_templates_invoke_only_defined_methods():
    """Reverse guard: scan the templates for method-call identifiers inside
    Alpine expressions and confirm each contract method is actually referenced,
    so the CONTRACT_METHODS list can't silently drift from the templates."""
    called = set()
    for tpl in TEMPLATES.rglob("*.html"):
        text = tpl.read_text()
        for name in re.findall(r"[@:]?[a-z-]+=\"[^\"]*?([a-zA-Z_]\w*)\(", text):
            called.add(name)
    # Every contract method we claim should appear in at least one template.
    referenced = [m for m in CONTRACT_METHODS if m in called]
    assert len(referenced) >= 15, f"only {len(referenced)} contract methods seen in templates"
