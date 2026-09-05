"""Audio-relay constants and group-name helpers (Spec 0 §5)."""

from django.conf import settings

AUDIO_PROTOCOL_VERSION = 1

# Dead-man TTL for PTT (seconds). Must exceed the 1 s control keepalive cadence.
# Default 3.0 s gives 2 missed keepalives before the gate closes.
AUDIO_PTT_TTL: float = getattr(settings, "AUDIO_PTT_TTL_SECONDS", 3.0)


# -- Group-name helpers -------------------------------------------------------
# All helpers take a station primary-key (int or str) and return a str
# channel-layer group name.  Group names must not contain spaces.


def agent_group(station_id) -> str:
    """The agent connection group for a station."""
    return f"audio_{station_id}_agent"


def browser_group(station_id) -> str:
    """All browser connections for a station (gate/stream-state broadcasts)."""
    return f"audio_{station_id}"


def src_group(station_id, stream_id: str) -> str:
    """Per-source fan-out group identified by stream_id string.

    Design note: we use stream_id (string, e.g. 'slot0.rx') rather than the
    numeric stream_ref for group names because stream_id is stable and browsers
    reference streams by id.  The agent reverse-maps its numeric stream_ref
    back to stream_id via self.stream_refs before fanning out.
    """
    return f"audio_{station_id}_src_{stream_id}"
