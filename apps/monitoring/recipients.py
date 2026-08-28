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
from django.db.models import Exists, OuterRef, Q

User = get_user_model()


def _topology_q(station):
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
    return q


def _base_topology_recipients(station):
    """Topology-routed, active, non-applicant users — WITHOUT the email
    exclusion (push recipients may legitimately have no email)."""
    return (
        User.objects.active()
        .filter(_topology_q(station))
        .exclude(is_active=False)
        .exclude(membership_level=User.MembershipLevel.APPLICANT)
        .distinct()
    )


def email_recipients_for_station_alert(station):
    """Users who should receive the alert e-mail.

    EMAIL/BOTH always; PUSH only as fallback when they have no working
    push subscription. Empty e-mails are excluded (can't mail them).
    """
    from apps.webpush.models import PushSubscription

    has_push = Exists(PushSubscription.objects.filter(user=OuterRef("pk")))
    return (
        _base_topology_recipients(station)
        .annotate(_has_push=has_push)
        .filter(
            Q(notify_channel__in=[User.NotifyChannel.EMAIL, User.NotifyChannel.BOTH])
            | Q(notify_channel=User.NotifyChannel.PUSH, _has_push=False)
        )
        .exclude(email="")
    )


def push_recipients_for_station_alert(station):
    """Users who should receive the alert as Web-Push (PUSH/BOTH with a
    registered device)."""
    return _base_topology_recipients(station).filter(
        notify_channel__in=[User.NotifyChannel.PUSH, User.NotifyChannel.BOTH],
        push_subscriptions__isnull=False,
    )


def recipients_for_station_alert(station):
    """Backward-compatible alias: the e-mail recipient set."""
    return email_recipients_for_station_alert(station)
