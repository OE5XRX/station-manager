import csv

from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.http import HttpResponse, JsonResponse
from django.views import View
from django.views.generic import ListView

from apps.accounts.views import AdminRequiredMixin
from apps.sso.models import SsoAuditLog
from apps.stations.models import Station, StationAuditLog

User = get_user_model()

# Per-source fetch cap before merging. The merged list is built in
# Python (sorted in-memory), so the upper bound on memory is
# O(STATION_FEED_CAP + SSO_FEED_CAP). Plenty of headroom at OE5XRX
# scale; tune if a single page of audit traffic ever exceeds this.
MERGE_FEED_CAP = 500


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

    def apply_sso_date_filters(self, queryset, params):
        """SSO logs share date filters with station logs but have no
        station/event/user equivalents in the existing filter sidebar."""
        date_from = params.get("date_from")
        if date_from:
            queryset = queryset.filter(created_at__date__gte=date_from)
        date_to = params.get("date_to")
        if date_to:
            queryset = queryset.filter(created_at__date__lte=date_to)
        return queryset


class AuditLogListView(AdminRequiredMixin, AuditLogFilterMixin, ListView):
    """Merged audit feed: StationAuditLog + SsoAuditLog.

    The template iterates ``page_obj`` as a list of ``(category, entry)``
    tuples and renders each row variant accordingly — see
    apps/audit/templates/audit/_audit_table.html.
    """

    template_name = "audit/audit_list.html"
    context_object_name = "audit_logs"
    paginate_by = 50

    def get_template_names(self):
        if self.request.htmx:
            return ["audit/_audit_table.html"]
        return [self.template_name]

    def get_queryset(self):
        params = self.request.GET
        category = params.get("category", "")

        # When the user has set station-specific filters but left
        # category=="" (All), treat that as an implicit "only Station feed".
        # Otherwise filtering by station X still surfaces every recent SSO
        # event in the merge, which is confusing UX. Date filters are
        # shared across feeds and don't trigger this narrowing.
        station_only_filters_active = any(
            [
                params.get("station"),
                params.get("event_type"),
                params.get("user"),
            ]
        )
        include_station = category in ("", "station")
        include_sso = category in ("", "sso") and not (
            category == "" and station_only_filters_active
        )

        station_entries = []
        sso_entries = []

        if include_station:
            station_qs = StationAuditLog.objects.select_related("station", "user")
            station_qs = self.apply_filters(station_qs, params)
            station_entries = list(station_qs.order_by("-created_at")[:MERGE_FEED_CAP])

        if include_sso:
            sso_qs = SsoAuditLog.objects.select_related(
                "actor", "target_user", "application"
            )
            sso_qs = self.apply_sso_date_filters(sso_qs, params)
            sso_entries = list(sso_qs.order_by("-created_at")[:MERGE_FEED_CAP])

        # Merge into a single chronological list of (category, entry)
        # tuples. The template unpacks the tuple to pick the right row
        # variant. O(N + M) memory; capped by MERGE_FEED_CAP per source.
        merged = [("station", e) for e in station_entries] + [
            ("sso", e) for e in sso_entries
        ]
        merged.sort(key=lambda pair: pair[1].created_at, reverse=True)
        return merged

    def paginate_queryset(self, queryset, page_size):
        # Override: the base ListView uses queryset.count() which doesn't
        # work on a Python list of tuples — Paginator handles lists fine,
        # so do the pagination manually and return the expected 4-tuple.
        paginator = Paginator(queryset, page_size)
        page_number = self.request.GET.get(self.page_kwarg, 1)
        page = paginator.get_page(page_number)
        return (paginator, page, page.object_list, page.has_other_pages())

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["stations"] = Station.objects.all().order_by("name")
        context["event_type_choices"] = StationAuditLog.EventType.choices
        # Group-backed filter: matches users added via Django Admin
        # without the deprecated `role` column being touched. distinct()
        # because a user could land in multiple matching groups in the
        # future (defensive).
        context["users"] = (
            User.objects.filter(groups__name="admin").distinct().order_by("username")
        )
        # Preserve current filter values for the template
        context["category"] = self.request.GET.get("category", "")
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
