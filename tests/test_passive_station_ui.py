import pytest
from django.urls import reverse
from django.utils import timezone

from apps.control.models import ControlLock
from apps.passive.forms import StationPassiveForm
from apps.passive.models import PassiveServiceRun, StationPassiveConfig
from apps.passive.status import passive_badge


def _edit_url(station):
    return reverse("stations:station_edit", kwargs={"pk": station.pk})


def _detail_url(station):
    return reverse("stations:station_detail", kwargs={"pk": station.pk})


def _post(client, user, station, **passive):
    client.force_login(user)
    data = {"name": station.name, "callsign": station.callsign}
    data.update({f"passive-{k}": v for k, v in passive.items()})
    return client.post(_edit_url(station), data)


def _form(station, **data):
    return StationPassiveForm(
        {f"passive-{k}": v for k, v in data.items()},
        instance=StationPassiveConfig(station=station),
    )


@pytest.mark.django_db
def test_igate_call_unique_and_normalised(station_factory):
    a, b = station_factory(), station_factory()
    form = _form(a, enabled="on", igate_callsign=" oe5xrx ", igate_ssid="10")
    assert form.is_valid(), form.errors
    form.save()
    assert StationPassiveConfig.objects.get(station=a).igate_callsign == "OE5XRX"
    clash = _form(b, enabled="on", igate_callsign="OE5XRX", igate_ssid="10")
    assert not clash.is_valid()
    assert "igate_ssid" in clash.errors
    # another SSID on the same call is fine
    assert _form(b, enabled="on", igate_callsign="OE5XRX", igate_ssid="11").is_valid()


@pytest.mark.django_db
def test_own_pair_is_not_a_clash(station):
    cfg = StationPassiveConfig.objects.create(
        station=station, enabled=True, igate_callsign="OE5XRX", igate_ssid=10
    )
    form = StationPassiveForm(
        {"passive-enabled": "on", "passive-igate_callsign": "OE5XRX", "passive-igate_ssid": "10"},
        instance=cfg,
    )
    assert form.is_valid(), form.errors


@pytest.mark.django_db
@pytest.mark.parametrize(
    "data,field",
    [
        ({"enabled": "on", "igate_ssid": "10"}, "igate_callsign"),
        ({"enabled": "on", "igate_callsign": "OE5XRX"}, "igate_ssid"),
        ({"enabled": "on"}, "igate_callsign"),
    ],
)
def test_enabled_requires_call_and_ssid(station, data, field):
    form = _form(station, **data)
    assert not form.is_valid()
    assert field in form.errors


@pytest.mark.django_db
def test_disabled_form_may_be_empty(station):
    assert _form(station).is_valid()


@pytest.mark.django_db
@pytest.mark.parametrize("call", ["OE5", "1234", "OE5XRX-1"])
def test_invalid_callsign_rejected(station, call):
    form = _form(station, enabled="on", igate_callsign=call, igate_ssid="10")
    assert not form.is_valid()
    assert "igate_callsign" in form.errors


@pytest.mark.django_db
def test_ssid_range(station):
    assert not _form(station, enabled="on", igate_callsign="OE5XRX", igate_ssid="16").is_valid()
    assert not _form(station, enabled="on", igate_callsign="OE5XRX", igate_ssid="-1").is_valid()
    assert _form(station, enabled="on", igate_callsign="OE5XRX", igate_ssid="0").is_valid()


@pytest.mark.django_db
def test_ssid_is_a_hint_not_a_default(station):
    html = str(_form(station)["igate_ssid"])
    assert 'placeholder="10"' in html and 'lang="en"' in html
    assert 'value="10"' not in html


@pytest.mark.django_db
def test_admin_saves_passive_fields_with_station(client, admin_user, station):
    resp = _post(
        client, admin_user, station, enabled="on", igate_callsign="oe5xrx", igate_ssid="10"
    )
    assert resp.status_code == 302
    cfg = StationPassiveConfig.objects.get(station=station)
    assert (cfg.enabled, cfg.igate_call) == (True, "OE5XRX-10")


@pytest.mark.django_db
def test_invalid_passive_form_blocks_station_save(client, admin_user, station):
    resp = _post(client, admin_user, station, enabled="on", igate_callsign="")
    assert resp.status_code == 200
    assert not StationPassiveConfig.objects.filter(station=station).exists()
    assert "Zum Aktivieren" in resp.content.decode()


@pytest.mark.django_db
def test_staff_post_cannot_change_passive_fields(client, staff_user, station):
    StationPassiveConfig.objects.create(station=station, enabled=False)
    resp = _post(
        client, staff_user, station, enabled="on", igate_callsign="OE5XRX", igate_ssid="10"
    )
    assert resp.status_code == 302  # the station form itself is saved
    cfg = StationPassiveConfig.objects.get(station=station)
    assert cfg.enabled is False and cfg.igate_callsign == ""


