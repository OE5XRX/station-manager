"""Platform-layer capability write policy (spec 2026-10-03 §4a).

Firmware ``describe`` is role-agnostic; the server decides who may change what. A capability
without an entry keeps today's behaviour (any lock-holding operator). ``persist`` marks
calibration-type values the server stores per station and re-applies through the agent.
Per-station overrides are a later extension (YAGNI).
"""

from __future__ import annotations

import dataclasses

ROLE_RANK = {"operator": 1, "station_manager": 2, "staff": 3, "admin": 4}


@dataclasses.dataclass(frozen=True)
class CapabilityPolicy:
    write_role: str = "operator"
    persist: bool = False

    def __post_init__(self):
        # Fail at definition time: an unranked role could otherwise only be caught by
        # can_write at request time (where it denies, see below).
        if self.write_role not in ROLE_RANK:
            raise ValueError(f"unranked write_role {self.write_role!r}")


_DEFAULT = CapabilityPolicy()
_STAFF_PERSISTED = CapabilityPolicy(write_role="staff", persist=True)

POLICY: dict[tuple[str, str | None], CapabilityPolicy] = {
    # SA818 filters (FW FilterCap, FW-RemoteStation #82): calibration, not a per-QSO control.
    ("filter_pre_emphasis", None): _STAFF_PERSISTED,
    ("filter_hpf", None): _STAFF_PERSISTED,
    ("filter_lpf", None): _STAFF_PERSISTED,
}


def policy_for(capability: str, module_type: str | None) -> CapabilityPolicy:
    # Malformed frames may carry any JSON value here; never raise on unhashable keys.
    # A non-str capability keeps today's behaviour (default policy); the agent rejects it.
    if not isinstance(capability, str):
        return _DEFAULT
    if not isinstance(module_type, str):
        module_type = None
    return POLICY.get((capability, module_type)) or POLICY.get((capability, None)) or _DEFAULT


def viewer_role(user, station) -> str | None:
    """Highest role on ``station``; ``None`` if the user may not use it (fail closed)."""
    if user is None or getattr(user, "is_anonymous", True) or not user.is_active:
        return None
    # Gate on can_use_station first so no higher rank (e.g. a stray assignment on an
    # applicant) can ever exceed what can_use_station allows.
    # COUPLING: today can_use_station is station-independent (membership-level based) and
    # admins/staff always pass it. If it ever becomes station-dependent, admins/staff must
    # still keep their role here (see test_admin_and_staff_keep_their_role_on_arbitrary_station).
    if not user.can_use_station(station):
        return None
    if user.is_admin:
        return "admin"
    if user.is_internal:
        return "staff"
    if user.can_administer_station(station):
        return "station_manager"
    return "operator"


def can_write(user, station, capability: str, module_type: str | None) -> bool:
    role = viewer_role(user, station)
    if role is None:
        return False
    required = ROLE_RANK.get(policy_for(capability, module_type).write_role)
    if required is None:
        return False  # unranked role (should be impossible, see __post_init__): deny
    return ROLE_RANK[role] >= required
