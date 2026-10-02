"""Central "what may user X touch" querysets.

Single source of truth for API permission filtering (and, over time, UI
scoping). Mirrors the membership + topology access model: staff/admin see
everything; members see only what their Region/Station assignments grant;
applicants and anonymous users see nothing.
"""

from apps.stations.models import Region, Station


def _is_authenticated_member(user):
    return bool(
        user and user.is_authenticated and user.membership_level != user.MembershipLevel.APPLICANT
    )


def accessible_regions(user):
    if not _is_authenticated_member(user):
        return Region.objects.none()
    if user.is_internal:
        return Region.objects.all()
    managed = user.region_assignments.filter(role="manager").values_list("region_id", flat=True)
    via_station = user.station_assignments.values_list("station__region_id", flat=True)
    region_ids = set(managed) | {r for r in via_station if r is not None}
    return Region.objects.filter(id__in=region_ids)


def accessible_stations(user):
    if not _is_authenticated_member(user):
        return Station.objects.none()
    if user.is_internal:
        return Station.objects.all()
    managed_regions = user.region_assignments.filter(role="manager").values_list(
        "region_id", flat=True
    )
    assigned = user.station_assignments.values_list("station_id", flat=True)
    return Station.objects.filter(region_id__in=managed_regions) | Station.objects.filter(
        id__in=assigned
    )
