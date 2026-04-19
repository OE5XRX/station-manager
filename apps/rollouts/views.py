import logging

from django.contrib import messages
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.accounts.views import AdminRequiredMixin
from apps.deployments.models import Deployment, DeploymentResult
from apps.deployments.supersession import (
    ActiveDeploymentConflictError,
    supersede_pending_for_station,
)
from apps.images.models import ImageRelease
from apps.stations.models import Station, StationAuditLog, StationTag

logger = logging.getLogger(__name__)


def _best_effort_audit_log(*, station, event_type, message, user=None):
    """Audit logging must never break the real operation."""
    try:
        StationAuditLog.log(
            station=station,
            event_type=event_type,
            message=message,
            user=user,
        )
    except Exception as exc:
        logger.warning("Audit log write failed (%s): %s", event_type, exc)


def _target_release_for(station) -> ImageRelease | None:
    """Look up the latest ImageRelease for the station's machine.

    Machine is taken from station.current_image_release.machine; if the
    station has never been provisioned through our flow, return None.
    """
    current = getattr(station, "current_image_release", None)
    if current is None:
        return None
    return ImageRelease.objects.filter(machine=current.machine, is_latest=True).first()


class UpgradeStationView(AdminRequiredMixin, View):
    """Create a Deployment targeting exactly this one station."""

    def post(self, request, station_pk):
        station = get_object_or_404(Station, pk=station_pk)
        target = _target_release_for(station)
        if target is None:
            messages.error(
                request,
                _("No image release available for this station's machine."),
            )
            return redirect("stations:station_detail", pk=station.pk)

        if station.current_image_release_id == target.pk:
            messages.info(request, _("Station is already on the latest release."))
            return redirect("stations:station_detail", pk=station.pk)

        try:
            with transaction.atomic():
                dep = Deployment.objects.create(
                    image_release=target,
                    target_type=Deployment.TargetType.STATION,
                    target_station=station,
                    status=Deployment.Status.IN_PROGRESS,
                    created_by=request.user,
                )
                DeploymentResult.objects.create(
                    deployment=dep,
                    station=station,
                    status=DeploymentResult.Status.PENDING,
                    previous_version=station.current_os_version or "",
                )
                supersede_pending_for_station(station=station, new_deployment=dep)
        except ActiveDeploymentConflictError as exc:
            messages.error(request, str(exc))
            return redirect("stations:station_detail", pk=station.pk)

        _best_effort_audit_log(
            station=station,
            event_type=StationAuditLog.EventType.FIRMWARE_UPDATE,
            message=(
                f"Upgrade triggered: {station.current_os_version or '?'} "
                f"\u2192 {target.tag} (deployment #{dep.pk}) by {request.user.username}"
            ),
            user=request.user,
        )
        messages.success(request, _("Upgrade to %(tag)s queued.") % {"tag": target.tag})
        return redirect("stations:station_detail", pk=station.pk)


class UpgradeGroupView(AdminRequiredMixin, View):
    """Create Deployments for every station carrying the given tag (grouped
    by machine: one Deployment per (tag, machine) tuple).
    """

    def post(self, request, tag_name):
        tag = get_object_or_404(StationTag, name=tag_name)
        stations = list(Station.objects.filter(tags=tag).select_related("current_image_release"))
        if not stations:
            messages.info(request, _("No stations carry this tag."))
            return redirect("rollouts:upgrade_dashboard")

        # Bucket by machine.
        by_machine: dict[str, list] = {}
        for s in stations:
            if not s.current_image_release:
                continue
            by_machine.setdefault(s.current_image_release.machine, []).append(s)

        created = 0
        skipped = 0
        with transaction.atomic():
            for machine, machine_stations in by_machine.items():
                target = ImageRelease.objects.filter(machine=machine, is_latest=True).first()
                if target is None:
                    skipped += len(machine_stations)
                    continue
                dep = Deployment.objects.create(
                    image_release=target,
                    target_type=Deployment.TargetType.TAG,
                    target_tag=tag,
                    status=Deployment.Status.IN_PROGRESS,
                    created_by=request.user,
                )
                for s in machine_stations:
                    if s.current_image_release_id == target.pk:
                        skipped += 1
                        continue
                    DeploymentResult.objects.create(
                        deployment=dep,
                        station=s,
                        status=DeploymentResult.Status.PENDING,
                        previous_version=s.current_os_version or "",
                    )
                    try:
                        supersede_pending_for_station(station=s, new_deployment=dep)
                    except ActiveDeploymentConflictError:
                        # Drop this station from the deployment - it will
                        # be picked up next time.
                        DeploymentResult.objects.filter(deployment=dep, station=s).delete()
                        skipped += 1
                        continue
                    _best_effort_audit_log(
                        station=s,
                        event_type=StationAuditLog.EventType.FIRMWARE_UPDATE,
                        message=(
                            f"Upgrade triggered (group '{tag.name}'): "
                            f"{s.current_os_version or '?'} \u2192 {target.tag} "
                            f"(deployment #{dep.pk}) by {request.user.username}"
                        ),
                        user=request.user,
                    )
                    created += 1

        messages.success(
            request,
            _("Queued %(n)d upgrades (%(s)d skipped)") % {"n": created, "s": skipped},
        )
        return redirect("rollouts:upgrade_dashboard")


def _upgrade_dashboard_placeholder(request):
    """Placeholder so `reverse('rollouts:upgrade_dashboard')` resolves.

    Replaced by the real dashboard in Task 10.
    """
    return HttpResponse("")
