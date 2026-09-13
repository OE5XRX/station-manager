"""E2E verification of the module ingestion chain through apply_inventory.

These tests drive the *public* entry point the control WebSocket consumer calls
(``apps.control.registry.apply_inventory``) rather than ``ingest_module``
directly, using the exact wire shape the agent's broker emits
(``{"slot": <int>, "modules": [{"module", "identity", "capabilities",
"state"}]}``). They cover the full agent-report -> server-state chain:
discovery -> swap / move -> Module + assignment history + StationAuditLog +
StationModule.tracked_module linkage.

The unit tests in test_ingest_*.py cover ingest_module in isolation; this file
closes the gap of asserting the same behaviors survive through apply_inventory,
including StationModule re-linking on swap and the full audit triple on first
discovery.
"""

import pytest

from apps.control.models import StationModule
from apps.control.registry import apply_inventory
from apps.module_firmware.models import Module, ModuleAssignmentHistory, ModuleType
from apps.stations.models import StationAuditLog

EV = StationAuditLog.EventType


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


def slot_frame(slot, uid=None, version="1.0.0", module_id="fm", mtype="fm"):
    """One inventory slot in the exact shape broker.emit_inventory produces.

    slot is an int (as discover_slots/emit_inventory emit); the "module" key
    (not "id") is what the broker remaps discovery's per-module "id" to before
    it hits the wire and apply_inventory.
    """
    identity = {"type": mtype, "model": "SA818", "version": version}
    if uid is not None:
        identity["uid"] = uid
    return {
        "slot": slot,
        "control": "/dev/oe5xrx/slot1/control",
        "modules": [
            {
                "module": module_id,
                "identity": identity,
                "capabilities": [],
                "state": {},
            }
        ],
    }


def audit_count(event_type, module=None):
    qs = StationAuditLog.objects.filter(event_type=event_type)
    if module is not None:
        qs = qs.filter(module=module)
    return qs.count()


@pytest.mark.django_db
def test_full_inventory_discovery_creates_everything(fm, station_factory):
    """Scenario 1: full UID'd inventory through apply_inventory yields Module +
    open assignment + the DISCOVERED/ASSIGNMENT_CHANGED/LIFECYCLE_CHANGED audit
    triple + linked StationModule.tracked_module + lifecycle=deployed."""
    station = station_factory()
    apply_inventory(station, [slot_frame(1, uid="UID-A")])

    module = Module.objects.get(uid="UID-A")
    assert module.module_type == fm
    assert module.lifecycle_status == Module.Lifecycle.DEPLOYED

    # StationModule created and linked (slot int 1 -> CharField "1").
    sm = StationModule.objects.get(station=station, slot="1", module_id="fm")
    assert sm.tracked_module_id == module.id
    assert sm.online is True

    # Exactly one open assignment at this station/slot.
    open_rows = ModuleAssignmentHistory.objects.filter(module=module, to_ts__isnull=True)
    assert open_rows.count() == 1
    assert open_rows.first().station == station and open_rows.first().slot == "1"

    # Full audit triple, all attributed to this module.
    assert audit_count(EV.MODULE_DISCOVERED, module) == 1
    assert audit_count(EV.MODULE_ASSIGNMENT_CHANGED, module) == 1
    assert audit_count(EV.MODULE_LIFECYCLE_CHANGED, module) == 1


