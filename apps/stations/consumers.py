import json
import logging

from asgiref.sync import async_to_sync, sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.layers import get_channel_layer

from apps.accounts.permissions import AreaConsumerMixin, can

logger = logging.getLogger(__name__)

GROUP_NAME = "station_status"


class StationStatusConsumer(AreaConsumerMixin, AsyncWebsocketConsumer):
    """WebSocket consumer that pushes live station status updates to clients.

    Connect needs ``stations.view``. IP addresses are Staff-only
    (``stations.manage``): the flag is computed at connect and on every
    periodic re-check from a fresh user, and ``last_ip_address`` is stripped
    from frames for everyone else.
    """

    area = "stations"
    can_see_ip = False

    async def connect(self):
        user = self.scope.get("user")
        if not user or user.is_anonymous:
            await self.close(code=4401)
            return
        # Accept first so the forbidden frame reaches the browser.
        await self.accept()
        if not await self.area_connect_gate():
            return
        await self.on_periodic_recheck(await self.fresh_user())
        await self.channel_layer.group_add(GROUP_NAME, self.channel_name)
        self.start_periodic_recheck()

    async def on_periodic_recheck(self, user):
        self.can_see_ip = user is not None and await sync_to_async(can)(user, "stations", "manage")

    async def disconnect(self, close_code):
        await self.stop_periodic_recheck()
        await self.channel_layer.group_discard(GROUP_NAME, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        # Server-push only; ignore any client messages.
        pass

    async def station_update(self, event):
        """Handle station_update messages from the channel layer group."""
        data = event["data"]
        if not self.can_see_ip:
            data = {k: v for k, v in data.items() if k != "last_ip_address"}
        await self.send(text_data=json.dumps(data))


def broadcast_station_status(station):
    """Broadcast a station's current status to all connected WebSocket clients.

    Call this from synchronous code (e.g. DRF views) after updating a station.
    """
    channel_layer = get_channel_layer()
    if channel_layer is None:
        logger.warning("Channel layer not available; skipping broadcast.")
        return

    data = {
        "id": station.id,
        "name": station.name,
        "callsign": station.callsign,
        "status": station.status,
        "last_seen": station.last_seen.isoformat() if station.last_seen else None,
        "current_os_version": station.current_os_version or "",
        "last_ip_address": station.last_ip_address or "",
    }

    async_to_sync(channel_layer.group_send)(
        GROUP_NAME,
        {
            "type": "station_update",
            "data": data,
        },
    )
