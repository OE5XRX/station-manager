import json
import logging

from asgiref.sync import async_to_sync
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.layers import get_channel_layer

from apps.accounts.permissions import AreaConsumerMixin
from apps.deployments.models import Deployment, DeploymentResult

logger = logging.getLogger(__name__)

GROUP_NAME = "deployment_status"


def _status_label(enum_cls, code):
    try:
        return str(enum_cls(code).label)
    except ValueError:
        return str(code)


def with_status_labels(data):
    """Return a copy of a broadcast payload plus translated ``status_label`` fields.

    The machine ``status`` codes stay untouched (backwards compatible); unknown
    codes fall back to the raw code.
    """
    out = dict(data)
    if "status" in out:
        out["status_label"] = _status_label(Deployment.Status, out["status"])
    result = out.get("result")
    if isinstance(result, dict) and "status" in result:
        out["result"] = {**result, "status_label": _status_label(DeploymentResult.Status, result["status"])}
    return out


class DeploymentStatusConsumer(AreaConsumerMixin, AsyncWebsocketConsumer):
    """WebSocket consumer for real-time deployment status updates (deployments.view)."""

    area = "deployments"

    async def connect(self):
        user = self.scope.get("user")
        if not user or user.is_anonymous:
            await self.close(code=4401)
            return
        # Accept first so the forbidden frame reaches the browser; join the group
        # only after the gate passed.
        await self.accept()
        if not await self.area_connect_gate():
            return
        await self.channel_layer.group_add(GROUP_NAME, self.channel_name)
        self.start_periodic_recheck()

    async def disconnect(self, close_code):
        await self.stop_periodic_recheck()
        await self.channel_layer.group_discard(GROUP_NAME, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        pass  # Server-push only

    async def deployment_update(self, event):
        # Runs under the connected user's language (AreaConsumerMixin.dispatch),
        # so the labels are translated per listener, not per broadcaster.
        await self.send(text_data=json.dumps(with_status_labels(event["data"])))


def broadcast_deployment_status(deployment, result=None):
    """Broadcast a deployment's current status to all connected WebSocket clients.

    Call this from synchronous code (e.g. DRF views) after updating a deployment.
    """
    channel_layer = get_channel_layer()
    if channel_layer is None:
        logger.warning("Channel layer not available; skipping broadcast.")
        return

    progress = deployment.progress
    data = {
        "deployment_id": deployment.id,
        "status": deployment.status,
        "progress": progress,
    }

    if result is not None:
        image = result.image_release
        data["result"] = {
            "id": result.id,
            "station_id": result.station_id,
            "station_name": result.station.name if hasattr(result, "station") else "",
            "status": result.status,
            "error_message": result.error_message or "",
            "started_at": result.started_at.isoformat() if result.started_at else None,
            "completed_at": result.completed_at.isoformat() if result.completed_at else None,
            "tag": image.tag if image else "",
            "machine": image.machine if image else "",
            "channel": image.channel if image else "",
        }

    async_to_sync(channel_layer.group_send)(
        GROUP_NAME,
        {
            "type": "deployment_update",
            "data": data,
        },
    )
