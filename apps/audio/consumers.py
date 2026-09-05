# apps/audio/consumers.py
"""Audio relay consumers (Spec 0 §5, Session C).

AgentAudioConsumer  — ws/agent/audio/<station_id>/   Ed25519 auth
AudioConsumer       — ws/audio/<station_id>/          session/OIDC auth

The server is a dumb relay: it authenticates both ends, fans out opaque Opus
frames (§5.3), and enforces lock+PTT gating on the uplink.  No DSP, no
decode, no re-encode.
"""

import json
import logging
import re
import time

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from . import constants, gate, subscriptions

# Channels group names must match ^[a-zA-Z\d\-_.]+$ and be <100 chars.
# We further restrict stream_ids to [a-zA-Z\d\-_.]{1,80} to keep group names
# like "audio_<id>_src_<stream_id>" safely under the 100-char limit.
VALID_STREAM_ID = re.compile(r"^[a-zA-Z\d\-_.]{1,80}$")

logger = logging.getLogger(__name__)


class AgentAudioConsumer(AsyncWebsocketConsumer):
    """Agent-facing audio WebSocket.  Path: ws/agent/audio/<station_id>/.

    Ed25519 query-param auth — identical to AgentControlConsumer._verify_agent.
    Pre-accept close (4404/4401) on failure; post-accept on success.
    """

    async def connect(self):
        from urllib.parse import parse_qs

        self.station_id = self.scope["url_route"]["kwargs"]["station_id"]
        self.station = None
        # stream_refs: {stream_id: stream_ref} mapping from the last advertise.
        # Used to reverse-map incoming binary frames' numeric stream_ref back
        # to the string stream_id for group routing (design §5 — stream_id in
        # group names is cleaner than the numeric ref for browsers).
        self.stream_refs: dict[str, int] = {}
        # Reverse map: stream_ref -> stream_id
        self._ref_to_id: dict[int, str] = {}
        # Last advertised streams list, cached so a browser that connects AFTER
        # the agent's one-shot advertise can request a replay (audio_request_advertise).
        self._last_streams: list = []

        # Assign groups BEFORE the auth guard so disconnect() can safely
        # reference self.agent_group / self.browser_group on the reject path
        # (B-1: auth-reject teardown must not raise AttributeError).
        self.agent_group = constants.agent_group(self.station_id)
        # The browser fan-out group is used only for SENDING (streams /
        # stream_state broadcasts).  The agent must NOT join it — joining would
        # only echo its own broadcasts back to itself.
        self.browser_group = constants.browser_group(self.station_id)

        query_string = self.scope.get("query_string", b"").decode()
        params = {k: v[0] for k, v in parse_qs(query_string).items() if v}

        station = await self._get_station()
        if station is None:
            await self.close(code=4404)
            return
        self.station = station

        if not await self._verify_agent(station, params):
            await self.close(code=4401)
            return

        await self.channel_layer.group_add(self.agent_group, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        try:
            station = self.station
            if station is not None:
                await self._clear_ptt(station)
                # Broadcast the cleared gate state so browser _gate_cache
                # does not stay stale (ptt_active=True) after the agent drops.
                await self._bridge_gate(station)
                # Notify browsers that each advertised source went idle.  §5.2
                # requires stream_state to carry a stream_id, so emit one frame
                # per known stream rather than a single id-less "all gone" frame.
                # Use the audio.stream_state event type so the browser's
                # audio_stream_state handler relays it (a bare "stream_state"
                # type is silently dropped on the browser).
                for entry in self._last_streams:
                    sid = entry.get("stream_id") if isinstance(entry, dict) else None
                    if not sid:
                        continue
                    await self.channel_layer.group_send(
                        self.browser_group,
                        {
                            "type": "audio.stream_state",
                            "msg": {
                                "v": constants.AUDIO_PROTOCOL_VERSION,
                                "type": "stream_state",
                                "stream_id": sid,
                                "state": "idle",
                                "detail": "agent disconnected",
                            },
                        },
                    )
        finally:
            # agent_group is always set (even on reject path) so no hasattr needed,
            # but guard defensively in case connect() raised before assignment.
            if hasattr(self, "agent_group"):
                await self.channel_layer.group_discard(self.agent_group, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        if bytes_data is not None:
            await self._handle_media(bytes_data)
            return
        if text_data is None:
            return
        try:
            msg = json.loads(text_data)
        except json.JSONDecodeError:
            return

        mtype = msg.get("type")
        if mtype == "advertise":
            await self._handle_advertise(msg)
        elif mtype == "stream_state":
            await self._handle_stream_state(msg)
        # Unknown types ignored (forward-compat).

    async def _handle_advertise(self, msg):
        """Rebuild stream_refs map; relay streams to browsers; re-subscribe demanded sources."""
        streams = msg.get("streams", [])
        # Rebuild maps.
        new_refs: dict[str, int] = {}
        new_ref_to_id: dict[int, str] = {}
        for entry in streams:
            sid = entry.get("stream_id")
            ref = entry.get("stream_ref")
            # Validate the stream_id: it becomes part of a Channels group name
            # (audio_<st>_src_<sid>) in _handle_media, and an invalid id (e.g.
            # containing '/') would raise in group_send and crash the consumer.
            if sid is not None and ref is not None and VALID_STREAM_ID.match(str(sid)):
                new_refs[sid] = ref
                new_ref_to_id[ref] = sid
        self.stream_refs = new_refs
        self._ref_to_id = new_ref_to_id
        self._last_streams = streams

        # Relay filtered streams list to all browsers.
        await self.channel_layer.group_send(
            self.browser_group,
            {
                "type": "audio.streams",
                "msg": {
                    "v": constants.AUDIO_PROTOCOL_VERSION,
                    "type": "streams",
                    "streams": streams,
                },
            },
        )

        # For any source that already has demand (browsers subscribed before
        # advertise arrived), re-send source_subscribe to the agent.
        if self.station is not None:
            for sid in new_refs:
                # op.mic is browser-produced — never demand-subscribe it (§5.2).
                if sid == constants.OP_MIC_STREAM_ID:
                    continue
                cnt = await self._sub_count(self.station, sid)
                if cnt > 0:
                    await self.send(
                        text_data=json.dumps(
                            {
                                "v": constants.AUDIO_PROTOCOL_VERSION,
                                "type": "source_subscribe",
                                "stream_id": sid,
                            }
                        )
                    )

    async def _handle_stream_state(self, msg):
        """Relay stream_state to browsers."""
        await self.channel_layer.group_send(
            self.browser_group,
            {"type": "audio.stream_state", "msg": msg},
        )

    async def _handle_media(self, data: bytes):
        """Binary frame from agent -> fan out to _src_<stream_id> group byte-identically."""
        from station_agent.audio.frame import FrameError, parse_frame

        try:
            frame = parse_frame(data)
        except FrameError:
            return
        stream_id = self._ref_to_id.get(frame.stream_ref)
        if stream_id is None:
            return
        src_grp = constants.src_group(self.station_id, stream_id)
        await self.channel_layer.group_send(
            src_grp,
            {"type": "audio.media", "data": data},
        )

    # -- channel handlers (group -> this agent) --------------------------------

    async def audio_to_agent(self, event):
        """Browser sent a JSON msg that needs to reach the agent (e.g. source_subscribe)."""
        await self.send(text_data=json.dumps(event["msg"]))

    async def audio_media(self, event):
        """Uplink mic frame from browser -> forward to agent byte-identically."""
        await self.send(bytes_data=event["data"])

    async def audio_request_advertise(self, event):
        """A browser (re)connected -> replay the cached streams to just that browser.

        The agent advertises once on connect/hotplug; a browser that connects
        afterwards would otherwise never learn the streams.  We reply only to
        the requesting channel (reply_channel) so we don't re-broadcast to all
        browsers on every new connection.  No-op if nothing is advertised yet.
        """
        if not self._last_streams:
            return
        reply_channel = event.get("reply_channel")
        if not reply_channel:
            return
        await self.channel_layer.send(
            reply_channel,
            {
                "type": "audio.streams",
                "msg": {
                    "v": constants.AUDIO_PROTOCOL_VERSION,
                    "type": "streams",
                    "streams": self._last_streams,
                },
            },
        )

    async def audio_gate(self, event):
        """Gate state changed -> recompute and send mic_state to agent.

        TX target fallback: if no explicit tx_route was set, fall back to the
        PTT'd module so the agent still injects when the operator just keys PTT
        on a module without a separate tx_route command.
        """
        state = event.get("state", {})
        tx_slot = state.get("tx_slot")
        if tx_slot is None:
            tx_slot = state.get("ptt_slot")
        tx_module = state.get("tx_module") or state.get("ptt_module") or ""
        active = bool(state.get("ptt_active")) and tx_slot is not None
        await self.send(
            text_data=json.dumps(
                {
                    "v": constants.AUDIO_PROTOCOL_VERSION,
                    "type": "mic_state",
                    "active": active,
                    "tx_slot": tx_slot,
                    "tx_module": tx_module,
                }
            )
        )

    # NOTE: the agent no longer joins browser_group, so streams / stream_state /
    # audio.gate-echo handlers are unreachable and intentionally omitted.  The
    # agent still receives audio.gate via agent_group (audio_gate handler above).

    async def _bridge_gate(self, station):
        """Read gate state and broadcast audio.gate to browser + agent groups.

        Called after PTT clear on agent disconnect so that AudioConsumers
        refresh their _gate_cache immediately (mirrors ControlConsumer /
        AgentControlConsumer._bridge_gate).  Broadcasts the msgpack-safe wire
        state (no datetime) to survive the prod channels_redis (msgpack) layer.
        """
        state = await self._get_gate_state(station)
        payload = {"type": "audio.gate", "state": state}
        await self.channel_layer.group_send(self.browser_group, payload)
        await self.channel_layer.group_send(self.agent_group, payload)

    @database_sync_to_async
    def _get_gate_state(self, station):
        return gate.get_wire_state(station)

    # -- DB helpers ------------------------------------------------------------

    @database_sync_to_async
    def _get_station(self):
        from apps.stations.models import Station

        try:
            return Station.objects.get(pk=self.station_id)
        except Station.DoesNotExist:
            return None

    @database_sync_to_async
    def _verify_agent(self, station, params):
        """Ed25519 verification — verbatim copy from AgentControlConsumer."""
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

    @database_sync_to_async
    def _clear_ptt(self, station):
        gate.clear_ptt(station)

    @database_sync_to_async
    def _sub_count(self, station, stream_id):
        return subscriptions.count(station, stream_id)


class AudioConsumer(AsyncWebsocketConsumer):
    """Browser-facing audio WebSocket.  Path: ws/audio/<station_id>/.

    Accept first (so every reject can send a human-readable error), then
    validate auth + access.  Mic uplink additionally requires holding the
    ControlLock and PTT being active.
    """

    async def connect(self):
        self.station_id = self.scope["url_route"]["kwargs"]["station_id"]
        self.user = self.scope.get("user")
        self.station = None
        self._gate_cache: dict = {}
        # Mic open/closed state for uplink-error throttling.
        self._mic_open = False
        # Set to True once an error has been sent for the current mic_open
        # attempt without lock; reset on mic_close or re-open.
        self._not_locked_error_sent = False

        # Track joined src groups early (used in disconnect cleanup) even if we
        # reject before joining any group.
        self._src_groups: set[str] = set()

        await self.accept()

        if not self.user or self.user.is_anonymous:
            await self._reject(4401, "Not signed in")
            return

        station = await self._get_station()
        if station is None:
            await self._reject(4404, "Station not found")
            return
        self.station = station

        if not await self._can_use(station):
            await self._reject(4403, "You are not permitted to use this station")
            return

        # Only an authorized socket joins the browser fan-out group — an
        # unauthorized socket must never receive gate/stream broadcasts.  A
        # broadcast racing the tiny auth window may be missed; that is
        # acceptable (mirrors ControlConsumer, which also joins only after
        # can_use passes).
        self.browser_group = constants.browser_group(self.station_id)
        await self.channel_layer.group_add(self.browser_group, self.channel_name)

        # Seed the in-memory gate cache with the msgpack-safe wire state (one DB
        # read at connect; refreshed by audio.gate broadcasts).  The uplink
        # decision is then a pure in-memory check — no per-frame DB query.
        self._gate_cache = await self._get_wire_state(station)

        # Ask the agent to replay its advertised streams to THIS browser.  The
        # agent advertises once (on connect/hotplug); without this a browser
        # that connects afterwards would never learn the current streams
        # (mirrors ControlConsumer sending an inventory snapshot on connect).
        # Sent after joining browser_group so the reply is not missed.
        await self.channel_layer.group_send(
            constants.agent_group(self.station_id),
            {"type": "audio.request_advertise", "reply_channel": self.channel_name},
        )

    async def _reject(self, close_code, reason, err_code="not_authorized"):
        """Accept-then-error reject so the browser sees a human-readable reason.

        ``err_code`` is the §5.2 error enum (default ``not_authorized``); the
        numeric ``close_code`` (4401/4403/4404) is carried only in the WS close
        frame, not in the contract ``error`` payload.
        """
        try:
            await self.send(
                text_data=json.dumps(
                    {
                        "v": constants.AUDIO_PROTOCOL_VERSION,
                        "type": "error",
                        "code": err_code,
                        "detail": reason,
                    }
                )
            )
        finally:
            await self.close(code=close_code)

    async def disconnect(self, close_code):
        try:
            station = self.station
            if station is not None:
                zero_streams = await self._drop_channel(station, self.channel_name)
                for sid in zero_streams:
                    # op.mic is browser-produced — no agent demand signal (§5.2).
                    if sid == constants.OP_MIC_STREAM_ID:
                        continue
                    await self.channel_layer.group_send(
                        constants.agent_group(self.station_id),
                        {
                            "type": "audio.to_agent",
                            "msg": {
                                "v": constants.AUDIO_PROTOCOL_VERSION,
                                "type": "source_unsubscribe",
                                "stream_id": sid,
                            },
                        },
                    )
        finally:
            if hasattr(self, "browser_group"):
                await self.channel_layer.group_discard(self.browser_group, self.channel_name)
            if hasattr(self, "_src_groups"):
                for grp in list(self._src_groups):
                    await self.channel_layer.group_discard(grp, self.channel_name)
                self._src_groups.clear()

    async def receive(self, text_data=None, bytes_data=None):
        if bytes_data is not None:
            await self._handle_uplink(bytes_data)
            return
        if text_data is None:
            return
        try:
            msg = json.loads(text_data)
        except json.JSONDecodeError:
            return

        mtype = msg.get("type")
        station = self.station
        if station is None:
            return

        if mtype == "hello":
            pass  # no-op / ack
        elif mtype == "subscribe":
            await self._handle_subscribe(station, msg)
        elif mtype == "unsubscribe":
            await self._handle_unsubscribe(station, msg)
        elif mtype == "mic_open":
            await self._handle_mic_open(station, msg)
        elif mtype == "mic_close":
            self._mic_open = False
            self._not_locked_error_sent = False
        # Unknown types ignored (forward-compat).

    async def _send_invalid_stream_error(self, sid):
        """Send an unknown_stream error for a rejected stream_id."""
        await self.send(
            text_data=json.dumps(
                {
                    "v": constants.AUDIO_PROTOCOL_VERSION,
                    "type": "error",
                    "code": "unknown_stream",
                    "detail": f"invalid stream_id: {sid!r}",
                }
            )
        )

    async def _handle_subscribe(self, station, msg):
        # Cap list length and validate each stream_id before touching the DB
        # or channel-layer groups (malformed ids would raise TypeError in
        # group_add and crash the consumer).
        stream_ids = msg.get("stream_ids", [])[:32]
        for sid in stream_ids:
            if not VALID_STREAM_ID.match(str(sid)):
                await self._send_invalid_stream_error(sid)
                continue
            result = await self._subscribe(station, sid, self.channel_name)
            # op.mic is browser-produced — never demand-subscribe it at the agent
            # (§5.2); still join the fan-out group below so listeners hear it.
            if result["first"] and sid != constants.OP_MIC_STREAM_ID:
                # First subscriber -> tell the agent to start producing.
                await self.channel_layer.group_send(
                    constants.agent_group(self.station_id),
                    {
                        "type": "audio.to_agent",
                        "msg": {
                            "v": constants.AUDIO_PROTOCOL_VERSION,
                            "type": "source_subscribe",
                            "stream_id": sid,
                        },
                    },
                )
            # Join the per-source fan-out group so we receive media frames.
            grp = constants.src_group(self.station_id, sid)
            if grp not in self._src_groups:
                await self.channel_layer.group_add(grp, self.channel_name)
                self._src_groups.add(grp)

    async def _handle_unsubscribe(self, station, msg):
        # Cap list length and validate each stream_id.
        stream_ids = msg.get("stream_ids", [])[:32]
        for sid in stream_ids:
            if not VALID_STREAM_ID.match(str(sid)):
                await self._send_invalid_stream_error(sid)
                continue
            result = await self._unsubscribe(station, sid, self.channel_name)
            if result["last"] and sid != constants.OP_MIC_STREAM_ID:
                # Last subscriber gone -> tell the agent to stop producing.
                await self.channel_layer.group_send(
                    constants.agent_group(self.station_id),
                    {
                        "type": "audio.to_agent",
                        "msg": {
                            "v": constants.AUDIO_PROTOCOL_VERSION,
                            "type": "source_unsubscribe",
                            "stream_id": sid,
                        },
                    },
                )
            # Leave the per-source fan-out group.
            grp = constants.src_group(self.station_id, sid)
            if grp in self._src_groups:
                await self.channel_layer.group_discard(grp, self.channel_name)
                self._src_groups.discard(grp)

    async def _handle_mic_open(self, station, msg):
        """Browser declares uplink format.  Validate lock+PTT from cache; error if not met."""
        self._mic_open = True
        self._not_locked_error_sent = False
        # Decide from the in-memory gate cache (same source as the uplink path).
        if not self._cache_mic_allowed():
            self._not_locked_error_sent = True
            await self.send(
                text_data=json.dumps(
                    {
                        "v": constants.AUDIO_PROTOCOL_VERSION,
                        "type": "error",
                        "code": "not_locked",
                        "detail": "uplink requires holding the station ControlLock",
                    }
                )
            )

    def _cache_mic_allowed(self) -> bool:
        """Pure in-memory uplink gate (no DB) — §5.5: holder + PTT + not-expired.

        Reads the cached wire-state seeded at connect and refreshed by every
        audio.gate broadcast, so the ~50 fps mic stream never hits the DB.  Does
        NOT require tx_route (the TX target fallback lives on the agent side).
        """
        c = self._gate_cache or {}
        if not c.get("ptt_active"):
            return False
        user_id = self.user.id if self.user else None
        if c.get("holder_id") != user_id:
            return False
        expires = c.get("ptt_expires_epoch")
        if expires is None:
            return False
        return time.time() < expires

    async def _handle_uplink(self, data: bytes):
        """Mic frame from browser -> gate check -> relay to agent + op.mic fans."""
        station = self.station
        if station is None:
            return

        if not self._cache_mic_allowed():
            # Throttle: only send one error per open-without-lock transition.
            if not self._not_locked_error_sent:
                self._not_locked_error_sent = True
                await self.send(
                    text_data=json.dumps(
                        {
                            "v": constants.AUDIO_PROTOCOL_VERSION,
                            "type": "error",
                            "code": "not_locked",
                            "detail": "uplink requires holding the station ControlLock",
                        }
                    )
                )
            return

        # Authorized: forward to agent and fan out to op.mic subscribers.
        agent_grp = constants.agent_group(self.station_id)
        await self.channel_layer.group_send(
            agent_grp,
            {"type": "audio.media", "data": data},
        )
        # Fan out to op.mic subscribers (including this browser if it subscribed).
        omic_grp = constants.src_group(self.station_id, constants.OP_MIC_STREAM_ID)
        await self.channel_layer.group_send(
            omic_grp,
            {"type": "audio.media", "data": data},
        )

    # -- channel handlers (group -> this browser) ------------------------------

    async def audio_media(self, event):
        """Downlink media frame -> send to browser byte-identically."""
        await self.send(bytes_data=event["data"])

    async def audio_streams(self, event):
        """Agent advertised streams -> relay to browser."""
        await self.send(text_data=json.dumps(event["msg"]))

    async def audio_stream_state(self, event):
        """Stream state changed -> relay to browser."""
        await self.send(text_data=json.dumps(event["msg"]))

    async def audio_gate(self, event):
        """Gate state changed -> refresh the in-memory cache (already wire-safe).

        Reset the error throttle whenever the uplink is NOT currently allowed,
        so a subsequent close->open transition surfaces a fresh error to the
        browser (one error per transition, never per dropped frame).
        """
        self._gate_cache = event.get("state", {})
        if not self._cache_mic_allowed():
            self._not_locked_error_sent = False

    # -- DB helpers ------------------------------------------------------------

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
    def _get_wire_state(self, station):
        return gate.get_wire_state(station)

    @database_sync_to_async
    def _subscribe(self, station, stream_id, channel_name):
        return subscriptions.subscribe(station, stream_id, channel_name)

    @database_sync_to_async
    def _unsubscribe(self, station, stream_id, channel_name):
        return subscriptions.unsubscribe(station, stream_id, channel_name)

    @database_sync_to_async
    def _drop_channel(self, station, channel_name):
        return subscriptions.drop_channel(station, channel_name)
