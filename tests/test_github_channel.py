import hashlib
from unittest import mock

from apps.images import github


def test_fetch_uses_channel_in_url():
    wic = b"payload"
    sha = hashlib.sha256(wic).hexdigest()
    responses = {
        "https://github.com/OE5XRX/linux-image/releases/download/v1/oe5xrx-qemux86-64-dev-v1.wic.bz2": wic,
        "https://github.com/OE5XRX/linux-image/releases/download/v1/oe5xrx-qemux86-64-dev-v1.wic.bz2.sha256": f"{sha}  x".encode(),
        "https://github.com/OE5XRX/linux-image/releases/download/v1/oe5xrx-qemux86-64-dev-v1.wic.bz2.bundle": b"bundle",
    }
    with mock.patch.object(github, "_get", side_effect=lambda u: responses[u]):
        asset = github.fetch_release_asset("OE5XRX/linux-image", "v1", "qemux86-64", "dev")
    assert asset.sha256 == sha
    assert asset.bundle_bytes == b"bundle"
