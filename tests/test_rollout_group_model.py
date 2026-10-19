"""RolloutGroup model: reserved-slug guard, channel default, station FK semantics."""

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.stations.models import RolloutGroup, Station


@pytest.mark.django_db
def test_reserved_slug_rejected():
    with pytest.raises(IntegrityError), transaction.atomic():
        RolloutGroup.objects.create(name="Unassigned", slug="__unassigned__")


@pytest.mark.django_db
def test_reserved_slug_rejected_by_clean():
    group = RolloutGroup(name="Unassigned", slug="__unassigned__")
    with pytest.raises(ValidationError) as exc:
        group.full_clean()
    assert "slug" in exc.value.message_dict


@pytest.mark.django_db
def test_station_group_set_null_on_delete():
    group = RolloutGroup.objects.create(name="Beta", slug="beta")
    station = Station.objects.create(name="S1", rollout_group=group)
    assert list(group.stations.all()) == [station]

    group.delete()

    station.refresh_from_db()
    assert station.rollout_group is None


@pytest.mark.django_db
def test_channel_default_release():
    group = RolloutGroup.objects.create(name="Prod", slug="prod")
    assert group.channel == RolloutGroup.Channel.RELEASE == "release"
    assert RolloutGroup.Channel.DEV == "dev"


@pytest.mark.django_db
def test_ordering_by_name():
    RolloutGroup.objects.create(name="Zulu", slug="zulu")
    RolloutGroup.objects.create(name="Alpha", slug="alpha")
    assert [g.name for g in RolloutGroup.objects.all()] == ["Alpha", "Zulu"]


def test_station_admin_rollout_group_read_only():
    """Admin must not change a station's group (unaudited path)."""
    from django.contrib import admin

    model_admin = admin.site._registry[Station]
    assert "rollout_group" in model_admin.readonly_fields
