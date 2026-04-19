from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.utils import timezone
from django.views.generic import TemplateView

from apps.api.models import DeviceKey
from apps.deployments.models import Deployment
from apps.firmware.models import FirmwareArtifact
from apps.monitoring.models import Alert
from apps.stations.models import Station, StationLogEntry

User = get_user_model()


class IndexView(LoginRequiredMixin, TemplateView):
    template_name = "dashboard/index.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        now = timezone.now()
        online_threshold = now - timedelta(minutes=2)

        context["station_count"] = Station.objects.count()
        context["stations_online"] = Station.objects.filter(
            last_seen__gte=online_threshold,
        ).count()
        context["firmware_count"] = FirmwareArtifact.objects.count()
        context["recent_log_entries"] = StationLogEntry.objects.select_related("station").order_by(
            "-created_at"
        )[:5]
        context["deployment_count"] = Deployment.objects.count()
        context["recent_deployments"] = Deployment.objects.select_related(
            "image_release",
        ).order_by("-created_at")[:5]
        context["stations"] = Station.objects.all().order_by("name")
        context["active_alerts_count"] = Alert.objects.filter(is_resolved=False).count()
        context["user_count"] = User.objects.count()
        context["device_key_count"] = DeviceKey.objects.filter(is_active=True).count()

        return context
