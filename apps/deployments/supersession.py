from __future__ import annotations

from django.db import transaction
from django.utils import timezone

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
            now = timezone.now()
            DeploymentResult.objects.filter(pk__in=to_supersede).update(
                status=DeploymentResult.Status.SUPERSEDED,
                completed_at=now,
            )
            # The Deployments that owned those results may now have no
            # non-terminal children left — if so, flip them out of
            # IN_PROGRESS so the dashboard doesn't show them as live
            # forever.
            _close_out_deployments_with_superseded_results(to_supersede)
    return to_supersede


def _close_out_deployments_with_superseded_results(result_pks: list[int]) -> None:
    """Flip Deployments whose results are now all terminal out of IN_PROGRESS.

    A superseded deployment's results are terminal by definition; if that
    was the last non-terminal child, the parent Deployment stays stuck
    at IN_PROGRESS forever unless we nudge it here.
    """
    terminal = {
        DeploymentResult.Status.SUCCESS,
        DeploymentResult.Status.FAILED,
        DeploymentResult.Status.ROLLED_BACK,
        DeploymentResult.Status.CANCELLED,
        DeploymentResult.Status.SUPERSEDED,
    }
    deployment_ids = set(
        DeploymentResult.objects.filter(pk__in=result_pks).values_list("deployment_id", flat=True)
    )
    for dep_id in deployment_ids:
        dep = Deployment.objects.filter(pk=dep_id, status=Deployment.Status.IN_PROGRESS).first()
        if dep is None:
            continue
        statuses = set(dep.results.values_list("status", flat=True))
        if statuses and statuses.issubset(terminal):
            # Every child is terminal — mark the parent as completed
            # (or cancelled if nothing actually succeeded). We pick
            # CANCELLED when the superseded set dominates so the UI
            # labels it honestly as "was displaced".
            if DeploymentResult.Status.SUCCESS in statuses:
                dep.status = Deployment.Status.COMPLETED
            else:
                dep.status = Deployment.Status.CANCELLED
            dep.save(update_fields=["status"])
