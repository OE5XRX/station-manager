# apps/control/consumers.py
import asyncio
import json
import logging

import django.conf
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from . import constants, lock, registry

logger = logging.getLogger(__name__)


class AgentControlConsumer(AsyncWebsocketConsumer):
    """Agent-facing control WebSocket. Path: ws/agent/control/<station_id>/.

    Relays §7 frames verbatim, updates the registry, and owns the lock sweep
    timer. A station runs a single agent process holding one persistent
    Control-WS, so in practice there is one connection per station — but that
    is a deployment invariant, not enforced here (matching the tunnel
    AgentTerminalConsumer). A brief overlap during an agent reconnect is
    tolerated: both connections share the agent group and the registry/lock
    state is authoritative in the DB.
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

    Access-controlled (can_use_station). Relays holder commands/PTT and lock
    actions to the agent; pushes state/inventory/result/event + lock status to
    all viewers.

    Audit note: CONTROL_PTT keepalives arrive ~1/sec — auditing every one would
    spam the log. Instead we audit command frames whose capability == "ptt" (i.e.
    the PTT-on command itself). ptt_keepalive frames are NOT audited.
    """

    async def connect(self):
        self.station_id = self.scope["url_route"]["kwargs"]["station_id"]
        self.group_name = f"control_{self.station_id}"
        self.agent_group_name = f"control_{self.station_id}_agent"
        self.pending = {}  # request_id -> asyncio.Task (command timeout)
        self.user = self.scope.get("user")

        await self.accept()

        if not self.user or self.user.is_anonymous:
            await self._reject(4401, "Not signed in — please sign in again")
            return
        station = await self._get_station()
        if station is None:
            await self._reject(4404, "Station not found")
            return
        if not await self._can_use(station):
            await self._reject(4403, "You are not permitted to control this station")
            return

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        # Reconnect within grace keeps a held lock.
        await self._holder_reconnected(station)
        # Initial snapshot + lock status to just this browser, as TWO frames.
        # The initial lock uses the IDENTICAL {type:"lock",...} shape as every
        # later lock mutation, so D5 (the UI) wires a single lock handler and
        # renders the lock panel from the very first frame on load.
        await self.send(
            text_data=json.dumps({"type": "inventory", "modules": await self._snapshot(station)})
        )
        await self._send_lock_status(station)

    async def _reject(self, code, reason):
        """Accept-then-error reject so the browser sees a real reason."""
        try:
            await self.send(
                text_data=json.dumps({"type": "error", "reason": reason, "code": code})
            )
        finally:
            await self.close(code=code)

    async def disconnect(self, close_code):
        pending = list(self.pending.values())
        self.pending.clear()
        for task in pending:
            task.cancel()
        # Await the cancelled tasks so they finish tearing down before the
        # consumer goes away — otherwise asyncio logs "Task was destroyed but
        # it is pending" on shutdown/reload and cleanup may be left unfinished.
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        station = await self._get_station()
        if station is not None and self.user and not self.user.is_anonymous:
            await self._holder_disconnected(station)
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        if text_data is None:
            return
        try:
            msg = json.loads(text_data)
        except json.JSONDecodeError:
            return
        mtype = msg.get("type")
        station = await self._get_station()
        if station is None:
            return

        if mtype == "command":
            await self._handle_command(station, msg)
        elif mtype == "ptt_keepalive":
            if await self._touch_if_holder(station):
                await self._relay(msg)
            else:
                await self._error(msg.get("request_id"), "not_locked", "You do not hold the lock")
        elif mtype in ("subscribe", "unsubscribe"):
            # Any viewer may subscribe/unsubscribe — access already checked at connect.
            await self._relay(msg)
        elif mtype == "lock_acquire":
            await self._lock_acquire(station)
        elif mtype == "lock_release":
            await self._lock_release(station)
        elif mtype == "lock_request":
            await self._lock_request(station)
        elif mtype == "lock_transfer":
            await self._lock_transfer(station, msg.get("to_user_id"))
        elif mtype == "lock_preempt":
            await self._lock_preempt(station)
        # Unknown types are ignored (forward-compat).

    # -- command + timeout ----------------------------------------------------

    async def _handle_command(self, station, msg):
        if not await self._touch_if_holder(station):
            await self._error(msg.get("request_id"), "not_locked", "You do not hold the lock")
            return
        await self._relay(msg)
        request_id = msg.get("request_id")
        if request_id is not None:
            self.pending[request_id] = asyncio.create_task(self._command_timeout(request_id))
        # Audit command frames. A PTT key (capability=="ptt") is logged under
        # the dedicated CONTROL_PTT event so audit trails can tell PTT apart
        # from other commands; ptt_keepalive frames stay unaudited (log-spam).
        capability = msg.get("capability")
        event_type = "control_ptt" if capability == "ptt" else "control_command"
        await self._audit(
            station,
            event_type,
            f"{self.user.username} {msg.get('op')} {capability}",
        )

    async def _command_timeout(self, request_id):
        """Fire a timeout error to the browser if no result arrives in time.

        Reads the timeout from settings at call-time (not the module-level
        constant) so the Task 7 settings fixture can override it without
        an importlib reload.
        """
        try:
            timeout = getattr(django.conf.settings, "CONTROL_COMMAND_TIMEOUT_SECONDS", 10)
            await asyncio.sleep(timeout)
            await self.send(
                text_data=json.dumps(
                    {
                        "type": "error",
                        "request_id": request_id,
                        "error": {"code": "timeout", "msg": "No result from agent"},
                    }
                )
            )
            self.pending.pop(request_id, None)
        except asyncio.CancelledError:
            raise

    async def _relay(self, frame):
        await self.channel_layer.group_send(
            self.agent_group_name, {"type": "control.to_agent", "frame": frame}
        )

    async def _error(self, request_id, code, msg):
        await self.send(
            text_data=json.dumps(
                {"type": "error", "request_id": request_id, "error": {"code": code, "msg": msg}}
            )
        )

    # -- lock actions ---------------------------------------------------------

    async def _lock_acquire(self, station):
        if await self._acquire(station):
            await self._audit(
                station, "control_lock_acquired", f"{self.user.username} acquired control"
            )
        await self._broadcast_lock(station)

    async def _lock_release(self, station):
        if await self._release(station):
            await self._audit(
                station, "control_lock_released", f"{self.user.username} released control"
            )
        await self._broadcast_lock(station)

    async def _lock_request(self, station):
        holder = await self._request(station)
        if holder is not None:
            await self.channel_layer.group_send(
                self.group_name,
                {
                    "type": "control.control_requested",
                    "holder_id": holder.holder_id,
                    "requester": {"id": self.user.id, "username": self.user.username},
                },
            )

    async def _lock_transfer(self, station, to_user_id):
        if to_user_id is not None and await self._transfer(station, to_user_id):
            await self._audit(
                station,
                "control_lock_transferred",
                f"{self.user.username} -> user {to_user_id}",
            )
        await self._broadcast_lock(station)

    async def _lock_preempt(self, station):
        if not await self._can_administer(station):
            await self._error(None, "forbidden", "Admin rights required to preempt")
            return
        await self._preempt(station)
        await self._audit(
            station, "control_lock_preempted", f"{self.user.username} preempted control"
        )
        await self._broadcast_lock(station)

    async def _broadcast_lock(self, station):
        status = await self._lock_status(station)
        await self.channel_layer.group_send(
            self.group_name, {"type": "control.lock", "lock": status}
        )

    async def _send_lock_status(self, station):
        status = await self._lock_status(station)
        await self._push_lock(status)

    async def _push_lock(self, status):
        payload = dict(status)
        payload["type"] = "lock"
        payload["you_hold"] = bool(self.user and status.get("holder_id") == self.user.id)
        await self.send(text_data=json.dumps(payload))

    # -- channel handlers (broadcast -> this browser) -------------------------

    async def control_state(self, event):
        await self.send(text_data=json.dumps(event["msg"]))

    async def control_inventory(self, event):
        await self.send(text_data=json.dumps(event["msg"]))

    async def control_result(self, event):
        msg = event["msg"]
        rid = msg.get("request_id")
        task = self.pending.pop(rid, None)
        if task is not None:
            task.cancel()
        await self.send(text_data=json.dumps(msg))

    async def control_event(self, event):
        await self.send(text_data=json.dumps(event["msg"]))

    async def control_lock(self, event):
        await self._push_lock(event["lock"])

    async def control_agent_offline(self, event):
        await self.send(text_data=json.dumps({"type": "agent_offline"}))

    async def control_control_requested(self, event):
        # Only the current holder should be prompted.
        if self.user and event.get("holder_id") == self.user.id:
            await self.send(
                text_data=json.dumps(
                    {"type": "control_requested", "requester": event["requester"]}
                )
            )

    async def control_to_agent(self, event):
        pass  # not for browsers

    # -- DB helpers -----------------------------------------------------------

    @database_sync_to_async
    def _get_station(self):
        from apps.stations.models import Station

        try:
            return Station.objects.get(pk=self.station_id)
        except Station.DoesNotExist:
            return None

    @database_sync_to_async
    def _can_use(self, station):
        return (
            bool(self.user) and not self.user.is_anonymous and self.user.can_use_station(station)
        )

    @database_sync_to_async
    def _can_administer(self, station):
        return (
            self.user.is_admin
            or self.user.is_station_admin(station)
            or self.user.can_administer_station(station)
        )

    @database_sync_to_async
    def _snapshot(self, station):
        from .models import StationModule

        out = []
        for m in StationModule.objects.filter(station=station):
            out.append(
                {
                    "slot": m.slot,
                    "module": m.module_id,
                    "identity": {"type": m.type, "model": m.model, "version": m.version},
                    "capabilities": m.capability_descriptor,
                    "state": m.last_state,
                    "online": m.online,
                }
            )
        return out

    @database_sync_to_async
    def _acquire(self, station):
        return lock.acquire(station, self.user)

    @database_sync_to_async
    def _release(self, station):
        return lock.release(station, self.user)

    @database_sync_to_async
    def _request(self, station):
        return lock.request_control(station, self.user)

    @database_sync_to_async
    def _transfer(self, station, to_user_id):
        return lock.transfer(station, self.user, to_user_id)

    @database_sync_to_async
    def _preempt(self, station):
        return lock.preempt(station, self.user)

    @database_sync_to_async
    def _touch_if_holder(self, station):
        return lock.touch(station, self.user)

    @database_sync_to_async
    def _holder_disconnected(self, station):
        lock.holder_disconnected(station, self.user, constants.RECONNECT_GRACE_SECONDS)

    @database_sync_to_async
    def _holder_reconnected(self, station):
        lock.holder_reconnected(station, self.user)

    @database_sync_to_async
    def _lock_status(self, station):
        return lock.lock_status(_lock_with_holder(station))

    @database_sync_to_async
    def _audit(self, station, event_type, message):
        from apps.stations.models import StationAuditLog

        StationAuditLog.log(
            station=station, event_type=event_type, message=message, user=self.user
        )
