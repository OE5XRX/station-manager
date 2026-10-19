"""Django admin honours PROTECT on RolloutGroup: a referenced group can't be deleted."""

import pytest
from django.urls import reverse

from apps.stations.models import RolloutGroup, Station

pytestmark = pytest.mark.django_db


def test_admin_delete_blocked_while_station_in_group(client, admin_user):
    group = RolloutGroup.objects.create(name="Canary", slug="canary")
    station = Station.objects.create(name="s1", rollout_group=group)
    client.force_login(admin_user)
    r = client.post(
        reverse("admin:stations_rolloutgroup_delete", args=[group.pk]), {"post": "yes"}
    )
    assert r.status_code == 200  # Django's "cannot delete: protected" page
    assert RolloutGroup.objects.filter(pk=group.pk).exists()
    station.refresh_from_db()
    assert station.rollout_group == group


def test_admin_deletes_unreferenced_group(client, admin_user):
    group = RolloutGroup.objects.create(name="Empty", slug="empty")
    client.force_login(admin_user)
    r = client.post(
        reverse("admin:stations_rolloutgroup_changelist"),
        {"action": "delete_selected", "_selected_action": [group.pk], "post": "yes"},
    )
    assert r.status_code == 302
    assert not RolloutGroup.objects.filter(pk=group.pk).exists()
