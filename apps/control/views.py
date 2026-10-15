from django.views.generic import DetailView

from apps.accounts.permissions import (
    AreaPermissionMixin,
    can_override_station,
    level_label,
    required_level,
)
from apps.stations.models import Station

from . import serializers


class StationControlView(AreaPermissionMixin, DetailView):
    """Control panel. ``control.view`` renders it read-only (RX audio still available);
    lock/PTT/mic controls render only with ``control.use`` (template ``can.control.use``)."""

    area = "control"
    verb = "view"
    model = Station
    template_name = "control/panel.html"
    context_object_name = "station"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        station = self.object
        # The audio-router is a virtual control-plane module (stream enumeration +
        # tx_route) owned by the dedicated audio panel — not a generic module card.
        # Rendering its `streams` (a list) in the generic widget shows
        # "[object Object],[object Object]". tx_route still flows over the control-WS
        # command path, so hiding the card changes nothing functional.
        ctx["modules"] = [
            m
            for m in station.modules.select_related("tracked_module__module_type").all()
            if m.type != "audio_router"
        ]
        ctx["initial_inventory"] = serializers.snapshot(station)
        # Cosmetic (Override button); the consumer re-checks on every preempt.
        ctx["can_admin"] = can_override_station(self.request.user, station)
        # Read-only hint: which level unlocks operating (from the registry, not hardcoded).
        ctx["control_use_label"] = level_label(required_level("control", "use"))
        ctx["ptt_default_key"] = " "
        return ctx
