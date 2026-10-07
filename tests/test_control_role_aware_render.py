import re

import pytest
from django.template import Context, Template
from django.urls import reverse

from apps.accounts.models import User
from apps.control import capability_policy as cp
from apps.control.models import StationModule
from apps.stations.models import Station

DESCRIPTOR = [
    {"name": "filter_hpf", "kind": "setting", "type": "bool"},
    {"name": "filter_pre_emphasis", "kind": "setting", "type": "bool"},
    {
        "name": "frequency",
        "kind": "setting",
        "type": "float",
        "ranges": [{"name": "vhf", "min": 134.0, "max": 174.0}],
    },
    {"name": "mode", "kind": "setting", "type": "enum", "values": ["a", "b"]},
]


@pytest.fixture
def station(db):
    return Station.objects.create(name="s1", status="online")


@pytest.fixture
def module(station):
    return StationModule.objects.create(
        station=station,
        slot="slot0",
        module_id="fm",
        type="fm",
        capability_descriptor=DESCRIPTOR,
        last_state={},
        online=True,
    )


def _user(name, level):
    return User.objects.create_user(username=name, password="x", membership_level=level)


@pytest.fixture
def member(db):
    return _user("m", User.MembershipLevel.MEMBER)


@pytest.fixture
def staff(db):
    return _user("st", User.MembershipLevel.STAFF)


def _page(client, user, station):
    client.force_login(user)
    return client.get(reverse("control:station_control", args=[station.pk])).content.decode()


def _widget(html, cap):
    start = html.index(f'data-cap="{cap}"')
    start = html.rindex("<div", 0, start)
    nxt = html.find('<div class="cp-widget', start + 10)
    end = html.index("</article>", start)
    return html[start : min(end, nxt) if nxt != -1 else end]


def test_member_sees_filter_read_only(client, station, module, member):
    html = _page(client, member, station)
    w = _widget(html, "filter_hpf")
    assert 'data-readonly="true"' in w
    assert "@click" not in w and "@change" not in w and ":disabled" not in w
    assert re.search(r"<button[^>]*\bdisabled\b[^>]*aria-disabled=\"true\"", w, re.S)
    assert "cp-cap-lock" in w
    assert "staff only" in w
    # value display bindings stay
    assert ":aria-checked" in w and "cp-toggle--on" in w
    f = _widget(html, "frequency")
    assert "data-readonly" not in f and "@change" in f and "cp-cap-lock" not in f


def test_staff_sees_filter_editable(client, station, module, staff):
    w = _widget(_page(client, staff, station), "filter_hpf")
    assert "data-readonly" not in w and "@click" in w and "cp-cap-lock" not in w


def test_number_and_enum_read_only_variants(client, station, module, member, monkeypatch):
    monkeypatch.setitem(cp.POLICY, ("frequency", None), cp.CapabilityPolicy("staff"))
    monkeypatch.setitem(cp.POLICY, ("mode", None), cp.CapabilityPolicy("staff"))
    html = _page(client, member, station)
    for cap in ("frequency", "mode"):
        w = _widget(html, cap)
        assert 'data-readonly="true"' in w, cap
        assert "@click" not in w and "@change" not in w and ":disabled" not in w, cap
        assert "aria-disabled" in w and "cp-cap-lock" in w, cap
    assert 'lang="en"' in _widget(html, "frequency")


def test_tag_fails_closed_without_role(station, module):
    tpl = Template(
        "{% load control_caps %}{% cap_access cap m as a %}{{ a.writable }}|{{ a.write_role }}"
    )
    cap = {"name": "filter_hpf"}
    # no request / unresolved role -> read-only
    assert tpl.render(Context({"cap": cap, "m": module, "station": station})) == "False|staff"
    assert (
        tpl.render(Context({"cap": cap, "m": module, "station": station, "viewer_role": None}))
        == "False|staff"
    )
    ok = tpl.render(Context({"cap": cap, "m": module, "viewer_role": "staff"}))
    assert ok == "True|staff"


def test_unknown_module_type_renders_strictest(client, station, member):
    unknown = StationModule.objects.create(
        station=station,
        slot="slot1",
        module_id="x",
        type="",
        capability_descriptor=[{"name": "filter_hpf", "kind": "setting", "type": "bool"}],
        last_state={},
        online=True,
    )
    tpl = Template("{% load control_caps %}{% cap_access cap m as a %}{{ a.writable }}")
    out = tpl.render(
        Context({"cap": {"name": "filter_hpf"}, "m": unknown, "viewer_role": "operator"})
    )
    assert out == "False"


@pytest.mark.parametrize(
    "role,need,ok",
    [
        ("operator", "operator", True),
        ("operator", "staff", False),
        ("admin", "staff", True),
        (None, "operator", False),
        ("bogus", "operator", False),
        ("staff", "bogus", False),
    ],
)
def test_role_allows(role, need, ok):
    assert cp.role_allows(role, need) is ok


def test_station_manager_policy_uses_human_label(client, station, module, member, monkeypatch):
    monkeypatch.setitem(cp.POLICY, ("mode", None), cp.CapabilityPolicy("station_manager"))
    w = _widget(_page(client, member, station), "mode")
    assert "station manager only" in w
    assert "station_manager" not in w.replace("data-", "")


def test_default_policy_label_is_operator():
    tpl = Template("{% load control_caps %}{% cap_access cap m as a %}{{ a.write_role_label }}")
    from types import SimpleNamespace

    out = tpl.render(
        Context({"cap": {"name": "x"}, "m": SimpleNamespace(type="fm"), "viewer_role": None})
    )
    assert out == "operator"  # label of the default policy


def test_unlabelled_role_key_shown_verbatim(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setitem(cp.POLICY, ("x", None), cp.CapabilityPolicy("admin"))
    monkeypatch.setattr(cp, "ROLE_LABELS", {})
    tpl = Template("{% load control_caps %}{% cap_access cap m as a %}{{ a.write_role_label }}")
    out = tpl.render(Context({"cap": {"name": "x"}, "m": SimpleNamespace(type="fm")}))
    assert out == "admin"


def test_text_and_action_read_only_variants(client, station, member, monkeypatch):
    StationModule.objects.create(
        station=station,
        slot="slot0",
        module_id="fm",
        type="fm",
        capability_descriptor=[
            {"name": "label", "kind": "setting", "type": "string"},
            {"name": "reset", "kind": "action", "type": "bool"},
        ],
        last_state={},
        online=True,
    )
    html = _page(client, member, station)
    for cap in ("label", "reset"):
        w = _widget(html, cap)
        assert "data-readonly" not in w and ("@click" in w or "@change" in w), cap
    monkeypatch.setitem(cp.POLICY, ("label", None), cp.CapabilityPolicy("staff"))
    monkeypatch.setitem(cp.POLICY, ("reset", None), cp.CapabilityPolicy("staff"))
    html = _page(client, member, station)
    for cap in ("label", "reset"):
        w = _widget(html, cap)
        assert 'data-readonly="true"' in w, cap
        assert "@click" not in w and "@change" not in w and ":disabled" not in w, cap
        assert "aria-disabled" in w and "cp-cap-lock" in w and "staff only" in w, cap
