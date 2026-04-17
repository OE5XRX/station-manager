from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    ListView,
    UpdateView,
)

from apps.api.models import DeviceKey
from apps.images.models import ImageRelease
from apps.provisioning.models import ProvisioningJob

from .forms import StationForm, StationLogEntryForm, StationPhotoForm, StationTagForm
from .models import Station, StationAuditLog, StationTag


def _get_client_ip(request):
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    return xff.split(",")[0].strip() if xff else request.META.get("REMOTE_ADDR")


def _track_changes(old_instance, new_instance, fields):
    """Compare two model instances and return a dict of changed fields."""
    changes = {}
    for field in fields:
        old_val = str(getattr(old_instance, field, "") or "")
        new_val = str(getattr(new_instance, field, "") or "")
        if old_val != new_val:
            changes[field] = {"old": old_val, "new": new_val}
    return changes


class AdminOrOperatorRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Restrict access to users with admin or operator role."""

    def test_func(self):
        return self.request.user.role in ("admin", "operator")


# ---------------------------------------------------------------------------
# Station views
# ---------------------------------------------------------------------------


class StationListView(LoginRequiredMixin, ListView):
    model = Station
    template_name = "stations/station_list.html"
    context_object_name = "stations"
    paginate_by = 25

    def get_template_names(self):
        if self.request.htmx:
            return ["stations/_station_table.html"]
        return [self.template_name]

    def get_queryset(self):
        qs = super().get_queryset().prefetch_related("tags")
        search = self.request.GET.get("q", "").strip()
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(callsign__icontains=search))
        tag_slug = self.request.GET.get("tag")
        if tag_slug:
            qs = qs.filter(tags__slug=tag_slug)
        return qs.distinct()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["search_query"] = self.request.GET.get("q", "")
        context["tags"] = StationTag.objects.all()
        context["selected_tag"] = self.request.GET.get("tag", "")
        return context


class StationDetailView(LoginRequiredMixin, DetailView):
    model = Station
    template_name = "stations/station_detail.html"
    context_object_name = "station"

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("device_key", "inventory")
            .prefetch_related("tags", "installed_modules", "photos", "log_entries", "audit_logs")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["photo_form"] = StationPhotoForm()
        context["log_form"] = StationLogEntryForm()
        # Show raw private key once after Ed25519 key generation
        privkey_session_key = f"raw_private_key_{self.object.pk}"
        raw_private_key = self.request.session.pop(privkey_session_key, None)
        if raw_private_key:
            context["show_raw_private_key"] = True
            context["raw_private_key"] = raw_private_key
        # Provisioning section (admin only)
        if self.request.user.role == "admin":
            context["image_releases"] = ImageRelease.objects.order_by(
                "machine", "-is_latest", "-imported_at"
            )
            context["machine_choices"] = ImageRelease.Machine.choices
            context["active_provisioning_job"] = (
                ProvisioningJob.objects.filter(
                    station=self.object,
                    status__in=[
                        ProvisioningJob.Status.PENDING,
                        ProvisioningJob.Status.RUNNING,
                        ProvisioningJob.Status.READY,
                    ],
                )
                .order_by("-created_at")
                .first()
            )
        return context


class StationCreateView(AdminOrOperatorRequiredMixin, CreateView):
    model = Station
    template_name = "stations/station_form.html"
    form_class = StationForm

    def get_success_url(self):
        return reverse("stations:station_detail", kwargs={"pk": self.object.pk})

    def form_valid(self, form):
        response = super().form_valid(form)
        StationAuditLog.log(
            station=self.object,
            event_type=StationAuditLog.EventType.CREATED,
            message=f"Station '{self.object.name}' created.",
            user=self.request.user,
            ip_address=_get_client_ip(self.request),
        )
        messages.success(self.request, _("Station created successfully."))
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_title"] = _("Create Station")
        return context


TRACKED_FIELDS = [
    "name",
    "callsign",
    "description",
    "location_name",
    "latitude",
    "longitude",
    "altitude",
    "hardware_revision",
    "notes",
    "status",
]


class StationUpdateView(AdminOrOperatorRequiredMixin, UpdateView):
    model = Station
    template_name = "stations/station_form.html"
    form_class = StationForm

    def get_success_url(self):
        return reverse("stations:station_detail", kwargs={"pk": self.object.pk})

    def form_valid(self, form):
        old = Station.objects.get(pk=self.object.pk)
        response = super().form_valid(form)
        changes = _track_changes(old, self.object, TRACKED_FIELDS)
        if changes:
            field_names = ", ".join(changes.keys())
            StationAuditLog.log(
                station=self.object,
                event_type=StationAuditLog.EventType.UPDATED,
                message=f"Fields changed: {field_names}",
                changes=changes,
                user=self.request.user,
                ip_address=_get_client_ip(self.request),
            )
        messages.success(self.request, _("Station updated successfully."))
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_title"] = _("Edit Station")
        return context


class StationDeleteView(AdminOrOperatorRequiredMixin, DeleteView):
    model = Station
    template_name = "stations/station_confirm_delete.html"
    context_object_name = "station"
    success_url = reverse_lazy("stations:station_list")

    def form_valid(self, form):
        messages.success(self.request, _("Station deleted successfully."))
        return super().form_valid(form)


class StationPhotoUploadView(AdminOrOperatorRequiredMixin, CreateView):
    form_class = StationPhotoForm
    template_name = "stations/_photo_upload_form.html"

    def form_valid(self, form):
        station = get_object_or_404(Station, pk=self.kwargs["pk"])
        form.instance.station = station
        form.instance.uploaded_by = self.request.user
        messages.success(self.request, _("Photo uploaded successfully."))
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("stations:station_detail", kwargs={"pk": self.kwargs["pk"]})

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["station"] = get_object_or_404(Station, pk=self.kwargs["pk"])
        return context


class StationLogEntryCreateView(AdminOrOperatorRequiredMixin, CreateView):
    form_class = StationLogEntryForm
    template_name = "stations/_log_entry_form.html"

    def form_valid(self, form):
        station = get_object_or_404(Station, pk=self.kwargs["pk"])
        form.instance.station = station
        form.instance.created_by = self.request.user
        messages.success(self.request, _("Log entry added successfully."))
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("stations:station_detail", kwargs={"pk": self.kwargs["pk"]})

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["station"] = get_object_or_404(Station, pk=self.kwargs["pk"])
        return context


# ---------------------------------------------------------------------------
# Device Key views (Ed25519)
# ---------------------------------------------------------------------------


class StationGenerateKeyView(AdminOrOperatorRequiredMixin, View):
    """Generate an Ed25519 keypair and link the public key to the station."""

    def post(self, request, pk):
        station = get_object_or_404(Station, pk=pk)
        try:
            station.device_key
            messages.warning(
                request, _("This station already has an Ed25519 key. Revoke it first.")
            )
            return redirect("stations:station_detail", pk=pk)
        except DeviceKey.DoesNotExist:
            pass

        private_pem, public_b64 = DeviceKey.generate_keypair()

        DeviceKey.objects.create(
            station=station,
            current_public_key=public_b64,
        )

        # Store private key PEM in session for one-time display
        request.session[f"raw_private_key_{station.pk}"] = private_pem.decode("ascii")

        StationAuditLog.log(
            station=station,
            event_type=StationAuditLog.EventType.TOKEN_GENERATED,
            message="Ed25519 device key generated.",
            user=request.user,
            ip_address=_get_client_ip(request),
        )
        messages.success(request, _("Ed25519 device key generated. Save the private key now!"))
        return redirect("stations:station_detail", pk=pk)


class StationRevokeKeyView(AdminOrOperatorRequiredMixin, View):
    """Revoke (delete) the Ed25519 DeviceKey linked to the station."""

    def post(self, request, pk):
        station = get_object_or_404(Station, pk=pk)
        try:
            device_key = station.device_key
        except DeviceKey.DoesNotExist:
            messages.info(request, _("This station has no Ed25519 key to revoke."))
            return redirect("stations:station_detail", pk=pk)

        device_key.delete()

        StationAuditLog.log(
            station=station,
            event_type=StationAuditLog.EventType.TOKEN_REVOKED,
            message="Ed25519 device key revoked.",
            user=request.user,
            ip_address=_get_client_ip(request),
        )
        messages.success(request, _("Ed25519 device key revoked."))
        return redirect("stations:station_detail", pk=pk)


# ---------------------------------------------------------------------------
# StationTag views
# ---------------------------------------------------------------------------


class StationTagListView(AdminOrOperatorRequiredMixin, ListView):
    model = StationTag
    template_name = "stations/tag_list.html"
    context_object_name = "tags"
    paginate_by = 25


class StationTagCreateView(AdminOrOperatorRequiredMixin, CreateView):
    model = StationTag
    template_name = "stations/tag_form.html"
    form_class = StationTagForm
    success_url = reverse_lazy("stations:tag_list")

    def form_valid(self, form):
        messages.success(self.request, _("Tag created successfully."))
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_title"] = _("Create Tag")
        return context


class StationTagUpdateView(AdminOrOperatorRequiredMixin, UpdateView):
    model = StationTag
    template_name = "stations/tag_form.html"
    form_class = StationTagForm
    success_url = reverse_lazy("stations:tag_list")

    def form_valid(self, form):
        messages.success(self.request, _("Tag updated successfully."))
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_title"] = _("Edit Tag")
        return context


class StationTagDeleteView(AdminOrOperatorRequiredMixin, DeleteView):
    model = StationTag
    template_name = "stations/tag_confirm_delete.html"
    context_object_name = "tag"
    success_url = reverse_lazy("stations:tag_list")

    def form_valid(self, form):
        messages.success(self.request, _("Tag deleted successfully."))
        return super().form_valid(form)
