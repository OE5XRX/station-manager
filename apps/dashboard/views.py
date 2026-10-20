from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
from django.views.generic import TemplateView

from apps.accounts.models import AccountAuditLog
from apps.accounts.permissions import AreaPermissionMixin, can
from apps.api.models import DeviceKey
from apps.deployments.models import Deployment
from apps.module_firmware import drain as module_drain
from apps.module_firmware.models import Module
from apps.monitoring.models import Alert
from apps.stations.models import Station, StationLogEntry

User = get_user_model()


class IndexView(AreaPermissionMixin, TemplateView):
    area = "dashboard"
    template_name = "dashboard/index.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        now = timezone.now()
        online_threshold = now - timedelta(minutes=2)

        context["station_count"] = Station.objects.count()
        context["stations_online"] = Station.objects.filter(
            last_seen__gte=online_threshold,
        ).count()
        context["recent_log_entries"] = StationLogEntry.objects.select_related("station").order_by(
            "-created_at"
        )[:5]
        context["deployment_count"] = Deployment.objects.count()
        context["recent_deployments"] = Deployment.objects.order_by("-created_at")[:5]
        context["stations"] = Station.objects.all().order_by("name")
        context["active_alerts_count"] = Alert.objects.filter(is_resolved=False).count()
        context["user_count"] = User.objects.count()
        context["device_key_count"] = DeviceKey.objects.filter(is_active=True).count()
        context["module_stats"] = {
            "total": Module.objects.count(),
            "unregistered": Module.objects.filter(
                registration_status=Module.Registration.UNREGISTERED
            ).count(),
            "attention": Module.objects.filter(
                lifecycle_status__in=[Module.Lifecycle.DEFECT, Module.Lifecycle.IN_LAB]
            ).count(),
            "quarantined": Module.objects.filter(
                firmware_convergence=Module.Convergence.QUARANTINED
            ).count(),
        }
        context["quarantined_modules"] = (
            Module.objects.filter(firmware_convergence=Module.Convergence.QUARANTINED)
            .select_related("module_type")
            .order_by("uid")
        )
        # Module uids / flash errors are module data: gated on modules.view, not just
        # dashboard.view. Station visibility is not topology-scoped anywhere (every
        # stations.view holder sees the whole fleet, see the Live fleet table above).
        if can(self.request.user, "modules", "view"):
            context["fw_waiting"] = module_drain.draining_stations()
            context["fw_dfu_stuck"] = module_drain.dfu_stuck_modules()
        else:
            context["fw_waiting"] = []
            context["fw_dfu_stuck"] = []
        if can(self.request.user, "audit", "view"):
            context["denials_24h"] = AccountAuditLog.objects.filter(
                event_type=AccountAuditLog.EventType.PERMISSION_DENIED,
                created_at__gte=now - timedelta(hours=24),
            ).count()

        return context
