"""Rollout-group CRUD UI and station → group assignment UI."""

import pytest
from django.urls import reverse

from apps.accounts.models import AccountAuditLog
from apps.deployments.models import Deployment
from apps.module_firmware.models import ModuleFirmwareTarget, ModuleType
from apps.rollouts.models import current_sequence
from apps.rollouts.services import add_entry
from apps.stations.models import RolloutGroup, Station, StationAuditLog

pytestmark = pytest.mark.django_db


def test_staff_can_create_group_with_channel(client, staff_user):
    client.force_login(staff_user)
    response = client.post(
        reverse("stations:group_create"),
        {"name": "Beta testers", "slug": "beta", "channel": "dev", "description": "Early"},
    )
    assert response.status_code == 302
    assert response["Location"] == reverse("stations:group_list")
    group = RolloutGroup.objects.get(slug="beta")
    assert group.name == "Beta testers"
    assert group.channel == RolloutGroup.Channel.DEV


def test_member_gets_403_on_create(client, member_user):
    client.force_login(member_user)
    assert client.get(reverse("stations:group_create")).status_code == 403
    response = client.post(
        reverse("stations:group_create"), {"name": "X", "slug": "x", "channel": "release"}
    )
    assert response.status_code == 403
    assert not RolloutGroup.objects.exists()


def test_member_gets_403_on_edit_and_delete(client, member_user, make_rollout_group):
    group = make_rollout_group("Beta")
    client.force_login(member_user)
    assert client.get(reverse("stations:group_edit", args=[group.pk])).status_code == 403
    assert client.post(reverse("stations:group_delete", args=[group.pk])).status_code == 403
    assert RolloutGroup.objects.filter(pk=group.pk).exists()


def test_group_list_visible_to_applicant_readonly(
    client, applicant_user, make_rollout_group, station_factory
):
    group = make_rollout_group("Canary", channel=RolloutGroup.Channel.DEV)
    station_factory(rollout_group=group)
    station_factory(rollout_group=group)
    entry = add_entry(current_sequence(), group, by_user=None)
    assert entry.position == 0  # stored 0-based, displayed 1-based
    make_rollout_group("Unsequenced")
    client.force_login(applicant_user)

    response = client.get(reverse("stations:group_list"))

    assert response.status_code == 200
    html = response.content.decode()
    assert "Canary" in html and "Unsequenced" in html
    assert 'class="pill pill-accent">DEV<' in html
    assert 'data-group-stations="2"' in html
    assert 'data-group-step="1"' in html
    assert reverse("stations:group_create") not in html
    assert reverse("stations:group_edit", args=[group.pk]) not in html
    assert "nur lesend" in html


def test_group_list_query_count_is_constant(
    client, staff_user, make_rollout_group, station_factory, django_assert_max_num_queries
):
    for i in range(5):
        g = make_rollout_group(f"G{i}")
        station_factory(rollout_group=g)
    client.force_login(staff_user)
    client.get(reverse("stations:group_list"))  # warm session/permission caches
    with django_assert_max_num_queries(8):
        assert client.get(reverse("stations:group_list")).status_code == 200


def test_staff_can_edit_group_but_not_its_channel(client, staff_user, make_rollout_group):
    group = make_rollout_group("Beta")
    client.force_login(staff_user)
    url = reverse("stations:group_edit", args=[group.pk])
    assert "disabled" in client.get(url).content.decode().split('name="channel"')[1][:200]

    response = client.post(
        url, {"name": "Beta wave", "slug": "beta", "channel": "dev", "description": ""}
    )

    assert response.status_code == 302
    group.refresh_from_db()
    assert group.name == "Beta wave"
    assert group.channel == RolloutGroup.Channel.RELEASE
    log = AccountAuditLog.objects.get(event_type=AccountAuditLog.EventType.CONFIG_CHANGED)
    assert log.actor == staff_user
    assert log.message == f"RolloutGroup beta updated by {staff_user}"
    assert log.details["changes"] == {"name": {"old": "Beta", "new": "Beta wave"}}


def test_create_group_is_audited(client, staff_user):
    client.force_login(staff_user)
    client.post(
        reverse("stations:group_create"),
        {"name": "Dev", "slug": "dev", "channel": "dev", "description": ""},
    )
    log = AccountAuditLog.objects.get(event_type=AccountAuditLog.EventType.CONFIG_CHANGED)
    assert log.message == f"RolloutGroup dev created by {staff_user}"
    assert log.details["changes"] == {"channel": "dev"}


def _station_post(station, **extra):
    data = {
        "name": station.name,
        "callsign": station.callsign,
        "description": "",
        "location_name": "",
        "latitude": "",
        "longitude": "",
        "altitude": "",
        "hardware_revision": "",
        "tx_audio_ceiling_dbfs": "-6",
        "notes": "",
    }
    data.update(extra)
    return data


