"""Repeater model, visible() manager, can_manage_repeaters."""

from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from apps.accounts.models import AccountAuditLog
from apps.repeaters.models import Repeater
from apps.repeaters.permissions import can_manage_repeaters
from apps.stations.models import Region, RegionAssignment

pytestmark = pytest.mark.django_db


def _rep(callsign="OE5XRX", **kw):
    defaults = {
        "output_frequency": Decimal("145.60000"),
        "input_frequency": Decimal("145.00000"),
        "source": "oevsv",
    }
    defaults.update(kw)
    return Repeater.objects.create(callsign=callsign, **defaults)


def test_defaults_and_simplex():
    r = _rep()
    assert (r.input_tone, r.output_tone, r.active) == ("none", "none", True)
    assert not r.is_simplex
    assert _rep("S", input_frequency=Decimal("145.60000")).is_simplex


def test_visible_hides_inactive():
    _rep("A")
    _rep("B", active=False)
    assert [r.callsign for r in Repeater.objects.visible()] == ["A"]


def test_visible_hides_oevsv_row_replaced_by_active_manual():
    orig = _rep("OE1")
    _rep("OE1", source="manual", replaces=orig)
    assert [(r.source) for r in Repeater.objects.visible()] == ["manual"]


def test_visible_shows_oevsv_row_when_replacement_inactive():
    orig = _rep("OE1")
    _rep("OE1", source="manual", replaces=orig, active=False)
    assert [r.pk for r in Repeater.objects.visible()] == [orig.pk]


def test_replaces_set_null_on_delete():
    orig = _rep("OE1")
    manual = _rep("OE1", source="manual", replaces=orig)
    orig.delete()
    manual.refresh_from_db()
    assert manual.replaces is None


def test_external_id_unique():
    _rep(external_id="x1")
    with pytest.raises(IntegrityError), transaction.atomic():
        _rep("B", external_id="x1")
    # NULL external ids (manual rows) never collide
    _rep("C", source="manual")
    _rep("D", source="manual")


def test_can_manage_repeaters(admin_user, staff_user, member_user, applicant_user):
    assert can_manage_repeaters(staff_user)
    assert can_manage_repeaters(admin_user)
    assert not can_manage_repeaters(member_user)
    assert not can_manage_repeaters(applicant_user)
    region = Region.objects.create(name="R", slug="r")
    RegionAssignment.objects.create(user=member_user, region=region, role="manager")
    assert can_manage_repeaters(member_user)
    assert not can_manage_repeaters(applicant_user)


def test_event_type():
    assert AccountAuditLog.EventType.REPEATER_CHANGED == "repeater_changed"
