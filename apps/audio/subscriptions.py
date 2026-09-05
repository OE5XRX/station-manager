"""Pure, synchronous demand-counting ops for AudioSubscription rows.

Callers (consumers) wrap these in database_sync_to_async.  All counts go
through the DB so they are worker-safe across multiple Channels workers,
mirroring the TerminalSession keepalive pattern.
"""

from django.db import IntegrityError, transaction

from .models import AudioSubscription


@transaction.atomic
def subscribe(station, stream_id: str, channel_name: str) -> dict:
    """Add a demand row for (station, stream_id, channel_name).

    Returns {"first": bool, "count": int} where first=True iff the count for
    (station, stream_id) went 0 -> 1 (i.e. this is the first subscriber and
    the agent should be told to start producing the source).
    """
    before = AudioSubscription.objects.filter(station=station, stream_id=stream_id).count()
    try:
        AudioSubscription.objects.create(
            station=station, stream_id=stream_id, channel_name=channel_name
        )
    except IntegrityError:
        # Already exists (idempotent re-subscribe).
        after = before
    else:
        after = before + 1
    return {"first": before == 0 and after == 1, "count": after}


@transaction.atomic
def unsubscribe(station, stream_id: str, channel_name: str) -> dict:
    """Remove a demand row for (station, stream_id, channel_name).

    Returns {"last": bool, "count": int} where last=True iff the count went
    1 -> 0 (agent should be told to stop producing the source).
    """
    AudioSubscription.objects.filter(
        station=station, stream_id=stream_id, channel_name=channel_name
    ).delete()
    after = AudioSubscription.objects.filter(station=station, stream_id=stream_id).count()
    return {"last": after == 0, "count": after}


@transaction.atomic
def drop_channel(station, channel_name: str) -> list[str]:
    """Remove ALL demand rows for channel_name on a station.

    Returns list of stream_ids that hit zero after removal (i.e. the agent
    should receive source_unsubscribe for each of them).  Called on browser
    disconnect.
    """
    # Collect stream_ids that have rows for this channel.
    stream_ids = list(
        AudioSubscription.objects.filter(
            station=station, channel_name=channel_name
        ).values_list("stream_id", flat=True).distinct()
    )
    # Delete the rows.
    AudioSubscription.objects.filter(station=station, channel_name=channel_name).delete()
    # Now check which streams hit zero.
    zero_streams = []
    for sid in stream_ids:
        remaining = AudioSubscription.objects.filter(station=station, stream_id=sid).count()
        if remaining == 0:
            zero_streams.append(sid)
    return zero_streams


def count(station, stream_id: str) -> int:
    """Current demand count for (station, stream_id)."""
    return AudioSubscription.objects.filter(station=station, stream_id=stream_id).count()
