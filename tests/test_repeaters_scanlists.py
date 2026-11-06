"""Scan list models + owner-only CRUD under repeaters/scanlists/."""

from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.urls import reverse

from apps.repeaters.models import Repeater, ScanList, ScanListEntry

pytestmark = pytest.mark.django_db


def _rep(callsign="OE5XLL", **kw):
    kw.setdefault("source", "manual")
    return Repeater.objects.create(
        callsign=callsign,
        output_frequency=Decimal("145.600"),
        input_frequency=Decimal("145.000"),
        **kw,
    )


def _login(client, user):
    client.force_login(user)
    return client


def _entry(scan_list, position=0, **kw):
    return ScanListEntry.objects.create(scan_list=scan_list, position=position, **kw)


# --- model ---------------------------------------------------------------


def test_entry_needs_exactly_one_of_repeater_or_frequency(member_user):
    sl = ScanList.objects.create(owner=member_user, name="x")
    for kw in ({}, {"repeater": _rep(), "frequency": Decimal("145.5")}):
        with pytest.raises(IntegrityError), transaction.atomic():
            ScanListEntry.objects.create(scan_list=sl, **kw)


def test_entry_clean_rejects_both_and_neither(member_user):
    from django.core.exceptions import ValidationError

    sl = ScanList.objects.create(owner=member_user, name="x")
    with pytest.raises(ValidationError):
        ScanListEntry(scan_list=sl).clean()
    with pytest.raises(ValidationError):
        ScanListEntry(scan_list=sl, repeater=_rep(), frequency=Decimal("145.5")).clean()
    ScanListEntry(scan_list=sl, frequency=Decimal("145.5")).clean()


# --- permissions ---------------------------------------------------------


def test_requires_login(client):
    assert client.get(reverse("repeaters:scanlists")).status_code == 302


def test_applicant_cannot_use_scanlists(client, applicant_user):
    c = _login(client, applicant_user)
    assert c.get(reverse("repeaters:scanlists")).status_code == 403
    assert c.post(reverse("repeaters:scanlist_create"), {"name": "x"}).status_code == 403
    assert c.get(reverse("repeaters:scanlists_mine")).status_code == 403
    assert not ScanList.objects.exists()


# --- CRUD ----------------------------------------------------------------


def test_create_list_sets_owner_and_redirects_to_detail(client, member_user):
    resp = _login(client, member_user).post(reverse("repeaters:scanlist_create"), {"name": "Home"})
    sl = ScanList.objects.get(name="Home")
    assert sl.owner == member_user
    assert resp.status_code == 302
    assert resp.url == reverse("repeaters:scanlist_detail", args=[sl.pk])


def test_blank_name_rejected(client, member_user):
    resp = _login(client, member_user).post(reverse("repeaters:scanlist_create"), {"name": "  "})
    assert resp.status_code == 200
    assert not ScanList.objects.exists()


def test_index_lists_only_own(client, member_user, staff_user):
    ScanList.objects.create(owner=member_user, name="MINE-LIST")
    ScanList.objects.create(owner=staff_user, name="THEIRS-LIST")
    body = _login(client, member_user).get(reverse("repeaters:scanlists")).content
    assert b"MINE-LIST" in body and b"THEIRS-LIST" not in body


def test_detail_shows_entries(client, member_user):
    sl = ScanList.objects.create(owner=member_user, name="L")
    _entry(sl, repeater=_rep("OE5XLL"))
    _entry(sl, 1, frequency=Decimal("145.5"), label="Calling")
    body = _login(client, member_user).get(reverse("repeaters:scanlist_detail", args=[sl.pk]))
    assert b"OE5XLL" in body.content and b"Calling" in body.content


def test_rename(client, member_user):
    sl = ScanList.objects.create(owner=member_user, name="old")
    url = reverse("repeaters:scanlist_detail", args=[sl.pk])
    assert _login(client, member_user).post(url, {"name": "new"}).status_code == 302
    sl.refresh_from_db()
    assert sl.name == "new"


