"""Control page: SSTV section and collapsible repeater/scanner/SSTV panels (rendered HTML)."""

import re
from pathlib import Path

import pytest
from django.urls import reverse
from django.utils import translation

from apps.accounts.models import User
from apps.control.models import AgentConnection
from apps.control.views import sstv_modes
from apps.stations.models import Station

TEMPLATES = Path(__file__).resolve().parent.parent / "apps" / "control" / "templates" / "control"
CONTROLS = (
    "startRx()",
    "stopRx()",
    "tuneIss()",
    "makePreview()",
    "send(false)",
    "abort()",
    'type="file"',
    'data-sv="confirm"',
)


def _user(username, level):
    u = User.objects.create_user(username=username, password="x")
    u.membership_level = level
    u.save(update_fields=["membership_level"])
    return u


@pytest.fixture
def station(db):
    return Station.objects.create(name="sstv-page", status="online", callsign="OE5XRX")


@pytest.fixture
def member(db):
    return _user("oe5abc", User.MembershipLevel.MEMBER)


@pytest.fixture
def applicant(db):
    return _user("oe5new", User.MembershipLevel.APPLICANT)


def connect(station, features=("sstv", "decoders")):
    AgentConnection.objects.create(
        station=station,
        kind=AgentConnection.Kind.CONTROL,
        channel_name="test-channel",
        features=list(features),
    )


def render(client, user, station, lang="en"):
    client.force_login(user)
    with translation.override(lang):  # the request activates it; restore afterwards
        response = client.get(reverse("control:station_control", args=[station.pk]))
    assert response.status_code == 200
    return response.content.decode()


def test_sstv_section_with_controls_for_members(client, member, station):
    connect(station)
    html = render(client, member, station)
    assert 'x-data="sstvPanel"' in html
    assert 'data-can-use="1"' in html
    assert 'data-station-callsign="1"' in html
    for control in CONTROLS:
        assert control in html, control
    assert reverse("sstv:preview", args=[station.pk]) in html
    assert reverse("sstv:gallery", args=[station.pk]) in html
    assert 'hx-trigger="load, sstv-gallery-refresh from:body"' in html
    assert 'id="sstv-modes"' in html
    for name in ("js/collapsible.js", "js/sstv-logic.js", "js/sstv-panel.js"):
        assert name in html


def test_viewer_sees_the_section_read_only(client, applicant, station):
    connect(station)
    html = render(client, applicant, station)
    assert 'x-data="sstvPanel"' in html
    assert 'data-can-use="0"' in html
    for control in CONTROLS:
        assert control not in html, control
    assert "sv-canvas" in html  # the live picture stays visible
    assert reverse("sstv:gallery", args=[station.pk]) in html


def test_section_hidden_when_agent_lacks_sstv(client, member, station):
    connect(station, features=("decoders",))
    html = render(client, member, station)
    assert 'x-data="sstvPanel"' not in html
    assert "data-sstv-unavailable" in html
    assert "The station software is too old for SSTV." in html
    for control in CONTROLS:
        assert control not in html, control


def test_section_hidden_while_agent_offline(client, member, station):
    html = render(client, member, station)
    assert 'x-data="sstvPanel"' not in html
    assert "SSTV is available once the station is online." in html


def test_german_labels(client, member, station):
    connect(station)
    member.language = "de"
    member.save(update_fields=["language"])
    client.cookies["django_language"] = "de"
    html = render(client, member, station, lang="de")
    for label in (
        "Empfang starten",
        "Empfang stoppen",
        "Mein Rufzeichen",
        "Stationsrufzeichen",
        "Ohne Rufzeichen senden?",
        "Vorschau",
        "Senden",
    ):
        assert label in html, label


def test_german_too_old_hint(client, member, station):
    connect(station, features=())
    member.language = "de"
    member.save(update_fields=["language"])
    client.cookies["django_language"] = "de"
    html = render(client, member, station, lang="de")
    assert "Station-Software zu alt für SSTV." in html


def test_station_callsign_checkbox_disabled_without_callsign(client, member, station):
    station.callsign = ""
    station.save(update_fields=["callsign"])
    connect(station)
    html = render(client, member, station)
    assert 'data-station-callsign="0"' in html
    assert "This station has no callsign." in html
    assert re.search(r'x-model="csStation" @change="invalidate\(\)" disabled>', html)


def test_three_collapsible_sections(client, member, station):
    connect(station)
    html = render(client, member, station)
    for key in ("repeaters", "scanner", "sstv"):
        assert f"x-data=\"cpCollapsible('{key}')\"" in html, key
    # every section body starts hidden until Alpine restores the stored state
    for body in ('id="rp-body"', 'id="sp-body"', 'id="sv-body"'):
        tag = html[html.index(body) : html.index(">", html.index(body))]
        assert 'x-show="open"' in tag and "x-cloak" in tag, body
    assert html.count('class="cp-collapse-toggle"') == 3
    assert html.count(":aria-expanded=\"open ? 'true' : 'false'\"") == 3


def test_mode_list_has_all_ten_modes_with_air_time(client, member, station):
    modes = sstv_modes()
    assert [m["key"] for m in modes] == [
        "martin1",
        "martin2",
        "scottie1",
        "scottie2",
        "scottiedx",
        "robot36",
        "robot72",
        "pd90",
        "pd120",
        "pd180",
    ]
    pd120 = next(m for m in modes if m["key"] == "pd120")
    assert (pd120["width"], pd120["height"]) == (640, 496)
    assert pd120["duration_label"] == "2:07"
    connect(station)
    html = render(client, member, station)
    assert "PD 120 (2:07 min)" in html


def test_ptt_and_mic_honour_the_sstv_block(client, member, station):
    connect(station)
    html = render(client, member, station)
    assert "sstvTx" in html  # mic button of the audio panel
    ptt = (TEMPLATES / "widgets" / "_ptt.html").read_text()
    assert ':disabled="!canKey(' in ptt


@pytest.mark.parametrize(
    "name", ["_sstv_panel.html", "_repeater_panel.html", "_scanner_panel.html", "panel.html"]
)
def test_templates_have_no_multiline_hash_comments(name):
    text = (TEMPLATES / name).read_text()
    for line in text.splitlines():
        if "{#" in line:
            assert "#}" in line, f"multi-line {{# in {name}: {line!r}"


def test_sstv_template_number_inputs_are_dot_decimal():
    text = (TEMPLATES / "_sstv_panel.html").read_text()
    for tag in re.findall(r"<input[^>]*>", text, flags=re.S):
        if 'type="number"' in tag or 'inputmode="decimal"' in tag:
            assert 'lang="en"' in tag, tag


def test_no_inline_script_in_sstv_section():
    text = (TEMPLATES / "_sstv_panel.html").read_text()
    assert "<script" not in text
