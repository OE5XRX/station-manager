from __future__ import annotations

from django.db import transaction

from apps.deployments.models import Deployment, DeploymentResult


class ActiveDeploymentConflict(Exception):
    """Raised when a station has a deployment beyond PENDING, which cannot be superseded."""


def supersede_pending_for_station(
    *,
    station,
    new_deployment: Deployment,
) -> list[int]:
    """Mark any PENDING DeploymentResult for `station` (other than for
    `new_deployment`) as SUPERSEDED. Raise ActiveDeploymentConflict if a
    non-PENDING, non-terminal result exists.

    Runs in a transaction with SELECT FOR UPDATE so concurrent calls don't race.
    """
    with transaction.atomic():
        active_statuses = {
            DeploymentResult.Status.DOWNLOADING,
            DeploymentResult.Status.INSTALLING,
            DeploymentResult.Status.REBOOTING,
            DeploymentResult.Status.VERIFYING,
        }

        qs = (
            DeploymentResult.objects.select_for_update()
            .filter(station=station)
            .exclude(deployment=new_deployment)
        )

        to_supersede = []
        for r in qs:
            if r.status == DeploymentResult.Status.PENDING:
                to_supersede.append(r.pk)
            elif r.status in active_statuses:
                raise ActiveDeploymentConflict(
                    f"Station {station.pk} is mid-deployment "
                    f"({r.get_status_display()} on deployment #{r.deployment_id})"
                )

        if to_supersede:
            DeploymentResult.objects.filter(pk__in=to_supersede).update(
                status=DeploymentResult.Status.SUPERSEDED,
            )
    return to_supersede