def test_delete_list_cascades(client, member_user):
    sl = ScanList.objects.create(owner=member_user, name="L")
    _entry(sl, frequency=Decimal("145.5"))
    c = _login(client, member_user)
    assert c.get(reverse("repeaters:scanlist_delete", args=[sl.pk])).status_code == 200
    c.post(reverse("repeaters:scanlist_delete", args=[sl.pk]))
    assert not ScanList.objects.exists() and not ScanListEntry.objects.exists()


def test_add_repeater_entry(client, member_user):
    sl = ScanList.objects.create(owner=member_user, name="L")
    rep = _rep()
    _login(client, member_user).post(
        reverse("repeaters:scanlist_entry_add", args=[sl.pk]), {"repeater": rep.pk}
    )
    entry = sl.entries.get()
    assert entry.repeater == rep and entry.frequency is None


def test_add_frequency_entry_appends_with_next_position(client, member_user):
    sl = ScanList.objects.create(owner=member_user, name="L")
    _entry(sl, 0, frequency=Decimal("145.5"))
    _login(client, member_user).post(
        reverse("repeaters:scanlist_entry_add", args=[sl.pk]),
        {"frequency": "433.5", "tone": "88.5", "label": "Hill"},
    )
    entry = sl.entries.order_by("-position").first()
    assert (entry.position, entry.frequency, entry.tone, entry.label) == (
        1,
        Decimal("433.50000"),
        "88.5",
        "Hill",
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"frequency": "145.5", "repeater": "{rep}"},
        {"frequency": "-1"},
        {"frequency": "145.5", "tone": "9"},
    ],
)
def test_add_invalid_entry_rejected(client, member_user, payload):
    sl = ScanList.objects.create(owner=member_user, name="L")
    rep = _rep()
    payload = {k: v.format(rep=rep.pk) if isinstance(v, str) else v for k, v in payload.items()}
    if payload.get("repeater") == "{rep}":
        payload["repeater"] = rep.pk
    _login(client, member_user).post(
        reverse("repeaters:scanlist_entry_add", args=[sl.pk]), payload
    )
    assert not sl.entries.exists()


def test_cannot_add_inactive_repeater(client, member_user):
    sl = ScanList.objects.create(owner=member_user, name="L")
    rep = _rep(active=False)
    _login(client, member_user).post(
        reverse("repeaters:scanlist_entry_add", args=[sl.pk]), {"repeater": rep.pk}
    )
    assert not sl.entries.exists()


def test_delete_entry_renumbers(client, member_user):
    sl = ScanList.objects.create(owner=member_user, name="L")
    a = _entry(sl, 0, frequency=Decimal("145.5"))
    b = _entry(sl, 1, frequency=Decimal("145.6"))
    c = _entry(sl, 2, frequency=Decimal("145.7"))
    _login(client, member_user).post(
        reverse("repeaters:scanlist_entry_delete", args=[sl.pk, b.pk])
    )
    assert [(e.pk, e.position) for e in sl.entries.all()] == [(a.pk, 0), (c.pk, 1)]


def test_move_swaps_neighbours(client, member_user):
    sl = ScanList.objects.create(owner=member_user, name="L")
    a = _entry(sl, 0, frequency=Decimal("145.5"))
    b = _entry(sl, 1, frequency=Decimal("145.6"))
    c = _login(client, member_user)
    c.post(reverse("repeaters:scanlist_entry_move", args=[sl.pk, b.pk]), {"direction": "up"})
    assert [e.pk for e in sl.entries.all()] == [b.pk, a.pk]
    # moving the first entry up is a no-op
    c.post(reverse("repeaters:scanlist_entry_move", args=[sl.pk, b.pk]), {"direction": "up"})
    assert [e.pk for e in sl.entries.all()] == [b.pk, a.pk]
    resp = c.post(reverse("repeaters:scanlist_entry_move", args=[sl.pk, a.pk]), {"direction": "x"})
    assert resp.status_code == 400


# --- owner-only ----------------------------------------------------------