@pytest.mark.django_db
def test_staff_does_not_see_passive_section(client, staff_user, admin_user, station):
    client.force_login(staff_user)
    assert "passive-igate_callsign" not in client.get(_edit_url(station)).content.decode()
    client.force_login(admin_user)
    assert "passive-igate_callsign" in client.get(_edit_url(station)).content.decode()


@pytest.mark.django_db
def test_detail_shows_aprs_tab_only_when_opted_in(client, applicant_user, station):
    client.force_login(applicant_user)
    assert 'data-tab="aprs"' not in client.get(_detail_url(station)).content.decode()
    StationPassiveConfig.objects.create(
        station=station, enabled=True, igate_callsign="OE5XRX", igate_ssid=10
    )
    body = client.get(_detail_url(station)).content.decode()
    assert 'data-tab="aprs"' in body and 'data-tab-panel="aprs"' in body
    assert f'data-station-id="{station.pk}"' in body and "OE5XRX-10" in body


@pytest.mark.django_db
def test_detail_hides_tab_when_opted_out(client, applicant_user, station):
    StationPassiveConfig.objects.create(station=station, enabled=False)
    client.force_login(applicant_user)
    assert 'data-tab="aprs"' not in client.get(_detail_url(station)).content.decode()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "run_state,label,level",
    [
        ("running", "Passiv: APRS aktiv", "ok"),
        ("starting", "Passiv: APRS startet", "ok"),
        ("stopping", "Passiv: in Benutzung", "muted"),
        ("agent_offline", "Passiv: Agent offline", "off"),
        (None, "Passiv: aus", "off"),
        ("error", "Passiv: Fehler: sa818 timeout", "error"),
    ],
)
def test_badge_labels(station, run_state, label, level):
    if run_state:
        PassiveServiceRun.objects.create(
            station=station, service="aprs", slot="1", state=run_state, last_error="sa818 timeout"
        )
    assert passive_badge(station) == {"label": label, "level": level}


@pytest.mark.django_db
def test_badge_shows_closed_error_run(station):
    PassiveServiceRun.objects.create(
        station=station,
        service="aprs",
        slot="1",
        state="error",
        last_error="no_rx_node",
        ended_at=timezone.now(),
    )
    assert passive_badge(station)["level"] == "error"


@pytest.mark.django_db
def test_badge_in_use_when_lock_held(station, admin_user):
    ControlLock.objects.create(station=station, holder=admin_user, acquired_at=timezone.now())
    assert passive_badge(station) == {"label": "Passiv: in Benutzung", "level": "muted"}


@pytest.mark.django_db
def test_control_page_has_passive_badge_and_mute_notice(client, admin_user, station):
    client.force_login(admin_user)
    body = client.get(
        reverse("control:station_control", kwargs={"pk": station.pk})
    ).content.decode()
    assert "data-passive-strip" in body
    assert "Passiv: APRS läuft, Audio stumm" in body


@pytest.mark.django_db
def test_untouched_default_passive_form_creates_no_config(client, admin_user, station):
    client.force_login(admin_user)
    resp = client.post(_edit_url(station), {"name": station.name, "callsign": station.callsign})
    assert resp.status_code == 302
    assert not StationPassiveConfig.objects.filter(station=station).exists()


@pytest.mark.django_db
def test_edit_rerenders_with_clash_error_and_saves_nothing(client, admin_user, station_factory):
    a, b = station_factory(), station_factory()
    StationPassiveConfig.objects.create(
        station=a, enabled=True, igate_callsign="OE5XRX", igate_ssid=10
    )
    resp = _post(client, admin_user, b, enabled="on", igate_callsign="OE5XRX", igate_ssid="10")
    assert resp.status_code == 200
    assert "gehört schon zur Station" in resp.content.decode()
    assert not StationPassiveConfig.objects.filter(station=b).exists()


@pytest.mark.django_db
def test_integrity_clash_after_validation_rerenders_not_500(
    client, admin_user, station_factory, monkeypatch
):
    a, b = station_factory(), station_factory()
    StationPassiveConfig.objects.create(
        station=a, enabled=True, igate_callsign="OE5XRX", igate_ssid=10
    )
    # Simulate the race: the form's own clash check passes, the DB constraint then fires.
    monkeypatch.setattr(StationPassiveForm, "clean", lambda self: self.cleaned_data)
    old_name = b.name
    client.force_login(admin_user)
    resp = client.post(
        _edit_url(b),
        {
            "name": "Renamed",
            "callsign": b.callsign,
            "passive-enabled": "on",
            "passive-igate_callsign": "OE5XRX",
            "passive-igate_ssid": "10",
        },
    )
    assert resp.status_code == 200
    b.refresh_from_db()
    assert b.name == old_name  # the station edit rolled back with it
