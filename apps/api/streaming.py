"""Chunked streaming of stored artifacts to HTTP clients.

Two layers both used to buffer a whole artifact before the first byte
left the server (OTA rootfs: 134 MB, 58 s TTFB → agent read timeout):

1. django-storages' ``S3File`` downloads the complete object into a temp
   file on the first ``read()``. ``open_storage_stream`` instead returns
   an ``S3RangeReader`` that issues a ranged ``GetObject`` and reads the
   response body incrementally. Other backends (FileSystemStorage in
   dev/tests) keep their own ``open()``.
2. Django's ASGI handler (we run daphne) drains a *sync* streaming
   iterator into a list before sending. ``response_iterator`` hands
   ASGI requests an async iterator that pulls one chunk at a time.

Known limit: daphne has no ASGI flow control — ``send()`` appends to
Twisted's transport buffer and returns at once. The S3 read is therefore
paced by S3, not by the client: if server↔S3 is faster than
server↔station, the unsent tail accumulates in daphne's memory (per
concurrent download). This fixes the time-to-first-byte; bounding memory
for slow clients needs a server with backpressure (uvicorn/hypercorn) and
is an ops decision. Likewise, "fully streamed" in the views means "fully
handed to the server", not "received by the client".
"""

from __future__ import annotations

import asyncio
import io
import logging

from asgiref.sync import sync_to_async
from botocore.exceptions import ClientError
from django.core.exceptions import SuspiciousOperation
from django.core.files.storage import default_storage
from django.core.handlers.asgi import ASGIRequest
from s3transfer.constants import ALLOWED_DOWNLOAD_ARGS
from storages.backends.s3 import S3Storage
from storages.utils import clean_name, safe_join

logger = logging.getLogger(__name__)


class S3RangeReader(io.RawIOBase):
    """Read-only, seekable file over one S3 object.

    The ``GetObject`` request is issued lazily on the first ``read()``
    with ``Range: bytes=<pos>-`` so a preceding ``seek()`` costs nothing
    and resumable downloads only transfer the requested tail. Seeking
    after reading closes the current body and re-requests from the new
    offset.
    """

    def __init__(self, client, bucket: str, key: str, size: int, params: dict | None = None):
        super().__init__()
        self._client = client
        self._bucket = bucket
        self._key = key
        self._size = size
        self._params = params or {}
        self._pos = 0
        self._body = None

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            pos = offset
        elif whence == io.SEEK_CUR:
            pos = self._pos + offset
        elif whence == io.SEEK_END:
            pos = self._size + offset
        else:
            raise ValueError(f"invalid whence {whence!r}")
        if pos < 0:
            raise ValueError("negative seek position")
        if pos != self._pos:
            self._close_body()
            self._pos = pos
        return self._pos

    def read(self, size: int = -1) -> bytes:
        if self.closed:
            raise ValueError("I/O operation on closed file")
        if self._pos >= self._size:
            return b""
        if self._body is None:
            try:
                resp = self._client.get_object(
                    Bucket=self._bucket,
                    Key=self._key,
                    Range=f"bytes={self._pos}-",
                    **self._params,
                )
            except ClientError as exc:
                # Object shrank since the HEAD — treat as EOF. The caller
                # already advertised the old length, so the client sees a
                # short body; leave a trace of why.
                if exc.response.get("Error", {}).get("Code") == "InvalidRange":
                    logger.warning(
                        "S3 object %s shorter than expected: range from %d of %d bytes invalid",
                        self._key,
                        self._pos,
                        self._size,
                    )
                    return b""
                raise
            self._body = resp["Body"]
        data = self._body.read() if size is None or size < 0 else self._body.read(size)
        self._pos += len(data)
        return data

    def readall(self) -> bytes:
        return self.read(-1)

    def readinto(self, buffer) -> int:
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def _close_body(self) -> None:
        body, self._body = self._body, None
        if body is not None:
            body.close()

    def close(self) -> None:
        if not self.closed:
            self._close_body()
        super().close()


def open_storage_stream(key: str, storage=None):
    """Open ``key`` for chunked sequential reading.

    Raises ``FileNotFoundError`` like ``storage.open()`` when the object
    does not exist (S3: an eager HEAD), so callers can keep mapping that
    to 502.
    """
    storage = storage if storage is not None else default_storage
    if not isinstance(storage, S3Storage):
        return storage.open(key, "rb")
    try:
        name = safe_join(storage.location, clean_name(key))
    except ValueError:
        raise SuspiciousOperation(f"Attempted access to {key!r} denied.") from None
    # Same filtered download params S3File would send (e.g. SSE-C keys).
    params = {
        k: v for k, v in storage.get_object_parameters(name).items() if k in ALLOWED_DOWNLOAD_ARGS
    }
    client = storage.connection.meta.client
    try:
        head = client.head_object(Bucket=storage.bucket_name, Key=name, **params)
    except ClientError as exc:
        if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
            raise FileNotFoundError(f"File does not exist: {name}") from exc
        raise
    return S3RangeReader(client, storage.bucket_name, name, head["ContentLength"], params)


def response_iterator(request, iterator):
    """Return ``iterator`` in the form the current server streams lazily.

    WSGI (and Django's sync test client) iterate sync generators lazily
    already. Under ASGI, a sync iterator would be drained into memory
    first, so wrap it: each ``next()`` runs via ``sync_to_async`` (the
    request's thread-sensitive executor, so ORM calls inside the
    generator stay legal) and the source is closed when the consumer
    stops early.
    """
    raw = getattr(request, "_request", request)  # unwrap DRF Request
    if not isinstance(raw, ASGIRequest):
        return iterator
    return _aiter_sync(iterator)


_DONE = object()


async def _aiter_sync(iterator):
    step = sync_to_async(next, thread_sensitive=True)
    try:
        while (chunk := await step(iterator, _DONE)) is not _DONE:
            yield chunk
    finally:
        close = getattr(iterator, "close", None)
        if close is not None:
            # Shield so a cancelled response task (client went away)
            # still releases the upstream S3 connection.
            await asyncio.shield(sync_to_async(close, thread_sensitive=True)())
