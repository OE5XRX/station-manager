from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Count, Min, Q
from django.db.models.deletion import ProtectedError
from django.http import HttpResponseRedirect
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

from apps.accounts.permissions import AreaPermissionMixin, can
from apps.api.models import DeviceKey
from apps.deployments.models import DeploymentResult
from apps.images.models import ImageRelease
from apps.module_firmware import drain as module_drain
from apps.provisioning.models import ProvisioningJob
from apps.rollouts import services as rollout_services
from apps.rollouts.targeting import reason_message, resolve_upgrade

from .forms import (
    RolloutGroupForm,
    StationForm,
    StationLogEntryForm,
    StationPhotoForm,
    StationTagForm,
)
from .inventory import inventory_for_viewer
from .models import (
    Region,
    RolloutGroup,
    Station,
    StationAssignment,
    StationAuditLog,
    StationTag,
)

User = get_user_model()


def _get_client_ip(request):
    from apps.accounts.client_ip import client_ip

    return client_ip(request)


def _audit_group(request, group, action, before=None):
    rollout_services.audit_group(
        group,
        action,
        actor=request.user,
        ip_address=_get_client_ip(request),
        suffix=f" by {request.user}",
        before=before,
    )


def _track_changes(old_instance, new_instance, fields):
    """Compare two model instances and return a dict of changed fields."""
    changes = {}
    for field in fields:
        old_val = str(getattr(old_instance, field, "") or "")
        new_val = str(getattr(new_instance, field, "") or "")
        if old_val != new_val:
            changes[field] = {"old": old_val, "new": new_val}
    return changes


# ---------------------------------------------------------------------------
# Station views
# ---------------------------------------------------------------------------


class StationListView(AreaPermissionMixin, ListView):
    area = "stations"
    model = Station
    template_name = "stations/station_list.html"
    context_object_name = "stations"
    paginate_by = 25

    def get_template_names(self):
        if self.request.htmx:
            return ["stations/_station_table.html"]
        return [self.template_name]

    def get_queryset(self):
        qs = super().get_queryset().select_related("rollout_group").prefetch_related("tags")
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


class StationDetailView(AreaPermissionMixin, DetailView):
    area = "stations"
    model = Station
    template_name = "stations/station_detail.html"
    context_object_name = "station"

    def get_queryset(self):
        prefetch = ["tags", "photos", "log_entries"]
        # The audit tab exposes IP addresses: only query it for managers.
        if can(self.request.user, "stations", "manage"):
            prefetch.append("audit_logs")
        return (
            super()
            .get_queryset()
            .select_related(
                "device_key", "inventory", "telemetry", "current_image_release", "rollout_group"
            )
            .prefetch_related(*prefetch)
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
        user = self.request.user
        context["drain"] = module_drain.drain_status(self.object)
        inventory = getattr(self.object, "inventory", None)
        context["inventory_data"] = inventory_for_viewer(
            inventory.data if inventory else None, can(user, "stations", "manage")
        )
        # Provisioning section (provisioning:manage)
        if can(user, "provisioning", "manage"):
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
        # Upgrade card (deployments:manage; the card is hidden otherwise)
        if can(user, "deployments", "manage"):
            upgrade = resolve_upgrade(self.object)
            context["upgrade"] = upgrade
            context["upgrade_message"] = reason_message(upgrade, self.object)
            context["recent_deployments"] = list(
                DeploymentResult.objects.filter(station=self.object)
                .select_related("deployment__image_release")
                .order_by("-pk")[:5]
            )
        if can(user, "topology", "manage"):
            # Topology card.
            context["all_regions"] = Region.objects.order_by("name")
            # Only the columns the topology card actually renders are
            # needed; defer the rest to keep this list lean on installs
            # with many users.
            context["all_users"] = (
                User.objects.exclude(membership_level=User.MembershipLevel.APPLICANT)
                .only("id", "username", "membership_level")
                .order_by("username")
            )
            topology_assignments = list(
                self.object.assignments.select_related("user").order_by("role", "user__username")
            )
            context["station_admin"] = next(
                (a for a in topology_assignments if a.role == StationAssignment.Role.ADMIN),
                None,
            )
            context["station_maintainers"] = [
                a for a in topology_assignments if a.role == StationAssignment.Role.MAINTAINER
            ]
        return context


class StationCreateView(AreaPermissionMixin, CreateView):
    area = "stations"
    verb = "manage"
    model = Station
    template_name = "stations/station_form.html"
    form_class = StationForm

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "user": self.request.user}

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
        if self.object.rollout_group:
            StationAuditLog.log(
                station=self.object,
                event_type=StationAuditLog.EventType.ROLLOUT_GROUP_CHANGED,
                message=f"Rollout group: none → {self.object.rollout_group.name} "
                f"by {self.request.user}",
                changes={"rollout_group": {"old": "none", "new": self.object.rollout_group.name}},
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
    "tx_audio_ceiling_dbfs",
    "notes",
    "status",
]


class StationUpdateView(AreaPermissionMixin, UpdateView):
    area = "stations"
    verb = "manage"
    model = Station
    template_name = "stations/station_form.html"
    form_class = StationForm

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "user": self.request.user}

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
        if old.rollout_group_id != self.object.rollout_group_id:
            old_name = old.rollout_group.name if old.rollout_group else "none"
            new_name = self.object.rollout_group.name if self.object.rollout_group else "none"
            StationAuditLog.log(
                station=self.object,
                event_type=StationAuditLog.EventType.ROLLOUT_GROUP_CHANGED,
                message=f"Rollout group: {old_name} → {new_name} by {self.request.user}",
                changes={"rollout_group": {"old": old_name, "new": new_name}},
                user=self.request.user,
                ip_address=_get_client_ip(self.request),
            )
        messages.success(self.request, _("Station updated successfully."))
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_title"] = _("Edit Station")
        return context


