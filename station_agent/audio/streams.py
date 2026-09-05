"""Audio stream registry — the single source of truth for ``stream_id ↔ stream_ref``.

A *stream* is ``(station, stream_id)`` where ``stream_id`` is stable for a module/direction
(Spec 0 §4). This registry assigns a stable numeric ``stream_ref`` per ``stream_id`` (so the
string id is not sent in every media-frame header, §5.3) and produces the §5 ``advertise``
payload. Pure data — no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

OP_MIC = "op.mic"


@dataclass(frozen=True)
class StreamInfo:
    stream_id: str
    slot: int | None
    module: str
    direction: str  # "rx" (source → browser, from the ear's perspective)
    rate: int
    channels: int
    stream_ref: int
    codec: str = "opus"

    def advertise_entry(self) -> dict:
        # stream_ref is explicit here: §5.3 requires the stream_ref↔stream_id mapping to be
        # established in advertise, and the agent (media producer) owns ref assignment, so
        # Session C learns it directly rather than via a fragile array-index convention.
        return {
            "stream_id": self.stream_id,
            "slot": self.slot,
            "module": self.module,
            "direction": self.direction,
            "format": {"rate": self.rate, "channels": self.channels},
            "codec": self.codec,
            "stream_ref": self.stream_ref,
        }


class StreamRegistry:
    """Builds and holds the advertised source streams with stable refs."""

    def __init__(self, *, rx_rate: int = 8000, mic_rate: int = 16000, rx_module: str = "fm"):
        self._rx_rate = rx_rate
        self._mic_rate = mic_rate
        self._rx_module = rx_module
        self._by_id: dict[str, StreamInfo] = {}
        self._by_ref: dict[int, StreamInfo] = {}

    def rebuild(self, slots: list[int]) -> None:
        """(Re)build the stream set from the audio slots the backend found.

        Refs are assigned deterministically: each RX slot ascending, then ``op.mic`` — so a
        given topology always yields the same mapping across reconnects/hotplug re-advertise.
        """
        self._by_id.clear()
        self._by_ref.clear()
        ref = 0
        for slot in sorted(slots):
            info = StreamInfo(
                stream_id=f"slot{slot}.rx",
                slot=slot,
                module=self._rx_module,
                direction="rx",
                rate=self._rx_rate,
                channels=1,
                stream_ref=ref,
            )
            self._register(info)
            ref += 1
        # op.mic is browser-produced (Spec 0 §5.2) but still needs a ref so the agent can
        # receive its media for TX injection.
        self._register(
            StreamInfo(
                stream_id=OP_MIC,
                slot=None,
                module="operator",
                direction="rx",
                rate=self._mic_rate,
                channels=1,
                stream_ref=ref,
            )
        )

    def _register(self, info: StreamInfo) -> None:
        self._by_id[info.stream_id] = info
        self._by_ref[info.stream_ref] = info

    def advertise_payload(self) -> dict:
        return {
            "v": 1,
            "type": "advertise",
            "streams": [self._by_id[k].advertise_entry() for k in self._ordered_ids()],
        }

    def _ordered_ids(self) -> list[str]:
        return [i.stream_id for i in sorted(self._by_id.values(), key=lambda x: x.stream_ref)]

    def get(self, stream_id: str) -> StreamInfo | None:
        return self._by_id.get(stream_id)

    def by_ref(self, ref: int) -> StreamInfo | None:
        return self._by_ref.get(ref)

    def ref_for(self, stream_id: str) -> int | None:
        info = self._by_id.get(stream_id)
        return info.stream_ref if info else None

    def rx_stream_ids(self) -> list[str]:
        return [i.stream_id for i in self._by_id.values() if i.stream_id != OP_MIC]

    @property
    def mic_ref(self) -> int | None:
        return self.ref_for(OP_MIC)
