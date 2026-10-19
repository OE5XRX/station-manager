"""Shared module-firmware operations used by both the GUI views and the REST API.

Each op performs the mutation's side effects (audit + reconcile) exactly once,
so the GUI and the API cannot drift apart. ``via`` is appended verbatim to every
audit message an op writes (e.g. the API's token suffix); the GUI passes "".
"""

from collections import defaultdict

from django.db import transaction

from apps.images import github_releases
from apps.stations.models import StationAuditLog

from .audit import audit_config, audit_station
from .models import (
    Module,
    ModuleFirmwareImportJob,
    ModuleFirmwareRelease,
    ModuleFirmwareTarget,
    ModuleType,
)
from .releases import parse_variant_assets
from .services import reconcile_module_type
from .trust import import_blockers, is_valid_tag

ACTIVE_JOB_STATUSES = [
    ModuleFirmwareImportJob.Status.PENDING,
    ModuleFirmwareImportJob.Status.RUNNING,
]


# ---------------------------------------------------------------------------
# Read helpers (page-level, one query each)
# ---------------------------------------------------------------------------


def active_release_keys(type_ids):
    """Set of (module_type_id, variant, version) of active (non-archived)
    releases for ``type_ids`` — lets ``firmware_row`` skip a per-row EXISTS."""
    return set(
        ModuleFirmwareRelease.objects.filter(module_type_id__in=type_ids).values_list(
            "module_type_id", "variant", "version"
        )
    )


def variant_coverage_map(type_ids):
    """(module_type_id, version) -> variants with an active release ("—" for
    a blank variant), unsorted."""
    coverage = defaultdict(list)
    for type_id, version, variant in ModuleFirmwareRelease.objects.filter(
        module_type_id__in=type_ids
    ).values_list("module_type_id", "version", "variant"):
        coverage[(type_id, version)].append(variant or "—")
    return coverage


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------


def target_details(target):
    """Audit payload describing a target (slugs/names, not pks)."""
    return {
        "module_type": target.module_type.key,
        "scope": target.scope,
        "version": target.version,
        "group": target.group.slug if target.group_id else None,
        "station": target.station.name if target.station_id else None,
        "canary_group": target.canary_group.slug if target.canary_group_id else None,
    }


def target_label(target):
    if target.scope == ModuleFirmwareTarget.Scope.GROUP:
        return f"{target.module_type.key} group {target.group.slug}"
    if target.scope == ModuleFirmwareTarget.Scope.STATION:
        return f"{target.module_type.key} station {target.station.name}"
    return f"{target.module_type.key} fleet"


def _audit_station_scope(request, target, event_type, message):
    if target.scope == ModuleFirmwareTarget.Scope.STATION and target.station_id:
        audit_station(
            request,
            event_type=event_type,
            station=target.station,
            message=message,
            changes={"module_type": target.module_type.key, "version": target.version},
        )


def after_target_created(request, target, *, via=""):
    audit_config(
        request,
        obj="module_firmware_target",
        action="created",
        message=f"Firmware target {target_label(target)} = {target.version} created{via}",
        details=target_details(target),
    )
    _audit_station_scope(
        request,
        target,
        StationAuditLog.EventType.FIRMWARE_TARGET_SET,
        f"Firmware target {target.module_type.key} = {target.version} set for station.{via}",
    )
    reconcile_module_type(target.module_type)


def after_target_updated(request, before, target, *, via=""):
    """Audit + reconcile an update. Returns False (and does nothing) on a no-op."""
    old = target_details(before)
    new = target_details(target)
    changes = {k: {"old": old[k], "new": new[k]} for k in new if old[k] != new[k]}
    if not changes:
        return False
    audit_config(
        request,
        obj="module_firmware_target",
        action="updated",
        message=f"Firmware target {target_label(target)} = {target.version} updated{via}",
        details={**new, "changes": changes},
    )
    _audit_station_scope(
        request,
        target,
        StationAuditLog.EventType.FIRMWARE_TARGET_SET,
        f"Firmware target {target.module_type.key} = {target.version} set for station.{via}",
    )
    if before.station_id and before.station_id != target.station_id:
        # Moved off a station (other station or other scope): that station
        # lost its override.
        _audit_station_scope(
            request,
            before,
            StationAuditLog.EventType.FIRMWARE_TARGET_REMOVED,
            f"Firmware target {before.module_type.key} = {before.version} removed "
            f"for station.{via}",
        )
    reconcile_module_type(target.module_type)
    if before.module_type_id != target.module_type_id:
        reconcile_module_type(before.module_type)
    return True


def after_target_deleted(request, target, details, label, *, via=""):
    """Audit + reconcile after ``target.delete()``; ``details``/``label`` are
    captured before the delete."""
    audit_config(
        request,
        obj="module_firmware_target",
        action="deleted",
        message=f"Firmware target {label} = {details['version']} deleted{via}",
        details=details,
    )
    _audit_station_scope(
        request,
        target,
        StationAuditLog.EventType.FIRMWARE_TARGET_REMOVED,
        f"Firmware target {target.module_type.key} = {target.version} removed for station.{via}",
    )
    reconcile_module_type(target.module_type)


# ---------------------------------------------------------------------------
# Releases
# ---------------------------------------------------------------------------


