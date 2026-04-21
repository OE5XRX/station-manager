"""Unit tests for apps.images.extraction.

The test helper `_build_synthetic_wic` constructs a minimal but valid
GPT image (MBR + GPT header + one partition entry + payload) in-memory.
That lets us verify `extract_rootfs`'s binary parsing without having
to keep a 4 GiB fixture under version control.
"""

from __future__ import annotations

import bz2
import hashlib
import struct
from pathlib import Path

import pytest

from apps.images.extraction import extract_rootfs

_SECTOR = 512


def _build_synthetic_wic(
    tmp_path: Path,
    *,
    partition_name: str = "root_a",
    start_lba: int = 8,
    end_lba: int = 15,  # 8 sectors = 4 kB partition
    ext4_magic: bool = True,
    signature: bytes = b"EFI PART",
    total_sectors: int = 64,
) -> tuple[Path, bytes]:
    """Build a minimal GPT-formatted file and return (path, partition_bytes).

    The caller gets the raw partition payload back so the happy-path test
    can compare extracted (decompressed) bytes against it.
    """
    buf = bytearray(total_sectors * _SECTOR)

    # LBA 1 (offset 0x200): GPT header — only the fields our parser reads
    # are filled; CRCs stay zero (parser doesn't verify them).
    header = bytearray(92)
    header[0:8] = signature
    header[8:12] = struct.pack("<I", 0x00010000)  # revision
    header[12:16] = struct.pack("<I", 92)  # header size
    header[72:80] = struct.pack("<Q", 2)  # partition entry start LBA
    header[80:84] = struct.pack("<I", 1)  # num partition entries
    header[84:88] = struct.pack("<I", 128)  # partition entry size
    buf[_SECTOR : _SECTOR + 92] = header

    # LBA 2 (offset 0x400): one partition entry, 128 bytes.
    entry = bytearray(128)
    entry[32:40] = struct.pack("<Q", start_lba)
    entry[40:48] = struct.pack("<Q", end_lba)
    name_utf16 = partition_name.encode("utf-16-le")
    entry[56 : 56 + len(name_utf16)] = name_utf16
    buf[2 * _SECTOR : 2 * _SECTOR + 128] = entry

    # Partition payload: only written when the declared range fits in the file
    # (out-of-bounds tests pass end_lba beyond total_sectors deliberately).
    partition_offset = start_lba * _SECTOR
    partition_size = (end_lba - start_lba + 1) * _SECTOR
    fits = (partition_offset + partition_size) <= len(buf)
    if fits:
        pattern = bytes(range(256))
        repeats = partition_size // 256
        tail = partition_size % 256
        payload = pattern * repeats + pattern[:tail]
        buf[partition_offset : partition_offset + partition_size] = payload

        # ext4 superblock magic at offset 1080 from partition start.
        if ext4_magic:
            buf[partition_offset + 1080 : partition_offset + 1082] = b"\x53\xef"

    wic = tmp_path / "synthetic.wic"
    wic.write_bytes(bytes(buf))
    return wic, bytes(buf[partition_offset : partition_offset + partition_size]) if fits else b""


def test_extract_rootfs_round_trip(tmp_path):
    wic, partition_bytes = _build_synthetic_wic(tmp_path)
    out = tmp_path / "rootfs.bz2"

    size_bytes, sha256_hex = extract_rootfs(wic, out)

    # The compressed blob round-trips to the raw partition.
    decompressed = bz2.decompress(out.read_bytes())
    assert decompressed == partition_bytes

    # Returned metadata matches the actual compressed file on disk.
    assert size_bytes == out.stat().st_size
    assert sha256_hex == hashlib.sha256(out.read_bytes()).hexdigest()


def test_extract_rootfs_rejects_missing_partition(tmp_path):
    wic, _ = _build_synthetic_wic(tmp_path, partition_name="rootfs")
    out = tmp_path / "rootfs.bz2"

    with pytest.raises(ValueError, match=r"no partition named 'root_a'"):
        extract_rootfs(wic, out)


def test_extract_rootfs_rejects_bad_ext4_magic(tmp_path):
    wic, _ = _build_synthetic_wic(tmp_path, ext4_magic=False)
    out = tmp_path / "rootfs.bz2"

    with pytest.raises(ValueError, match=r"not ext4"):
        extract_rootfs(wic, out)


def test_extract_rootfs_rejects_non_gpt(tmp_path):
    wic, _ = _build_synthetic_wic(tmp_path, signature=b"\x00" * 8)
    out = tmp_path / "rootfs.bz2"

    with pytest.raises(ValueError, match=r"not a GPT image"):
        extract_rootfs(wic, out)


def test_extract_rootfs_rejects_out_of_bounds(tmp_path):
    # end_lba points past the 64-sector file (start=8, end=999).
    wic, _ = _build_synthetic_wic(tmp_path, start_lba=8, end_lba=999)
    out = tmp_path / "rootfs.bz2"

    with pytest.raises(ValueError, match=r"exceeds wic size"):
        extract_rootfs(wic, out)
