"""Version-based deployment creation.

The operator picks a *version* — ``(tag, channel)`` — not a machine-specific
``ImageRelease``. Every target station gets the release of that version for
its own machine (``current_image_release.machine``); stations that cannot be
served are skipped with a reason, never silently. All deployment creators
(UI form, upgrade dashboard, user API) go through :func:`create_deployment`
so the outcome is identical everywhere and equals the preview
(:func:`plan_deployment`).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from django.db import transaction
from django.utils.translation import gettext as _

from apps.deployments.models import Deployment, DeploymentResult
from apps.deployments.supersession import (
    ActiveDeploymentConflictError,
    supersede_pending_for_station,
)
from apps.images.models import ImageRelease
from apps.rollouts.targeting import is_newer, release_key, station_channel, station_platform
from apps.stations.models import Station, StationAuditLog

logger = logging.getLogger(__name__)

PLAN_OK = "ok"
SKIP_NO_PLATFORM = "no_platform"
SKIP_NO_IMAGE = "no_image"
SKIP_NOT_OTA_READY = "not_ota_ready"
SKIP_ACTIVE = "active_deployment"


@dataclass(frozen=True)
class StationPlan:
    """What a deployment of a version would do for one station."""

    station: Station
    release: ImageRelease | None
    reason: str
    message: str = ""
    # Informational (preview): the version's channel differs from the
    # station's rollout-group channel / the version is not newer than what
    # the station runs.
    channel_switch: bool = False
    downgrade: bool = False

    @property
    def ok(self) -> bool:
        return self.reason == PLAN_OK


class NoEligibleStationsError(Exception):
    """No target station can be served; nothing was created."""

    def __init__(self, plans: list[StationPlan]):
        self.plans = plans
        super().__init__(
            "; ".join(f"{p.station.name}: {p.message}" for p in plans if not p.ok)
            or _("No target stations.")
        )


def _skip(station, reason, message, **flags) -> StationPlan:
    return StationPlan(station=station, release=None, reason=reason, message=message, **flags)


def plan_deployment(tag: str, channel: str, stations: Iterable[Station]) -> list[StationPlan]:
    """Resolve ``(tag, channel)`` per station. One query for the releases.

    Stations should come with ``current_image_release`` and ``rollout_group``
    selected. Archived releases are not deployable (default manager).
    """
    stations = list(stations)
    releases = {r.machine: r for r in ImageRelease.objects.filter(tag=tag, channel=channel)}
    plans = []
    for station in stations:
        machine = station_platform(station)
        if machine is None:
            plans.append(
                _skip(
                    station,
                    SKIP_NO_PLATFORM,
                    _("Platform unknown — station was not provisioned via station-manager."),
                )
            )
            continue
        channel_switch = station_channel(station) != channel
        release = releases.get(machine)
        if release is None:
            plans.append(
                _skip(
                    station,
                    SKIP_NO_IMAGE,
                    _("Version %(tag)s (%(channel)s) is not imported for machine %(machine)s.")
                    % {"tag": tag, "channel": channel, "machine": machine},
                    channel_switch=channel_switch,
                )
            )
            continue
        current = station.current_image_release
        downgrade = release.pk != current.pk and not is_newer(release, current)
        if not release.is_ota_ready:
            plans.append(
                StationPlan(
                    station=station,
                    release=release,
                    reason=SKIP_NOT_OTA_READY,
                    message=_("Release %(tag)s is not OTA-ready (rootfs artifact missing).")
                    % {"tag": release.tag},
                    channel_switch=channel_switch,
                    downgrade=downgrade,
                )
            )
            continue
        plans.append(
            StationPlan(
                station=station,
                release=release,
                reason=PLAN_OK,
                channel_switch=channel_switch,
                downgrade=downgrade,
            )
        )
    return plans


def available_versions() -> list[tuple[str, str, list[str]]]:
    """Deployable versions: ``[(channel, tag, [machines…]), …]``.

    Only OTA-ready, non-archived releases. Ordered by channel, then newest
    tag first.
    """
    by_version: dict[tuple[str, str], set[str]] = {}
    rows = (
        ImageRelease.objects.exclude(rootfs_s3_key="")
        .exclude(rootfs_sha256="")
        .filter(rootfs_size_bytes__gt=0)
        .values_list("channel", "tag", "machine")
    )
    for channel, tag, machine in rows:
        by_version.setdefault((channel, tag), set()).add(machine)
    # Newest tag first (release_key desc, tag string as tie-break), then a
    # stable sort by channel keeps that order within each channel.
    ordered = sorted(by_version, key=lambda ct: (release_key(ct[1]), ct[1]), reverse=True)
    ordered.sort(key=lambda ct: ct[0])
    return [(channel, tag, sorted(by_version[(channel, tag)])) for channel, tag in ordered]


def target_stations(*, target_type, target_group=None, target_station=None):
    """Stations a deployment with this target would cover (unsaved helper)."""
    return Deployment(
        target_type=target_type,
        target_group=target_group,
        target_station=target_station,
    ).get_target_stations()


def defer_audit_log(*, station, event_type, message, user=None, ip_address=None):
    """Queue a best-effort audit log to run after the current transaction commits.

    Inside a transaction.atomic() block, catching a DatabaseError from an
    INSERT puts the whole transaction into a rollback-only state, which
    then poisons every subsequent query in that block. Queuing the write
    with transaction.on_commit() sidesteps that entirely: the callback
    runs only after the enclosing atomic block has already committed, so
    any failure during the audit INSERT can't mark that block rollback-
    only or abort the real operation.
    """
    station_pk = station.pk
    # Resolve SimpleLazyObject (request.user) to a concrete User *now*,
    # so each on_commit callback doesn't do a separate SELECT for the
    # actor — in batch flows this was an extra query per station.
    # Touching .pk materializes the lazy wrapper; ._wrapped is then the
    # real User instance.
    resolved_user = None
    if user is not None and getattr(user, "pk", None):
        resolved_user = getattr(user, "_wrapped", user) or user

    def _write() -> None:
        try:
            # station_id directly: saves a query AND avoids a ValueError
            # if the station row was deleted between post and commit.
            StationAuditLog.log(
                station_id=station_pk,
                event_type=event_type,
                message=message,
                user=resolved_user,
                ip_address=ip_address,
            )
        except Exception as exc:
            logger.warning("Deferred audit log failed (%s): %s", event_type, exc)

    transaction.on_commit(_write)


def _default_audit_message(station, deployment, release) -> str:
    return f"Deployment #{deployment.pk} created: {release} targeting {station.name}."


def create_deployment(
    *,
    tag: str,
    channel: str,
    target_type: str,
    target_group=None,
    target_station=None,
    stations: Iterable[Station] | None = None,
    user,
    strategy: str = Deployment.Strategy.IMMEDIATE,
    phase_config: dict | None = None,
    audit: bool = True,
    audit_message: Callable[[Station, Deployment, ImageRelease], str] | None = None,
    ip_address: str | None = None,
) -> Deployment:
    """Create a deployment of version ``(tag, channel)``.

    ``stations`` defaults to the target's stations
    (:meth:`Deployment.get_target_stations` semantics); pass it explicitly to
    restrict a deployment to a subset (group upgrade buckets). One result per
    servable station, pinned to that station's machine release; the rest is
    recorded in ``skipped_stations``. Pending results of other deployments
    are superseded; a station mid-flight elsewhere is skipped as
    ``active_deployment``.

    Raises :class:`NoEligibleStationsError` (nothing written) if no station
    can be served. ``audit`` writes one deferred FIRMWARE_UPDATE audit entry
    per station that got a result; ``audit_message(station, deployment,
    release)`` overrides its text.
    """
    if stations is None:
        stations = target_stations(
            target_type=target_type, target_group=target_group, target_station=target_station
        )
    if hasattr(stations, "select_related"):
        stations = stations.select_related("current_image_release", "rollout_group")
    plans = plan_deployment(tag, channel, stations)
    if not any(p.ok for p in plans):
        raise NoEligibleStationsError(plans)

    message_for = audit_message or _default_audit_message
    final_plans: list[StationPlan] = []
    with transaction.atomic():
        # Acquire the station row locks BEFORE any FK-bearing inserts. On
        # PostgreSQL, inserting a Deployment/DeploymentResult with a station
        # FK takes an implicit key-share lock on the Station row; two
        # concurrent creators that each INSERT first and then call
        # supersede_pending_for_station() (UPDATE lock) deadlock upgrading
        # key-share → update. Locking up front (in pk order, so two
        # multi-station creators can't lock-order deadlock either) makes the
        # second caller wait until the first commits.
        ok_pks = sorted(p.station.pk for p in plans if p.ok)
        list(Station.objects.select_for_update().filter(pk__in=ok_pks).order_by("pk"))

        deployment = Deployment.objects.create(
            image_tag=tag,
            image_channel=channel,
            target_type=target_type,
            target_group=target_group,
            target_station=target_station,
            strategy=strategy,
            phase_config=phase_config or {},
            status=Deployment.Status.IN_PROGRESS,
            created_by=user if getattr(user, "pk", None) else None,
        )
        for plan in plans:
            if not plan.ok:
                final_plans.append(plan)
                continue
            station = plan.station
            result = DeploymentResult.objects.create(
                deployment=deployment,
                station=station,
                image_release=plan.release,
                status=DeploymentResult.Status.PENDING,
                previous_version=station.current_os_version or "",
            )
            try:
                supersede_pending_for_station(station=station, new_deployment=deployment)
            except ActiveDeploymentConflictError as exc:
                result.delete()
                final_plans.append(
                    StationPlan(
                        station=station,
                        release=plan.release,
                        reason=SKIP_ACTIVE,
                        message=str(exc),
                        channel_switch=plan.channel_switch,
                        downgrade=plan.downgrade,
                    )
                )
                continue
            final_plans.append(plan)
            if audit:
                defer_audit_log(
                    station=station,
                    event_type=StationAuditLog.EventType.FIRMWARE_UPDATE,
                    message=message_for(station, deployment, plan.release),
                    user=user,
                    ip_address=ip_address,
                )

        if not any(p.ok for p in final_plans):
            # Rolls back the deployment and drops the queued audit callbacks.
            raise NoEligibleStationsError(final_plans)

        deployment.skipped_stations = [
            {
                "station_id": p.station.pk,
                "station_name": p.station.name,
                "reason": p.reason,
                "message": p.message,
            }
            for p in final_plans
            if not p.ok
        ]
        if deployment.skipped_stations:
            deployment.save(update_fields=["skipped_stations", "updated_at"])
    deployment.plans = final_plans
    return deployment
