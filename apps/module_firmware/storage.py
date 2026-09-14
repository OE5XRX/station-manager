from __future__ import annotations

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage


def _variant_seg(variant: str) -> str:
    return variant or "_"


def release_key(type_key: str, version: str, variant: str) -> str:
    return f"module_firmware/{type_key}/{version}/{_variant_seg(variant)}.signed.bin"


def bundle_key(type_key: str, version: str, variant: str) -> str:
    return f"{release_key(type_key, version, variant)}.bundle"


def upload_bytes(key: str, data: bytes) -> None:
    if default_storage.exists(key):
        default_storage.delete(key)
    default_storage.save(key, ContentFile(data))


def open_stream(key: str):
    return default_storage.open(key, "rb")


def delete(key: str) -> None:
    if default_storage.exists(key):
        default_storage.delete(key)
