"""MCUboot image helpers: extract the signing-key identity from a signed image."""

from __future__ import annotations

import re
import struct

BOOT_KEY_PREFIX_LEN = 16
# sha256(SPKI DER of MCUboot's public root-ec-p256.pem)[:8] as hex: the well-known dev key.
DEV_KEY_HASH_PREFIX = "e30466f6b8470c1f"

_IMAGE_MAGIC = 0x96F3B83D
_TLV_INFO_MAGIC = 0x6907
_TLV_PROT_INFO_MAGIC = 0x6908
_TLV_KEYHASH = 0x01
_KEYHASH_LEN = 32
_BOOT_KEY_RE = re.compile(r"[0-9a-f]{16}")


def _keyhash_in_area(blob: bytes, off: int, magic: int) -> tuple[str, int]:
    """Scan the TLV area at ``off`` (info header + entries); return (prefix, end offset)."""
    info_magic, total = struct.unpack_from("<HH", blob, off)
    if info_magic != magic or total < 4 or off + total > len(blob):
        raise struct.error("bad TLV info")
    pos, end = off + 4, off + total
    while pos + 4 <= end:
        etype, elen = struct.unpack_from("<HH", blob, pos)
        pos += 4
        if pos + elen > end:
            raise struct.error("TLV entry overflows area")
        if etype == _TLV_KEYHASH and elen == _KEYHASH_LEN:
            return blob[pos : pos + _KEYHASH_LEN].hex()[:BOOT_KEY_PREFIX_LEN], end
        pos += elen
    return "", end


def parse_boot_key_hash(blob: bytes) -> str:
    """16-hex prefix of the image's KEYHASH TLV, or "" if absent/malformed. Never raises."""
    try:
        if struct.unpack_from("<I", blob, 0)[0] != _IMAGE_MAGIC:
            return ""
        hdr_size = struct.unpack_from("<H", blob, 8)[0]
        img_size = struct.unpack_from("<I", blob, 12)[0]
        off = hdr_size + img_size
        if struct.unpack_from("<H", blob, off)[0] == _TLV_PROT_INFO_MAGIC:
            found, off = _keyhash_in_area(blob, off, _TLV_PROT_INFO_MAGIC)
            if found:
                return found
        return _keyhash_in_area(blob, off, _TLV_INFO_MAGIC)[0]
    except (struct.error, TypeError):
        return ""


def normalize_boot_key(value: object) -> str:
    """Lowercase a reported boot key; "" unless it is exactly 16 hex chars."""
    if not isinstance(value, str):
        return ""
    value = value.lower()
    return value if _BOOT_KEY_RE.fullmatch(value) else ""


def is_dev_key(prefix: str) -> bool:
    return prefix == DEV_KEY_HASH_PREFIX


# --- Boot-key check (spec §7, §5.5) ------------------------------------------------

_SYNTHETIC = "synthetic"  # Module.UidSource.SYNTHETIC; no models import (keeps this module pure)

NOT_UPDATABLE_DEV_TEXT = (
    "Nicht updatebar: Bootloader-Schlüssel passt nicht zur Release (Dev-Key) "
    "– SWD-Reflash an der Bench nötig"
)
NOT_UPDATABLE_OTHER_TEXT = (
    "Nicht updatebar: Bootloader-Schlüssel passt nicht zur Release (anderer Schlüssel) "
    "– SWD-Reflash an der Bench nötig"
)
NOT_UPDATABLE_NEUTRAL_TEXT = (
    "Nicht updatebar: Bootloader-Schlüssel passt nicht zur Release "
    "– SWD-Reflash an der Bench nötig"
)
KEY_UNKNOWN_HINT = (
    "Mögliche Ursache: Bootloader-Schlüssel passt nicht zur Release (z. B. Dev-Key) "
    "– Modul-Firmware zu alt, um das zu prüfen."
)


def boot_key_mismatch(module, release) -> bool:
    """True only if both keys are known and differ. Synthetic (sim) modules never
    carry an MCUboot TLV, so they always skip the check."""
    if module.uid_source == _SYNTHETIC:
        return False
    if not module.reported_boot_key or not release.boot_key_hash:
        return False
    return module.reported_boot_key != release.boot_key_hash


def not_updatable_text(module) -> str:
    if not module.reported_boot_key:
        # The module no longer reports a key: say nothing about which key it is.
        return NOT_UPDATABLE_NEUTRAL_TEXT
    if is_dev_key(module.reported_boot_key):
        return NOT_UPDATABLE_DEV_TEXT
    return NOT_UPDATABLE_OTHER_TEXT


def key_hint_applies(module, release, error_mode: str) -> bool:
    """§5.5: a rolled-back flash on a real module whose key could not be compared."""
    if error_mode != "rolled_back" or module.uid_source == _SYNTHETIC:
        return False
    return module.reported_boot_key == "" or release.boot_key_hash == ""
