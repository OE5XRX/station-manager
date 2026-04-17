from __future__ import annotations

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage


def release_key(tag: str, machine: str) -> str:
    return f"images/{tag}/{machine}.wic.bz2"


def release_bundle_key(tag: str, machine: str) -> str:
    return f"{release_key(tag, machine)}.bundle"


def upload_bytes(key: str, data: bytes) -> None:
    """Upload bytes to S3 (or local media) under `key`, overwriting."""
    if default_storage.exists(key):
        default_storage.delete(key)
    default_storage.save(key, ContentFile(data))


def open_stream(key: str):
    """Return a file-like opened on the stored object."""
    return default_storage.open(key, "rb")


def delete(key: str) -> None:
    if default_storage.exists(key):
        default_storage.delete(key)
