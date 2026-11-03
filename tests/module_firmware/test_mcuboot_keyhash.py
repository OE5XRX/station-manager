import hashlib
import struct
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization

from apps.module_firmware.mcuboot import (
    DEV_KEY_HASH_PREFIX,
    is_dev_key,
    normalize_boot_key,
    parse_boot_key_hash,
)

FIX = Path(__file__).parent.parent / "fixtures" / "mcuboot"


def _spki_hash(pem_path: Path) -> str:
    key = serialization.load_pem_private_key(pem_path.read_bytes(), password=None)
    der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return hashlib.sha256(der).hexdigest()


def _dev() -> bytes:
    return (FIX / "dev-signed.bin").read_bytes()


def test_dev_signed_fixture_yields_dev_prefix():
    assert parse_boot_key_hash(_dev()) == DEV_KEY_HASH_PREFIX
    assert is_dev_key(DEV_KEY_HASH_PREFIX)
    assert not is_dev_key("")


def test_dev_prefix_matches_dev_pem():
    assert _spki_hash(FIX / "root-ec-p256.pem")[:16] == DEV_KEY_HASH_PREFIX


def test_other_key_differs_from_dev():
    h = parse_boot_key_hash((FIX / "other-signed.bin").read_bytes())
    assert len(h) == 16 and h != DEV_KEY_HASH_PREFIX
    assert h == _spki_hash(FIX / "other-key.pem")[:16]


@pytest.mark.parametrize("cut", [600, 10, 0])
def test_parse_truncated_returns_empty(cut):
    assert parse_boot_key_hash(_dev()[:cut]) == ""


def test_parse_wrong_magic_returns_empty():
    assert parse_boot_key_hash(b"\x00" * 4 + _dev()[4:]) == ""


def _tlv_info_offset(blob: bytes) -> int:
    hdr_size = struct.unpack_from("<H", blob, 8)[0]
    img_size = struct.unpack_from("<I", blob, 12)[0]
    return hdr_size + img_size


def test_parse_tlv_len_overflow_returns_empty():
    blob = bytearray(_dev())
    off = _tlv_info_offset(blob)
    # imgtool emits no protected area here: first info is the unprotected one.
    assert struct.unpack_from("<H", blob, off)[0] == 0x6907
    struct.pack_into("<H", blob, off + 2, 0xFFFF)
    assert parse_boot_key_hash(bytes(blob)) == ""


def test_parse_without_keyhash_returns_empty():
    blob = bytearray(_dev())
    off = _tlv_info_offset(blob) + 4 + 4 + 32  # skip info header and the SHA256 entry
    assert struct.unpack_from("<HH", blob, off) == (0x01, 32)
    struct.pack_into("<H", blob, off, 0x7F)
    assert parse_boot_key_hash(bytes(blob)) == ""


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("E30466F6B8470C1F", "e30466f6b8470c1f"),
        ("e30466f6", ""),
        ("zz0466f6b8470c1f", ""),
        (None, ""),
        (123, ""),
        ("", ""),
    ],
)
def test_normalize_boot_key(raw, expected):
    assert normalize_boot_key(raw) == expected
