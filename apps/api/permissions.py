from rest_framework.permissions import BasePermission

from apps.accounts.models import User as _User
from apps.api.models import DeviceKey


class IsDevice(BasePermission):
    """Allow access only to authenticated station agent devices."""

    def has_permission(self, request, view):
        return isinstance(request.auth, DeviceKey)


class TopologyScopedPermission(BasePermission):
    """Gate the user/automation API: authenticated, real user, not applicant.

    Object-level scope is enforced by each view's ``get_queryset()`` (out-of-
    scope objects are absent → detail 404), so ``has_object_permission``
    trusts the queryset. Applicants mirror ``_ApplicantForbiddenMixin`` and
    get nothing; a DeviceKey principal (no ``membership_level``) is rejected.
    """

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        if not (user and getattr(user, "is_authenticated", False)):
            return False
        level = getattr(user, "membership_level", None)
        if level is None:  # e.g. DeviceKey principal
            return False
        return level != _User.MembershipLevel.APPLICANT

    def has_object_permission(self, request, view, obj):
        return True
