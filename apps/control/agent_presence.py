"""Which agent WebSocket is the current one per (station, kind).

See :class:`apps.control.models.AgentConnection`. Sync helpers — call them from
consumers via ``database_sync_to_async``.
"""

from .models import AgentConnection


def claim(station, kind, channel_name):
    """Make ``channel_name`` the current agent connection (newest connect wins).

    Returns True if it superseded a still-registered connection, i.e. the agent
    reconnected before the server noticed the old socket died.
    """
    previous = (
        AgentConnection.objects.filter(station=station, kind=kind)
        .values_list("channel_name", flat=True)
        .first()
    )
    AgentConnection.objects.update_or_create(
        station=station, kind=kind, defaults={"channel_name": channel_name}
    )
    return previous is not None and previous != channel_name


def release(station, kind, channel_name):
    """Atomically drop the row iff ``channel_name`` is still current.

    True → this was the live connection, the caller must run its teardown.
    False → a newer connection superseded it (stale disconnect), leave state alone.
    """
    deleted, _ = AgentConnection.objects.filter(
        station=station, kind=kind, channel_name=channel_name
    ).delete()
    return deleted > 0
