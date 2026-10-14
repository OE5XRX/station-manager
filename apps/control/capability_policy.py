"""Platform-layer capability write policy (spec 2026-10-03 §4a).

Firmware ``describe`` is role-agnostic; the server decides who may change what, expressed as
a ``control`` area verb (spec 2026-10-14 §3.4): ``use`` (default — any lock holder with
``control.use``) or ``manage`` (calibration). ``persist`` marks
calibration-type values the server stores per station and re-applies through the agent.
Per-station overrides are a later extension (YAGNI).
"""

from __future__ import annotations

import dataclasses

from apps.accounts.permissions import can

# Monotonic: a later verb implies the earlier ones (control.use ⊆ control.manage).
_VERB_ORDER = ("use", "manage")


@dataclasses.dataclass(frozen=True)
class CapabilityPolicy:
    verb: str = "use"
    persist: bool = False

    def __post_init__(self):
        # Fail at definition time instead of denying at request time.
        if self.verb not in _VERB_ORDER:
            raise ValueError(f"capability verb must be use|manage, got {self.verb!r}")


_DEFAULT = CapabilityPolicy()
_MANAGE_PERSISTED = CapabilityPolicy(verb="manage", persist=True)

POLICY: dict[tuple[str, str | None], CapabilityPolicy] = {
    # SA818 filters (FW FilterCap, FW-RemoteStation #82): calibration, not a per-QSO control.
    ("filter_pre_emphasis", None): _MANAGE_PERSISTED,
    ("filter_hpf", None): _MANAGE_PERSISTED,
    ("filter_lpf", None): _MANAGE_PERSISTED,
    # SA818 AF-out volume = RX level into the fm_board ADC for every listener; the SA818
    # boots back to its default on a power cycle, so it must be persisted and re-applied.
    ("volume", "fm_transceiver"): _MANAGE_PERSISTED,
}


def policy_for(capability, module_type) -> CapabilityPolicy:
    # Malformed frames may carry any JSON value here; never raise on unhashable keys.
    # A non-str capability keeps the default policy; the agent rejects it.
    if not isinstance(capability, str):
        return _DEFAULT
    if not isinstance(module_type, str):
        return _strictest_for(capability)
    return POLICY.get((capability, module_type)) or POLICY.get((capability, None)) or _DEFAULT


def _strictest_for(capability: str) -> CapabilityPolicy:
    """Unknown module type (unregistered module id / malformed type): the strictest verb
    of ANY entry for this capability name, ``persist`` if any entry persists — so an
    unregistered module can never loosen a type-specific policy."""
    entries = [p for (cap, _type), p in POLICY.items() if cap == capability]
    if not entries:
        return _DEFAULT
    verb = max((p.verb for p in entries), key=_VERB_ORDER.index)
    return CapabilityPolicy(verb=verb, persist=any(p.persist for p in entries))


def can_write(user, capability, module_type) -> bool:
    """May ``user`` change ``capability``? = ``control.<policy verb>`` (station-independent)."""
    return can(user, "control", policy_for(capability, module_type).verb)
