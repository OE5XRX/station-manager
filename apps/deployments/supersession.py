from __future__ import annotations

from django.db import transaction

from apps.deployments.models import Deployment, DeploymentResult


class ActiveDeploymentConflictError(Exception):
    """Raised when a station has a deployment beyond PENDING, which cannot be superseded."""


def supersede_pending_for_station(
    *,
    station,
    new_deployment: Deployment,
) -> list[int]:
    """Mark any PENDING DeploymentResult for `station` (other than for
    `new_deployment`) as SUPERSEDED. Raise ActiveDeploymentConflictError if a
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
        # Only lock rows that could possibly matter: PENDING (will be
        # superseded) or active (will raise). Terminal statuses (SUCCESS,
        # FAILED, CANCELLED, ROLLED_BACK, SUPERSEDED) are irrelevant and
        # don't need to sit under SELECT FOR UPDATE — a station with a
        # year of deployment history otherwise pays for every one of
        # them on every new deployment.
        relevant_statuses = active_statuses | {DeploymentResult.Status.PENDING}

        qs = (
            DeploymentResult.objects.select_for_update()
            .filter(station=station, status__in=relevant_statuses)
            .exclude(deployment=new_deployment)
        )

        to_supersede = []
        for r in qs:
            if r.status == DeploymentResult.Status.PENDING:
                to_supersede.append(r.pk)
            else:
                # status is in active_statuses by construction of the filter
                raise ActiveDeploymentConflictError(
                    f"Station {station.pk} is mid-deployment "
                    f"({r.get_status_display()} on deployment #{r.deployment_id})"
                )

        if to_supersede:
            DeploymentResult.objects.filter(pk__in=to_supersede).update(
                status=DeploymentResult.Status.SUPERSEDED,
            )
    return to_supersede