def test_other_users_list_is_404_everywhere(client, member_user, staff_user):
    sl = ScanList.objects.create(owner=staff_user, name="theirs")
    e = _entry(sl, frequency=Decimal("145.5"))
    c = _login(client, member_user)
    for name, args in [
        ("scanlist_detail", [sl.pk]),
        ("scanlist_delete", [sl.pk]),
    ]:
        assert c.get(reverse(f"repeaters:{name}", args=args)).status_code == 404
    assert (
        c.post(reverse("repeaters:scanlist_detail", args=[sl.pk]), {"name": "x"}).status_code
        == 404
    )
    assert c.post(reverse("repeaters:scanlist_delete", args=[sl.pk])).status_code == 404
    assert (
        c.post(
            reverse("repeaters:scanlist_entry_add", args=[sl.pk]), {"frequency": "145.5"}
        ).status_code
        == 404
    )
    assert (
        c.post(reverse("repeaters:scanlist_entry_delete", args=[sl.pk, e.pk])).status_code == 404
    )
    assert (
        c.post(
            reverse("repeaters:scanlist_entry_move", args=[sl.pk, e.pk]), {"direction": "up"}
        ).status_code
        == 404
    )
    sl.refresh_from_db()
    assert sl.name == "theirs" and sl.entries.count() == 1


def test_entry_of_other_list_not_reachable_via_own_list(client, member_user, staff_user):
    mine = ScanList.objects.create(owner=member_user, name="mine")
    theirs = ScanList.objects.create(owner=staff_user, name="theirs")
    e = _entry(theirs, frequency=Decimal("145.5"))
    resp = _login(client, member_user).post(
        reverse("repeaters:scanlist_entry_delete", args=[mine.pk, e.pk])
    )
    assert resp.status_code == 404 and ScanListEntry.objects.filter(pk=e.pk).exists()


# --- mine.json -----------------------------------------------------------


def test_mine_json_shape_and_scope(client, member_user, staff_user):
    sl = ScanList.objects.create(owner=member_user, name="Home")
    _entry(sl, 0, frequency=Decimal("145.5"))
    _entry(sl, 1, frequency=Decimal("145.6"))
    ScanList.objects.create(owner=staff_user, name="theirs")
    resp = _login(client, member_user).get(reverse("repeaters:scanlists_mine"))
    assert resp.status_code == 200
    assert resp.json() == [{"id": sl.pk, "name": "Home", "count": 2}]
    assert reverse("repeaters:scanlists_mine").endswith("/repeaters/scanlists/mine.json")


def test_move_down_swaps_and_last_down_is_noop(client, member_user):
    sl = ScanList.objects.create(owner=member_user, name="L")
    a = _entry(sl, 0, frequency=Decimal("145.5"))
    b = _entry(sl, 1, frequency=Decimal("145.6"))
    c = _login(client, member_user)
    c.post(reverse("repeaters:scanlist_entry_move", args=[sl.pk, a.pk]), {"direction": "down"})
    assert [e.pk for e in sl.entries.all()] == [b.pk, a.pk]
    c.post(reverse("repeaters:scanlist_entry_move", args=[sl.pk, a.pk]), {"direction": "down"})
    assert [e.pk for e in sl.entries.all()] == [b.pk, a.pk]


def test_list_cap(client, member_user, monkeypatch):
    monkeypatch.setattr("apps.repeaters.views.MAX_SCAN_LISTS", 1)
    ScanList.objects.create(owner=member_user, name="one")
    _login(client, member_user).post(reverse("repeaters:scanlist_create"), {"name": "two"})
    assert ScanList.objects.filter(owner=member_user).count() == 1


def test_entry_cap(client, member_user, monkeypatch):
    monkeypatch.setattr("apps.repeaters.views.MAX_ENTRIES_PER_LIST", 1)
    sl = ScanList.objects.create(owner=member_user, name="L")
    _entry(sl, 0, frequency=Decimal("145.5"))
    _login(client, member_user).post(
        reverse("repeaters:scanlist_entry_add", args=[sl.pk]), {"frequency": "145.6"}
    )
    assert sl.entries.count() == 1
