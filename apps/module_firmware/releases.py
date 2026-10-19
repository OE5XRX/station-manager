from __future__ import annotations

import hashlib
import logging
import re
import urllib.request
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from apps.images.cosign import verify_blob_identity
from apps.images.github_releases import fetch_release_by_tag

from . import storage
from .models import ModuleFirmwareImportJob, ModuleFirmwareRelease
from .trust import is_trusted_repo, is_valid_tag, trusted_owners

logger = logging.getLogger(__name__)

_SIGNED_SUFFIX = ".signed.bin"
_SIM_SUFFIX = ".native_sim"
_VARIANT_RE = re.compile(r"[a-z0-9][a-z0-9-]*")


@dataclass(frozen=True)
class VariantAsset:
    variant: str
    signed_name: str
    bundle_name: str


def _variant_from_name(name: str, prefix: str, suffix: str) -> str | None:
    """Return the variant token of ``{prefix}[-{variant}]{suffix}`` or None.

    A bare ``{prefix}{suffix}`` yields variant "". A prefix that is only a
    substring of a longer token (e.g. ``fm-sa818x-...``) is rejected — the
    char after the prefix must be '-' or the suffix itself. Only lowercase
    alnum + internal hyphens are accepted as variant: this blocks "_"
    (collides with the bandless storage segment), path chars ("/", "..")
    and anything that could shape a storage key outside its segment.
    """
    if not (name.startswith(prefix) and name.endswith(suffix)):
        return None
    middle = name[len(prefix) : -len(suffix)]
    if middle == "":
        return ""
    if not middle.startswith("-"):
        return None  # prefix is a proper substring of a longer token
    variant = middle[1:]
    if not _VARIANT_RE.fullmatch(variant):
        return None
    return variant


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
        variant = _variant_from_name(name, prefix, _SIGNED_SUFFIX)
        if variant is None:
            continue
        bundle_name = name + ".bundle"
        if bundle_name not in asset_names:
            continue
        out.append(VariantAsset(variant=variant, signed_name=name, bundle_name=bundle_name))
    return out


def parse_sim_assets(asset_names: set[str], prefix: str) -> dict[str, str]:
    """From a release's asset names, extract {variant: native_sim asset name}.

    Matches ``{prefix}[-{variant}].native_sim`` with the same variant rules
    as :func:`parse_variant_assets`. Only entries with a ``{name}.bundle``
    sibling count — an unsigned sim blob is never imported (it is executed
    as root on sim stations).
    """
    out: dict[str, str] = {}
    for name in sorted(asset_names):
        variant = _variant_from_name(name, prefix, _SIM_SUFFIX)
        if variant is None or name + ".bundle" not in asset_names:
            continue
        out[variant] = name
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
# Upper bound for any single release asset held in memory (signed.bin, native_sim ELF,
# bundles, SHA256SUMS).
MAX_FIRMWARE_ASSET_BYTES = 64 * 1024 * 1024
_DOWNLOAD_CHUNK = 1024 * 1024
_DEFAULT_BRANCH = "main"  # FW-RemoteStation release.yml runs workflow_dispatch on main


def _fw_identity_regexp(repo: str, branch: str = _DEFAULT_BRANCH) -> str:
    return (
        rf"^https://github\.com/{re.escape(repo)}"
        rf"/\.github/workflows/release\.yml@refs/heads/{re.escape(branch)}$"
    )


def _asset_url(repo: str, tag: str, name: str) -> str:
    return f"https://github.com/{repo}/releases/download/{tag}/{name}"


