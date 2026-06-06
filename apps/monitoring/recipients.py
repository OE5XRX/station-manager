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

Excludes Applicants (defense-in-depth — the _ApplicantForbiddenMixin
invariant blocks them anyway) and inactive / no-email users.

Vereins-Staff is intentionally NOT routed by `membership_level` alone
— staff is an operative role, not an escalation inbox blanket. But a
Staff (or any non-Applicant) user who additionally holds a
Station-Admin/Maintainer or Region-Manager assignment IS routed
because the four Q clauses above are OR-ed: the Q on membership_level
matches ADMIN only, and the topology Qs match assignment-holders of
any non-excluded level. To opt a Vereins-Staff into alerts, give them
a topology assignment.
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
