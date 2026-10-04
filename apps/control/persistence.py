"""Per-station persistence + drift re-apply for role-gated calibration capabilities
(spec §4a). The FW persists nothing; the server stores the chosen value and, whenever an
agent inventory shows the module disagreeing (reconnect, module reboot, SA818 power-cycle),
sends a ``set`` through the agent. Idempotent: no drift → no frames.

Safety: only capabilities whose policy has ``persist=True`` are stored or re-applied, only
as ``op: "set"`` on a non-readonly ``setting`` present in the module's *current* descriptor,
and only with a value that type-checks against that descriptor. Re-apply never keys TX.
"""

import uuid

from . import capability_policy
from .models import PersistedCapability, StationModule


def save(station, slot, module_id, capability, value, user):
    """Upsert the persisted value. Callers must have validated ``value`` (see
    :func:`persist_if_valid`)."""
    PersistedCapability.objects.update_or_create(
        station=station,
        slot=str(slot),
        module_id=module_id,
        capability=capability,
        defaults={"value": value, "updated_by": user},
    )


def value_fits(cap, value):
    """True iff ``value`` is a well-typed value for descriptor entry ``cap``.

    Mirrors the agent's ``descriptor._check_value`` type rules so only scalar values of
    the declared type are ever persisted (never arbitrary JSON blobs).
    """
    vtype = cap.get("type")
    if vtype == "bool":
        return isinstance(value, bool)
    if vtype == "int":
        return isinstance(value, int) and not isinstance(value, bool) and _in_range(cap, value)
    if vtype == "float":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and _in_range(cap, value)
        )
    if vtype == "string":
        return isinstance(value, str)
    if vtype == "enum":
        values = cap.get("values")
        return (
            isinstance(values, list)
            and isinstance(value, (str, int, float))
            and not isinstance(value, bool)
            and value in values
        )
    return False


def _in_range(cap, value):
    lo, hi = cap.get("min"), cap.get("max")
    if isinstance(lo, (int, float)) and value < lo:
        return False
    if isinstance(hi, (int, float)) and value > hi:
        return False
    return True


def _settable_cap(desc, name):
    """The descriptor entry for ``name`` if it is a writable setting, else ``None``."""
    if not isinstance(desc, list):
        return None
    for d in desc:
        if (
            isinstance(d, dict)
            and d.get("name") == name
            and d.get("kind") == "setting"
            and not d.get("readonly")
        ):
            return d
    return None


def persist_if_valid(station, slot, module_id, capability, value, user):
    """Persist after a successful set, iff the module is registered, the capability is a
    persist-policy writable setting in its descriptor, and ``value`` type-checks.
    Returns True if a row was written."""
    if not isinstance(capability, str) or not isinstance(module_id, str):
        return False
    sm = StationModule.objects.filter(station=station, slot=str(slot), module_id=module_id).first()
    if sm is None:
        return False
    if not capability_policy.policy_for(capability, sm.type).persist:
        return False
    cap = _settable_cap(sm.capability_descriptor, capability)
    if cap is None or not value_fits(cap, value):
        return False
    save(station, slot, module_id, capability, value, user)
    return True


def _same(a, b):
    # Type-strict for bools: Python treats 0 == False / 1 == True.
    return a == b and isinstance(a, bool) == isinstance(b, bool)


def reapply_frames(station, slots):
    """``set`` frames for every persisted value the reported inventory disagrees with."""
    if not isinstance(slots, list):
        return []
    rows = {}
    for r in PersistedCapability.objects.filter(station=station):
        rows.setdefault((r.slot, r.module_id), []).append(r)
    if not rows:
        return []
    frames = []
    for entry in slots:
        if not isinstance(entry, dict) or not isinstance(entry.get("modules"), list):
            continue
        slot = entry.get("slot")
        if not isinstance(slot, (str, int)) or isinstance(slot, bool):
            continue
        for mod in entry["modules"]:
            if not isinstance(mod, dict) or not isinstance(mod.get("module"), str):
                continue
            module_id = mod["module"]
            identity = mod.get("identity") if isinstance(mod.get("identity"), dict) else {}
            module_type = identity.get("type")
            desc = mod.get("capabilities")
            state = mod.get("state") if isinstance(mod.get("state"), dict) else {}
            for row in rows.get((str(slot), module_id), []):
                if not capability_policy.policy_for(row.capability, module_type).persist:
                    continue
                cap = _settable_cap(desc, row.capability)
                if cap is None or not value_fits(cap, row.value):
                    continue
                if row.capability in state and _same(state[row.capability], row.value):
                    continue
                frames.append(
                    {
                        "v": 1,
                        "type": "command",
                        "request_id": f"persist-{row.pk}-{uuid.uuid4().hex[:8]}",
                        "slot": slot,
                        "module": module_id,
                        "capability": row.capability,
                        "op": "set",
                        "value": row.value,
                    }
                )
    return frames
