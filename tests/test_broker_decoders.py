import asyncio

from station_agent.broker import Broker


class Collector:
    def __init__(self):
        self.sent = []

    async def __call__(self, msg):
        self.sent.append(msg)


class FakeHost:
    def __init__(self):
        self.calls = []

    async def start(self, decoder, slot, params, request_id, max_runtime_s=None):
        self.calls.append(("start", decoder, slot, params, request_id, max_runtime_s))

    async def stop(self, decoder, slot, request_id=None, reason="requested"):
        self.calls.append(("stop", decoder, slot, request_id))

    async def ack(self, epoch, seqs):
        self.calls.append(("ack", epoch, seqs))


def _run(coro):
    return asyncio.run(coro)


def _frame(t, **kw):
    return {"v": 1, "type": t, **kw}


def test_broker_routes_decoder_frames_to_host():
    async def go():
        host = FakeHost()
        b = Broker(Collector(), decoder_host=host)
        await b.handle(
            _frame(
                "decoder_start",
                decoder="aprs",
                slot=1,
                params={"x": 1},
                request_id="r1",
                max_runtime_s=60,
            )
        )
        await b.handle(_frame("decoder_stop", decoder="aprs", slot=1, request_id="r2"))
        await b.handle(_frame("decoder_ack", epoch="E", seqs=[1, 2, "x", True]))
        await b.drain_decoder_tasks()
        return host.calls

    calls = _run(go())
    assert ("start", "aprs", 1, {"x": 1}, "r1", 60) in calls
    assert ("stop", "aprs", 1, "r2") in calls
    assert ("ack", "E", [1, 2, True]) in calls or ("ack", "E", [1, 2]) in calls


def test_broker_without_host_rejects_start_unsupported():
    async def go():
        col = Collector()
        b = Broker(col)
        await b.handle(_frame("decoder_start", decoder="aprs", slot=1, request_id="r1"))
        await b.handle(_frame("decoder_stop", decoder="aprs", slot=1, request_id="r2"))
        await b.handle(_frame("decoder_ack", epoch="E", seqs=[1]))
        return col.sent

    sent = _run(go())
    assert len(sent) == 1
    assert sent[0]["request_id"] == "r1" and sent[0]["ok"] is False
    assert sent[0]["error"]["code"] == "unsupported"


def test_broker_ignores_malformed_decoder_start():
    async def go():
        host, col = FakeHost(), Collector()
        b = Broker(col, decoder_host=host)
        await b.handle(_frame("decoder_start", decoder="aprs", request_id="r1"))  # no slot
        await b.handle(_frame("decoder_start", slot=1, decoder="aprs"))  # no request_id
        await b.handle(_frame("decoder_start", slot=True, decoder="aprs", request_id="r3"))
        await b.handle(_frame("decoder_start", slot=1, decoder="aprs", request_id="r4", params=[]))
        await b.handle(
            _frame("decoder_start", slot=1, decoder="aprs", request_id="r5", max_runtime_s=-1)
        )
        await b.drain_decoder_tasks()
        return host.calls, col.sent

    calls, sent = _run(go())
    assert calls == []
    assert [m["request_id"] for m in sent] == ["r1", "r3", "r4", "r5"]
    assert all(m["error"]["code"] == "validation_failed" for m in sent)


def test_broker_does_not_stop_decoders_on_disconnect():
    async def go():
        host = FakeHost()
        b = Broker(Collector(), decoder_host=host)
        await b.on_disconnect()
        return host.calls

    assert _run(go()) == []
