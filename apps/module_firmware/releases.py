from __future__ import annotations

import hashlib
import re
import urllib.request
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from apps.images.cosign import verify_blob_identity
from apps.images.github_releases import fetch_releases

from . import storage
from .models import ModuleFirmwareImportJob, ModuleFirmwareRelease

_SIGNED_SUFFIX = ".signed.bin"


@dataclass(frozen=True)
class VariantAsset:
    variant: str
    signed_name: str
    bundle_name: str


def parse_variant_assets(asset_names: set[str], prefix: str) -> list[VariantAsset]:
    """From a release's asset names, extract (variant, signed, bundle) tuples.

    Matches ``{prefix}[-{variant}].signed.bin`` where a matching
    ``.bundle`` sibling exists. A bare ``{prefix}.signed.bin`` yields
    variant "". A prefix that is only a substring of a longer token
    (e.g. ``fm-sa818x-...``) is rejected — the char after the prefix
    must be '-' or the suffix itself.
    """
    out: list[VariantAsset] = []
    for name in sorted(asset_names):
        if not (name.startswith(prefix) and name.endswith(_SIGNED_SUFFIX)):
            continue
        middle = name[len(prefix) : -len(_SIGNED_SUFFIX)]
        if middle == "":
            variant = ""
        elif middle.startswith("-"):
            variant = middle[1:]
            if not variant:
                continue
        else:
            continue  # prefix is a proper substring of a longer token
        bundle_name = name + ".bundle"
        if bundle_name not in asset_names:
            continue
        out.append(VariantAsset(variant=variant, signed_name=name, bundle_name=bundle_name))
    return out


def parse_sha256sums(text: str) -> dict[str, str]:
    """Parse ``<hex>  <filename>`` lines into {filename: hex}."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            result[parts[-1]] = parts[0].lower()
    return result


_DOWNLOAD_TIMEOUT = 60
_DEFAULT_BRANCH = "main"  # FW-RemoteStation release.yml runs workflow_dispatch on main


def _fw_identity_regexp(repo: str, branch: str = _DEFAULT_BRANCH) -> str:
    return (
        rf"^https://github\.com/{re.escape(repo)}"
        rf"/\.github/workflows/release\.yml@refs/heads/{re.escape(branch)}$"
    )


def _asset_url(repo: str, tag: str, name: str) -> str:
    return f"https://github.com/{repo}/releases/download/{tag}/{name}"


def _download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT) as resp:
        return resp.read()


def import_release_tag(job: ModuleFirmwareImportJob) -> None:
    repo = job.source_repo
    prefix = job.module_type.release_asset_prefix
    uploaded: list[str] = []
    try:
        if not prefix:
            raise ValueError(f"ModuleType {job.module_type.key} has no release_asset_prefix")
        releases_list = fetch_releases(repo)
        gh = next((r for r in releases_list if r.tag == job.tag), None)
        if gh is None:
            raise ValueError(f"release tag {job.tag} not found in {repo}")
        variants = parse_variant_assets(set(gh.asset_names), prefix)
        if not variants:
            raise ValueError(f"no {prefix}-*.signed.bin (+bundle) assets in {job.tag}")
        sums = parse_sha256sums(_download(_asset_url(repo, job.tag, "SHA256SUMS")).decode("utf-8"))
        identity = _fw_identity_regexp(repo)
        last_release = None
        for va in variants:
            blob = _download(_asset_url(repo, job.tag, va.signed_name))
            expected = sums.get(va.signed_name)
            if not expected or hashlib.sha256(blob).hexdigest() != expected:
                raise ValueError(f"sha256 mismatch for {va.signed_name}")
            bundle = _download(_asset_url(repo, job.tag, va.bundle_name))
            verify_blob_identity(blob, bundle, identity)
            skey = storage.release_key(job.module_type.key, job.tag, va.variant)
            bkey = storage.bundle_key(job.module_type.key, job.tag, va.variant)
            storage.upload_bytes(skey, blob)
            uploaded.append(skey)
            storage.upload_bytes(bkey, bundle)
            uploaded.append(bkey)
            with transaction.atomic():
                last_release, _ = ModuleFirmwareRelease.all_objects.update_or_create(
                    module_type=job.module_type,
                    variant=va.variant,
                    version=job.tag,
                    defaults={
                        "storage_key": skey,
                        "sha256": expected,
                        "size_bytes": len(blob),
                        "cosign_bundle_key": bkey,
                        "source_repo": repo,
                        "source_tag": job.tag,
                        "source_github_url": _asset_url(repo, job.tag, va.signed_name),
                        "imported_by": job.requested_by,
                        "archived_at": None,
                    },
                )
        job.release = last_release
        job.status = ModuleFirmwareImportJob.Status.READY
        job.completed_at = timezone.now()
        job.save(update_fields=["release", "status", "completed_at"])
    except Exception as exc:
        # Best-effort cleanup of keys we uploaded that no active release row references.
        in_use = set(
            ModuleFirmwareRelease.all_objects.filter(
                module_type=job.module_type, version=job.tag
            ).values_list("storage_key", flat=True)
        ) | set(
            ModuleFirmwareRelease.all_objects.filter(
                module_type=job.module_type, version=job.tag
            ).values_list("cosign_bundle_key", flat=True)
        )
        for key in uploaded:
            if key in in_use:
                continue
            try:
                storage.delete(key)
            except Exception:
                pass
        job.status = ModuleFirmwareImportJob.Status.FAILED
        job.error_message = str(exc)
        job.completed_at = timezone.now()
        job.save(update_fields=["status", "error_message", "completed_at"])
