"""Default bridge factory — wires real Opus bridges with UDP port management.

Split out so the engine depends only on a small ``make_rx``/``make_tx`` interface and tests
inject a fake factory (no GStreamer/sockets). The default owns the :class:`PortAllocator`
so port assignment is invisible to the engine.
"""

from __future__ import annotations

from station_agent.audio.opus_bridge import (
    PortAllocator,
    RxBridge,
    TxBridge,
    probe_dsp_available,
)
from station_agent.audio.tx_dsp import TxDspConfig
from station_agent.audio.tx_settings import CEILING_DEFAULT_DBFS


class BridgeFactory:
    def __init__(self, port_base: int = 47000, tx_settings=None, dsp_probe=None):
        self._ports = PortAllocator(base=port_base)
        self._tx_settings = tx_settings
        self._dsp_probe = dsp_probe or probe_dsp_available

    def make_rx(self, node: str, rate: int, on_opus):
        port = self._ports.acquire()
        return _PortBoundRx(node, port, rate, on_opus, self._ports)

    def make_tx(self, node: str, rate: int, on_meter=None):
        port = self._ports.acquire()
        ceiling = self._tx_settings.ceiling_dbfs if self._tx_settings else CEILING_DEFAULT_DBFS
        dsp = TxDspConfig(ceiling_dbfs=ceiling, enabled=self._dsp_probe())
        return _PortBoundTx(node, port, rate, self._ports, dsp=dsp, on_meter=on_meter)

    def make_diag_u(self, node: str, rate: int):
        port = self._ports.acquire()
        return _PortBoundDiagU(node, port, rate, self._ports)


class _PortBoundRx(RxBridge):
    """RxBridge that returns its UDP port to the allocator on stop."""

    def __init__(self, node, port, rate, on_opus, ports):
        super().__init__(node, port, rate, on_opus)
        self._ports = ports
        self._port_value = port

    def stop(self) -> None:
        super().stop()
        self._ports.release(self._port_value)


class _PortBoundTx(TxBridge):
    def __init__(self, node, port, rate, ports, *, dsp=None, on_meter=None):
        super().__init__(node, port, rate, dsp=dsp, on_meter=on_meter)
        self._ports = ports
        self._port_value = port

    def stop(self) -> None:
        super().stop()
        self._ports.release(self._port_value)


class _PortBoundDiagU:
    """MeasuredTxBridge wrapper that returns its UDP port to the allocator on stop."""

    def __init__(self, node, port, rate, ports):
        from station_agent.audio.diagnostics import MeasuredTxBridge

        self._impl = MeasuredTxBridge(node, port, rate)
        self._ports = ports
        self._port = port

    def start(self):
        self._impl.start()

    def feed_opus(self, payload):
        self._impl.feed_opus(payload)

    def read_measurement(self, nbytes, timeout):
        return self._impl.read_measurement(nbytes, timeout)

    def stop(self):
        self._impl.stop()
        self._ports.release(self._port)
