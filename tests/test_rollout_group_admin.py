"""Django admin deletes of a RolloutGroup go through rollouts.services.delete_group."""

import pytest
from django.urls import reverse

from apps.stations.models import RolloutGroup, Station, StationAuditLog

pytestmark = pytest.mark.django_db


def _event():
    return StationAuditLog.EventType.ROLLOUT_GROUP_CHANGED


def test_admin_delete_view_audits_members(client, admin_user):
    group = RolloutGroup.objects.create(name="Canary", slug="canary")
    station = Station.objects.create(name="s1", rollout_group=group)
    client.force_login(admin_user)
    r = client.post(
        reverse("admin:stations_rolloutgroup_delete", args=[group.pk]), {"post": "yes"}
    )
    assert r.status_code == 302
    assert not RolloutGroup.objects.filter(pk=group.pk).exists()
    log = StationAuditLog.objects.get(station=station, event_type=_event())
    assert log.user == admin_user


def test_admin_bulk_delete_audits_members(client, admin_user):
    group = RolloutGroup.objects.create(name="Canary", slug="canary")
    station = Station.objects.create(name="s1", rollout_group=group)
    client.force_login(admin_user)
    r = client.post(
        reverse("admin:stations_rolloutgroup_changelist"),
        {"action": "delete_selected", "_selected_action": [group.pk], "post": "yes"},
    )
    assert r.status_code == 302
    assert not RolloutGroup.objects.filter(pk=group.pk).exists()
    assert StationAuditLog.objects.filter(station=station, event_type=_event()).count() == 1
