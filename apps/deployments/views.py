from django.contrib import messages
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import CreateView, DetailView, ListView

from apps.stations.models import StationAuditLog
from apps.stations.views import AdminOrOperatorRequiredMixin

from .forms import DeploymentForm
from .models import Deployment, DeploymentResult


def _get_client_ip(request):
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    return xff.split(",")[0].strip() if xff else request.META.get("REMOTE_ADDR")


class DeploymentListView(AdminOrOperatorRequiredMixin, ListView):
    model = Deployment
    template_name = "deployments/deployment_list.html"
    context_object_name = "deployments"
    paginate_by = 25

    def get_queryset(self):
        qs = (
            super()
            .get_queryset()
            .select_related("image_release", "target_tag", "target_station", "created_by")
            .annotate(
                result_total=Count("results"),
                result_completed=Count(
                    "results",
                    filter=Q(results__status=DeploymentResult.Status.SUCCESS),
                ),
                result_failed=Count(
                    "results",
                    filter=Q(
                        results__status__in=[
                            DeploymentResult.Status.FAILED,
                            DeploymentResult.Status.ROLLED_BACK,
                        ]
                    ),
                ),
            )
        )

        status_filter = self.request.GET.get("status")
        if status_filter and status_filter in Deployment.Status.values:
            qs = qs.filter(status=status_filter)

        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["current_status"] = self.request.GET.get("status", "")
        context["status_choices"] = Deployment.Status.choices
        return context


class DeploymentDetailView(AdminOrOperatorRequiredMixin, DetailView):
    model = Deployment
    template_name = "deployments/deployment_detail.html"
    context_object_name = "deployment"

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("image_release", "target_tag", "target_station", "created_by")
            .prefetch_related("results__station")
        )


class DeploymentCreateView(AdminOrOperatorRequiredMixin, CreateView):
    model = Deployment
    form_class = DeploymentForm
    template_name = "deployments/deployment_form.html"

    def get_success_url(self):
        return reverse("deployments:deployment_detail", kwargs={"pk": self.object.pk})

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        response = super().form_valid(form)

        # Create DeploymentResult for each target station
        stations = self.object.get_target_stations()
        results = []
        for station in stations:
            results.append(
                DeploymentResult(
                    deployment=self.object,
                    station=station,
                    status=DeploymentResult.Status.PENDING,
                    previous_version=station.current_os_version or "",
                )
            )
        DeploymentResult.objects.bulk_create(results)

        # Update deployment status
        if results:
            self.object.status = Deployment.Status.IN_PROGRESS
            self.object.save(update_fields=["status", "updated_at"])

        # Audit log for each target station
        for station in stations:
            StationAuditLog.log(
                station=station,
                event_type=StationAuditLog.EventType.FIRMWARE_UPDATE,
                message=(
                    f"Deployment #{self.object.pk} created: "
                    f"{self.object.image_release} targeting {station.name}."
                ),
                user=self.request.user,
                ip_address=_get_client_ip(self.request),
            )

        messages.success(
            self.request,
            _("Deployment created with %(count)d target station(s).") % {"count": len(results)},
        )
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_title"] = _("Create Deployment")
        return context


class DeploymentCancelView(AdminOrOperatorRequiredMixin, View):
    """Cancel a deployment (POST only)."""

    def post(self, request, pk):
        deployment = get_object_or_404(Deployment, pk=pk)

        if deployment.status not in (Deployment.Status.PENDING, Deployment.Status.IN_PROGRESS):
            messages.warning(request, _("This deployment cannot be cancelled."))
            return redirect("deployments:deployment_detail", pk=pk)

        deployment.status = Deployment.Status.CANCELLED
        deployment.save(update_fields=["status", "updated_at"])

        # Cancel all pending results
        deployment.results.filter(status=DeploymentResult.Status.PENDING).update(
            status=DeploymentResult.Status.CANCELLED
        )

        # Broadcast update
        try:
            from apps.deployments.consumers import broadcast_deployment_status

            broadcast_deployment_status(deployment)
        except Exception:
            pass

        messages.success(request, _("Deployment cancelled."))
        return redirect("deployments:deployment_detail", pk=pk)
