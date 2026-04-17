import csv

from django.contrib.auth import get_user_model
from django.http import HttpResponse, JsonResponse
from django.views import View
from django.views.generic import ListView

from apps.accounts.views import AdminRequiredMixin
from apps.stations.models import Station, StationAuditLog

User = get_user_model()


class AuditLogFilterMixin:
    """Shared filtering logic for audit log list and export views."""

    def apply_filters(self, queryset, params):
        station = params.get("station")
        if station:
            queryset = queryset.filter(station_id=station)

        event_type = params.get("event_type")
        if event_type:
            queryset = queryset.filter(event_type=event_type)

        user = params.get("user")
        if user:
            queryset = queryset.filter(user_id=user)

        date_from = params.get("date_from")
        if date_from:
            queryset = queryset.filter(created_at__date__gte=date_from)

        date_to = params.get("date_to")
        if date_to:
            queryset = queryset.filter(created_at__date__lte=date_to)

        return queryset


class AuditLogListView(AdminRequiredMixin, AuditLogFilterMixin, ListView):
    model = StationAuditLog
    template_name = "audit/audit_list.html"
    context_object_name = "audit_logs"
    paginate_by = 50

    def get_template_names(self):
        if self.request.htmx:
            return ["audit/_audit_table.html"]
        return [self.template_name]

    def get_queryset(self):
        qs = super().get_queryset().select_related("station", "user")
        return self.apply_filters(qs, self.request.GET)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["stations"] = Station.objects.all().order_by("name")
        context["event_type_choices"] = StationAuditLog.EventType.choices
        context["users"] = User.objects.filter(role="admin").order_by("username")
        # Preserve current filter values for the template
        context["current_station"] = self.request.GET.get("station", "")
        context["current_event_type"] = self.request.GET.get("event_type", "")
        context["current_user"] = self.request.GET.get("user", "")
        context["current_date_from"] = self.request.GET.get("date_from", "")
        context["current_date_to"] = self.request.GET.get("date_to", "")
        return context


class AuditLogExportView(AdminRequiredMixin, AuditLogFilterMixin, View):
    EXPORT_LIMIT = 10_000

    def get(self, request):
        export_format = request.GET.get("format", "csv")

        qs = StationAuditLog.objects.select_related("station", "user")
        qs = self.apply_filters(qs, request.GET)
        qs = qs[: self.EXPORT_LIMIT]

        if export_format == "json":
            return self._export_json(qs)
        return self._export_csv(qs)

    def _entry_to_dict(self, entry):
        return {
            "id": entry.pk,
            "station": entry.station.name if entry.station else "",
            "event_type": entry.event_type,
            "message": entry.message,
            "changes": entry.changes,
            "user": entry.user.username if entry.user else "",
            "ip_address": entry.ip_address or "",
            "created_at": entry.created_at.isoformat(),
        }

    def _export_csv(self, queryset):
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="audit_log.csv"'

        writer = csv.writer(response)
        writer.writerow(
            [
                "ID",
                "Station",
                "Event Type",
                "Message",
                "Changes",
                "User",
                "IP Address",
                "Created At",
            ]
        )

        for entry in queryset:
            d = self._entry_to_dict(entry)
            writer.writerow(
                [
                    d["id"],
                    d["station"],
                    d["event_type"],
                    d["message"],
                    str(d["changes"]),
                    d["user"],
                    d["ip_address"],
                    d["created_at"],
                ]
            )

        return response

    def _export_json(self, queryset):
        data = [self._entry_to_dict(entry) for entry in queryset]
        return JsonResponse(data, safe=False)
