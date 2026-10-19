"""Module firmware targets scoped to rollout groups (station > group > fleet,
canary gate on canary_group)."""

import pytest
from django.db import IntegrityError, transaction
from django.urls import reverse

from apps.module_firmware import ops
from apps.module_firmware.models import (
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
    ModuleType,
)
from apps.module_firmware.reconciler import effective_target
from apps.stations.models import RolloutGroup

pytestmark = pytest.mark.django_db


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


@pytest.fixture
def beta(db):
    return RolloutGroup.objects.create(name="Beta", slug="beta")


@pytest.fixture
def other(db):
    return RolloutGroup.objects.create(name="Other", slug="other")


def _target(fm, scope, version, **kw):
    return ModuleFirmwareTarget.objects.create(module_type=fm, scope=scope, version=version, **kw)


def test_precedence_station_over_group_over_fleet(fm, beta, station_factory):
    st = station_factory(rollout_group=beta)
    _target(fm, ModuleFirmwareTarget.Scope.FLEET, "fleet-v")
    assert effective_target(st, fm).version == "fleet-v"
    _target(fm, ModuleFirmwareTarget.Scope.GROUP, "group-v", group=beta)
    assert effective_target(st, fm).version == "group-v"
    _target(fm, ModuleFirmwareTarget.Scope.STATION, "station-v", station=st)
    assert effective_target(st, fm).version == "station-v"


def test_group_target_ignored_for_other_group(fm, beta, other, station_factory):
    st = station_factory(rollout_group=other)
    ungrouped = station_factory()
    _target(fm, ModuleFirmwareTarget.Scope.FLEET, "fleet-v")
    _target(fm, ModuleFirmwareTarget.Scope.GROUP, "group-v", group=beta)
    assert effective_target(st, fm).version == "fleet-v"
    assert effective_target(ungrouped, fm).version == "fleet-v"


def test_canary_group_gates_fleet_target(fm, beta, other, station_factory):
    canary_st = station_factory(rollout_group=beta)
    other_st = station_factory(rollout_group=other)
    ungrouped = station_factory()
    _target(fm, ModuleFirmwareTarget.Scope.FLEET, "fleet-v", canary_group=beta)
    assert effective_target(canary_st, fm).version == "fleet-v"
    assert effective_target(other_st, fm) is None
    assert effective_target(ungrouped, fm) is None


def test_unique_group_target_per_type(fm, beta, other):
    _target(fm, ModuleFirmwareTarget.Scope.GROUP, "1", group=beta)
    _target(fm, ModuleFirmwareTarget.Scope.GROUP, "1", group=other)
    with pytest.raises(IntegrityError), transaction.atomic():
        _target(fm, ModuleFirmwareTarget.Scope.GROUP, "2", group=beta)


def test_group_scope_requires_group_only(fm, beta, station_factory):
    st = station_factory()
    with pytest.raises(IntegrityError), transaction.atomic():
        _target(fm, ModuleFirmwareTarget.Scope.GROUP, "1")
    with pytest.raises(IntegrityError), transaction.atomic():
        _target(fm, ModuleFirmwareTarget.Scope.GROUP, "1", group=beta, station=st)
    with pytest.raises(IntegrityError), transaction.atomic():
        _target(fm, ModuleFirmwareTarget.Scope.FLEET, "1", group=beta)
    with pytest.raises(IntegrityError), transaction.atomic():
        _target(fm, ModuleFirmwareTarget.Scope.STATION, "1", station=st, group=beta)
    with pytest.raises(IntegrityError), transaction.atomic():
        _target(fm, ModuleFirmwareTarget.Scope.GROUP, "1", group=beta, canary_group=beta)
    assert set(ModuleFirmwareTarget.Scope.values) == {"fleet", "group", "station"}


def test_api_target_group_fields(api_topology, bearer, fm, beta, monkeypatch):
    monkeypatch.setattr(ops, "reconcile_module_type", lambda mt: None)
    rel = ModuleFirmwareRelease.objects.create(
        module_type=fm,
        variant="vhf",
        version="1.0",
        storage_key="k",
        sha256="a" * 64,
        size_bytes=1,
        source_repo="r",
        source_tag="v1.0",
    )
    client = bearer(api_topology["staff"])
    url = reverse("api:module-firmware-target-list")

    resp = client.post(url, {"release": rel.pk, "scope": "group", "group": beta.pk}, format="json")
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body["scope"] == "group"
    assert body["group"] == beta.pk
    assert "tag" not in body and "canary_tag" not in body

    resp = client.post(
        url, {"release": rel.pk, "scope": "fleet", "canary_group": beta.pk}, format="json"
    )
    assert resp.status_code == 201, resp.content
    assert resp.json()["canary_group"] == beta.pk

    resp = client.post(url, {"release": rel.pk, "scope": "group"}, format="json")
    assert resp.status_code == 400
    assert "non_field_errors" in resp.json()

    resp = client.get(url, {"group": beta.pk})
    assert resp.status_code == 200
    assert [r["scope"] for r in resp.json()["results"]] == ["group"]
