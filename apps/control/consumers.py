# apps/control/consumers.py
import asyncio
import json
import logging

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from . import constants, lock, registry

logger = logging.getLogger(__name__)


class AgentControlConsumer(AsyncWebsocketConsumer):
    """Agent-facing control WebSocket. Path: ws/agent/control/<station_id>/.

    Exactly one per station (one persistent agent Control-WS). Relays §7
    frames verbatim, updates the registry, and owns the lock sweep timer.
    """

    async def connect(self):
        self.station_id = self.scope["url_route"]["kwargs"]["station_id"]
        self.group_name = f"control_{self.station_id}"
        self.agent_group_name = f"control_{self.station_id}_agent"
        self.sweep_task = None

        from urllib.parse import parse_qs

        query_string = self.scope.get("query_string", b"").decode()
        params = {k: v[0] for k, v in parse_qs(query_string).items() if v}

        station = await self._get_station()
        if station is None:
            await self.close(code=4404)
            return
        if not await self._verify_agent(station, params):
            await self.close(code=4401)
            return

        await self.channel_layer.group_add(self.agent_group_name, self.channel_name)
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        self.sweep_task = asyncio.create_task(self._sweep_loop())

    async def disconnect(self, close_code):
        if getattr(self, "sweep_task", None):
            self.sweep_task.cancel()
            try:
                await self.sweep_task
            except asyncio.CancelledError:
                pass
            self.sweep_task = None

        try:
            station = await self._get_station()
            if station is not None:
                await self._mark_offline(station)
                freed = await self._force_free(station)
                await self._broadcast("control.agent_offline", {})
                if freed:
                    status = await self._lock_status(station)
                    await self._broadcast("control.lock", {"lock": status})
        finally:
            # Always release group membership for this dead channel, even if
            # the offline/free/broadcast steps above raise (e.g. channel layer down).
            await self.channel_layer.group_discard(self.agent_group_name, self.channel_name)
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        if text_data is None:
            return
        try:
            msg = json.loads(text_data)
        except json.JSONDecodeError:
            return
        mtype = msg.get("type")
        if mtype == "inventory":
            station = await self._get_station()
            if station is not None:
                await self._apply_inventory(station, msg.get("slots", []))
            await self._broadcast("control.inventory", {"msg": msg})
        elif mtype == "state":
            station = await self._get_station()
            if station is not None:
                await self._apply_state(
                    station, msg.get("slot"), msg.get("module"), msg.get("values", {})
                )
            await self._broadcast("control.state", {"msg": msg})
        elif mtype == "result":
            await self._broadcast("control.result", {"msg": msg})
        elif mtype == "event":
            await self._broadcast("control.event", {"msg": msg})
        # Unknown types are ignored (forward-compat).

    # -- server -> agent (channel handler) ------------------------------------

    async def control_to_agent(self, event):
        """A ControlConsumer relayed a §7 downstream frame -> send to agent."""
        await self.send(text_data=json.dumps(event["frame"]))

    # -- broadcasts we must ignore when echoed back to our own group ----------

    async def control_inventory(self, event):
        pass

    async def control_state(self, event):
        pass

    async def control_result(self, event):
        pass

    async def control_event(self, event):
        pass

    async def control_lock(self, event):
        pass

    async def control_agent_offline(self, event):
        pass

    async def control_control_requested(self, event):
        pass

    # -- sweep loop -----------------------------------------------------------

    async def _sweep_loop(self):
        try:
            while True:
                await asyncio.sleep(constants.LOCK_SWEEP_INTERVAL_SECONDS)
                try:
                    station = await self._get_station()
                    if station is None:
                        continue
                    freed = await self._sweep(station)
                    if freed:
                        status = await self._lock_status(station)
                        await self._broadcast("control.lock", {"lock": status})
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("control: lock sweep iteration failed; continuing")
        except asyncio.CancelledError:
            raise

    async def _broadcast(self, msg_type, payload):
        await self.channel_layer.group_send(self.group_name, {"type": msg_type, **payload})

    # -- DB helpers -----------------------------------------------------------

    @database_sync_to_async
    def _get_station(self):
        from apps.stations.models import Station

        try:
            return Station.objects.get(pk=self.station_id)
        except Station.DoesNotExist:
            return None

    @database_sync_to_async
    def _apply_inventory(self, station, slots):
        registry.apply_inventory(station, slots)

    @database_sync_to_async
    def _apply_state(self, station, slot, module_id, values):
        registry.apply_state(station, slot, module_id, values)

    @database_sync_to_async
    def _mark_offline(self, station):
        registry.mark_station_offline(station)

    @database_sync_to_async
    def _force_free(self, station):
        return lock.force_free(station)

    @database_sync_to_async
    def _sweep(self, station):
        from django.utils import timezone

        return lock.sweep_lock(station, now=timezone.now(), idle_seconds=constants.T_IDLE_SECONDS)

    @database_sync_to_async
    def _lock_status(self, station):
        return lock.lock_status(_lock_with_holder(station))

    @database_sync_to_async
    def _verify_agent(self, station, params):
        import hashlib
        import time

        from apps.api.models import DeviceKey

        signature = params.get("signature", "")
        timestamp = params.get("timestamp", "")
        if not signature or not timestamp:
            return False
        try:
            device_key = DeviceKey.objects.get(station=station, is_active=True)
        except DeviceKey.DoesNotExist:
            return False
        try:
            ts = float(timestamp)
        except (ValueError, TypeError):
            return False
        if time.time() - ts > 60 or ts > time.time() + 5:
            return False
        body_hash = hashlib.sha256(b"").hexdigest()
        signed_data = f"{timestamp}:{body_hash}".encode()
        if DeviceKey.verify_signature(device_key.current_public_key, signature, signed_data):
            return True
        if device_key.next_public_key and DeviceKey.verify_signature(
            device_key.next_public_key, signature, signed_data
        ):
            return True
        return False


def _lock_with_holder(station, scope="station"):
    """Fetch the lock with select_related('holder') to avoid an extra query."""
    from .models import ControlLock

    lock_obj, _ = ControlLock.objects.select_related("holder").get_or_create(
        station=station, scope=scope
    )
    return lock_obj


class ControlConsumer(AsyncWebsocketConsumer):
    """Browser-facing control WebSocket. Path: ws/control/<station_id>/.

    # NOTE: full implementation in Task 6
    This is a minimal placeholder to allow routing.py and config.asgi to import
    cleanly. Task 6 replaces this stub with the real viewer/controller logic.
    """

    async def connect(self):
        await self.close()
