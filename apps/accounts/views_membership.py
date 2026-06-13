"""Membership-level set (promote/demote) view.

POST /accounts/users/<pk>/membership/  data: {"level": "<value>"}

Returns 200 on success (HTMX-friendly), 400 on validation error
(invalid level, self-promote/demote, demote-to-applicant blocked by
existing assignments), 403 on permission denied.

Emits AccountAuditLog MEMBERSHIP_PROMOTED / _DEMOTED with the actor
(request.user) — this is the reason promote/demote lives in a view
and not in a model signal: signals don't know who initiated the
change. Same level → no audit entry.
"""

from django.contrib.auth import get_user_model
from django.http import (
    HttpResponseBadRequest,
    JsonResponse,
)
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext as _
from django.views import View

from apps.accounts.models import AccountAuditLog
from apps.accounts.views import AdminRequiredMixin

User = get_user_model()


def _get_client_ip(request):
    """XFF-aware client IP — matches the pattern used in apps/sso/views.py
    and apps/stations/views.py. Production runs behind cloudflared/Caddy,
    so REMOTE_ADDR is the proxy. The first hop in X-Forwarded-For is the
    operator's real address.
    """
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    return xff.split(",")[0].strip() if xff else request.META.get("REMOTE_ADDR")


# Sequential ordering of membership levels. Index defines "up" (promote)
# vs "down" (demote). Sourced from the TextChoices order in
# apps/accounts/models.py so a single edit-point governs both.
MEMBERSHIP_ORDER = [
    User.MembershipLevel.APPLICANT,
    User.MembershipLevel.MEMBER,
    User.MembershipLevel.STAFF,
    User.MembershipLevel.ADMIN,
]


class MembershipSetView(AdminRequiredMixin, View):
    def post(self, request, pk):
        # 2b: soft-deleted users are off-limits to mutation endpoints.
        # The UI disables the membership card for them, but a hand-rolled
        # POST would otherwise bypass the lifecycle invariant.
        target = get_object_or_404(User, pk=pk, deleted_at__isnull=True)
        if target.pk == request.user.pk:
            return HttpResponseBadRequest(_("Cannot change your own membership level."))

        new_level = request.POST.get("level", "").strip()
        valid_levels = {x.value for x in User.MembershipLevel}
        if new_level not in valid_levels:
            return HttpResponseBadRequest(_("Invalid level: %s") % new_level)

        old_level = target.membership_level
        if new_level == old_level:
            return JsonResponse({"success": True, "unchanged": True})

        # Demote-to-applicant block when assignments exist.
        # _ApplicantForbiddenMixin would otherwise let the demote
        # silently break the existing assignment invariant (only
        # newly-saved assignments check membership_level).
        if new_level == User.MembershipLevel.APPLICANT:
            n_station = target.station_assignments.count()
            n_region = target.region_assignments.count()
            if n_station or n_region:
                return HttpResponseBadRequest(
                    _(
                        "Cannot demote to Applicant: user has %(s)d "
                        "station + %(r)d region assignment(s). "
                        "Remove them first."
                    )
                    % {"s": n_station, "r": n_region}
                )

        target.membership_level = new_level
        target.save(update_fields=["membership_level"])
        User._invalidate_role_cache(target)

        new_index = MEMBERSHIP_ORDER.index(User.MembershipLevel(new_level))
        old_index = MEMBERSHIP_ORDER.index(User.MembershipLevel(old_level))
        is_promote = new_index > old_index
        event = (
            AccountAuditLog.EventType.MEMBERSHIP_PROMOTED
            if is_promote
            else AccountAuditLog.EventType.MEMBERSHIP_DEMOTED
        )
        AccountAuditLog.log(
            event_type=event,
            actor=request.user,
            target_user=target,
            message=f"{old_level} → {new_level}",
            ip_address=_get_client_ip(request),
        )

        return JsonResponse({"success": True})