@pytest.mark.django_db
def test_swap_same_slot_new_uid(fm, station_factory):
    """Scenario 2: same slot, different UID => old assignment closed, new opened,
    MODULE_SWAPPED audit, StationModule re-linked to the new physical module,
    and the displaced module falls back from deployed to ready."""
    station = station_factory()
    apply_inventory(station, [slot_frame(1, uid="OLD")])
    old = Module.objects.get(uid="OLD")
    assert old.lifecycle_status == Module.Lifecycle.DEPLOYED

    apply_inventory(station, [slot_frame(1, uid="NEW")])
    new = Module.objects.get(uid="NEW")
    old.refresh_from_db()

    # Old assignment closed, new one open.
    old_row = ModuleAssignmentHistory.objects.get(module=old)
    assert old_row.to_ts is not None
    assert ModuleAssignmentHistory.objects.filter(module=new, to_ts__isnull=True).count() == 1
    assert old.assignments.filter(to_ts__isnull=True).count() == 0

    # SWAPPED audit attributed to the incoming module.
    assert audit_count(EV.MODULE_SWAPPED, new) == 1

    # New module deployed (correct).
    assert new.lifecycle_status == Module.Lifecycle.DEPLOYED

    # Displaced module lost its slot => lifecycle re-derived back to ready,
    # with a LIFECYCLE_CHANGED audit recorded against it.
    assert old.lifecycle_status == Module.Lifecycle.READY
    assert audit_count(EV.MODULE_LIFECYCLE_CHANGED, old) == 2  # deployed→ (initial), →ready

    # StationModule row for (station, slot1, fm) now tracks the NEW physical module.
    sm = StationModule.objects.get(station=station, slot="1", module_id="fm")
    assert sm.tracked_module_id == new.id


@pytest.mark.django_db
def test_module_moved_to_other_station_and_slot(fm, station_factory):
    """Scenario 3: module physically moved to another station/slot => old open
    row closed, new open row at the new location."""
    station_a = station_factory()
    station_b = station_factory()

    apply_inventory(station_a, [slot_frame(1, uid="ROAM")])
    module = Module.objects.get(uid="ROAM")

    # Move to station_b, slot 2. station_a's next report no longer lists it.
    apply_inventory(station_a, [])  # slot now empty on A
    apply_inventory(station_b, [slot_frame(2, uid="ROAM")])

    rows = ModuleAssignmentHistory.objects.filter(module=module).order_by("from_ts")
    assert rows.count() == 2
    assert rows[0].station == station_a and rows[0].slot == "1" and rows[0].to_ts is not None
    assert rows[1].station == station_b and rows[1].slot == "2" and rows[1].to_ts is None
    assert module.assignments.filter(to_ts__isnull=True).count() == 1

    # StationModule on A goes offline (not reported); B has a fresh linked row.
    sm_a = StationModule.objects.get(station=station_a, slot="1", module_id="fm")
    assert sm_a.online is False
    sm_b = StationModule.objects.get(station=station_b, slot="2", module_id="fm")
    assert sm_b.tracked_module_id == module.id


@pytest.mark.django_db
def test_legacy_no_uid_leaves_tracked_module_null(fm, station_factory):
    """Scenario 4: legacy no-UID module => StationModule created, tracked_module
    stays None, no Module row, no module_firmware audit rows."""
    station = station_factory()
    apply_inventory(station, [slot_frame(1, uid=None)])

    sm = StationModule.objects.get(station=station, slot="1", module_id="fm")
    assert sm.tracked_module is None
    assert Module.objects.count() == 0
    assert ModuleAssignmentHistory.objects.count() == 0
    assert audit_count(EV.MODULE_DISCOVERED) == 0


@pytest.mark.django_db
def test_synthetic_uid_shape_flows_through_apply_inventory(fm, station_factory):
    """Scenario 5/6: the fake_fw / discover_slots synthetic-UID identity shape
    ({type, model, version, uid, uid_source}) is exactly what apply_inventory +
    ingest_module consume. Feeding the discovered identity dict verbatim tracks
    the module with the synthetic UID and records uid_source=synthetic."""
    station = station_factory()
    # This is the identity dict discover_slots emits for the sim FM module
    # (fake_fw._describe injects uid + uid_source when the spec omits them).
    discovered_identity = {
        "type": "fm",
        "model": "sim",
        "version": "0.0.1",
        "uid": "SIM-FM-0001",
        "uid_source": "synthetic",
    }
    frame = {
        "slot": 1,
        "control": "/dev/oe5xrx/slot1/control",
        # broker remaps discovery's "id" -> "module"; identity/capabilities pass through.
        "modules": [
            {
                "module": "fm",
                "identity": discovered_identity,
                "capabilities": [],
                "state": {},
            }
        ],
    }
    apply_inventory(station, [frame])

    module = Module.objects.get(uid="SIM-FM-0001")
    assert module.uid_source == Module.UidSource.SYNTHETIC
    sm = StationModule.objects.get(station=station, slot="1", module_id="fm")
    assert sm.tracked_module_id == module.id
