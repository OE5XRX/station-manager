"""Pick the module a passive service runs on."""

from decimal import Decimal

from apps.repeaters.selection import module_band_of


def _slot_key(module):
    return int(module.slot)


def pick_slot(station, frequency_mhz: Decimal):
    """Online module with the lowest numeric slot whose ``rx_frequency`` ranges contain
    ``frequency_mhz`` and which is not being flashed; ``None`` if there is none. Only
    numeric slots count: decoder frames address slots by integer."""
    freq = Decimal(str(frequency_mhz))
    candidates = [
        m
        for m in station.modules.filter(online=True, firmware_updating=False)
        if m.slot.isdigit() and module_band_of(m, freq) is not None
    ]
    return min(candidates, key=_slot_key, default=None)
