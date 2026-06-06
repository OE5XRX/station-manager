"""Region CRUD views (admin-only).

ListView shows all regions with station counts.
CreateView + UpdateView use RegionForm.
DeleteView shows a confirmation page with the count of stations
that will lose their region (SET_NULL via FK).

Audit-log emission is wired via signals in apps/stations/signals.py:
REGION_CREATED on post_save, REGION_UPDATED on post_save with
created=False, REGION_DELETED on post_delete.
"""

from django.db.models import Count
from django.urls import reverse_lazy
from django.views.generic import (
    CreateView,
    DeleteView,
    ListView,
    UpdateView,
)

from apps.accounts.views import AdminRequiredMixin
from apps.stations.forms import RegionForm
from apps.stations.models import Region


class RegionListView(AdminRequiredMixin, ListView):
    model = Region
    template_name = "stations/region_list.html"
    context_object_name = "regions"

    def get_queryset(self):
        # annotate stations_count to avoid N+1 in the template.
        return super().get_queryset().annotate(stations_count=Count("stations")).order_by("name")


class RegionCreateView(AdminRequiredMixin, CreateView):
    model = Region
    form_class = RegionForm
    template_name = "stations/region_form.html"
    success_url = reverse_lazy("stations:region_list")


class RegionUpdateView(AdminRequiredMixin, UpdateView):
    model = Region
    form_class = RegionForm
    template_name = "stations/region_form.html"
    success_url = reverse_lazy("stations:region_list")


class RegionDeleteView(AdminRequiredMixin, DeleteView):
    model = Region
    template_name = "stations/region_confirm_delete.html"
    success_url = reverse_lazy("stations:region_list")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["affected_stations_count"] = self.object.stations.count()
        return context
