"""Object-level write gate for the user/automation API."""

from rest_framework.permissions import SAFE_METHODS

from apps.api.permissions import TopologyScopedPermission


class TopologyScopedWritePermission(TopologyScopedPermission):
    """Reads pass the base membership gate; writes additionally require the
    view's per-object predicate. Create-scope (no object yet) is enforced in
    ``perform_create``. ``get_queryset`` scoping still hides out-of-scope
    objects, so update/delete of an invisible object is 404, not 403.
    """

    def has_object_permission(self, request, view, obj):
        if request.method in SAFE_METHODS:
            return True
        checker = getattr(view, "can_write_object", None)
        if checker is None:
            return False
        return bool(checker(request.user, obj, request.method))
