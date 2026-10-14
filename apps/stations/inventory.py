"""Inventory visibility shared by the station detail UI and the read API."""

#: Agent-inventory keys only station managers (stations.manage) may see:
#: interface IPs and MACs.
MANAGER_ONLY_INVENTORY_KEYS = frozenset({"network"})


def inventory_for_viewer(data, can_manage: bool):
    """``data`` without manager-only keys unless ``can_manage``; never mutates it."""
    if can_manage or not isinstance(data, dict):
        return data
    return {k: v for k, v in data.items() if k not in MANAGER_ONLY_INVENTORY_KEYS}
