"""Write-permission predicates for the user/automation API (Phase 3).

Single source of truth for *who may mutate what*, built on
apps.stations.scoping (read scope). "staff"/"staff/admin" minimums map to
``user.is_internal`` (admin ⊇ staff); "region-manager / staff" to
``is_internal or is_region_manager(region)``. Applicants never reach here
(gated by TopologyScopedPermission.has_permission).
"""

from apps.stations.scoping import accessible_stations


def is_any_region_manager(user):
    return bool(user.is_internal or user.region_assignments.filter(role="manager").exists())


def can_write_station(user, station):
    return bool(user.is_internal or user.is_region_manager(station.region))


def can_delete_station(user, station):
    return bool(user.is_internal)


def can_create_station_in(user, region):
    return bool(user.is_internal or user.is_region_manager(region))


def can_write_region(user):
    return bool(user.is_internal)


def can_write_station_tag(user):
    return bool(user.is_internal)


def can_write_station_assignment(user, station):
    return bool(user.is_internal or user.is_region_manager(station.region))


def can_write_region_assignment(user):
    return bool(user.is_internal)


def can_write_station_content(user, station):
    return accessible_stations(user).filter(pk=station.pk).exists()


def can_write_rollouts(user):
    return is_any_region_manager(user)


def can_trigger_deployment(user, station):
    return bool(user.is_internal or user.is_region_manager(station.region))


def can_write_alert_rule(user):
    return is_any_region_manager(user)


def can_trigger_provisioning(user, station):
    return bool(user.is_internal)


def can_write_user(user):
    return bool(user.is_internal)


def can_manage_images(user):
    return bool(user.is_internal)
