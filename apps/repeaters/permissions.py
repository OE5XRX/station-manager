from apps.accounts.permissions import can


def can_manage_repeaters(user) -> bool:
    """``repeaters.manage`` OR Region-Manager of any region (assignment-based)."""
    if can(user, "repeaters", "manage"):
        return True
    if not getattr(user, "is_authenticated", False):
        return False
    from apps.stations.models import RegionAssignment

    return RegionAssignment.objects.filter(user=user, role=RegionAssignment.Role.MANAGER).exists()
