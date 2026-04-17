import json
import logging

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.utils import timezone

logger = logging.getLogger(__name__)

MAX_SESSIONS_PER_STATION = 2


class TerminalConsumer(AsyncWebsocketConsumer):
    """Browser-side WebSocket consumer for terminal sessions.

    Path: ws/terminal/<station_id>/
    Bridges user keystrokes to the station agent and streams output back.
    """

    async def connect(self):
        self.station_id = self.scope["url_route"]["kwargs"]["station_id"]
        self.group_name = f"terminal_{self.station_id}"
        self.session = None

        user = self.scope.get("user")
        if not user or user.is_anonymous:
            await self.close(code=4401)
            return

        if user.role not in ("admin", "operator"):
            await self.close(code=4403)
            return

        station = await self._get_station()
        if station is None:
            await self.close(code=4404)
            return

        if station.status != "online":
            await self.close(code=4409)
            return

        active_count = await self._count_active_sessions()
        if active_count >= MAX_SESSIONS_PER_STATION:
            await self.close(code=4429)
            return

        self.session = await self._create_session(user)

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        await self._update_session_status("active")

        await self._audit_log(
            station,
            "updated",
            f"Terminal session opened by {user.username}",
            user,
        )

    async def disconnect(self, close_code):
        if self.session:
            await self._close_session(close_reason=f"disconnect (code={close_code})")

            station = await self._get_station()
            if station:
                user = self.scope.get("user")
                await self._audit_log(
                    station,
                    "updated",
                    f"Terminal session closed by "
                    f"{user.username if user and not user.is_anonymous else 'unknown'}",
                    user if user and not user.is_anonymous else None,
                )

            await self.channel_layer.group_send(
                f"{self.group_name}_agent",
                {"type": "terminal_close", "data": "browser disconnected"},
            )

        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        if text_data is None:
            return
        try:
            payload = json.loads(text_data)
        except json.JSONDecodeError:
            return

        msg_type = payload.get("type", "input")

        if msg_type == "resize":
            await self.channel_layer.group_send(
                f"{self.group_name}_agent",
                {
                    "type": "terminal_resize",
                    "cols": payload.get("cols", 80),
                    "rows": payload.get("rows", 24),
                },
            )
        elif msg_type == "close":
            await self.channel_layer.group_send(
                f"{self.group_name}_agent",
                {"type": "terminal_close", "data": ""},
            )
        else:
            # Input — forward data to agent
            await self.channel_layer.group_send(
                f"{self.group_name}_agent",
                {"type": "terminal_input", "data": payload.get("data", "")},
            )

    # -- Channel layer handlers -----------------------------------------------

    async def terminal_output(self, event):
        """Agent sends output -> forward to browser."""
        await self.send(text_data=json.dumps({"type": "output", "data": event["data"]}))

    async def terminal_closed(self, event):
        """Agent closed terminal -> notify browser and close."""
        await self.send(
            text_data=json.dumps(
                {"type": "closed", "reason": event.get("reason", "agent disconnected")}
            )
        )
        await self.close()

    # -- Database helpers ------------------------------------------------------

    @database_sync_to_async
    def _get_station(self):
        from apps.stations.models import Station

        try:
            return Station.objects.get(pk=self.station_id)
        except Station.DoesNotExist:
            return None

    @database_sync_to_async
    def _count_active_sessions(self):
        from apps.tunnel.models import TerminalSession

        return TerminalSession.objects.filter(
            station_id=self.station_id,
            status__in=("connecting", "active"),
        ).count()

    @database_sync_to_async
    def _create_session(self, user):
        from apps.tunnel.models import TerminalSession

        return TerminalSession.objects.create(
            station_id=self.station_id,
            user=user,
            status="connecting",
        )

    @database_sync_to_async
    def _update_session_status(self, status):
        if self.session:
            self.session.status = status
            self.session.save(update_fields=["status"])

    @database_sync_to_async
    def _close_session(self, close_reason=""):
        if self.session:
            self.session.status = "closed"
            self.session.ended_at = timezone.now()
            self.session.close_reason = close_reason
            self.session.save(update_fields=["status", "ended_at", "close_reason"])

    @database_sync_to_async
    def _audit_log(self, station, event_type, message, user):
        from apps.stations.models import StationAuditLog

        StationAuditLog.log(
            station=station,
            event_type=event_type,
            message=message,
            user=user,
        )


