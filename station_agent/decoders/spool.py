"""Bounded RAM spool for reliable decoder output (epoch/seq/ack)."""

from __future__ import annotations

import json
import uuid
from collections import OrderedDict
from collections.abc import Iterable

from station_agent import protocol


class Spool:
    """Holds reliable ``decoder_output`` frames until the server acks them.

    ``epoch`` is fixed for the process lifetime so the server can tell a restarted
    agent (new epoch, seq restarts at 1) from a resend. When a bound is exceeded the
    oldest entries are dropped and counted.
    """

    def __init__(
        self, max_items: int = 2000, max_bytes: int = 1_000_000, *, epoch: str | None = None
    ):
        self.max_items = max_items
        self.max_bytes = max_bytes
        self.epoch = epoch or uuid.uuid4().hex
        self.dropped = 0
        self._seq = 0
        self._bytes = 0
        self._items: OrderedDict[int, tuple[dict, int]] = OrderedDict()

    def add(self, decoder: str, slot: int, kind: str, data: dict, ts: float) -> dict:
        self._seq += 1
        frame = protocol.build_decoder_output(
            decoder, slot, self.epoch, self._seq, kind, True, data, ts
        )
        size = len(json.dumps(frame))
        self._items[self._seq] = (frame, size)
        self._bytes += size
        while self._items and (len(self._items) > self.max_items or self._bytes > self.max_bytes):
            _, (_, old_size) = self._items.popitem(last=False)
            self._bytes -= old_size
            self.dropped += 1
        return frame

    def ack(self, epoch: str, seqs: Iterable[int]) -> int:
        if epoch != self.epoch:
            return 0
        removed = 0
        for seq in seqs:
            entry = self._items.pop(seq, None) if isinstance(seq, int) else None
            if entry is not None:
                self._bytes -= entry[1]
                removed += 1
        return removed

    def pending(self) -> list[dict]:
        return [frame for frame, _ in self._items.values()]
