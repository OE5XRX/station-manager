"""Resolve email recipients for a station alert.

Single-responsibility helper. Lives in a dedicated module so the
notification dispatch in apps/monitoring/notifications.py stays
focused on SMTP delivery, and the routing logic is unit-testable in
isolation from email-backend mocking.

Routing contract (see docs/superpowers/specs/
2026-06-05-membership-levels-and-topology-roles-design.md §4.7):
  - Vereins-Admins (membership_level=ADMIN), vereinsweit
  - Region-Manager der zugeh. Region (sofern station.region gesetzt)
  - Station-Admin dieser Station
  - Station-Maintainer dieser Station

Excludes Vereins-Staff (operative role, not escalation inbox),
Applicants (defense-in-depth — invariant blocks them anyway), and
inactive / no-email users.
"""

from django.contrib.auth import get_user_model
from django.db.models import Q

User = get_user_model()


def recipients_for_station_alert(station):
    q = Q(membership_level=User.MembershipLevel.ADMIN)

    if station.region_id is not None:
        q |= Q(
            region_assignments__region_id=station.region_id,
            region_assignments__role="manager",
        )

    q |= Q(
        station_assignments__station=station,
        station_assignments__role__in=["admin", "maintainer"],
    )

    return (
        User.objects.filter(q)
        .exclude(email="")
        .exclude(is_active=False)
        .exclude(membership_level=User.MembershipLevel.APPLICANT)
        .distinct()
    )