def _audit_release(request, release, action, via):
    audit_config(
        request,
        obj="module_firmware_release",
        action=action,
        message=f"Firmware release {action}: {release}{via}",
        details={
            "module_type": release.module_type.key,
            "variant": release.variant,
            "version": release.version,
        },
    )


def archive_release(request, release, *, via=""):
    """Archive ``release``; audit + reconcile only if this call changed it."""
    if not release.archive():
        return False
    _audit_release(request, release, "archived", via)
    reconcile_module_type(release.module_type)
    return True


def restore_release(request, release, *, via=""):
    """Restore ``release``; audit + reconcile only if this call changed it."""
    if not release.restore():
        return False
    _audit_release(request, release, "restored", via)
    reconcile_module_type(release.module_type)
    return True


# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------


class ImportRejected(Exception):  # noqa: N818 — name fixed by the API contract
    """A firmware import request that can never succeed.

    ``field`` names the offending input (``tag`` / ``module_type``), ``code`` is
    ``invalid_tag`` or ``not_ready``. ``module_type`` is set for ``not_ready``.
    """

    def __init__(self, message, *, field, code, module_type=None):
        super().__init__(message)
        self.field = field
        self.code = code
        self.module_type = module_type


def queue_import(request, module_type_id, tag, *, via=""):
    """Queue an import job for ``tag`` of the given module type.

    Returns ``(job, True)`` when a job was created (and audited), or
    ``(existing_active_job, False)`` when the same tag is already in flight.
    Raises ``ImportRejected`` for an invalid tag / not-import-ready type and
    ``ModuleType.DoesNotExist`` for an unknown type.
    """
    if not is_valid_tag(tag):
        raise ImportRejected("Invalid release tag.", field="tag", code="invalid_tag")
    # Resolve + lock the module type row INSIDE the atomic block to serialise
    # concurrent requests for the same type on Postgres. SQLite silently
    # ignores select_for_update but stays green.
    with transaction.atomic():
        module_type = ModuleType.objects.select_for_update().get(pk=module_type_id)
        # Reject not-import-ready types (missing repo/prefix, untrusted owner)
        # — a doomed job can never succeed and just adds noise.
        blockers = import_blockers(module_type)
        if blockers:
            raise ImportRejected(
                ", ".join(str(b) for b in blockers),
                field="module_type",
                code="not_ready",
                module_type=module_type,
            )
        # Dedup: only prevent duplicate in-flight work. The importer is
        # per-variant idempotent (skips active variants, restores archived), so
        # we do NOT block on active-release existence — a tag with some variants
        # active and others archived/missing must be re-importable.
        existing = (
            ModuleFirmwareImportJob.objects.filter(
                module_type=module_type, tag=tag, status__in=ACTIVE_JOB_STATUSES
            )
            .order_by("-pk")
            .first()
        )
        if existing is not None:
            return existing, False
        job = ModuleFirmwareImportJob.objects.create(
            module_type=module_type,
            source_repo=module_type.firmware_repo,
            tag=tag,
            status=ModuleFirmwareImportJob.Status.PENDING,
            requested_by=request.user,
        )
    audit_config(
        request,
        obj="module_firmware_import",
        action="queued",
        message=f"Firmware import queued: {module_type.key} {tag}{via}",
        details={"module_type": module_type.key, "tag": tag},
    )
    return job, True


def github_release_rows(module_type):
    """GitHub releases of ``module_type``'s firmware repo, annotated with their
    import state. Caller checks ``import_blockers`` first.

    Raises ``github_releases.GitHubAPIError``.
    """
    releases = github_releases.fetch_releases(module_type.firmware_repo)

    in_flight_tags = set(
        ModuleFirmwareImportJob.objects.filter(
            module_type=module_type, status__in=ACTIVE_JOB_STATUSES
        ).values_list("tag", flat=True)
    )
    # Variants actually present in the fleet for this type — a release lacking
    # one of them would leave those modules without a candidate.
    fleet_variants = set(
        Module.objects.filter(module_type=module_type)
        .exclude(variant="")
        .values_list("variant", flat=True)
    )
    existing = set(
        ModuleFirmwareRelease.objects.filter(module_type=module_type).values_list(
            "source_tag", "variant"
        )
    )

    rows = []
    for rel in releases:
        parsed_variants = parse_variant_assets(
            set(rel.asset_names), module_type.release_asset_prefix
        )
        variants = [va.variant for va in parsed_variants]
        # invalid_tag first: queue_import refuses such a tag, so it must never
        # get an Import button. Then no_assets: a release with no matching asset
        # pair can never import, whatever job history it has. A tag is only
        # "imported" once EVERY variant it offers has an active row; a partial
        # import stays importable so the operator can fill the gap.
        if not is_valid_tag(rel.tag):
            state = "invalid_tag"
        elif not parsed_variants:
            state = "no_assets"
        elif all((rel.tag, v) in existing for v in variants):
            state = "imported"
        elif rel.tag in in_flight_tags:
            state = "queued"
        else:
            state = "ready"
        rows.append(
            {
                "release": rel,
                "state": state,
                "variants": variants,
                "missing_variants": sorted(fleet_variants - set(variants)),
            }
        )
    return rows
