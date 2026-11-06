"""Manual repeater CRUD + list views."""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.accounts.models import AccountAuditLog
from apps.repeaters.forms import RepeaterForm
from apps.repeaters.models import Repeater
from apps.stations.models import Region, RegionAssignment

pytestmark = pytest.mark.django_db


def _rep(callsign="OE5XLL", **kw):
    defaults = {
        "output_frequency": Decimal("145.60000"),
        "input_frequency": Decimal("145.00000"),
        "source": "oevsv",
    }
    defaults.update(kw)
    return Repeater.objects.create(callsign=callsign, **defaults)


def _login(client, user):
    client.force_login(user)
    return client


def _payload(**kw):
    data = {
        "callsign": "OE5NEW",
        "site_name": "Linz",
        "locator": "JN78",
        "output_frequency": "145.7000",
        "input_frequency": "145.1000",
        "input_tone": "none",
        "output_tone": "none",
        "bandwidth": "",
        "latitude": "",
        "longitude": "",
        "comment": "",
        "replaces": "",
    }
    data.update(kw)
    return data


def test_list_requires_login(client):
    resp = client.get(reverse("repeaters:list"))
    assert resp.status_code == 302


def test_applicant_can_view_list(client, applicant_user):
    _rep()
    resp = _login(client, applicant_user).get(reverse("repeaters:list"))
    assert resp.status_code == 200
    assert b"OE5XLL" in resp.content
    assert b"CC BY 4.0" in resp.content
    assert reverse("repeaters:create").encode() not in resp.content


def test_list_filter_active_vs_all(client, staff_user):
    _rep("ACTIVE1")
    _rep("GONE1", active=False)
    c = _login(client, staff_user)
    assert b"GONE1" not in c.get(reverse("repeaters:list")).content
    assert b"GONE1" in c.get(reverse("repeaters:list") + "?show=all").content


def test_applicant_cannot_create(client, applicant_user):
    c = _login(client, applicant_user)
    assert c.get(reverse("repeaters:create")).status_code == 403
    assert c.post(reverse("repeaters:create"), _payload()).status_code == 403
    assert not Repeater.objects.filter(callsign="OE5NEW").exists()


def test_member_cannot_create(client, member_user):
    assert _login(client, member_user).get(reverse("repeaters:create")).status_code == 403


def test_region_manager_can_create(client, member_user):
    region = Region.objects.create(name="R", slug="r")
    RegionAssignment.objects.create(user=member_user, region=region, role="manager")
    resp = _login(client, member_user).post(reverse("repeaters:create"), _payload())
    assert resp.status_code == 302
    assert Repeater.objects.get(callsign="OE5NEW").source == "manual"


def test_staff_can_create_and_sets_creator(client, staff_user):
    resp = _login(client, staff_user).post(reverse("repeaters:create"), _payload())
    assert resp.status_code == 302
    r = Repeater.objects.get(callsign="OE5NEW")
    assert r.source == "manual"
    assert r.created_by == staff_user


def test_create_audits(client, staff_user):
    _login(client, staff_user).post(reverse("repeaters:create"), _payload())
    r = Repeater.objects.get(callsign="OE5NEW")
    log = AccountAuditLog.objects.get(event_type=AccountAuditLog.EventType.REPEATER_CHANGED)
    assert log.actor == staff_user
    assert log.details["repeater_id"] == r.pk
    assert "OE5NEW" in log.message


def test_update_manual_audits_changes(client, staff_user):
    r = _rep("MAN1", source="manual")
    resp = _login(client, staff_user).post(
        reverse("repeaters:update", args=[r.pk]), _payload(callsign="MAN1", site_name="Wels")
    )
    assert resp.status_code == 302
    r.refresh_from_db()
    assert r.site_name == "Wels"
    log = AccountAuditLog.objects.get(event_type=AccountAuditLog.EventType.REPEATER_CHANGED)
    assert "site_name" in log.details["changes"]


def test_update_oevsv_row_forbidden(client, staff_user):
    r = _rep()
    c = _login(client, staff_user)
    assert c.get(reverse("repeaters:update", args=[r.pk])).status_code == 403
    assert c.post(reverse("repeaters:update", args=[r.pk]), _payload()).status_code == 403


def test_replaces_must_be_oevsv(client, staff_user):
    manual = _rep("MAN2", source="manual")
    resp = _login(client, staff_user).post(
        reverse("repeaters:create"), _payload(replaces=str(manual.pk))
    )
    assert resp.status_code == 200
    assert "replaces" in resp.context["form"].errors


def test_replaces_oevsv_hides_original(client, staff_user):
    orig = _rep()
    _login(client, staff_user).post(reverse("repeaters:create"), _payload(replaces=str(orig.pk)))
    assert [r.callsign for r in Repeater.objects.visible()] == ["OE5NEW"]


def test_deactivate_and_reactivate(client, staff_user):
    r = _rep("MAN3", source="manual")
    c = _login(client, staff_user)
    url = reverse("repeaters:deactivate", args=[r.pk])
    assert c.get(url).status_code == 200
    assert c.post(url).status_code == 302
    r.refresh_from_db()
    assert r.active is False
    assert AccountAuditLog.objects.filter(
        event_type=AccountAuditLog.EventType.REPEATER_CHANGED
    ).exists()
    assert c.post(url).status_code == 302
    r.refresh_from_db()
    assert r.active is True


def test_deactivate_oevsv_forbidden(client, staff_user):
    r = _rep()
    assert (
        _login(client, staff_user).post(reverse("repeaters:deactivate", args=[r.pk])).status_code
        == 403
    )


def test_deactivate_denied_for_member(client, member_user):
    r = _rep("MAN4", source="manual")
    assert (
        _login(client, member_user).post(reverse("repeaters:deactivate", args=[r.pk])).status_code
        == 403
    )


def test_number_inputs_have_lang_en():
    form = RepeaterForm()
    for name in ("output_frequency", "input_frequency", "latitude", "longitude"):
        assert form.fields[name].widget.attrs.get("lang") == "en", name


def test_form_rejects_bad_frequency():
    form = RepeaterForm(data=_payload(output_frequency="-1"))
    assert not form.is_valid()
    assert "output_frequency" in form.errors
