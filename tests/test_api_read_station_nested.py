"""Tests for nested station sub-resources under /api/v1/stations/{id}/…"""

import pytest

from tests.test_api_read_fixtures import bearer, topology  # noqa: F401


def _mk_children(topology):  # noqa: F811
    from apps.control.models import StationModule
    from apps.stations.models import StationInventory, StationLogEntry, StationTelemetry

    s = topology["station_in"]
    StationTelemetry.objects.create(station=s, boot_count=3, active_slot="A", image_version="v1")
    StationInventory.objects.create(station=s, data={"cpu": "rp4"})
    StationLogEntry.objects.create(station=s, entry_type="note", title="t", message="m")
    StationModule.objects.create(station=s, slot="1", module_id="fm0", type="fm")


@pytest.mark.django_db
def test_telemetry_nested_in_scope(topology):  # noqa: F811
    _mk_children(topology)
    pk = topology["station_in"].pk
    resp = bearer(topology["station_user"]).get(f"/api/v1/stations/{pk}/telemetry/")
    assert resp.status_code == 200
    assert resp.data["boot_count"] == 3
    # internal alerting bookkeeping must NOT leak
    assert "power_alerted_boot_id" not in resp.data
    assert "alerted_io_error_count" not in resp.data


@pytest.mark.django_db
def test_telemetry_out_of_scope_parent_404(topology):  # noqa: F811
    pk = topology["station_out"].pk
    resp = bearer(topology["station_user"]).get(f"/api/v1/stations/{pk}/telemetry/")
    assert resp.status_code == 404


@pytest.mark.django_db
def test_telemetry_missing_is_404(topology):  # noqa: F811
    pk = topology["station_in"].pk  # no telemetry row created
    resp = bearer(topology["station_user"]).get(f"/api/v1/stations/{pk}/telemetry/")
    assert resp.status_code == 404


@pytest.mark.django_db
def test_inventory_nested(topology):  # noqa: F811
    _mk_children(topology)
    pk = topology["station_in"].pk
    resp = bearer(topology["station_user"]).get(f"/api/v1/stations/{pk}/inventory/")
    assert resp.status_code == 200
    assert resp.data["data"] == {"cpu": "rp4"}


@pytest.mark.django_db
def test_log_entries_and_modules_lists(topology):  # noqa: F811
    _mk_children(topology)
    pk = topology["station_in"].pk
    c = bearer(topology["station_user"])
    le = c.get(f"/api/v1/stations/{pk}/log-entries/")
    assert le.status_code == 200 and le.data["count"] == 1
    mods = c.get(f"/api/v1/stations/{pk}/modules/")
    assert mods.status_code == 200 and mods.data["count"] == 1
