from __future__ import annotations

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from apps.api.streaming import open_storage_stream


def _variant_seg(variant: str) -> str:
    return variant or "_"


def release_key(type_key: str, version: str, variant: str) -> str:
    return f"module_firmware/{type_key}/{version}/{_variant_seg(variant)}.signed.bin"


def bundle_key(type_key: str, version: str, variant: str) -> str:
    return f"{release_key(type_key, version, variant)}.bundle"


def sim_release_key(type_key: str, version: str, variant: str) -> str:
    return f"module_firmware/{type_key}/{version}/{_variant_seg(variant)}.native_sim"


def sim_bundle_key(type_key: str, version: str, variant: str) -> str:
    return f"{sim_release_key(type_key, version, variant)}.bundle"


def upload_bytes(key: str, data: bytes) -> None:
    if default_storage.exists(key):
        default_storage.delete(key)
    default_storage.save(key, ContentFile(data))


def open_stream(key: str):
    """Lazy, seekable stream for proxying ``key`` to an HTTP client.

    First byte after one HEAD + ranged GET; a single connection without
    mid-stream retry. Batch consumers that read the whole object should
    use ``open_file`` instead.
    """
    return open_storage_stream(key)


def open_file(key: str):
    """Open ``key`` for a full read via the storage backend.

    On S3 the object is fetched up front with s3transfer (parallel
    multipart, retries) — the right trade-off for background jobs, where
    total time and robustness matter more than time-to-first-byte.
    """
    return default_storage.open(key, "rb")


def delete(key: str) -> None:
    if default_storage.exists(key):
        default_storage.delete(key)
