from __future__ import annotations

import hashlib
import urllib.request
from dataclasses import dataclass

RELEASE_URL_FMT = "https://github.com/{repo}/releases/download/{tag}/oe5xrx-{machine}-{tag}.{ext}"

# GitHub asset downloads are ~70 MB; a healthy connection finishes in seconds.
# Without a timeout a stalled connection would pin the worker indefinitely.
_REQUEST_TIMEOUT = 60  # seconds


@dataclass
class ReleaseAsset:
    wic_bytes: bytes
    sha256: str
    bundle_bytes: bytes


def _get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=_REQUEST_TIMEOUT) as resp:
        return resp.read()


def fetch_release_asset(repo: str, tag: str, machine: str) -> ReleaseAsset:
    base = RELEASE_URL_FMT.format(repo=repo, tag=tag, machine=machine, ext="wic.bz2")
    wic_bytes = _get(base)
    sha_text = _get(base + ".sha256").decode("utf-8").strip()
    # Format from `sha256sum`: "<64-hex>  <filename>"
    sha256 = sha_text.split()[0]
    if len(sha256) != 64:
        raise ValueError(f"malformed .sha256 sidecar: {sha_text!r}")
    if hashlib.sha256(wic_bytes).hexdigest() != sha256:
        raise ValueError("sha256 mismatch: the downloaded .wic.bz2 is corrupt or tampered")
    bundle_bytes = _get(base + ".bundle")
    return ReleaseAsset(wic_bytes=wic_bytes, sha256=sha256, bundle_bytes=bundle_bytes)
