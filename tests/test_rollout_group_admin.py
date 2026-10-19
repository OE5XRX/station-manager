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


def _config_rows():
    from apps.accounts.models import AccountAuditLog

    return AccountAuditLog.objects.filter(event_type=AccountAuditLog.EventType.CONFIG_CHANGED)


def test_admin_channel_fixed_after_create_and_writes_audited(client, admin_user):
    client.force_login(admin_user)
    client.post(
        reverse("admin:stations_rolloutgroup_add"),
        {"name": "Dev", "slug": "dev", "channel": "dev", "description": ""},
    )
    group = RolloutGroup.objects.get(slug="dev")
    created = _config_rows().get()
    assert created.message == f"RolloutGroup dev created by {admin_user} (admin)"
    assert created.details["changes"] == {"channel": "dev"}

    r = client.post(
        reverse("admin:stations_rolloutgroup_change", args=[group.pk]),
        {"name": "Dev boxes", "slug": "dev", "channel": "release", "description": ""},
    )
    assert r.status_code == 302
    group.refresh_from_db()
    assert group.channel == RolloutGroup.Channel.DEV  # read-only field ignored
    updated = _config_rows().latest("pk")
    assert updated.details["changes"] == {"name": {"old": "Dev", "new": "Dev boxes"}}


def test_admin_delete_is_audited(client, admin_user):
    group = RolloutGroup.objects.create(name="Gone", slug="gone")
    client.force_login(admin_user)
    client.post(reverse("admin:stations_rolloutgroup_delete", args=[group.pk]), {"post": "yes"})
    assert not RolloutGroup.objects.filter(pk=group.pk).exists()
    assert _config_rows().get().message == f"RolloutGroup gone deleted by {admin_user} (admin)"
