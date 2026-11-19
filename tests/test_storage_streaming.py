"""Real streaming of stored artifacts (OTA rootfs, module firmware,
provisioning bundles).

Regression: django-storages' ``S3File`` downloads the *whole* object into
a temp file on the first ``read()`` — a 134 MB rootfs took 58 s before
the first byte, the agent's 30 s read timeout fired and every OTA failed.
On top of that, Django's ASGI handler drains a *sync* streaming iterator
into a list before sending anything. Both must stream chunk by chunk.
"""

import asyncio
import io

import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber
from django.core.files.storage import FileSystemStorage
from django.test import AsyncRequestFactory, RequestFactory
from storages.backends.s3 import S3Storage

from apps.api import streaming


@pytest.fixture
def s3():
    storage = S3Storage(
        access_key="k",
        secret_key="s",
        bucket_name="bucket",
        region_name="us-east-1",
        location="media",
    )
    client = storage.connection.meta.client
    with Stubber(client) as stub:
        yield storage, stub
        stub.assert_no_pending_responses()


def _body(data: bytes) -> dict:
    return {
        "Body": StreamingBody(io.BytesIO(data), len(data)),
        "ContentLength": len(data),
    }


class TestOpenStorageStream:
    def test_filesystem_storage_falls_back_to_open(self, tmp_path):
        fs = FileSystemStorage(location=tmp_path)
        fs.save("a/b.bin", io.BytesIO(b"hello"))
        with streaming.open_storage_stream("a/b.bin", storage=fs) as fh:
            assert fh.read() == b"hello"

    def test_missing_s3_object_raises_at_open(self, s3):
        storage, stub = s3
        stub.add_client_error("head_object", http_status_code=404, service_error_code="404")
        with pytest.raises(Exception):
            streaming.open_storage_stream("img.bz2", storage=storage)

    def test_s3_stream_is_lazy_ranged_and_chunked(self, s3):
        storage, stub = s3
        data = bytes(range(256)) * 40
        stub.add_response(
            "head_object",
            {"ContentLength": len(data)},
            {"Bucket": "bucket", "Key": "media/img.bz2"},
        )
        stub.add_response(
            "get_object",
            _body(data),
            {"Bucket": "bucket", "Key": "media/img.bz2", "Range": "bytes=0-"},
        )
        fh = streaming.open_storage_stream("img.bz2", storage=storage)
        assert fh.seekable()
        # Only the HEAD happened so far — the GET is issued on first read
        # and served incrementally, never buffered as a whole.
        assert fh.read(1000) == data[:1000]
        assert fh.read(1000) == data[1000:2000]
        assert fh.read() == data[2000:]
        assert fh.read(10) == b""
        fh.close()
        assert fh.closed

    def test_seek_before_read_uses_s3_range(self, s3):
        storage, stub = s3
        data = b"0123456789"
        stub.add_response("head_object", {"ContentLength": 10})
        stub.add_response(
            "get_object",
            _body(data[4:]),
            {"Bucket": "bucket", "Key": "media/img.bz2", "Range": "bytes=4-"},
        )
        fh = streaming.open_storage_stream("img.bz2", storage=storage)
        assert fh.seek(4) == 4
        assert fh.read(3) == b"456"
        assert fh.tell() == 7

    def test_seek_after_read_reopens_at_new_offset(self, s3):
        storage, stub = s3
        data = b"0123456789"
        stub.add_response("head_object", {"ContentLength": 10})
        stub.add_response("get_object", _body(data))
        stub.add_response(
            "get_object",
            _body(data[8:]),
            {"Bucket": "bucket", "Key": "media/img.bz2", "Range": "bytes=8-"},
        )
        fh = streaming.open_storage_stream("img.bz2", storage=storage)
        assert fh.read(2) == b"01"
        fh.seek(8)
        assert fh.read() == b"89"

    def test_read_at_or_past_eof_returns_empty_without_request(self, s3):
        storage, stub = s3
        stub.add_response("head_object", {"ContentLength": 10})
        fh = streaming.open_storage_stream("img.bz2", storage=storage)
        fh.seek(10)
        assert fh.read(5) == b""

    def test_open_storage_stream_uses_default_storage(self, s3, monkeypatch):
        storage, stub = s3
        stub.add_response("head_object", {"ContentLength": 1})
        monkeypatch.setattr(streaming, "default_storage", storage)
        fh = streaming.open_storage_stream("img.bz2")
        assert isinstance(fh, streaming.S3RangeReader)


class TestResponseIterator:
    def _gen(self, log):
        try:
            for i in range(3):
                log.append(f"read{i}")
                yield f"c{i}".encode()
            log.append("done")
        finally:
            log.append("closed")

    def test_wsgi_request_keeps_sync_iterator(self):
        request = RequestFactory().get("/")
        it = self._gen([])
        assert streaming.response_iterator(request, it) is it

    def test_asgi_request_gets_incremental_async_iterator(self):
        request = AsyncRequestFactory().get("/")
        log = []
        ait = streaming.response_iterator(request, self._gen(log))
        assert hasattr(ait, "__aiter__")

        async def consume():
            first = await anext(ait)
            # The sync source must NOT have been drained up front.
            assert log == ["read0"]
            rest = [chunk async for chunk in ait]
            return [first, *rest]

        assert asyncio.run(consume()) == [b"c0", b"c1", b"c2"]
        assert log == ["read0", "read1", "read2", "done", "closed"]

    def test_asgi_early_close_closes_sync_source(self):
        request = AsyncRequestFactory().get("/")
        log = []
        ait = streaming.response_iterator(request, self._gen(log))

        async def consume_one():
            await anext(ait)
            await ait.aclose()

        asyncio.run(consume_one())
        assert log == ["read0", "closed"]

    def test_drf_request_is_unwrapped(self):
        from rest_framework.request import Request

        request = Request(AsyncRequestFactory().get("/"))
        ait = streaming.response_iterator(request, iter([b"x"]))
        assert hasattr(ait, "__aiter__")
