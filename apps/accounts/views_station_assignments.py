"""HTMX views for managing StationAssignments on a target user.

Both endpoints are Vereins-Admin only.

Create path:
  POST /accounts/users/<user_pk>/station_assignments/
       body: {"station": "<pk>", "role": "admin"|"maintainer",
              "takeover": "1" (optional, admin role only)}

  Special admin-takeover logic: each station has a partial-unique
  constraint allowing at most one ADMIN-role assignment. When the
  request asks to make `target` the admin AND someone else already
  is the admin:
    - if takeover=="1" -> atomically delete the existing admin row
      then create the new one (single transaction).
    - else -> return 409 Conflict so the UI can show a confirm
      dialog ("Take over from <existing>?") then re-post with
      takeover=1.

Returns: 200 success, 400 validation (applicant target, invalid
role), 409 admin-conflict-without-takeover, 404 station not found.

Audit emission for create+delete is already wired via signals in
apps/stations/signals.py - these views just hit the ORM.
"""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404
from django.views import View

from apps.accounts.views import AdminRequiredMixin
from apps.stations.models import Station, StationAssignment

User = get_user_model()


class StationAssignmentCreateView(AdminRequiredMixin, View):
    def post(self, request, user_pk):
        target = get_object_or_404(User, pk=user_pk)
        station_pk = request.POST.get("station")
        station = get_object_or_404(Station, pk=station_pk)

        role = request.POST.get("role", "").strip()
        if role not in {"admin", "maintainer"}:
            return HttpResponseBadRequest(f"Invalid role: {role!r}")

        takeover = request.POST.get("takeover") == "1"

        if role == "admin":
            # Open the transaction BEFORE reading existing_admin so we
            # can use select_for_update to lock the admin row against
            # concurrent takeover requests. Mirrors the pattern in
            # apps/sso/views.py:GrantToggleView.
            try:
                with transaction.atomic():
                    # Pre-check: does the TARGET already have any
                    # assignment on this station? If yes, the
                    # uniq_user_per_station_assignment constraint will
                    # block the create. Reject up front (or treat as
                    # idempotent if they're already admin) rather than
                    # raising IntegrityError mid-transaction.
                    existing_target = StationAssignment.objects.filter(
                        station=station, user=target
                    ).first()
                    if existing_target:
                        if existing_target.role == StationAssignment.Role.ADMIN:
                            return JsonResponse({"success": True, "unchanged": True})
                        return HttpResponseBadRequest(
                            "Target already has another assignment on this "
                            "station. Revoke it first."
                        )

                    existing_admin = (
                        StationAssignment.objects.select_for_update()
                        .select_related("user")
                        .filter(
                            station=station,
                            role=StationAssignment.Role.ADMIN,
                        )
                        .first()
                    )
                    if existing_admin:
                        if not takeover:
                            # Release the lock (Django releases on
                            # transaction exit). 409 lets the UI confirm.
                            return JsonResponse(
                                {
                                    "error": "admin_conflict",
                                    "current_admin_username": (existing_admin.user.username),
                                    "current_admin_id": existing_admin.user_id,
                                },
                                status=409,
                            )
                        # Takeover: delete the old admin row.
                        existing_admin.delete()

                    # Whether takeover or fresh admin: create the new row.
                    StationAssignment.objects.create(
                        user=target,
                        station=station,
                        role=StationAssignment.Role.ADMIN,
                        assigned_by=request.user,
                    )
            except ValidationError as e:
                # Applicant target etc — atomic rolls back the
                # takeover delete (if it ran) before we get here.
                return HttpResponseBadRequest(str(e))
            return JsonResponse({"success": True})

        # Plain create path
        try:
            StationAssignment.objects.create(
                user=target,
                station=station,
                role=role,
                assigned_by=request.user,
            )
        except ValidationError as e:
            return HttpResponseBadRequest(str(e))
        except IntegrityError:
            return HttpResponseBadRequest("Assignment already exists.")
        return JsonResponse({"success": True})


class StationAssignmentRevokeView(AdminRequiredMixin, View):
    def post(self, request, pk):
        assignment = get_object_or_404(StationAssignment, pk=pk)
        assignment.delete()
        return JsonResponse({"success": True})