class StationDeleteView(AreaPermissionMixin, DeleteView):
    area = "stations"
    verb = "manage"
    model = Station
    template_name = "stations/station_confirm_delete.html"
    context_object_name = "station"
    success_url = reverse_lazy("stations:station_list")

    def form_valid(self, form):
        messages.success(self.request, _("Station deleted successfully."))
        return super().form_valid(form)


class StationPhotoUploadView(AreaPermissionMixin, CreateView):
    area = "stations"
    verb = "manage"
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


class StationLogEntryCreateView(AreaPermissionMixin, CreateView):
    area = "stations"
    verb = "manage"
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


class StationGenerateKeyView(AreaPermissionMixin, View):
    """Generate an Ed25519 keypair and link the public key to the station."""

    area = "stations"
    verb = "manage"

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


class StationRevokeKeyView(AreaPermissionMixin, View):
    """Revoke (delete) the Ed25519 DeviceKey linked to the station."""

    area = "stations"
    verb = "manage"

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


class StationTagListView(AreaPermissionMixin, ListView):
    area = "stations"
    model = StationTag
    template_name = "stations/tag_list.html"
    context_object_name = "tags"
    paginate_by = 25


class StationTagCreateView(AreaPermissionMixin, CreateView):
    area = "stations"
    verb = "manage"
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


class StationTagUpdateView(AreaPermissionMixin, UpdateView):
    area = "stations"
    verb = "manage"
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


class StationTagDeleteView(AreaPermissionMixin, DeleteView):
    area = "stations"
    verb = "manage"
    model = StationTag
    template_name = "stations/tag_confirm_delete.html"
    context_object_name = "tag"
    success_url = reverse_lazy("stations:tag_list")

    def form_valid(self, form):
        messages.success(self.request, _("Tag deleted successfully."))
        return super().form_valid(form)


# ---------------------------------------------------------------------------
# RolloutGroup views
# ---------------------------------------------------------------------------


class RolloutGroupListView(AreaPermissionMixin, ListView):
    area = "stations"
    model = RolloutGroup
    template_name = "stations/group_list.html"
    context_object_name = "groups"
    paginate_by = 25

    def get_queryset(self):
        # Station count and sequence position in the same query (no N+1).
        # The rollout sequence is a singleton, so a group has at most one
        # entry and Min() is just "its position, or NULL".
        return (
            super()
            .get_queryset()
            .annotate(
                station_count=Count("stations", distinct=True),
                sequence_position=Min("rollout_entries__position"),
            )
            .order_by("name")
        )


class RolloutGroupCreateView(AreaPermissionMixin, CreateView):
    area = "stations"
    verb = "manage"
    model = RolloutGroup
    template_name = "stations/group_form.html"
    form_class = RolloutGroupForm
    success_url = reverse_lazy("stations:group_list")

    def form_valid(self, form):
        with transaction.atomic():
            response = super().form_valid(form)
            _audit_group(self.request, self.object, "created")
        messages.success(self.request, _("Rollout group created successfully."))
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_title"] = _("Create rollout group")
        return context


class RolloutGroupUpdateView(AreaPermissionMixin, UpdateView):
    area = "stations"
    verb = "manage"
    model = RolloutGroup
    template_name = "stations/group_form.html"
    form_class = RolloutGroupForm
    success_url = reverse_lazy("stations:group_list")

    def form_valid(self, form):
        # form.instance already carries the POSTed values; diff against the DB row.
        before = rollout_services.group_snapshot(RolloutGroup.objects.get(pk=form.instance.pk))
        with transaction.atomic():
            response = super().form_valid(form)
            if form.has_changed():
                _audit_group(self.request, self.object, "updated", before)
        messages.success(self.request, _("Rollout group updated successfully."))
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_title"] = _("Edit rollout group")
        return context


class RolloutGroupDeleteView(AreaPermissionMixin, DeleteView):
    """Delete an unreferenced group. Every FK to a group is PROTECT, so the
    confirm page lists what has to be moved off first instead of a button."""

    area = "stations"
    verb = "manage"
    model = RolloutGroup
    template_name = "stations/group_confirm_delete.html"
    context_object_name = "group"
    success_url = reverse_lazy("stations:group_list")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        refs = rollout_services.group_references(self.object)
        context["refs"] = refs
        context["blocked"] = any(refs.values())
        context["deployment_count"] = self.object.deployments.count()
        return context

    def form_valid(self, form):
        group = self.object
        pk = group.pk  # delete() sets it to None before a deferred-FK IntegrityError
        try:
            with transaction.atomic():
                _audit_group(self.request, group, "deleted")
                group.delete()
        except (ProtectedError, IntegrityError):
            # IntegrityError: a concurrent write referenced the group between
            # the PROTECT check and commit (deferred FK on PostgreSQL).
            messages.error(
                self.request,
                _("Rollout group is still in use and cannot be deleted."),
            )
            return redirect("stations:group_delete", pk=pk)
        messages.success(self.request, _("Rollout group deleted successfully."))
        return HttpResponseRedirect(self.get_success_url())
