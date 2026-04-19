import json
import logging

from asgiref.sync import async_to_sync
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.layers import get_channel_layer

logger = logging.getLogger(__name__)

GROUP_NAME = "deployment_status"


class DeploymentStatusConsumer(AsyncWebsocketConsumer):
    """WebSocket consumer for real-time deployment status updates."""

    async def connect(self):
        user = self.scope.get("user")
        if not user or user.is_anonymous:
            await self.close(code=4401)
            return
        await self.channel_layer.group_add(GROUP_NAME, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(GROUP_NAME, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        pass  # Server-push only

    async def deployment_update(self, event):
        await self.send(text_data=json.dumps(event["data"]))


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
        image = deployment.image_release
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
        }

    async_to_sync(channel_layer.group_send)(
        GROUP_NAME,
        {
            "type": "deployment_update",
            "data": data,
        },
    )
