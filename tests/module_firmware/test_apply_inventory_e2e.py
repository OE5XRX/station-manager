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
def test_reingest_same_int_slot_is_idempotent(fm, station_factory):
    """Slots arrive as ints on the wire but are stored as CharField. A repeated
    heartbeat for the same int slot must NOT churn the assignment history — the
    idempotency comparison has to survive the int/str round-trip."""
    station = station_factory()
    apply_inventory(station, [slot_frame(1, uid="A")])
    apply_inventory(station, [slot_frame(1, uid="A", version="1.1.0")])
    module = Module.objects.get(uid="A")
    assert ModuleAssignmentHistory.objects.filter(module=module).count() == 1
    assert audit_count(EV.MODULE_SWAPPED) == 0
    assert audit_count(EV.MODULE_ASSIGNMENT_CHANGED, module) == 1


@pytest.mark.django_db
def test_pulled_module_closes_assignment_and_reverts_to_ready(fm, station_factory):
    """A module whose slot drops out of a still-populated snapshot loses its open
    assignment and reverts deployed -> ready (reconcile_station), even though
    ingest_module never runs for the vanished module. Reconciliation only fires
    on a NON-empty snapshot (an empty one may be a transient discovery failure),
    so the surviving module keeps slot 2 populated."""
    station = station_factory()
    apply_inventory(station, [slot_frame(1, uid="PULLED"), slot_frame(2, uid="KEEP")])
    module = Module.objects.get(uid="PULLED")
    assert module.lifecycle_status == Module.Lifecycle.DEPLOYED

    # Next snapshot: slot 1 gone, slot 2 still there (non-empty).
    apply_inventory(station, [slot_frame(2, uid="KEEP")])
    module.refresh_from_db()

    assert module.assignments.filter(to_ts__isnull=True).count() == 0
    assert module.lifecycle_status == Module.Lifecycle.READY


@pytest.mark.django_db
def test_responsive_empty_slot_reconciles(fm, station_factory):
    """A responsive-but-empty slot list (slots=[{slot, modules: []}]) is a real
    snapshot, so a module previously in that slot is released — unlike slots=[]
    (discovery failure)."""
    station = station_factory()
    apply_inventory(station, [slot_frame(1, uid="GONE")])
    module = Module.objects.get(uid="GONE")
    assert module.lifecycle_status == Module.Lifecycle.DEPLOYED

    apply_inventory(station, [{"slot": 1, "control": "/dev/x", "modules": []}])
    module.refresh_from_db()
    assert module.assignments.filter(to_ts__isnull=True).count() == 0
    assert module.lifecycle_status == Module.Lifecycle.READY


@pytest.mark.django_db
def test_type_mismatch_report_preserves_valid_assignment(fm, station_factory):
    """A spurious type-mismatch report for an already-tracked UID must NOT clear
    its link or close its assignment (the same uid is still reported in its
    slot)."""
    ModuleType.objects.create(key="power", display_name="Power")
    station = station_factory()
    apply_inventory(station, [slot_frame(1, uid="DUP")])
    module = Module.objects.get(uid="DUP")
    sm_before = StationModule.objects.get(station=station, slot="1", module_id="fm")
    assert sm_before.tracked_module_id == module.id

    # slot 1 re-reports uid DUP but with a different registered type (mismatch).
    mismatch = {"type": "power", "model": "x", "version": "1.0.0", "uid": "DUP"}
    apply_inventory(
        station,
        [
            {
                "slot": 1,
                "control": "/dev/x",
                "modules": [
                    {"module": "fm", "identity": mismatch, "capabilities": [], "state": {}}
                ],
            }
        ],
    )
    module.refresh_from_db()

    # Assignment preserved, still deployed, still typed fm, still linked.
    assert module.module_type.key == "fm"
    assert module.assignments.filter(to_ts__isnull=True).count() == 1
    assert module.lifecycle_status == Module.Lifecycle.DEPLOYED
    sm_after = StationModule.objects.get(station=station, slot="1", module_id="fm")
    assert sm_after.tracked_module_id == module.id


@pytest.mark.django_db
def test_rejected_report_with_different_uid_clears_link(fm, station_factory):
    """A uid-bearing rejection whose uid differs from the linked module clears
    the link (the slot no longer holds that module), keeping the persisted link
    consistent with the assignment history."""
    station = station_factory()
    apply_inventory(station, [slot_frame(1, uid="A"), slot_frame(2, uid="KEEP")])
    a = Module.objects.get(uid="A")

    # slot 1 now reports an UNKNOWN type with a different uid B (rejected, no link).
    unknown = {"type": "mystery", "model": "x", "version": "1.0.0", "uid": "B"}
    apply_inventory(
        station,
        [
            {
                "slot": 1,
                "control": "/dev/x",
                "modules": [
                    {"module": "fm", "identity": unknown, "capabilities": [], "state": {}}
                ],
            },
            slot_frame(2, uid="KEEP"),
        ],
    )
    a.refresh_from_db()

    sm = StationModule.objects.get(station=station, slot="1", module_id="fm")
    assert sm.tracked_module_id is None  # link cleared
    assert a.assignments.filter(to_ts__isnull=True).count() == 0  # A released
    assert a.lifecycle_status == Module.Lifecycle.READY


@pytest.mark.django_db
def test_empty_snapshot_does_not_reconcile(fm, station_factory):
    """A fully empty inventory (indistinguishable from a discovery failure) must
    NOT tear down assignments — the deployed module keeps its open assignment."""
    station = station_factory()
    apply_inventory(station, [slot_frame(1, uid="SAFE")])
    module = Module.objects.get(uid="SAFE")

    apply_inventory(station, [])  # empty — treated as "no fresh data", not "empty rack"
    module.refresh_from_db()

    assert module.assignments.filter(to_ts__isnull=True).count() == 1
    assert module.lifecycle_status == Module.Lifecycle.DEPLOYED


@pytest.mark.django_db
def test_pulled_sticky_module_keeps_state_but_closes_assignment(fm, station_factory):
    """A pulled module marked defect stays defect (sticky) but still loses its
    open assignment."""
    station = station_factory()
    apply_inventory(station, [slot_frame(1, uid="STICKY"), slot_frame(2, uid="KEEP")])
    module = Module.objects.get(uid="STICKY")
    module.lifecycle_status = Module.Lifecycle.DEFECT
    module.save(update_fields=["lifecycle_status"])

    apply_inventory(station, [slot_frame(2, uid="KEEP")])
    module.refresh_from_db()

    assert module.assignments.filter(to_ts__isnull=True).count() == 0
    assert module.lifecycle_status == Module.Lifecycle.DEFECT


@pytest.mark.django_db
def test_legacy_replacing_tracked_module_releases_assignment(fm, station_factory):
    """A legacy no-UID module reported into a slot that previously held a tracked
    module must release the old module's assignment (the slot no longer holds a
    linked Module, so it's excluded from tracked_slots)."""
    station = station_factory()
    apply_inventory(station, [slot_frame(1, uid="WASTRACKED"), slot_frame(2, uid="KEEP")])
    module = Module.objects.get(uid="WASTRACKED")

    # slot 1 now reports a legacy module with no uid; slot 2 keeps the snapshot non-empty.
    apply_inventory(station, [slot_frame(1, uid=None), slot_frame(2, uid="KEEP")])
    module.refresh_from_db()

    assert module.assignments.filter(to_ts__isnull=True).count() == 0
    assert module.lifecycle_status == Module.Lifecycle.READY


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
