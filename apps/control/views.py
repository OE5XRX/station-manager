from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.views.generic import DetailView

from apps.stations.models import Station

from . import serializers


class StationControlView(LoginRequiredMixin, UserPassesTestMixin, DetailView):
    model = Station
    template_name = "control/panel.html"
    context_object_name = "station"
    raise_exception = True  # 403 for authenticated-but-unauthorized, not a redirect loop

    def get_object(self, queryset=None):
        # Memoize: test_func() and DetailView.get() both call get_object,
        # which would otherwise fire two identical station queries per request.
        if not hasattr(self, "_cached_object"):
            self._cached_object = super().get_object(queryset=queryset)
        return self._cached_object

    def test_func(self):
        return self.request.user.can_use_station(self.get_object())

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        station = self.object
        # The audio-router is a virtual control-plane module (stream enumeration +
        # tx_route) owned by the dedicated audio panel — not a generic module card.
        # Rendering its `streams` (a list) in the generic widget shows
        # "[object Object],[object Object]". tx_route still flows over the control-WS
        # command path, so hiding the card changes nothing functional.
        ctx["modules"] = [m for m in station.modules.all() if m.type != "audio_router"]
        ctx["initial_inventory"] = serializers.snapshot(station)
        u = self.request.user
        ctx["can_admin"] = (
            u.is_admin or u.is_station_admin(station) or u.can_administer_station(station)
        )
        ctx["ptt_default_key"] = " "
        return ctx
