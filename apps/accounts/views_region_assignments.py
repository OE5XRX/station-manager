"""HTMX views for managing RegionAssignments on a target user.

Both endpoints are Vereins-Admin only. Audit-log emission for
create+delete is already wired via signal handlers in
apps/stations/signals.py — these views just create or delete the
ORM row and let the signal fire.

Create path:
  POST /accounts/users/<user_pk>/region_assignments/
       body: {"region": "<region_pk>"}
  Returns 200 on success, 400 on ValidationError (e.g., target is
  APPLICANT or duplicate assignment), 404 if region not found.

Revoke path:
  POST /accounts/region_assignments/<pk>/revoke/
  Returns 200 on success, 404 if assignment not found.
"""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404
from django.views import View

from apps.accounts.views import AdminRequiredMixin
from apps.stations.models import Region, RegionAssignment

User = get_user_model()


class RegionAssignmentCreateView(AdminRequiredMixin, View):
    def post(self, request, user_pk):
        target = get_object_or_404(User, pk=user_pk)
        region_pk = request.POST.get("region")
        region = get_object_or_404(Region, pk=region_pk)
        try:
            RegionAssignment.objects.create(
                user=target,
                region=region,
                role=RegionAssignment.Role.MANAGER,
                assigned_by=request.user,
            )
        except ValidationError as e:
            # _ApplicantForbiddenMixin raises on save()
            return HttpResponseBadRequest(str(e))
        except IntegrityError:
            # uniq_user_role_per_region constraint
            return HttpResponseBadRequest("Assignment already exists.")
        return JsonResponse({"success": True})


class RegionAssignmentRevokeView(AdminRequiredMixin, View):
    def post(self, request, pk):
        assignment = get_object_or_404(RegionAssignment, pk=pk)
        assignment.delete()
        return JsonResponse({"success": True})
