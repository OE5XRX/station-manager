"""Unit test for the audio link-stats server payload.

The periodic push loop sleeps 2 s, so the payload construction is factored into
a pure sync method (`_link_stats_payload`) that we can assert directly without
driving the loop.
"""

from apps.audio import constants
from apps.audio.consumers import AudioConsumer


def test_link_stats_payload_reflects_relay_counters():
    c = AudioConsumer()
    # Counters are normally seeded in connect(); set them directly here.
    c._dl_frames = 7
    c._ul_frames = 3

    payload = c._link_stats_payload()

    assert payload["type"] == "link_stats"
    assert payload["v"] == constants.AUDIO_PROTOCOL_VERSION
    assert payload["server"] == {"downlink_frames": 7, "uplink_frames": 3}
