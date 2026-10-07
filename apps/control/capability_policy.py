"""Platform-layer capability write policy (spec 2026-10-03 §4a).

Firmware ``describe`` is role-agnostic; the server decides who may change what. A capability
without an entry keeps today's behaviour (any lock-holding operator). ``persist`` marks
calibration-type values the server stores per station and re-applies through the agent.
Per-station overrides are a later extension (YAGNI).
"""

from __future__ import annotations

import dataclasses

from django.utils.translation import gettext_lazy as _

ROLE_RANK = {"operator": 1, "station_manager": 2, "staff": 3, "admin": 4}

# Human-readable, translatable names for the badge shown on read-only widgets.
ROLE_LABELS = {
    "operator": _("operator"),
    "station_manager": _("station manager"),
    "staff": _("staff"),
    "admin": _("admin"),
}


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
        return _strictest_for(capability)
    return POLICY.get((capability, module_type)) or POLICY.get((capability, None)) or _DEFAULT


def _strictest_for(capability: str) -> CapabilityPolicy:
    """Unknown module type (unregistered module id / malformed type): the strictest
    ``write_role`` of ANY entry for this capability name, ``persist`` if any entry
    persists — so an unregistered module can never loosen a type-specific policy."""
    entries = [p for (cap, _type), p in POLICY.items() if cap == capability]
    if not entries:
        return _DEFAULT
    role = max((p.write_role for p in entries), key=lambda r: ROLE_RANK.get(r, 0))
    return CapabilityPolicy(write_role=role, persist=any(p.persist for p in entries))


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


def role_allows(role: str | None, write_role: str) -> bool:
    """Does ``role`` (from :func:`viewer_role`) satisfy ``write_role``? Fails closed."""
    have = ROLE_RANK.get(role) if role is not None else None
    need = ROLE_RANK.get(write_role)
    return have is not None and need is not None and have >= need


def can_write(user, station, capability: str, module_type: str | None) -> bool:
    return role_allows(viewer_role(user, station), policy_for(capability, module_type).write_role)
