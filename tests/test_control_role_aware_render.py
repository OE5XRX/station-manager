import re

import pytest
from django.template import Context, Template
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.permissions import Level
from apps.control import capability_policy as cp
from apps.control.models import StationModule
from apps.control.templatetags import control_caps
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


STAFF_ONLY = "Vereins-Staff only"  # level_label(required_level("control", "manage"))


def _render(tpl_src, user=None, **ctx):
    from types import SimpleNamespace

    if user is not None:
        ctx["request"] = SimpleNamespace(user=user)
    return Template(tpl_src).render(Context(ctx))


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
    assert STAFF_ONLY in w
    # value display bindings stay
    assert ":aria-checked" in w and "cp-toggle--on" in w
    f = _widget(html, "frequency")
    assert "data-readonly" not in f and "@change" in f and "cp-cap-lock" not in f


def test_staff_sees_filter_editable(client, station, module, staff):
    w = _widget(_page(client, staff, station), "filter_hpf")
    assert "data-readonly" not in w and "@click" in w and "cp-cap-lock" not in w


def test_number_and_enum_read_only_variants(client, station, module, member, monkeypatch):
    monkeypatch.setitem(cp.POLICY, ("frequency", None), cp.CapabilityPolicy("manage"))
    monkeypatch.setitem(cp.POLICY, ("mode", None), cp.CapabilityPolicy("manage"))
    html = _page(client, member, station)
    for cap in ("frequency", "mode"):
        w = _widget(html, cap)
        assert 'data-readonly="true"' in w, cap
        assert "@click" not in w and "@change" not in w and ":disabled" not in w, cap
        assert "aria-disabled" in w and "cp-cap-lock" in w, cap
    assert 'lang="en"' in _widget(html, "frequency")


TAG = "{% load control_caps %}{% cap_access cap m as a %}{{ a.writable }}|{{ a.verb }}"


def test_tag_fails_closed_without_user(station, module, staff):
    cap = {"name": "filter_hpf"}
    # no request / no user -> read-only
    assert _render(TAG, cap=cap, m=module, station=station) == "False|manage"
    assert _render(TAG, cap=cap, m=module, request=None) == "False|manage"
    assert _render(TAG, staff, cap=cap, m=module) == "True|manage"


def test_unknown_module_type_renders_strictest(station, member):
    unknown = StationModule.objects.create(
        station=station,
        slot="slot1",
        module_id="x",
        type="",
        capability_descriptor=[{"name": "filter_hpf", "kind": "setting", "type": "bool"}],
        last_state={},
        online=True,
    )
    tpl = "{% load control_caps %}{% cap_access cap m as a %}{{ a.writable }}"
    assert _render(tpl, member, cap={"name": "filter_hpf"}, m=unknown) == "False"


@pytest.mark.parametrize(
    "level,cap,ok",
    [
        (User.MembershipLevel.APPLICANT, "frequency", False),
        (User.MembershipLevel.MEMBER, "frequency", True),
        (User.MembershipLevel.MEMBER, "filter_hpf", False),
        (User.MembershipLevel.STAFF, "filter_hpf", True),
        (User.MembershipLevel.ADMIN, "filter_hpf", True),
    ],
)
def test_writable_follows_control_verbs(db, level, cap, ok):
    from types import SimpleNamespace

    user = _user(f"lv-{level}", level)
    tpl = "{% load control_caps %}{% cap_access cap m as a %}{{ a.writable }}"
    assert _render(tpl, user, cap={"name": cap}, m=SimpleNamespace(type="fm")) == str(ok)


def test_manage_policy_uses_human_level_label(client, station, module, member, monkeypatch):
    monkeypatch.setitem(cp.POLICY, ("mode", None), cp.CapabilityPolicy("manage"))
    w = _widget(_page(client, member, station), "mode")
    assert STAFF_ONLY in w
    assert "manage only" not in w


def test_default_policy_label_is_member_level():
    from types import SimpleNamespace

    tpl = "{% load control_caps %}{% cap_access cap m as a %}{{ a.required_label }}"
    out = _render(tpl, cap={"name": "x"}, m=SimpleNamespace(type="fm"))
    assert out == "Vereins-Mitglied"  # label of the control.use level


def test_label_follows_the_registry(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setitem(cp.POLICY, ("x", None), cp.CapabilityPolicy("manage"))
    monkeypatch.setattr(control_caps, "required_level", lambda area, verb: Level.ADMIN)
    tpl = "{% load control_caps %}{% cap_access cap m as a %}{{ a.required_label }}"
    out = _render(tpl, cap={"name": "x"}, m=SimpleNamespace(type="fm"))
    assert out == "Vereins-Admin"


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
    monkeypatch.setitem(cp.POLICY, ("label", None), cp.CapabilityPolicy("manage"))
    monkeypatch.setitem(cp.POLICY, ("reset", None), cp.CapabilityPolicy("manage"))
    html = _page(client, member, station)
    for cap in ("label", "reset"):
        w = _widget(html, cap)
        assert 'data-readonly="true"' in w, cap
        assert "@click" not in w and "@change" not in w and ":disabled" not in w, cap
        assert "aria-disabled" in w and "cp-cap-lock" in w and STAFF_ONLY in w, cap


def test_applicant_page_is_read_only_with_rx_audio(client, station, module, db):
    applicant = _user("app", User.MembershipLevel.APPLICANT)
    StationModule.objects.filter(pk=module.pk).update(
        capability_descriptor=DESCRIPTOR + [{"name": "ptt", "kind": "action", "type": "bool"}]
    )
    client.force_login(applicant)
    r = client.get(reverse("control:station_control", args=[station.pk]))
    assert r.status_code == 200
    html = r.content.decode()
    # No lock / PTT / mic / TX-route controls; readonly badge instead.
    for absent in ('@click="acquire()"', 'data-cap="ptt"', "enableMic()", 'id="ap-tx-route"'):
        assert absent not in html, absent
    assert "nur lesend" in html
    # RX audio panel (subscribe/mixer) is still there.
    assert 'x-data="audioPanel"' in html and "toggleSub(" in html
    assert 'data-can-admin="0"' in html


def test_member_page_keeps_operating_controls(client, station, module, member):
    StationModule.objects.filter(pk=module.pk).update(
        capability_descriptor=DESCRIPTOR + [{"name": "ptt", "kind": "action", "type": "bool"}]
    )
    html = _page(client, member, station)
    for present in ('@click="acquire()"', 'data-cap="ptt"', "enableMic()", 'id="ap-tx-route"'):
        assert present in html, present
    assert "cp-lock-readonly-hint" not in html
