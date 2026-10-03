import pytest
from rest_framework.permissions import SAFE_METHODS

from apps.api import write_scoping as ws
from apps.api.write_permissions import TopologyScopedWritePermission


@pytest.mark.django_db
def test_can_write_station_region_manager_and_staff(api_topology):
    t = api_topology
    assert ws.can_write_station(t["region_mgr"], t["station_in"]) is True
    assert ws.can_write_station(t["region_mgr"], t["station_out"]) is False
    assert ws.can_write_station(t["staff"], t["station_out"]) is True
    assert ws.can_write_station(t["station_user"], t["station_in"]) is False  # assigned != manager


@pytest.mark.django_db
def test_delete_station_internal_only(api_topology):
    t = api_topology
    assert ws.can_delete_station(t["region_mgr"], t["station_in"]) is False
    assert ws.can_delete_station(t["admin"], t["station_in"]) is True


@pytest.mark.django_db
def test_station_content_follows_accessible(api_topology):
    t = api_topology
    assert ws.can_write_station_content(t["station_user"], t["station_in"]) is True
    assert ws.can_write_station_content(t["station_user"], t["station_out"]) is False
    # accessible_stations() returns none() for applicants → content-write never granted
    assert ws.can_write_station_content(t["applicant"], t["station_in"]) is False


@pytest.mark.django_db
def test_global_role_predicates(api_topology):
    t = api_topology
    assert ws.can_write_region(t["staff"]) is True
    assert ws.can_write_region(t["region_mgr"]) is False
    assert ws.can_write_alert_rule(t["region_mgr"]) is True   # region-mgr/staff global
    assert ws.can_write_user(t["region_mgr"]) is False
    assert ws.can_trigger_provisioning(t["region_mgr"], t["station_in"]) is False


class _View:
    def can_write_object(self, user, obj, method):
        return user == "owner"


class _Req:
    def __init__(self, method, user):
        self.method = method
        self.user = user


def test_write_permission_safe_methods_pass():
    perm = TopologyScopedWritePermission()
    for m in SAFE_METHODS:
        assert perm.has_object_permission(_Req(m, "nobody"), _View(), object()) is True


def test_write_permission_delegates_unsafe():
    perm = TopologyScopedWritePermission()
    assert perm.has_object_permission(_Req("DELETE", "owner"), _View(), object()) is True
    assert perm.has_object_permission(_Req("DELETE", "other"), _View(), object()) is False
