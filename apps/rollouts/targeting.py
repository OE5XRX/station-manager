"""Channel-aware resolution of the image a station should be upgraded to."""

from __future__ import annotations

import re
from dataclasses import dataclass

from django.utils.translation import gettext as _

from apps.images.models import ImageRelease
from apps.stations.models import RolloutGroup

_NUM = re.compile(r"\d+")


def release_key(tag: str) -> tuple[int, ...]:
    """All digit runs in ``tag`` as ints (``"2026.10.19-04"`` -> 2026,10,19,4)."""
    return tuple(int(n) for n in _NUM.findall(tag or ""))


def is_newer(candidate: ImageRelease, current: ImageRelease) -> bool:
    """True if ``candidate`` is strictly newer than ``current``.

    Compares tag keys when both tags are numeric, else falls back to import time.
    """
    ck, uk = release_key(candidate.tag), release_key(current.tag)
    if ck and uk:
        return ck > uk
    return candidate.imported_at > current.imported_at


def station_channel(station) -> str:
    group = station.rollout_group
    return group.channel if group else RolloutGroup.Channel.RELEASE


def station_platform(station) -> str | None:
    current = station.current_image_release
    return current.machine if current else None


def latest_index() -> dict[tuple[str, str], ImageRelease]:
    """Latest release per ``(machine, channel)`` in a single query."""
    return {(r.machine, r.channel): r for r in ImageRelease.objects.filter(is_latest=True)}


@dataclass(frozen=True)
class UpgradeTarget:
    target: ImageRelease | None
    reason: str

    OK = "ok"
    NO_PLATFORM = "no_platform"
    NO_RELEASE = "no_release"
    NOT_OTA_READY = "not_ota_ready"
    UP_TO_DATE = "up_to_date"
    NOT_NEWER = "not_newer"

    @property
    def ok(self) -> bool:
        return self.reason == self.OK


def resolve_upgrade(station, index: dict | None = None) -> UpgradeTarget:
    machine = station_platform(station)
    if machine is None:
        return UpgradeTarget(None, UpgradeTarget.NO_PLATFORM)
    if index is None:
        index = latest_index()
    target = index.get((machine, station_channel(station)))
    if target is None:
        return UpgradeTarget(None, UpgradeTarget.NO_RELEASE)
    current = station.current_image_release
    if target.pk == current.pk:
        return UpgradeTarget(target, UpgradeTarget.UP_TO_DATE)
    if not is_newer(target, current):
        return UpgradeTarget(target, UpgradeTarget.NOT_NEWER)
    if not target.is_ota_ready:
        return UpgradeTarget(target, UpgradeTarget.NOT_OTA_READY)
    return UpgradeTarget(target, UpgradeTarget.OK)


def reason_message(result: UpgradeTarget, station) -> str:
    """Translated, user-facing explanation for a non-OK result."""
    channel = station_channel(station)
    machine = station_platform(station)
    current = station.current_image_release
    target = result.target
    params = {
        "channel": channel,
        "machine": machine,
        "current": current.tag if current else "",
        "target": target.tag if target else "",
    }
    reason = result.reason
    if reason == UpgradeTarget.OK:
        return ""
    if reason == UpgradeTarget.NO_PLATFORM:
        return _("Platform unknown — station was not provisioned via station-manager.")
    if reason == UpgradeTarget.NO_RELEASE:
        return (
            _("No image release imported for machine %(machine)s in channel %(channel)s.") % params
        )
    if reason == UpgradeTarget.UP_TO_DATE:
        return (
            _("Station already runs the latest image (%(current)s) of channel %(channel)s.")
            % params
        )
    if reason == UpgradeTarget.NOT_NEWER:
        return (
            _(
                "No newer image in channel %(channel)s: station runs %(current)s, "
                "latest is %(target)s (no downgrade)."
            )
            % params
        )
    if reason == UpgradeTarget.NOT_OTA_READY:
        return _("Release %(target)s is not OTA-ready (rootfs artifact missing).") % params
    return reason
