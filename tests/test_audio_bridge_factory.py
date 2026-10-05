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


class _FakeSock:
    def sendto(self, *a):
        pass

    def close(self):
        pass


def test_make_diag_u_uses_tx_dsp_and_calibrated_ceiling():
    """RF safety: the diag measurement must run the SAME DSP/limiter as TX, at the
    calibrated (heartbeat) ceiling — not the default and not without DSP."""
    from station_agent.audio import tx_dsp
    from station_agent.audio.tx_settings import CEILING_DEFAULT_DBFS, TxAudioSettings

    assert CEILING_DEFAULT_DBFS != -20.0  # otherwise the test cannot tell them apart
    s = TxAudioSettings()
    s.update_from_heartbeat({"tx_audio": {"ceiling_dbfs": -20.0}})
    probed = []
    f = BridgeFactory(port_base=48100, tx_settings=s, dsp_probe=lambda: probed.append(1) or True)
    br = f.make_diag_u("tx.node", 16000)
    assert probed == []  # make_diag_u runs on the WS loop: no probe here
    dsp = br._impl._dsp
    assert dsp is not None and dsp.enabled
    assert dsp.ceiling_dbfs == -20.0

    calls = []

    def fake_spawn(make_argv):
        calls.append(make_argv(9))
        return object(), 9

    br._impl._spawn = fake_spawn
    br._impl._socket_factory = _FakeSock
    br.start()
    assert probed == [1]  # availability probed in start() (worker thread)
    argv = calls[0]
    want = f"threshold={tx_dsp.TxDspConfig(ceiling_dbfs=-20.0).limiter_threshold:.6f}"
    assert "audiodynamic" in argv
    assert want in argv
    default = (
        f"threshold={tx_dsp.TxDspConfig(ceiling_dbfs=CEILING_DEFAULT_DBFS).limiter_threshold:.6f}"
    )
    assert default not in argv
