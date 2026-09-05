"""Default bridge factory — wires real Opus bridges with UDP port management.

Split out so the engine depends only on a small ``make_rx``/``make_tx`` interface and tests
inject a fake factory (no GStreamer/sockets). The default owns the :class:`PortAllocator`
so port assignment is invisible to the engine.
"""

from __future__ import annotations

from station_agent.audio.opus_bridge import PortAllocator, RxBridge, TxBridge


class BridgeFactory:
    def __init__(self, port_base: int = 47000):
        self._ports = PortAllocator(base=port_base)

    def make_rx(self, node: str, rate: int, on_opus):
        port = self._ports.acquire()
        return _PortBoundRx(node, port, rate, on_opus, self._ports)

    def make_tx(self, node: str, rate: int):
        port = self._ports.acquire()
        return _PortBoundTx(node, port, rate, self._ports)


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
    def __init__(self, node, port, rate, ports):
        super().__init__(node, port, rate)
        self._ports = ports
        self._port_value = port

    def stop(self) -> None:
        super().stop()
        self._ports.release(self._port_value)
