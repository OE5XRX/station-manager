from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import DetailView, FormView, ListView

from apps.accounts.permissions import AreaPermissionMixin

from .forms import DeploymentForm
from .models import Deployment, DeploymentResult
from .services import NoEligibleStationsError, create_deployment


def _get_client_ip(request):
    from apps.accounts.client_ip import client_ip

    return client_ip(request)


class DeploymentListView(AreaPermissionMixin, ListView):
    area = "deployments"
    model = Deployment
    template_name = "deployments/deployment_list.html"
    context_object_name = "deployments"
    paginate_by = 25

    def get_queryset(self):
        qs = (
            super()
            .get_queryset()
            .select_related("target_group", "target_station", "created_by")
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
            # The Count() aggregations force a GROUP BY, and Django drops the
            # model's Meta.ordering ("-created_at") once that happens — leaving
            # the list in arbitrary DB order (and triggering
            # UnorderedObjectListWarning under pagination). Re-assert the order
            # explicitly. "-pk" is a tiebreaker so rows sharing a created_at
            # second stay deterministic across paginated pages.
            .order_by("-created_at", "-pk")
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


class DeploymentDetailView(AreaPermissionMixin, DetailView):
    area = "deployments"
    model = Deployment
    template_name = "deployments/deployment_detail.html"
    context_object_name = "deployment"

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("target_group", "target_station", "created_by")
            .prefetch_related("results__station", "results__image_release")
        )


class DeploymentCreateView(AreaPermissionMixin, FormView):
    area = "deployments"
    verb = "manage"
    form_class = DeploymentForm
    template_name = "deployments/deployment_form.html"

    def form_valid(self, form):
        data = form.cleaned_data
        release = data["image_release"]
        try:
            deployment = create_deployment(
                tag=release.tag,
                channel=release.channel,
                target_type=data["target_type"],
                target_group=data.get("target_group"),
                target_station=data.get("target_station"),
                user=self.request.user,
                strategy=data["strategy"],
                phase_config=data["phase_config"],
                ip_address=_get_client_ip(self.request),
            )
        except NoEligibleStationsError as exc:
            form.add_error(None, str(exc))
            return self.form_invalid(form)

        messages.success(
            self.request,
            _("Deployment created with %(count)d target station(s).")
            % {"count": deployment.results.count()},
        )
        return redirect("deployments:deployment_detail", pk=deployment.pk)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_title"] = _("Create Deployment")
        return context


class DeploymentCancelView(AreaPermissionMixin, View):
    """Cancel a deployment (POST only)."""

    area = "deployments"
    verb = "manage"

    def post(self, request, pk):
        deployment = get_object_or_404(Deployment, pk=pk)

        if deployment.status not in (Deployment.Status.PENDING, Deployment.Status.IN_PROGRESS):
            messages.warning(request, _("This deployment cannot be cancelled."))
            return redirect("deployments:deployment_detail", pk=pk)

        # Cascade the cancel onto every non-terminal child result, not
        # just PENDING. An in-flight result (DOWNLOADING / INSTALLING /
        # REBOOTING / VERIFYING) left at its old status stays in
        # ``DeploymentResult.ACTIVE_STATUSES`` and blocks every future
        # upgrade for that station with ActiveDeploymentConflictError —
        # even though the parent deployment was cancelled long ago. The
        # orphan persists until manually reconciled.
        with transaction.atomic():
            deployment.status = Deployment.Status.CANCELLED
            deployment.save(update_fields=["status", "updated_at"])
            deployment.results.filter(status__in=DeploymentResult.NON_TERMINAL_STATUSES).update(
                status=DeploymentResult.Status.CANCELLED,
                completed_at=timezone.now(),
                error_message="Parent deployment was cancelled.",
            )

        # Broadcast update
        try:
            from apps.deployments.consumers import broadcast_deployment_status

            broadcast_deployment_status(deployment)
        except Exception:
            pass

        messages.success(request, _("Deployment cancelled."))
        return redirect("deployments:deployment_detail", pk=pk)
