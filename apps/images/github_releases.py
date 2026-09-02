from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

GITHUB_API = "https://api.github.com"
_USER_AGENT = "oe5xrx-station-manager"
_ACCEPT = "application/vnd.github+json"
_TIMEOUT = 10  # seconds

# A channel must be a lowercase slug that fits ImageRelease.channel
# (max_length=32) — the same rule QuickQueueView enforces on import.
_CHANNEL_MAX_LEN = 32


def _is_valid_channel(channel: str) -> bool:
    return (
        bool(channel)
        and len(channel) <= _CHANNEL_MAX_LEN
        and bool(re.fullmatch(r"[a-z0-9-]+", channel))
    )


class GitHubAPIError(Exception):
    """Wraps HTTP / network / decode failures from GitHub's REST API."""


@dataclass(frozen=True)
class GitHubRelease:
    tag: str
    html_url: str
    is_latest: bool
    asset_names: frozenset[str]

    def has_assets_for(self, machine: str, channel: str) -> bool:
        base = f"oe5xrx-{machine}-{channel}-{self.tag}.wic.bz2"
        return all(name in self.asset_names for name in (base, f"{base}.bundle", f"{base}.sha256"))

    def channels_for(self, machine: str) -> frozenset[str]:
        """Channels with a complete (wic+sha256+bundle) asset triple.

        Extract the channel token by stripping the known prefix and
        suffix -- NEVER split on '-', because ``machine`` itself contains
        hyphens (qemux86-64, raspberrypi4-64).
        """
        prefix = f"oe5xrx-{machine}-"
        suffix = f"-{self.tag}.wic.bz2"
        channels = set()
        for name in self.asset_names:
            if name.startswith(prefix) and name.endswith(suffix):
                channel = name[len(prefix) : -len(suffix)]
                # Only surface channels that are valid, queueable slugs
                # (lowercase [a-z0-9-], <= the model's max_length). A
                # malformed token would render a row QuickQueueView always
                # rejects (400) and could overflow ImageRelease.channel on
                # import — drop it at discovery instead.
                if not _is_valid_channel(channel):
                    continue
                if self.has_assets_for(machine, channel):
                    channels.add(channel)
        return frozenset(channels)


def _get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept": _ACCEPT})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read())
    except (
        urllib.error.URLError,
        TimeoutError,
        ValueError,
        http.client.HTTPException,
    ) as exc:
        raise GitHubAPIError(str(exc)) from exc


def fetch_releases(repo: str, limit: int = 30) -> list[GitHubRelease]:
    """Return releases newest-first, with is_latest annotated.

    Two API calls: list (paginated to `limit`) + /releases/latest.
    Raises GitHubAPIError on any HTTP, timeout, or decode failure on the
    list call. /releases/latest failures are tolerated (is_latest=False
    for all rows).
    """
    raw = _get_json(f"{GITHUB_API}/repos/{repo}/releases?per_page={limit}")
    try:
        latest = _get_json(f"{GITHUB_API}/repos/{repo}/releases/latest")
        latest_tag = latest.get("tag_name") if isinstance(latest, dict) else None
    except GitHubAPIError:
        latest_tag = None
    return [
        GitHubRelease(
            tag=r["tag_name"],
            html_url=r.get("html_url", ""),
            is_latest=(r["tag_name"] == latest_tag),
            asset_names=frozenset(a["name"] for a in r.get("assets", [])),
        )
        for r in raw
    ]
