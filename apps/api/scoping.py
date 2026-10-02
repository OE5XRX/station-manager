"""Cross-app scope querysets for the user/automation API.

Built on apps.stations.scoping (single source of truth). Imports models at
call time to avoid import cycles (stations must not depend on these apps).
"""

from apps.stations.scoping import accessible_stations


def accessible_deployments(user):
    from django.db.models import Q

    from apps.deployments.models import Deployment

    if getattr(user, "is_internal", False):
        return Deployment.objects.all()
    stations = accessible_stations(user)
    return Deployment.objects.filter(
        Q(target_station__in=stations) | Q(results__station__in=stations)
    ).distinct()


def accessible_deployment_results(user):
    from apps.deployments.models import DeploymentResult

    if getattr(user, "is_internal", False):
        return DeploymentResult.objects.all()
    return DeploymentResult.objects.filter(station__in=accessible_stations(user))


def accessible_alerts(user):
    from apps.monitoring.models import Alert

    if getattr(user, "is_internal", False):
        return Alert.objects.all()
    return Alert.objects.filter(station__in=accessible_stations(user))


def accessible_provisioning_jobs(user):
    from apps.provisioning.models import ProvisioningJob

    if getattr(user, "is_internal", False):
        return ProvisioningJob.objects.all()
    return ProvisioningJob.objects.filter(station__in=accessible_stations(user))
