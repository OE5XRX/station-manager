from __future__ import annotations

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from apps.api.streaming import open_storage_stream


def release_key(tag: str, machine: str, channel: str = "release") -> str:
    return f"images/{tag}/{channel}/{machine}.wic.bz2"


def release_bundle_key(tag: str, machine: str, channel: str = "release") -> str:
    return f"{release_key(tag, machine, channel)}.bundle"


def release_rootfs_key(tag: str, machine: str, channel: str = "release") -> str:
    """S3 key for the extracted root partition artifact.

    Lives alongside the full wic under the same ``images/<tag>/<channel>/``
    prefix so cleanup of a given release can delete the whole folder.
    """
    return f"images/{tag}/{channel}/{machine}.rootfs.bz2"


def upload_bytes(key: str, data: bytes) -> None:
    """Upload bytes to S3 (or local media) under `key`, overwriting."""
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
