"""Extract the `root_a` partition from a GPT-formatted wic image,
bz2-compressed, for use as the OTA download artifact.

Keeps the full wic (partition table + all partitions) available for
bare-metal flashing; the OTA agent only needs the rootfs bytes that
go onto an A/B slot, and producing that artifact here avoids a
dependency on the linux-image build pipeline to ship two outputs.
"""

from __future__ import annotations

import bz2
import hashlib
import struct
from pathlib import Path

_SECTOR = 512
_GPT_SIGNATURE = b"EFI PART"
_ROOT_PARTITION_NAME = "root_a"
_EXT4_MAGIC = b"\x53\xef"
_EXT4_MAGIC_OFFSET = 1080
_CHUNK = 1 << 20  # 1 MiB read/compress chunk


def extract_rootfs(wic_path: Path, out_path: Path) -> tuple[int, str]:
    """Extract the `root_a` partition from ``wic_path``, bz2-compress
    it, and write the result to ``out_path``.

    Returns the (compressed_size_bytes, compressed_sha256_hex) of the
    written file. Raises ValueError on:

    - not a GPT image (header signature mismatch)
    - no partition named 'root_a'
    - the partition's declared range exceeds the wic file size
    - the partition does not begin with an ext4 superblock magic
      (the cheapest sanity check against a corrupt partition table)

    On any ValueError raised mid-stream (e.g. partition read ended
    early), ``out_path`` may be partially written — callers should
    discard the file on any exception. The intended caller places
    ``out_path`` inside a ``tempfile.TemporaryDirectory()`` so the
    partial is reaped automatically.
    """
    wic_path = Path(wic_path)
    out_path = Path(out_path)

    with open(wic_path, "rb") as src:
        start_lba, end_lba = _locate_root_partition(src)
        _verify_bounds(wic_path, start_lba, end_lba)
        _verify_ext4_magic(src, start_lba)
        return _compress_partition(src, out_path, start_lba, end_lba)


def _locate_root_partition(src) -> tuple[int, int]:
    """Parse the GPT header + partition entries, return (start_lba,
    end_lba) of the partition named `root_a`.
    """
    src.seek(_SECTOR)  # LBA 1
    header = src.read(96)
    if header[0:8] != _GPT_SIGNATURE:
        raise ValueError("not a GPT image")

    # Only the fields we need from the 92-byte GPT header. CRCs are
    # intentionally not verified — this file came from a cosign-verified
    # release asset, we're inside the import worker, and the downstream
    # ext4 magic check catches the "header pointed us at garbage"
    # failure modes that matter to us.
    entry_start_lba = struct.unpack("<Q", header[72:80])[0]
    num_entries = struct.unpack("<I", header[80:84])[0]
    entry_size = struct.unpack("<I", header[84:88])[0]

    src.seek(entry_start_lba * _SECTOR)
    for _ in range(num_entries):
        entry = src.read(entry_size)
        # UEFI spec minimum is 128 bytes per entry; if a malformed header
        # claims fewer, `entry[56:128]` would silently slice to whatever
        # was read and produce a spurious decode. Bail out instead and
        # fall through to the "no partition named 'root_a'" error below,
        # which an operator can investigate.
        if len(entry) != entry_size or entry_size < 128:
            break
        name = entry[56:128].decode("utf-16-le").rstrip("\x00")
        if name == _ROOT_PARTITION_NAME:
            start_lba = struct.unpack("<Q", entry[32:40])[0]
            end_lba = struct.unpack("<Q", entry[40:48])[0]
            return start_lba, end_lba

    raise ValueError(f"no partition named {_ROOT_PARTITION_NAME!r}")


def _verify_bounds(wic_path: Path, start_lba: int, end_lba: int) -> None:
    file_size = wic_path.stat().st_size
    partition_end_byte = (end_lba + 1) * _SECTOR
    if partition_end_byte > file_size:
        raise ValueError(
            f"{_ROOT_PARTITION_NAME} (start={start_lba}, end={end_lba}) "
            f"exceeds wic size {file_size}"
        )


def _verify_ext4_magic(src, start_lba: int) -> None:
    src.seek(start_lba * _SECTOR + _EXT4_MAGIC_OFFSET)
    magic = src.read(2)
    if magic != _EXT4_MAGIC:
        raise ValueError(
            f"{_ROOT_PARTITION_NAME} is not ext4 "
            f"(magic at byte +{_EXT4_MAGIC_OFFSET} was {magic!r}, "
            f"expected {_EXT4_MAGIC!r})"
        )


def _compress_partition(src, out_path: Path, start_lba: int, end_lba: int) -> tuple[int, str]:
    src.seek(start_lba * _SECTOR)
    remaining = (end_lba - start_lba + 1) * _SECTOR
    compressor = bz2.BZ2Compressor(9)
    sha = hashlib.sha256()
    size = 0

    with open(out_path, "wb") as dst:
        while remaining > 0:
            chunk = src.read(min(_CHUNK, remaining))
            if not chunk:
                raise ValueError(
                    f"{_ROOT_PARTITION_NAME} read ended early ({remaining} bytes unread)"
                )
            remaining -= len(chunk)
            compressed = compressor.compress(chunk)
            if compressed:
                dst.write(compressed)
                sha.update(compressed)
                size += len(compressed)
        tail = compressor.flush()
        if tail:
            dst.write(tail)
            sha.update(tail)
            size += len(tail)

    return size, sha.hexdigest()
