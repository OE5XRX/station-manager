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
        station=station, kind=kind, defaults={"channel_name": channel_name, "features": []}
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


MAX_FEATURES = 32


def set_features(station, kind, channel_name, features):
    """Store the features ``channel_name`` advertised (only while it is the current one)."""
    clean = (
        sorted({f[:32] for f in features if isinstance(f, str)})[:MAX_FEATURES]
        if isinstance(features, list)
        else []
    )
    AgentConnection.objects.filter(station=station, kind=kind, channel_name=channel_name).update(
        features=clean
    )


def has_feature(station, feature):
    """True iff the station's current control connection advertised ``feature``."""
    row = (
        AgentConnection.objects.filter(station=station, kind=AgentConnection.Kind.CONTROL)
        .values_list("features", flat=True)
        .first()
    )
    return isinstance(row, list) and feature in row