def test_station_form_sets_group_and_audits(client, staff_user, station, make_rollout_group):
    beta = make_rollout_group("Beta", channel=RolloutGroup.Channel.DEV)
    client.force_login(staff_user)
    url = reverse("stations:station_edit", args=[station.pk])

    response = client.post(url, _station_post(station, rollout_group=beta.pk))

    assert response.status_code == 302
    station.refresh_from_db()
    assert station.rollout_group == beta
    log = StationAuditLog.objects.get(
        station=station, event_type=StationAuditLog.EventType.ROLLOUT_GROUP_CHANGED
    )
    assert log.message == f"Rollout group: none → Beta by {staff_user.username}"
    assert log.user == staff_user

    # Clearing the group audits the reverse transition.
    client.post(url, _station_post(station, rollout_group=""))
    station.refresh_from_db()
    assert station.rollout_group is None
    messages = list(
        StationAuditLog.objects.filter(
            station=station, event_type=StationAuditLog.EventType.ROLLOUT_GROUP_CHANGED
        ).values_list("message", flat=True)
    )
    assert f"Rollout group: Beta → none by {staff_user.username}" in messages

    # Saving without a group change writes no further group audit entry.
    client.post(url, _station_post(station, rollout_group="", notes="hi"))
    assert (
        StationAuditLog.objects.filter(
            station=station, event_type=StationAuditLog.EventType.ROLLOUT_GROUP_CHANGED
        ).count()
        == 2
    )


def test_station_create_with_group(client, staff_user, make_rollout_group):
    beta = make_rollout_group("Beta")
    client.force_login(staff_user)
    data = _station_post(Station(name="New", callsign="OE5NEW"), rollout_group=beta.pk)
    assert client.post(reverse("stations:station_create"), data).status_code == 302
    created = Station.objects.get(name="New")
    assert created.rollout_group == beta
    log = StationAuditLog.objects.get(
        station=created, event_type=StationAuditLog.EventType.ROLLOUT_GROUP_CHANGED
    )
    assert log.message == f"Rollout group: none → Beta by {staff_user.username}"


def test_station_create_without_group_writes_no_group_audit(client, staff_user):
    client.force_login(staff_user)
    data = _station_post(Station(name="New", callsign="OE5NEW"), rollout_group="")
    assert client.post(reverse("stations:station_create"), data).status_code == 302
    assert not StationAuditLog.objects.filter(
        event_type=StationAuditLog.EventType.ROLLOUT_GROUP_CHANGED
    ).exists()


def test_station_list_shows_group_badge(client, staff_user, station_factory, make_rollout_group):
    beta = make_rollout_group("Beta", channel=RolloutGroup.Channel.DEV)
    station_factory(rollout_group=beta)
    station_factory()
    client.force_login(staff_user)

    html = client.get(reverse("stations:station_list")).content.decode()

    assert 'data-station-group="beta"' in html
    assert 'data-station-group=""' in html
    assert 'class="pill pill-accent">DEV<' in html
    assert "No group" in html


def test_station_detail_shows_group(client, staff_user, station_factory, make_rollout_group):
    beta = make_rollout_group("Beta", channel=RolloutGroup.Channel.DEV)
    st = station_factory(rollout_group=beta)
    client.force_login(staff_user)
    html = client.get(reverse("stations:station_detail", args=[st.pk])).content.decode()
    assert 'data-station-group="beta"' in html
    assert 'class="pill pill-accent">DEV<' in html


def test_delete_group_view_blocked_while_referenced(
    client, staff_user, station, make_rollout_group
):
    group = make_rollout_group("Canary", channel=RolloutGroup.Channel.DEV)
    station.rollout_group = group
    station.save()
    seq = current_sequence()
    add_entry(seq, group, by_user=None)
    fm = ModuleType.objects.create(key="fm", display_name="FM")
    ModuleFirmwareTarget.objects.create(
        module_type=fm, scope=ModuleFirmwareTarget.Scope.GROUP, version="1.0", group=group
    )
    ModuleFirmwareTarget.objects.create(
        module_type=fm, scope=ModuleFirmwareTarget.Scope.FLEET, version="1.0", canary_group=group
    )
    client.force_login(staff_user)
    url = reverse("stations:group_delete", args=[group.pk])

    page = client.get(url).content.decode()
    assert "is still in use" in page
    assert "1 station is in this group" in page
    assert "a step in the rollout sequence" in page
    assert "1 group firmware target points at this group" in page
    assert "1 fleet target uses it as its canary group" in page
    assert "Delete group</button>" not in page

    response = client.post(url, follow=True)

    assert "still in use and cannot be deleted" in response.content.decode()
    assert RolloutGroup.objects.filter(pk=group.pk).exists()
    station.refresh_from_db()
    assert station.rollout_group == group
    assert not AccountAuditLog.objects.filter(
        event_type=AccountAuditLog.EventType.CONFIG_CHANGED
    ).exists()


def test_delete_unreferenced_group_keeps_deployment_history(
    client, staff_user, make_rollout_group, image_release
):
    group = make_rollout_group("Old")
    dep = Deployment.objects.create(
        image_release=image_release,
        target_type=Deployment.TargetType.GROUP,
        target_group=group,
    )
    client.force_login(staff_user)
    url = reverse("stations:group_delete", args=[group.pk])

    page = client.get(url).content.decode()
    assert "Delete group</button>" in page
    assert "1 past deployment keeps its history" in page

    response = client.post(url)

    assert response.status_code == 302
    assert not RolloutGroup.objects.filter(pk=group.pk).exists()
    dep.refresh_from_db()
    assert dep.target_group is None
    log = AccountAuditLog.objects.get(event_type=AccountAuditLog.EventType.CONFIG_CHANGED)
    assert log.message == f"RolloutGroup old deleted by {staff_user}"
