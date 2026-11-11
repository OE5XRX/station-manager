"""Radio profiles for passive services: which capabilities to write, and in what order."""

from apps.control.models import PersistedCapability
from apps.repeaters.selection import _caps

# Order matters: frequency before the tones/squelch that depend on the band.
PROFILE_ORDER = (
    "frequency",
    "rx_frequency",
    "tx_frequency",
    "bandwidth",
    "rx_tone",
    "tx_tone",
    "squelch",
    "volume",
)


def profile_caps(values: dict, module) -> set[str]:
    """Capability names ``profile_commands`` would send for ``module``."""
    return {name for name, _value in profile_commands(values, module)}


def profile_commands(values: dict, module) -> list[tuple[str, object]]:
    """``(capability, value)`` pairs in ``PROFILE_ORDER``.

    Only capabilities the module declares as ``setting`` survive; ``filter_*`` and anything
    outside ``PROFILE_ORDER`` is dropped."""
    caps = _caps(module)
    out = []
    for name in PROFILE_ORDER:
        if name not in values:
            continue
        if caps.get(name, {}).get("kind") != "setting":
            continue
        out.append((name, values[name]))
    return out


def handover_values(station, module, handover: dict) -> dict:
    """``handover`` plus the persisted ``volume`` of this slot/module unless the hand-over
    profile sets one itself (a passive run must not leave the radio at its RX volume)."""
    values = dict(handover)
    if "volume" in values:
        return values
    persisted = PersistedCapability.objects.filter(
        station=station, slot=module.slot, module_id=module.module_id, capability="volume"
    ).first()
    if persisted is not None:
        values["volume"] = persisted.value
    return values
