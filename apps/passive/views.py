from datetime import timedelta

from django.contrib import messages
from django.db import transaction
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.generic import TemplateView, View

from apps.accounts.client_ip import client_ip
from apps.accounts.models import AccountAuditLog
from apps.accounts.permissions import AreaPermissionMixin
from apps.control.models import ControlLock
from apps.stations.models import Station

from . import status
from .forms import AprsSettingsForm, HandoverProfileForm, PassiveServiceConfigForm
from .models import (
    AprsPacket,
    AprsSettings,
    HandoverProfile,
    PassiveServiceConfig,
    PassiveServiceRun,
    StationPassiveConfig,
)

MAX_PACKETS = 500
POSITION_SCAN = 5000
DEFAULT_HOURS = 24
MAX_HOURS = 24 * 30


def _int_param(raw, default, lo, hi):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


passive_badge = status.passive_badge


class AprsPageView(AreaPermissionMixin, TemplateView):
    area = "passive_services"
    template_name = "passive/aprs.html"

    # Set by AprsSettingsView when re-rendering a rejected POST.
    bound_forms = None

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        can_manage = self.request.user.can("passive_services", "manage")
        ctx["can_manage"] = can_manage
        ctx["stations"] = Station.objects.order_by("name")
        ctx["active_tab"] = "einstellungen" if self.bound_forms else "karte"
        if can_manage:
            forms = self.bound_forms or settings_forms()
            ctx.update(forms)
        return ctx


def settings_forms(data=None):
    return {
        "config_form": PassiveServiceConfigForm(
            data, prefix="cfg", instance=PassiveServiceConfig.get("aprs")
        ),
        "handover_form": HandoverProfileForm(data, prefix="hov", instance=HandoverProfile.get()),
        "aprs_form": AprsSettingsForm(data, prefix="aprs", instance=AprsSettings.get()),
    }


class AprsSettingsView(AreaPermissionMixin, View):
    """POST-only; every form must be valid or nothing is saved (verb: manage)."""

    area = "passive_services"
    http_method_names = ["post"]

    def post(self, request):
        forms = settings_forms(request.POST)
        if not all(f.is_valid() for f in forms.values()):
            page = AprsPageView()
            page.setup(request)
            page.bound_forms = forms
            messages.error(request, _("Settings not saved. Check the highlighted fields."))
            response = page.get(request)
            response.render()
            return response
        with transaction.atomic():
            for form in forms.values():
                form.save()
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.CONFIG_CHANGED,
            actor=request.user,
            message="Passive services (APRS) settings changed",
            ip_address=client_ip(request),
            details={
                "area": "passive_services",
                "fields": sorted(request.POST.keys() - {"csrfmiddlewaretoken"}),
            },
        )
        messages.success(request, _("Settings saved."))
        return redirect(reverse("passive:aprs") + "#einstellungen")


def _packet_json(p):
    return {
        "id": p.id,
        "station_id": p.station_id,
        "station_name": p.station.name,
        "heard_at": p.heard_at.isoformat(),
        "source": p.source,
        "packet_type": p.packet_type,
        "latitude": float(p.latitude) if p.latitude is not None else None,
        "longitude": float(p.longitude) if p.longitude is not None else None,
        "symbol": p.symbol,
        "comment": p.comment,
        "raw_tnc2": p.raw_tnc2,
        "gated": p.gated_at is not None,
    }


