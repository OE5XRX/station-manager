"""The TX modulation meter (agent tx_meter, post-DSP) renders in the audio panel."""

import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.stations.models import Station


@pytest.fixture
def station(db):
    return Station.objects.create(name="txm1", status="online")


@pytest.fixture
def member(db):
    u = User.objects.create_user(username="op-txm", password="x")
    u.membership_level = User.MembershipLevel.MEMBER
    u.save(update_fields=["membership_level"])
    return u


@pytest.fixture
def html(client, member, station):
    client.force_login(member)
    return client.get(reverse("control:station_control", args=[station.pk])).content.decode()


def test_tx_meter_block_is_bound_to_view_model(html):
    assert "data-tx-meter" in html
    assert 'x-show="txMeter.active"' in html
    assert "txMeter.hubFrac" in html
    assert 'x-show="txMeter.limiting"' in html


def test_degraded_and_failed_are_separate_indicators(html):
    assert 'x-show="txMeter.degraded"' in html
    assert 'x-show="txMeter.failed"' in html
    assert "TX audio pipeline failed" in html
    assert "DSP off" in html


def test_template_source_has_no_multiline_hash_comment():
    import pathlib

    src = pathlib.Path("apps/control/templates/control/_audio_panel.html").read_text()
    for chunk in src.split("{#")[1:]:
        assert "#}" in chunk.split("\n", 1)[0], "multi-line {# #} comment"
