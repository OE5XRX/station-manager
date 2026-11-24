"""What the UI says about a station's passive service (badge, iGate state, connect snapshot).

Every surface derives from the same DB facts: the open ``PassiveServiceRun`` (or the latest
closed one when it ended in an error), the control lock, and the eligibility reasons.
"""

from datetime import timedelta

from django.utils import timezone
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from apps.control.models import AgentConnection, ControlLock

from .eligibility import DECODERS_FEATURE, ineligible_reason
from .models import PassiveServiceConfig, PassiveServiceRun

SERVICE = "aprs"
# A closed run that ended in ``error`` keeps showing until this long after it ended.
ERROR_VISIBLE = timedelta(hours=24)
# Idle reasons that mean "not set up / not reachable": never shown in the UI.
QUIET_REASONS = frozenset({"disabled", "not_opted_in", "offline"})

IDLE_REASON_TEXT = {
    "agent_too_old": _("Agent does not support decoders"),
    "updating": _("Station is updating"),
    "draining": _("Station is draining"),
    "lock_held": _("Station in use"),
    "idle_wait": _("Waiting for idle"),
    "no_module": _("No suitable radio module"),
}


def recent_run_error(station_id, *, now=None):
    """The latest run when it ended in an error shortly ago, else ``None``."""
    now = now or timezone.now()
    run = (
        PassiveServiceRun.objects.filter(
            station_id=station_id, ended_at__isnull=False, ended_at__gte=now - ERROR_VISIBLE
        )
        .order_by("-started_at")
        .first()
    )
    return run if run is not None and run.state == PassiveServiceRun.State.ERROR else None


def status_run(station_id, *, now=None):
    """The run that describes the station right now: the open one, else a recent error."""
    return PassiveServiceRun.open_for(station_id) or recent_run_error(station_id, now=now)


def _lock_held(station) -> bool:
    return ControlLock.objects.filter(
        station=station, scope="station", holder__isnull=False
    ).exists()


def passive_badge(station) -> dict:
    """``{"label", "level"}`` for the station detail and control pages."""
    run_states = PassiveServiceRun.State
    run = status_run(station.pk)
    if run is not None:
        if run.state == run_states.ERROR:
            detail = run.last_error or gettext("unknown")
            return {
                "label": gettext("Passive: error: %(reason)s") % {"reason": detail},
                "level": "error",
            }
        if run.state in (run_states.RUNNING, run_states.STARTING):
            label = (
                gettext("Passive: APRS active")
                if run.state == run_states.RUNNING
                else gettext("Passive: APRS starting")
            )
            return {"label": label, "level": "ok"}
        if run.state == run_states.STOPPING:
            return {"label": gettext("Passive: in use"), "level": "muted"}
        if run.state == run_states.AGENT_OFFLINE:
            return {"label": gettext("Passive: agent offline"), "level": "off"}
    if _lock_held(station):
        return {"label": gettext("Passive: in use"), "level": "muted"}
    return {"label": gettext("Passive: off"), "level": "off"}


def idle_reason(station) -> str | None:
    """Why the service is not running although set up, from DB facts only.

    Agent presence comes from ``AgentConnection``; the agent's feature list lives in the
    agent consumer's memory, so ``decoders`` is assumed present here (``agent_too_old``
    reaches a browser that was already connected, via the live frame).
    """
    station_cfg = getattr(station, "passive_config", None)
    if station_cfg is None or not station_cfg.enabled:
        return None  # the common case on connect: nothing set up, skip the other queries
    cfg = PassiveServiceConfig.objects.filter(service=SERVICE).first()
    if cfg is None:
        return None
    connected = AgentConnection.objects.filter(station=station, kind="control").exists()
    reason = ineligible_reason(
        station,
        cfg,
        now=timezone.now(),
        agent_connected=connected,
        agent_features={DECODERS_FEATURE},
    )
    return None if reason in QUIET_REASONS else reason


def browser_frame(station) -> dict:
    """``passive_state`` frame for a browser that just connected.

    Always sent (``off`` with no reason for a quiet station): the panel expires a passive
    state it held over a reconnect on exactly this frame, never on a timer.
    """
    run = PassiveServiceRun.open_for(station.pk)
    if run is not None:
        return {
            "type": "passive_state",
            "service": SERVICE,
            "state": run.state,
            "slot": run.slot,
            "reason": run.last_error or None,
        }
    failed = recent_run_error(station.pk)
    reason = (
        failed.last_error or gettext("unknown") if failed is not None else idle_reason(station)
    )
    return {
        "type": "passive_state",
        "service": SERVICE,
        "state": "off",
        "slot": None,
        "reason": reason,
    }
