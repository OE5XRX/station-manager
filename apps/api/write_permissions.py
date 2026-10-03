"""Object-level write gate for the user/automation API."""

from rest_framework.permissions import SAFE_METHODS

from apps.api.permissions import TopologyScopedPermission


class TopologyScopedWritePermission(TopologyScopedPermission):
    """Reads pass the base membership gate; writes additionally require the
    view's per-object predicate. Create-scope (no object yet) is enforced in
    ``perform_create``. ``get_queryset`` scoping still hides out-of-scope
    objects, so update/delete of an invisible object is 404, not 403.

    B5 — create-role pre-check:
    For viewsets whose create gate is PAYLOAD-INDEPENDENT (e.g. Region,
    StationTag — gated purely by role, not by a target FK in the body), the
    serializer runs BEFORE perform_create, so a non-authorised caller could
    receive uniqueness/field-validation errors and probe hidden resource names
    instead of the promised 403.

    Viewsets can declare a ``create_requires`` callable:

        create_requires = staticmethod(ws.can_write_region)

    When a POST request arrives, ``has_permission`` calls
    ``create_requires(request.user)`` and returns False (→ 403) immediately,
    before DRF ever touches the serializer.

    KEEP perform_create scope checks for TARGET-DEPENDENT gates (Station,
    assignments, log/photo, deployments, provisioning) — those need the
    validated body FK, so the 403 legitimately comes after validation.  Only
    payload-INDEPENDENT role checks belong in ``create_requires``.
    """

    def has_permission(self, request, view):
        # Base membership gate first (authenticated, not applicant, etc.)
        if not super().has_permission(request, view):
            return False
        # B5: payload-independent create-role pre-check (runs before serializer)
        if request.method == "POST":
            create_requires = getattr(view, "create_requires", None)
            if create_requires is not None:
                return bool(create_requires(request.user))
        return True

    def has_object_permission(self, request, view, obj):
        if request.method in SAFE_METHODS:
            return True
        checker = getattr(view, "can_write_object", None)
        if checker is None:
            return False
        return bool(checker(request.user, obj, request.method))
