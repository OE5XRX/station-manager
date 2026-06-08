from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from dataclasses import dataclass

GITHUB_API = "https://api.github.com"
_USER_AGENT = "oe5xrx-station-manager"
_ACCEPT = "application/vnd.github+json"
_TIMEOUT = 10  # seconds


class GitHubAPIError(Exception):
    """Wraps HTTP / network / decode failures from GitHub's REST API."""


@dataclass(frozen=True)
class GitHubRelease:
    tag: str
    html_url: str
    is_latest: bool
    asset_names: frozenset[str]

    def has_assets_for(self, machine: str) -> bool:
        prefix = f"oe5xrx-{machine}-{self.tag}.wic.bz2"
        return all(
            name in self.asset_names for name in (prefix, f"{prefix}.bundle", f"{prefix}.sha256")
        )


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