class AprsPacketsView(AreaPermissionMixin, View):
    area = "passive_services"

    def get(self, request):
        qs = AprsPacket.objects.select_related("station")
        hours = _int_param(request.GET.get("hours"), DEFAULT_HOURS, 1, MAX_HOURS)
        qs = qs.filter(heard_at__gte=timezone.now() - timedelta(hours=hours))
        after_id = _int_param(request.GET.get("after_id"), 0, 0, 2**62)
        if after_id:
            qs = qs.filter(id__gt=after_id)
        station = _int_param(request.GET.get("station"), 0, 0, 2**62)
        if station:
            qs = qs.filter(station_id=station)
        if after_id:
            # Oldest first so a burst larger than the cap is consumed in order
            # (last_id advances only as far as what was returned: no gap).
            rows = list(qs.order_by("id")[:MAX_PACKETS])[::-1]
        else:
            rows = list(qs.order_by("-id")[:MAX_PACKETS])
        packets = [_packet_json(p) for p in rows]
        payload = {"packets": packets, "last_id": rows[0].id if rows else after_id}
        if not after_id:
            payload["positions"] = [_packet_json(p) for p in self._latest_positions(qs)]
        return JsonResponse(payload)

    @staticmethod
    def _latest_positions(qs):
        """Newest positioned packet per source, so the map is not limited by the log cap."""
        seen = {}
        for p in qs.filter(latitude__isnull=False, longitude__isnull=False).order_by("-id")[
            :POSITION_SCAN
        ]:
            seen.setdefault(p.source, p)
        return list(seen.values())


def igate_state(station, config, run, lock_held) -> tuple[str, str]:
    """(state, reason) for the iGates tab.

    ``run`` is the open ``PassiveServiceRun`` or, failing that, a recent errored one; the run
    state decides first, the control lock only speaks for a station without a live run.
    """
    run_states = PassiveServiceRun.State
    if config is None or not config.enabled:
        return "not_opted_in", ""
    if not station.is_online:
        return "agent_offline", ""
    if run is not None:
        if run.state == run_states.RUNNING:
            return "running", ""
        if run.state == run_states.STARTING:
            return "running", "startet"
        if run.state == run_states.AGENT_OFFLINE:
            return "agent_offline", ""
        if run.state == run_states.STOPPING:
            return ("in_use", "") if lock_held else ("idle", "beendet")
        if run.state == run_states.ERROR and not lock_held:
            return "error", run.last_error
    if lock_held:
        return "in_use", ""
    return "idle", ""


def igate_warning(station, config):
    if config is None or not config.enabled:
        return None
    if config.igate_call is None:
        return "no_igate_call"
    if station.latitude is None or station.longitude is None:
        return "no_coordinates"
    return None


class AprsIgatesView(AreaPermissionMixin, View):
    area = "passive_services"

    def get(self, request):
        since = timezone.now() - timedelta(hours=24)
        counts = {
            row["station_id"]: row
            for row in AprsPacket.objects.filter(heard_at__gte=since)
            .values("station_id")
            .annotate(n24=Count("id"))
        }
        configs = {c.station_id: c for c in StationPassiveConfig.objects.all()}
        runs = {r.station_id: r for r in PassiveServiceRun.objects.filter(ended_at__isnull=True)}
        recent = PassiveServiceRun.objects.filter(
            ended_at__isnull=False, ended_at__gte=timezone.now() - status.ERROR_VISIBLE
        ).order_by("-started_at")
        latest_closed = {}
        for r in recent:
            latest_closed.setdefault(r.station_id, r)
        for station_id, r in latest_closed.items():
            if station_id not in runs and r.state == PassiveServiceRun.State.ERROR:
                runs[station_id] = r
        held = set(
            ControlLock.objects.filter(scope="station", holder__isnull=False).values_list(
                "station_id", flat=True
            )
        )
        out = []
        for st in Station.objects.order_by("name"):
            cfg = configs.get(st.id)
            state, reason = igate_state(st, cfg, runs.get(st.id), st.id in held)
            row = counts.get(st.id)
            last = (
                AprsPacket.objects.filter(station=st)
                .order_by("-heard_at")
                .values_list("heard_at", flat=True)
                .first()
            )
            out.append(
                {
                    "station_id": st.id,
                    "name": st.name,
                    "call": cfg.igate_call if cfg else None,
                    "state": state,
                    "reason": reason,
                    "warning": igate_warning(st, cfg),
                    "last_packet_at": last.isoformat() if last else None,
                    "packets_24h": row["n24"] if row else 0,
                }
            )
        return JsonResponse(out, safe=False)