def _download(url: str, max_bytes: int | None = None) -> bytes:
    """Fetch ``url`` with a hard size cap: staged blobs are held in memory until every
    variant verifies, so an oversized asset (declared via Content-Length or only
    discovered while streaming) fails the import instead of exhausting memory."""
    if max_bytes is None:
        max_bytes = MAX_FIRMWARE_ASSET_BYTES
    with urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT) as resp:
        declared = resp.headers.get("Content-Length") if resp.headers else None
        if declared is not None and declared.isdigit() and int(declared) > max_bytes:
            raise ValueError(f"asset {url} is {declared} bytes, exceeds cap of {max_bytes}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = resp.read(_DOWNLOAD_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"asset {url} exceeds cap of {max_bytes} bytes")
            chunks.append(chunk)
        return b"".join(chunks)


@dataclass
class _StagedVariant:
    va: VariantAsset
    skey: str
    bkey: str
    size: int
    sha256: str
    url: str
    blob: bytes
    bundle: bytes
    sim: _StagedSim | None = None


@dataclass
class _StagedSim:
    skey: str
    bkey: str
    size: int
    sha256: str
    blob: bytes
    bundle: bytes


def _reconcile_after_publish(module_type) -> None:
    from .services import reconcile_module_type  # services imports reconciler; avoid cycles

    try:
        with transaction.atomic():
            reconcile_module_type(module_type)
    except Exception:
        logger.warning("post-publish reconcile for %s failed", module_type.key, exc_info=True)


def import_release_tag(job: ModuleFirmwareImportJob) -> None:
    repo = job.source_repo
    prefix = job.module_type.release_asset_prefix
    uploaded: list[str] = []
    try:
        if not is_valid_tag(job.tag):
            raise ValueError(f"invalid release tag {job.tag!r}; refusing to import")
        if not is_trusted_repo(repo):
            raise ValueError(
                f"source repo {repo!r} is not owned by a trusted owner "
                f"({', '.join(trusted_owners())}); refusing to import"
            )
        if not prefix:
            raise ValueError(f"ModuleType {job.module_type.key} has no release_asset_prefix")
        gh = fetch_release_by_tag(repo, job.tag)
        if gh is None:
            raise ValueError(f"release tag {job.tag} not found in {repo}")
        variants = parse_variant_assets(set(gh.asset_names), prefix)
        sim_assets = parse_sim_assets(set(gh.asset_names), prefix)
        if not variants:
            raise ValueError(
                f"no {prefix}-*.signed.bin or {prefix}.signed.bin (+bundle) assets in {job.tag}"
            )

        # --- Partition variants: active-existing vs to_import ---
        active_existing: list[ModuleFirmwareRelease] = []
        to_import: list[VariantAsset] = []
        for va in variants:
            row = ModuleFirmwareRelease.all_objects.filter(
                module_type=job.module_type, variant=va.variant, version=job.tag
            ).first()
            if row is not None and row.archived_at is None:
                # Already active — skip entirely (idempotent)
                active_existing.append(row)
            else:
                # No row or archived row — needs import/restore
                to_import.append(va)

        # --- Stage phase: download + verify all to_import variants ---
        staged: list[_StagedVariant] = []
        if to_import:
            sums = parse_sha256sums(
                _download(_asset_url(repo, job.tag, "SHA256SUMS")).decode("utf-8")
            )
            identity = _fw_identity_regexp(repo)
            for va in to_import:
                blob = _download(_asset_url(repo, job.tag, va.signed_name))
                expected = sums.get(va.signed_name)
                if not expected or hashlib.sha256(blob).hexdigest() != expected:
                    raise ValueError(f"sha256 mismatch for {va.signed_name}")
                bundle = _download(_asset_url(repo, job.tag, va.bundle_name))
                verify_blob_identity(blob, bundle, identity)
                skey = storage.release_key(job.module_type.key, job.tag, va.variant)
                bkey = storage.bundle_key(job.module_type.key, job.tag, va.variant)
                # Optional native_sim artifact: verified EXACTLY like the
                # signed.bin (SUMS sha + cosign identity). Any failure fails
                # the whole job — an unverified sim blob is never published.
                sim = None
                sim_name = sim_assets.get(va.variant)
                if sim_name is not None:
                    sim_blob = _download(_asset_url(repo, job.tag, sim_name))
                    sim_expected = sums.get(sim_name)
                    if not sim_expected or hashlib.sha256(sim_blob).hexdigest() != sim_expected:
                        raise ValueError(f"sha256 mismatch for {sim_name}")
                    sim_bundle = _download(_asset_url(repo, job.tag, sim_name + ".bundle"))
                    verify_blob_identity(sim_blob, sim_bundle, identity)
                    sim = _StagedSim(
                        skey=storage.sim_release_key(job.module_type.key, job.tag, va.variant),
                        bkey=storage.sim_bundle_key(job.module_type.key, job.tag, va.variant),
                        size=len(sim_blob),
                        sha256=sim_expected,
                        blob=sim_blob,
                        bundle=sim_bundle,
                    )
                # Finding 5: do NOT upload here — keep bytes in memory until all
                # variants are verified.  Uploads happen in the publish phase only.
                staged.append(
                    _StagedVariant(
                        va=va,
                        skey=skey,
                        bkey=bkey,
                        size=len(blob),
                        sha256=expected,
                        url=_asset_url(repo, job.tag, va.signed_name),
                        blob=blob,
                        bundle=bundle,
                        sim=sim,
                    )
                )

        # --- Publish phase: single atomic transaction for all staged variants ---
        last_release = None
        with transaction.atomic():
            for sv in staged:
                # Finding 6: lock the row (or absence) before writing to serialise
                # concurrent imports.  If a concurrent import already published an
                # active pin for this variant, skip — no upload, no overwrite.
                existing = (
                    ModuleFirmwareRelease.all_objects.select_for_update()
                    .filter(
                        module_type=job.module_type,
                        variant=sv.va.variant,
                        version=job.tag,
                    )
                    .first()
                )
                if existing is not None and existing.archived_at is None:
                    # Concurrent import already published an active pin — skip.
                    last_release = existing
                    continue
                # Upload canonical objects inside the transaction so that any
                # subsequent failure (e.g. DB constraint) rolls back the DB row
                # while the cleanup path handles the uploaded key.
                storage.upload_bytes(sv.skey, sv.blob)
                uploaded.append(sv.skey)
                storage.upload_bytes(sv.bkey, sv.bundle)
                uploaded.append(sv.bkey)
                if sv.sim is not None:
                    storage.upload_bytes(sv.sim.skey, sv.sim.blob)
                    uploaded.append(sv.sim.skey)
                    storage.upload_bytes(sv.sim.bkey, sv.sim.bundle)
                    uploaded.append(sv.sim.bkey)
                    sim_defaults = {
                        "sim_storage_key": sv.sim.skey,
                        "sim_sha256": sv.sim.sha256,
                        "sim_size_bytes": sv.sim.size,
                        "sim_cosign_bundle_key": sv.sim.bkey,
                    }
                else:
                    # Re-import (restore of an archived row) must clear stale
                    # sim fields when the tag has no (valid) sim asset.
                    sim_defaults = {
                        "sim_storage_key": "",
                        "sim_sha256": "",
                        "sim_size_bytes": None,
                        "sim_cosign_bundle_key": "",
                    }
                last_release, _ = ModuleFirmwareRelease.all_objects.update_or_create(
                    module_type=job.module_type,
                    variant=sv.va.variant,
                    version=job.tag,
                    defaults={
                        "storage_key": sv.skey,
                        "sha256": sv.sha256,
                        "size_bytes": sv.size,
                        "cosign_bundle_key": sv.bkey,
                        "source_repo": repo,
                        "source_tag": job.tag,
                        "source_github_url": sv.url,
                        "imported_by": job.requested_by,
                        "archived_at": None,
                        **sim_defaults,
                    },
                )

            if last_release is None and active_existing:
                last_release = active_existing[-1]

            job.release = last_release
            job.status = ModuleFirmwareImportJob.Status.READY
            job.completed_at = timezone.now()
            job.save(update_fields=["release", "status", "completed_at"])
            # A new release can make a pinned target resolvable: reconcile and
            # nudge agents once the publish is committed.
            module_type = job.module_type
            transaction.on_commit(lambda: _reconcile_after_publish(module_type))
    except Exception as exc:
        # Best-effort cleanup of keys we uploaded that no active release row references.
        in_use: set[str] = set()
        for row in ModuleFirmwareRelease.all_objects.filter(
            module_type=job.module_type, version=job.tag
        ).values_list(
            "storage_key", "cosign_bundle_key", "sim_storage_key", "sim_cosign_bundle_key"
        ):
            in_use.update(k for k in row if k)
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
