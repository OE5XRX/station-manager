"""Pick the module a passive service runs on."""

from decimal import Decimal

from apps.repeaters.selection import module_band_of


def _slot_key(module):
    slot = module.slot
    return (0, int(slot), module.module_id) if slot.isdigit() else (1, 0, slot + module.module_id)


def pick_slot(station, frequency_mhz: Decimal):
    """Online module with the lowest numeric slot whose ``rx_frequency`` ranges contain
    ``frequency_mhz`` and which is not being flashed; ``None`` if there is none."""
    freq = Decimal(str(frequency_mhz))
    candidates = [
        m
        for m in station.modules.filter(online=True, firmware_updating=False)
        if module_band_of(m, freq) is not None
    ]
    return min(candidates, key=_slot_key, default=None)
