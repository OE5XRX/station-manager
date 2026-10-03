"""Tests for BridgeFactory port-managed bridge creation."""

from station_agent.audio.bridge_factory import BridgeFactory


def test_make_diag_u_acquires_and_releases_port():
    f = BridgeFactory(port_base=48000)
    br = f.make_diag_u("tx.node", 16000)
    assert br._port == 48000  # first acquired port
    # same allocator → a second acquire must not reuse 48000 until released
    br2 = f.make_diag_u("tx.node", 16000)
    assert br2._port == 48001
    br.stop()  # releases 48000
    br3 = f.make_diag_u("tx.node", 16000)
    assert br3._port == 48000  # reused after release