class AgentTerminalConsumer(AsyncWebsocketConsumer):
    """Station-agent-side WebSocket consumer for terminal sessions.

    Path: ws/agent/terminal/<station_id>/
    The station agent connects here and provides shell I/O.
    """

    async def connect(self):
        self.station_id = self.scope["url_route"]["kwargs"]["station_id"]
        self.group_name = f"terminal_{self.station_id}"
        self.agent_group_name = f"terminal_{self.station_id}_agent"

        from urllib.parse import parse_qs

        query_string = self.scope.get("query_string", b"").decode()
        # Use parse_qs so URL-encoded values (e.g. '+', '/', '=' in base64
        # signatures) are properly decoded.
        params = {k: v[0] for k, v in parse_qs(query_string).items() if v}

        station = await self._get_station()
        if station is None:
            await self.close(code=4404)
            return

        is_valid = await self._verify_agent(station, params)
        if not is_valid:
            await self.close(code=4401)
            return

        # Join the agent group (receives terminal_input, terminal_close from browser)
        await self.channel_layer.group_add(self.agent_group_name, self.channel_name)
        # Also join the main group to receive browser group messages if needed
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        # Notify browsers that the agent disconnected
        await self.channel_layer.group_send(
            self.group_name,
            {
                "type": "terminal_closed",
                "reason": f"agent disconnected (code={close_code})",
            },
        )

        await self.channel_layer.group_discard(self.agent_group_name, self.channel_name)
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        """Agent sends shell output -> broadcast to browser group."""
        if text_data is None:
            return
        try:
            payload = json.loads(text_data)
        except json.JSONDecodeError:
            return

        await self.channel_layer.group_send(
            self.group_name,
            {"type": "terminal_output", "data": payload.get("data", "")},
        )

    # -- Channel layer handlers -----------------------------------------------

    async def terminal_input(self, event):
        """Browser user typed something -> forward to agent."""
        await self.send(text_data=json.dumps({"type": "input", "data": event["data"]}))

    async def terminal_resize(self, event):
        """Browser resized terminal -> forward to agent."""
        await self.send(
            text_data=json.dumps({"type": "resize", "cols": event["cols"], "rows": event["rows"]})
        )

    async def terminal_close(self, event):
        """Browser requests close -> forward to agent."""
        await self.send(text_data=json.dumps({"type": "close", "reason": event.get("data", "")}))

    async def terminal_output(self, event):
        """Ignore own output messages relayed back through the group."""
        pass

    async def terminal_closed(self, event):
        """Ignore closed messages relayed back through the group."""
        pass

    # -- Database helpers ------------------------------------------------------

    @database_sync_to_async
    def _get_station(self):
        from apps.stations.models import Station

        try:
            return Station.objects.get(pk=self.station_id)
        except Station.DoesNotExist:
            return None

    @database_sync_to_async
    def _verify_agent(self, station, params):
        """Verify the agent via Ed25519 signature.

        Accepts query params:
        - signature + timestamp: Ed25519 signature (verified against DeviceKey)
        """
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

        # Replay protection: 60 second window
        if time.time() - ts > 60 or ts > time.time() + 5:
            return False

        # Verify signature (signed data matches agent: "timestamp:sha256('')")
        body_hash = hashlib.sha256(b"").hexdigest()
        signed_data = f"{timestamp}:{body_hash}".encode()
        if DeviceKey.verify_signature(device_key.current_public_key, signature, signed_data):
            return True
        if device_key.next_public_key and DeviceKey.verify_signature(
            device_key.next_public_key, signature, signed_data
        ):
            return True

        return False
