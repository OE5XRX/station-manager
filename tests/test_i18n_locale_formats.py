"""Numbers stay machine-readable under the ``de`` locale (spec 2026-11-24-i18n-de).

German formatting turns ``0.5`` into ``0,5``. That is fine as display text but
breaks every attribute a browser/JS parses (``value``, ``min``, ``data-*`` ...).
"""

import re

import pytest
from django.urls import reverse
from django.utils import translation

from apps.accounts.models import User
from apps.control.models import StationModule
from apps.monitoring.models import AlertRule, MetricDefinition
from apps.stations.forms import StationForm
from apps.stations.models import Station

MACHINE_ATTR = re.compile(r'\b((?:value|data-[\w-]+|min|max|step|style)="[^"]*\d,\d[^"]*")')

FM = [
    {
        "name": "frequency",
        "kind": "setting",
        "type": "float",
        "step": 0.5,
        "ranges": [{"name": "vhf", "min": 134.5, "max": 174.5}],
    },
    {
        "name": "temp",
        "kind": "telemetry",
        "type": "float",
        "readonly": True,
        "unit": "C",
        "ranges": [{"name": "t", "min": 0.5, "max": 99.5}],
    },
]


@pytest.fixture
def staff(db):
    u = User.objects.create_user(username="deadmin", password="x", is_superuser=True)
    u.is_staff = True
    u.language = "de"
    u.membership_level = User.MembershipLevel.ADMIN
    u.save()
    return u


@pytest.fixture
def seeded(db):
    station = Station.objects.create(
        name="s1",
        status="online",
        latitude="48.123456",
        longitude="13.654321",
        altitude=512,
        tx_audio_ceiling_dbfs=-3.5,
    )
    StationModule.objects.create(
        station=station,
        slot="slot0",
        module_id="fm",
        type="fm",
        capability_descriptor=FM,
        last_state={"frequency": 145.5, "temp": 36.5},
        online=True,
    )
    MetricDefinition.objects.update_or_create(
        key="pwr.12v.voltage_v",
        defaults={
            "label": "12 V",
            "unit": "V",
            "group": "power",
            "precision": 2,
            "min_value": 0.5,
            "max_value": 30.5,
        },
    )
    AlertRule.objects.create(
        alert_type=AlertRule.AlertType.THRESHOLD,
        name="Undervoltage",
        metric_key="pwr.12v.voltage_v",
        operator="lt",
        aggregate="avg",
        threshold=11.5,
        duration_seconds=60,
        severity="warning",
    )
    return station


def _urls(station):
    return {
        "dashboard": reverse("dashboard:index"),
        "station_detail": reverse("stations:station_detail", args=[station.pk]),
        "station_edit": reverse("stations:station_edit", args=[station.pk]),
        "control": reverse("control:station_control", args=[station.pk]),
        "alert_settings": reverse("monitoring:alert_settings"),
        "metric_definitions": reverse("monitoring:metric_definitions"),
    }


def test_machine_attributes_have_no_decimal_comma_under_de(client, staff, seeded):
    client.force_login(staff)
    client.cookies["django_language"] = "de"
    offenders = []
    with translation.override("de"):
        urls = _urls(seeded)
    for page, url in urls.items():
        assert url.startswith("/de/"), url
        resp = client.get(url)
        assert resp.status_code == 200, (page, resp.status_code)
        html = resp.content.decode()
        offenders += [(page, m) for m in MACHINE_ATTR.findall(html)]
    assert not offenders, offenders


def test_scan_pages_really_contain_float_data(client, staff, seeded):
    """Guard against a vacuous scan: the float data must reach the HTML."""
    client.force_login(staff)
    client.cookies["django_language"] = "de"
    with translation.override("de"):
        control = reverse("control:station_control", args=[seeded.pk])
    html = client.get(control).content.decode()
    assert 'data-min="0.5"' in html
    assert 'data-max="99.5"' in html


def test_station_form_numeric_inputs_force_dot_decimal():
    form = StationForm()
    for name in ("latitude", "longitude", "altitude"):
        assert 'lang="en"' in str(form[name]), name
