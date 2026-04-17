from rest_framework.permissions import BasePermission

from apps.api.models import DeviceKey


class IsDevice(BasePermission):
    """Allow access only to authenticated station agent devices."""

    def has_permission(self, request, view):
        return isinstance(request.auth, DeviceKey)
