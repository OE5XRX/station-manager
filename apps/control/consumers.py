# apps/control/consumers.py
import asyncio
import collections
import json
import logging

import django.conf
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.utils import timezone
from django.utils.translation import gettext

from apps.accounts.permissions import (
    AreaConsumerMixin,
    can,
    can_override_station,
    level_label,
    required_level,
)
from apps.passive.models import PassiveServiceRun

from . import (
    agent_presence,
    capability_policy,
    constants,
    lock,
    lock_hooks,
    persistence,
    registry,
    scan,
    telemetry_subscriptions,
)
from .models import AgentConnection, StationModule

# Prefix of request ids the passive-services supervisor puts on its system commands: their
# results go to the supervisor, never to the browsers (apps.passive.supervisor).
_PASSIVE_REQUEST_PREFIX = "passive-"

logger = logging.getLogger(__name__)

_AUDIT_FIELD_MAX = 64


def _clip(value):
    """Bound attacker-controlled frame fields before they reach the audit log."""
    return str(value)[:_AUDIT_FIELD_MAX]


def _fresh_user(user):
    """Re-read ``user`` from the DB (sync). The scope user is loaded once at connect and
    its role properties are cached, so a demotion/deactivation would otherwise only take
    effect on reconnect. Missing or inactive row → ``None`` (callers deny)."""
    pk = getattr(user, "pk", None)
    if pk is None:
        return None
    from apps.accounts.models import User

    fresh = User.objects.filter(pk=pk).first()
    if fresh is None or not fresh.is_active:
        return None
    return fresh


# Browser frames that operate the station: each re-checks control.use (fresh user).
_USE_ACTIONS = frozenset(
    {
        "lock_acquire",
        "lock_request",
        "lock_transfer",
        "ptt_keepalive",
        "apply_repeater",
        "scan_start",
        "scan_control",
    }
)

# User-facing texts for scan_start refusals (codes are the contract, texts are cosmetic).
_SCAN_ERRORS = {
    "invalid_scan": "Invalid scan request",
    "forbidden": "Scan could not be authorized",
    "no_coordinates": "Station has no coordinates — radius scan unavailable",
    "empty_scan_list": "No scan entries for this station",
    "internal_error": "Scan could not be started",
}

# Agent scan states that tune a module: the operator mic must follow (gate tx route).
_SCAN_TUNED_STATES = frozenset({"active", "held"})


class AgentControlConsumer(AsyncWebsocketConsumer):
    """Agent-facing control WebSocket. Path: ws/agent/control/<station_id>/.

    Relays §7 frames verbatim, updates the registry, and owns the lock sweep
    timer. A station runs a single agent process holding one persistent
    Control-WS, so in practice there is one connection per station. A reconnect
    overlaps its predecessor (the server may notice the old socket died only after
    the new one connected and sent inventory): both share the agent group, and only
    the CURRENT connection (``agent_presence``) runs the offline/lock/PTT teardown
    on disconnect — a stale one must not wipe the live connection's state.
    """

    permission_exempt = "agent: ed25519"

    async def connect(self):
        self.station_id = self.scope["url_route"]["kwargs"]["station_id"]
        self.group_name = f"control_{self.station_id}"
        self.agent_group_name = f"control_{self.station_id}_agent"
        self.sweep_task = None
        self.station = None  # cached after connect to avoid a DB fetch per frame
        self._claimed = False  # True once this connection was made the current one
        # Deferred drift re-apply (skipped because the station was keyed), retried once PTT
        # is released (see _retry_pending_reapply). ``_reapply_pending`` holds the
        # inventory generation that armed it (None = nothing pending); the retry re-derives
        # frames from CURRENT state, never from the keyed-time snapshot.
        self._reapply_pending = None
        self._reapply_gen = 0
        # (slot, module, capability) a browser ``set`` was relayed for since the last
        # inventory: a deliberate user write the deferred retry must not race.
        self._reapply_touched = set()
        # str(slot) -> slot exactly as the agent reports it (an int slot must go back as int).
        self._wire_slots = {}
        # (scan_id, {ref: (label, rx_frequency)}, count) of the last scan_start/scan_update
        # relayed to this agent: enriches its scan_state frames for the browser panel.
        self._scan_index = None
        # (scan_id, state) last stored via scan.note_state: one DB write per transition.
        self._scan_noted = None
        from apps.passive.supervisor import PassiveSupervisor

        self.passive = PassiveSupervisor(
            self.station_id,
            send_to_agent=self._send_json,
            broadcast=self._broadcast_passive,
            wire_slot=lambda slot: self._wire_slots.get(str(slot), slot),
        )

        from urllib.parse import parse_qs

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

        await self.channel_layer.group_add(self.agent_group_name, self.channel_name)
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self._claim(station)
        self._claimed = True
        # A (re)connected agent runs no scan (it stops on disconnect): any row left over
        # (server restart, superseded half-open socket) is stale and would keep the lock
        # from idling out.
        await self._end_scan(station, "agent reconnect")
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
            station = self.station if getattr(self, "_claimed", False) else None
            # Stale (superseded by a reconnect) or never authenticated: tear down only
            # this channel, never the station state the live connection owns.
            if station is not None and await self._release(station):
                await self._mark_offline(station)
                await self.passive.on_agent_disconnect()
                # The agent stops its scan on disconnect (dead-man): only drop the row.
                await self._end_scan(station, "disconnect")
                freed = await self._force_free(station)
                if freed:
                    # Agent disconnect clears any held lock -> PTT must die too.
                    await lock_hooks.on_lock_released(station, "agent_disconnect", stop_scan=False)
                await self._broadcast("control.agent_offline", {})
                if freed:
                    status = await self._lock_status(station)
                    await self._broadcast("control.lock", {"lock": status})
        finally:
            # Always release group membership for this dead channel, even if
            # the offline/free/broadcast steps above raise (e.g. channel layer down).
            await self.passive.shutdown()
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
            station = await self._cached_station()
            drain_changed, drain_after = False, None
            if station is not None:
                drain_changed, drain_after = await self._apply_inventory(
                    station, msg.get("slots", [])
                )
                await self._reapply_persisted(station, msg.get("slots", []))
                await self._replay_telemetry(station)
            self.passive.set_agent_features(msg.get("features"))
            await self.passive.on_inventory()
            await self._broadcast("control.inventory", {"msg": msg})
            if drain_changed:
                # E.g. the post-flash inventory reports the target version: the row
                # converges and the drain ends -> refresh every open panel's banner.
                # Reuse the drain evaluated in _apply_inventory (no 3rd evaluation).
                await self._broadcast_lock_refresh(drain=drain_after)
        elif mtype == "state":
            station = await self._cached_station()
            if station is not None:
                await self._apply_state(
                    station, msg.get("slot"), msg.get("module"), msg.get("values", {})
                )
            await self._broadcast("control.state", {"msg": msg})
        elif mtype == "result":
            rid = msg.get("request_id")
            if isinstance(rid, str) and rid.startswith(_PASSIVE_REQUEST_PREFIX):
                await self.passive.on_result(msg)
            else:
                await self._broadcast("control.result", {"msg": msg})
        elif mtype == "decoder_state":
            await self.passive.on_decoder_state(msg)
        elif mtype == "decoder_snapshot":
            await self.passive.on_snapshot(msg)
        elif mtype == "decoder_output":
            pass  # ingest: apps.passive.ingest (packet ingest task)
        elif mtype == "scan_state":
            station = await self._cached_station()
            if station is not None:
                await self._apply_scan_state(station, msg)
            await self._broadcast("control.scan_state", {"msg": self._enrich_scan_state(msg)})
        elif mtype == "decoder_output":
            await self._ingest_decoder_output(msg)
        elif mtype == "event":
            updating = registry.FIRMWARE_UPDATING_EVENTS.get(msg.get("event"))
            if updating is not None:
                station = await self._cached_station()
                if station is not None:
                    detail = msg.get("detail")
                    result = detail.get("result") if isinstance(detail, dict) else None
                    await self._set_firmware_updating(
                        station, msg.get("slot"), msg.get("module"), updating, result
                    )
            await self._broadcast("control.event", {"msg": msg})
            if updating is not None:
                # A flash started / ended: refresh every open panel's drain banner.
                await self._broadcast_lock_refresh()
        # Unknown types are ignored (forward-compat).
        # Deferred re-apply retry runs AFTER the frame was processed, so it sees the module
        # state this very frame carried (an inventory frame does its own check above).
        if mtype != "inventory" and self._reapply_pending is not None:
            station = await self._cached_station()
            if station is not None:
                await self._retry_pending_reapply(station)

    async def _ingest_decoder_output(self, msg):
        """Store a decoder packet, then ack it so the agent can drop it from its spool."""
        from apps.passive.ingest import ingest_output

        try:
            ack = await database_sync_to_async(ingest_output)(
                self.station_id, msg, recv_now=timezone.now()
            )
        except Exception:
            logger.exception("decoder_output ingest failed (station %s)", self.station_id)
            return  # unacked: the agent resends
        if ack:
            ack_frame = {
                "v": 1,
                "type": "decoder_ack",
                "epoch": msg["epoch"],
                "seqs": [msg["seq"]],
            }
            await self.send(text_data=json.dumps(ack_frame))

    async def _replay_telemetry(self, station):
        """Re-arm the agent with the browsers' aggregated telemetry demand: a
        (re)connected agent has no subscriptions, and redundant browser
        re-subscribes are not relayed (see telemetry_subscriptions)."""
        try:
            frames = await database_sync_to_async(telemetry_subscriptions.active_subscriptions)(
                station
            )
            for frame in frames:
                await self.send(text_data=json.dumps(frame))
        except Exception:
            logger.exception("control: telemetry replay failed")

    # -- server -> agent (channel handler) ------------------------------------

    async def control_to_agent(self, event):
        """A ControlConsumer relayed a §7 downstream frame -> send to agent."""
        frame = event["frame"]
        self._note_user_set(frame)
        self._note_scan_entries(frame)
        await self.send(text_data=json.dumps(frame))
        if isinstance(frame, dict) and frame.get("type") == "firmware_check":
            # Target/release changed: the drain state may have changed with it.
            await self._broadcast_lock_refresh()

    def _note_scan_entries(self, frame):
        """Remember label/frequency per entry ref of a relayed scan list (in memory only)."""
        if not isinstance(frame, dict) or frame.get("type") not in ("scan_start", "scan_update"):
            return
        entries = frame.get("entries")
        if not isinstance(entries, list):
            return
        index = {
            e["ref"]: (e.get("label"), e.get("rx_frequency"))
            for e in entries
            if isinstance(e, dict) and isinstance(e.get("ref"), str)
        }
        self._scan_index = (frame.get("scan_id"), index, len(entries))

    def _enrich_scan_state(self, msg):
        """Add ``label``/``rx_frequency``/``count`` from the relayed scan list to an agent
        ``scan_state`` (the agent sends only ``ref``/``index``). Agent-sent fields win."""
        if self._scan_index is None or not isinstance(msg, dict):
            return msg
        scan_id, index, count = self._scan_index
        if msg.get("scan_id") != scan_id:
            return msg
        extra = {"count": count}
        ref = msg.get("ref")
        if isinstance(ref, str) and ref in index:
            extra["label"], extra["rx_frequency"] = index[ref]
        return {**extra, **msg}

    async def _broadcast_lock_refresh(self, drain=lock.DRAIN_UNSET):
        """Re-broadcast ``control.lock`` with a fresh drain payload (best effort).

        ``drain`` = an already-evaluated drain payload for the current state."""
        try:
            station = await self._cached_station()
            if station is None:
                return
            status = await self._lock_status(station, drain=drain)
            await self._broadcast("control.lock", {"lock": status})
        except Exception:
            logger.exception("control: lock/drain refresh broadcast failed")

    def _note_user_set(self, frame):
        """Remember a relayed browser ``set`` so a deferred re-apply leaves that capability
        alone: the user's write (and its own persistence on the ok result) is authoritative,
        and neither the DB nor ``last_state`` reflects it until result/state come back."""
        if not isinstance(frame, dict) or frame.get("op") != "set":
            return
        slot, module, cap = frame.get("slot"), frame.get("module"), frame.get("capability")
        if (isinstance(slot, str) or type(slot) is int) and isinstance(module, str):
            if isinstance(cap, str):
                self._reapply_touched.add((str(slot), module, cap))

    # -- broadcasts we must ignore when echoed back to our own group ----------

    async def control_inventory(self, event):
        pass

    async def control_state(self, event):
        pass

    async def control_result(self, event):
        pass

    async def control_scan_state(self, event):
        pass

    async def control_event(self, event):
        pass

    async def control_lock(self, event):
        pass

    async def control_agent_offline(self, event):
        pass

    async def control_control_requested(self, event):
        pass

    async def control_passive(self, event):
        pass

    async def control_passive_preempt(self, event):
        """A user took the control lock: hand the radio back (does not block the grant)."""
        self.passive.request_preempt(str(event.get("reason") or "acquired"), handover=True)

    async def control_passive_republish(self, event):
        await self.passive.republish()

    async def _send_json(self, frame):
        await self.send(text_data=json.dumps(frame))

    async def _broadcast_passive(self, payload):
        await self._broadcast("control.passive", {"msg": payload})

    # -- sweep loop -----------------------------------------------------------

    async def _sweep_loop(self):
        try:
            while True:
                await asyncio.sleep(constants.LOCK_SWEEP_INTERVAL_SECONDS)
                try:
                    station = await self._cached_station()
                    if station is None:
                        continue
                    freed = await self._sweep(station)
                    if freed:
                        # Lock swept (idle/grace) -> PTT and the holder's scan must die.
                        await lock_hooks.on_lock_released(station, "expired")
                        status = await self._lock_status(station)
                        await self._broadcast("control.lock", {"lock": status})
                    await self._retry_pending_reapply(station)
                    await self.passive.tick()
                    await self._purge_stale_telemetry(station)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("control: lock sweep iteration failed; continuing")
        except asyncio.CancelledError:
            raise

    async def _broadcast(self, msg_type, payload):
        await self.channel_layer.group_send(self.group_name, {"type": msg_type, **payload})

    async def _purge_stale_telemetry(self, station):
        """Release telemetry demand of browser connections that died without
        disconnect() (no one else would). Relayed via the agent group so the CURRENT
        agent connection gets it even while a stale one is still sweeping."""
        frames = await database_sync_to_async(telemetry_subscriptions.purge_stale)(station)
        for frame in frames:
            await self.channel_layer.group_send(
                self.agent_group_name, {"type": "control.to_agent", "frame": frame}
            )

    async def _reapply_persisted(self, station, slots):
        """Drift re-apply (spec §4a): the FW persists nothing, so whenever the reported
        inventory disagrees with a server-persisted calibration value, send a ``set`` for
        it straight to this agent. Digital module settings only (never PTT/keying — see
        ``persistence.reapply_frames``). A failure here must never break the inventory path.

        While the station is keyed the re-apply is deferred, not dropped: it is armed as
        pending and retried by :meth:`_retry_pending_reapply` once PTT is released.
        """
        # A fresh inventory is a fresh baseline: supersedes any pending/in-flight retry.
        self._reapply_gen += 1
        gen = self._reapply_gen
        self._reapply_touched.clear()
        if isinstance(slots, list):
            self._wire_slots = {
                str(e["slot"]): e["slot"]
                for e in slots
                if isinstance(e, dict)
                and (isinstance(e.get("slot"), str) or type(e.get("slot")) is int)
            }
        try:
            frames = await self._reapply_frames(station, slots)
            if frames is None:
                if self._reapply_gen == gen:
                    self._reapply_pending = gen
                return
            if self._reapply_gen == gen:
                self._reapply_pending = None
            await self._send_reapply(station, frames)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("control: persisted-capability re-apply failed")

    async def _send_reapply(self, station, frames):
        for frame in frames:
            # Re-checked per frame (after every await): a user ``set`` relayed while an
            # earlier frame was being sent/audited wins over the re-apply.
            key = (str(frame["slot"]), frame["module"], frame["capability"])
            if key in self._reapply_touched or key in self.passive.protected_caps():
                continue
            await self.send(text_data=json.dumps(frame))
            await self._audit_reapply(
                station, f"re-apply persisted {frame['capability']}={frame['value']!r}"
            )

    async def _retry_pending_reapply(self, station):
        """Retry a re-apply that was skipped while keyed. The agent only sends inventory on
        connect / slot-set change, so without this a skip would never be retried. Driven by
        the sweep tick (covers explicit PTT clear, lock loss AND silent dead-man expiry) and
        by any subsequent agent frame (telemetry → low latency), after that frame was
        processed. Bounded: frames go out once per skipped inventory.

        Frames are re-derived from CURRENT state — persisted rows vs the module's
        ``last_state`` (kept current by inventory + state frames) — never replayed from the
        keyed-time snapshot, and capabilities the user ``set`` since the inventory are left
        alone (their own write wins). The pending marker is claimed before the first await
        so concurrent triggers can't double-send; if still keyed it is re-armed only when no
        newer inventory check superseded it."""
        armed = self._reapply_pending
        if armed is None:
            return
        self._reapply_pending = None
        try:
            frames = await self._current_reapply_frames(station)
            if self._reapply_gen != armed:
                return  # a newer inventory already checked (or re-armed) drift
            if frames is None:
                self._reapply_pending = armed  # still keyed: re-arm
                return
            frames = [dict(f, slot=self._wire_slots.get(f["slot"], f["slot"])) for f in frames]
            await self._send_reapply(station, frames)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("control: deferred persisted-capability re-apply failed")

    # -- DB helpers -----------------------------------------------------------

    async def _cached_station(self):
        """Return the Station, fetching once and caching on the instance.

        The station is fixed for the life of the connection, so re-querying it
        on every (potentially high-frequency) inventory/state frame is wasted
        work. Falls back to a DB fetch if the cache is somehow unset.
        """
        if self.station is None:
            self.station = await self._get_station()
        return self.station

    @database_sync_to_async
    def _get_station(self):
        from apps.stations.models import Station

        try:
            return Station.objects.get(pk=self.station_id)
        except Station.DoesNotExist:
            return None

    @database_sync_to_async
    def _claim(self, station):
        agent_presence.claim(station, AgentConnection.Kind.CONTROL, self.channel_name)

    @database_sync_to_async
    def _release(self, station):
        return agent_presence.release(station, AgentConnection.Kind.CONTROL, self.channel_name)

    @database_sync_to_async
    def _apply_inventory(self, station, slots):
        """Apply the inventory; ``(changed, after)`` where ``changed`` is True iff the
        station's drain state changed with it and ``after`` is the post-apply drain
        payload (reused for the broadcast, so the predicate runs twice per frame)."""
        before = lock.drain_payload(station)
        registry.apply_inventory(station, slots)
        after = lock.drain_payload(station)
        return after != before, after

    @database_sync_to_async
    def _reapply_frames(self, station, slots):
        from apps.audio import gate as audio_gate

        # Never touch module settings while the station is keyed (expiry-aware: a PTT row
        # whose dead-man expired is NOT keyed). ``None`` = deferred, caller re-arms a retry.
        if audio_gate.ptt_keyed(station):
            return None
        return persistence.reapply_frames(station, slots)

    @database_sync_to_async
    def _current_reapply_frames(self, station):
        """Drift frames from current state: persisted rows vs registry ``last_state`` of
        the online modules. ``None`` while keyed (caller re-arms)."""
        from apps.audio import gate as audio_gate

        from . import serializers

        if audio_gate.ptt_keyed(station):
            return None
        slots = [
            {"slot": e["slot"], "modules": [m for m in e["modules"] if m.get("online")]}
            for e in serializers.snapshot(station)
        ]
        return persistence.reapply_frames(station, slots)

    @database_sync_to_async
    def _audit_reapply(self, station, message):
        from apps.stations.models import StationAuditLog

        StationAuditLog.log(station=station, event_type="control_command", message=message)

    @database_sync_to_async
    def _apply_state(self, station, slot, module_id, values):
        registry.apply_state(station, slot, module_id, values)

    @database_sync_to_async
    def _set_firmware_updating(self, station, slot, module_id, value, result=None):
        registry.set_firmware_updating(station, slot, module_id, value, result)

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
    def _lock_status(self, station, drain=lock.DRAIN_UNSET):
        return _lock_status_with_drain(station, drain=drain)

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

    async def _apply_scan_state(self, station, msg):
        """Server side of an agent ``scan_state`` (spec §7.4): ``stopped`` drops the
        ``ActiveScan`` row (+ audit); ``active``/``held`` on a tuned entry points the
        authoritative audio-gate TX route at that module — the agent's router state
        alone does not move the operator mic. Steps are neither persisted nor audited."""
        try:
            scan_id = msg.get("scan_id")
            state = msg.get("state")
            if not isinstance(scan_id, str):
                return
            if state == "stopped":
                await self._end_scan(station, msg.get("reason") or "stopped", scan_id)
                return
            coarse = scan.coarse_state(state)
            if coarse is not None and self._scan_noted != (scan_id, coarse):
                await database_sync_to_async(scan.note_state)(station, scan_id, coarse)
                self._scan_noted = (scan_id, coarse)
            if state in _SCAN_TUNED_STATES:
                slot, module = msg.get("slot"), msg.get("module")
                if slot is None or not isinstance(module, str):
                    return  # nothing tuned yet
                if not (isinstance(slot, str) or type(slot) is int):
                    return
                if await self._scan_set_tx_route(station, scan_id, slot, module):
                    await self._bridge_gate(station)
        except Exception:
            logger.exception("control: applying scan_state failed")

    @database_sync_to_async
    def _scan_set_tx_route(self, station, scan_id, slot, module):
        """Move the gate TX route for the station's current scan; True if it changed."""
        from apps.audio import gate as audio_gate

        if scan.current_scan_id(station) != scan_id:
            return False  # a stale/replaced scan must not move the mic
        before = audio_gate.get_state(station)
        if audio_gate.ptt_keyed(station) and (
            str(before.get("ptt_slot")),
            before.get("ptt_module"),
        ) != (str(slot), module):
            return False  # never move the mic away from a keyed module (agent does the same)
        audio_gate.set_tx_route(station, slot, module)
        after = audio_gate.get_state(station)
        return (before.get("tx_slot"), before.get("tx_module")) != (
            after.get("tx_slot"),
            after.get("tx_module"),
        )

    async def _end_scan(self, station, reason, scan_id=None):
        """Drop the station's ``ActiveScan``. Without ``scan_id`` the server ends it itself
        (agent gone/reconnected): no agent ``stopped`` will follow, so the browsers get a
        synthetic one — otherwise their scanner panel would stay "running"."""
        try:
            removed = await database_sync_to_async(scan.end_scan)(station, reason, scan_id)
            if removed is not None and scan_id is None:
                await self._broadcast(
                    "control.scan_state",
                    {
                        "msg": {
                            "v": constants.PROTOCOL_VERSION,
                            "type": "scan_state",
                            "scan_id": removed,
                            "state": "stopped",
                            "index": None,
                            "ref": None,
                            "slot": None,
                            "module": None,
                            "reason": "disconnect",
                        }
                    },
                )
        except Exception:
            logger.exception("control: ending scan failed")

    async def _bridge_gate(self, station):
        """Read gate state and broadcast audio.gate to browser + agent audio groups.

        Called after any PTT / tx_route / lock change so that AudioConsumers
        and AgentAudioConsumer update their cached state.  Lazy import of the
        audio app avoids a circular import at module load time.

        Broadcasts the msgpack-safe wire state (no datetime) so the payload
        round-trips through the prod channels_redis (msgpack) layer.
        """
        from apps.audio import constants as audio_constants

        state = await self._get_gate_state(station)
        payload = {"type": "audio.gate", "state": state}
        await self.channel_layer.group_send(audio_constants.browser_group(station.pk), payload)
        await self.channel_layer.group_send(audio_constants.agent_group(station.pk), payload)

    @database_sync_to_async
    def _get_gate_state(self, station):
        from apps.audio import gate as audio_gate

        return audio_gate.get_wire_state(station)


def _resolve_repeater(station, repeater_id, slot, module_id=None):
    """Resolve a repeater + slot (+ optional module) into the ``apply_preset`` plan (§5.3).

    A slot may host several modules: ``module_id`` (from the panel's band map) pins one,
    otherwise the first usable module of the slot whose band covers both frequencies wins.
    ``not_applicable``: malformed ids, invisible repeater, no usable online module, or a
    frequency outside every candidate's bands. ``not_supported``: cross-band (needs two
    modules, not yet implemented)."""
    from apps.repeaters import selection
    from apps.repeaters.models import Repeater

    if type(repeater_id) is not int or not isinstance(slot, str):
        return "not_applicable", None
    if module_id is not None and not isinstance(module_id, str):
        return "not_applicable", None
    repeater = Repeater.objects.visible().filter(pk=repeater_id).first()
    if repeater is None:
        return "not_applicable", None
    candidates = [
        m
        for m in selection.usable_modules(station)
        if m.slot == slot and (module_id is None or m.module_id == module_id)
    ]
    if not candidates:
        return "not_applicable", None
    module = None
    cross_band = False
    for candidate in candidates:
        rx_band = selection.module_band_of(candidate, repeater.output_frequency)
        tx_band = selection.module_band_of(candidate, repeater.input_frequency)
        if rx_band is not None and rx_band == tx_band:
            module = candidate
            break
        cross_band = cross_band or (rx_band is not None and tx_band is not None)
    if module is None:
        if not cross_band:
            bands = selection.station_bands(station)
            station_rx = selection.band_of(repeater.output_frequency, bands, station)
            station_tx = selection.band_of(repeater.input_frequency, bands, station)
            cross_band = station_rx is not None and station_tx is not None
            cross_band = cross_band and station_rx != station_tx
        return ("not_supported" if cross_band else "not_applicable"), None
    values = {
        "tx_frequency": float(repeater.input_frequency),
        "rx_frequency": float(repeater.output_frequency),
        "tx_tone": repeater.input_tone or "none",
        "rx_tone": repeater.output_tone or "none",
    }
    if repeater.bandwidth and selection.has_capability(module, "bandwidth"):
        values["bandwidth"] = repeater.bandwidth
    return None, {
        "slot": int(slot) if slot.isdigit() else slot,
        "module": module.module_id,
        "module_type": module.type,
        "callsign": repeater.callsign,
        "values": values,
    }


def _lock_status_with_drain(station, drain=lock.DRAIN_UNSET):
    """Lock frame payload plus ``drain`` — see ``lock.status_with_drain``."""
    return lock.status_with_drain(station, drain=drain)


def _lock_with_holder(station, scope="station"):
    """Fetch the lock with select_related('holder') to avoid an extra query."""
    from .models import ControlLock

    lock_obj, _ = ControlLock.objects.select_related("holder").get_or_create(
        station=station, scope=scope
    )
    return lock_obj


class ControlConsumer(AreaConsumerMixin, AsyncWebsocketConsumer):
    """Browser-facing control WebSocket. Path: ws/control/<station_id>/.

    Permissions (spec 2026-10-14 §3.4): connect = ``control.view`` (watch only);
    lock acquire/request/transfer, PTT keepalive and every non-``get`` command
    re-check ``control.use`` against a freshly loaded user, so a demotion bites on
    the next frame instead of on reconnect. Commands additionally need the
    capability policy's verb (``use``/``manage``); preempt follows
    ``can_override_station``. Relays holder commands/PTT and lock actions to the
    agent; pushes state/inventory/result/event + lock status to all viewers.

    Audit note: CONTROL_PTT keepalives arrive ~1/sec — auditing every one would
    spam the log. Instead we audit command frames whose capability == "ptt" (i.e.
    the PTT-on command itself). ptt_keepalive frames are NOT audited.
    """

    area = "control"

    async def connect(self):
        self.station_id = self.scope["url_route"]["kwargs"]["station_id"]
        self.group_name = f"control_{self.station_id}"
        self.agent_group_name = f"control_{self.station_id}_agent"
        self.pending = {}  # request_id -> asyncio.Task (command timeout)
        # request_id -> (slot, module, capability, value) of an in-flight set on a
        # persist-policy capability; persisted only once the agent reports ok.
        self._persist_pending = {}
        self.user = self.scope.get("user")
        self.station = None  # cached after connect to avoid a DB fetch per frame
        self._telemetry_frames = collections.deque()  # subscribe/unsubscribe budget

        await self.accept()

        if not self.user or self.user.is_anonymous:
            await self._reject(4401, "Not signed in — please sign in again")
            return
        station = await self._get_station()
        if station is None:
            await self._reject(4404, "Station not found")
            return
        self.station = station
        # control.view: forbidden frame + close 4403 on deny.
        if not await self.area_connect_gate():
            return

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        self.start_periodic_recheck()  # passive viewers never send a frame
        # Reconnect within grace keeps a held lock — but only while the user may still
        # operate (connect itself needs only control.view). A holder demoted while
        # disconnected loses the lock here instead of getting it back.
        if await self._fresh_can_use():
            await self._holder_reconnected(station)
        else:
            await self._revoke_if_holder(station)
        # Initial snapshot + lock status to just this browser, as TWO frames.
        # The initial lock uses the IDENTICAL {type:"lock",...} shape as every
        # later lock mutation, so D5 (the UI) wires a single lock handler and
        # renders the lock panel from the very first frame on load.
        await self.send(
            text_data=json.dumps(
                {
                    "v": constants.PROTOCOL_VERSION,
                    "type": "inventory",
                    "slots": await self._snapshot(station),
                }
            )
        )
        await self._send_lock_status(station)
        await self._send_scan_snapshot(station)
        await self._send_passive_snapshot(station)

    async def _send_passive_snapshot(self, station):
        """State of the passive service from the DB (works while the agent is offline), then
        ask the connected agent consumer to republish its fresher view (ineligible reason)."""
        try:
            run = await database_sync_to_async(PassiveServiceRun.open_for)(station.pk)
            if run is not None:  # nothing to say about an idle service: no extra frame
                await self.send(
                    text_data=json.dumps(
                        {
                            "type": "passive_state",
                            "service": "aprs",
                            "state": run.state,
                            "slot": run.slot,
                            "reason": run.last_error or None,
                        }
                    )
                )
            await self.channel_layer.group_send(
                self.agent_group_name, {"type": "control.passive_republish"}
            )
        except Exception:
            logger.exception("control: passive snapshot failed")

    async def _send_scan_snapshot(self, station):
        """A (re)loaded page must learn about a scan already running: one synthetic
        ``scan_state`` with the coarse stored state (no step — the next agent frame fills
        that in). ``starting`` is shown as ``active``."""
        try:
            current = await database_sync_to_async(scan.current_scan)(station)
        except Exception:
            logger.exception("control: reading the running scan failed")
            return
        if current is None:
            return
        scan_id, state = current
        await self.send(
            text_data=json.dumps(
                {
                    "v": constants.PROTOCOL_VERSION,
                    "type": "scan_state",
                    "scan_id": scan_id,
                    "state": state if state in scan.NOTED_STATES else "active",
                    "index": None,
                    "ref": None,
                    "slot": None,
                    "module": None,
                }
            )
        )

    async def _reject(self, code, reason):
        """Accept-then-error reject so the browser sees a real reason."""
        try:
            await self.send(
                text_data=json.dumps({"type": "error", "reason": reason, "code": code})
            )
        finally:
            await self.close(code=code)

    async def disconnect(self, close_code):
        await self.stop_periodic_recheck()
        pending = list(self.pending.values())
        self.pending.clear()
        for task in pending:
            task.cancel()
        # Await the cancelled tasks so they finish tearing down before the
        # consumer goes away — otherwise asyncio logs "Task was destroyed but
        # it is pending" on shutdown/reload and cleanup may be left unfinished.
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._persist_pending.clear()
        station = await self._cached_station()
        if station is not None and self.user and not self.user.is_anonymous:
            await self._holder_disconnected(station)
            await self._telemetry_drop_channel(station)
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        if text_data is None:
            return
        try:
            msg = json.loads(text_data)
        except json.JSONDecodeError:
            return
        mtype = msg.get("type")
        station = await self._cached_station()
        if station is None:
            return

        # Operating actions re-check control.use with a fresh user on every frame.
        # EVERY command counts (incl. op:get): any command touches the lock, so a
        # demoted holder could otherwise keep it alive forever with reads.
        # subscribe/unsubscribe and lock_release stay open to every connected viewer
        # (release only ever affects the caller's own lock).
        if mtype in _USE_ACTIONS or mtype == "command":
            if not await self.area_allows("use", request_id=msg.get("request_id")):
                # A holder who lost control.use must not keep the lock (and TX path).
                await self._revoke_if_holder(station)
                return

        if mtype == "command":
            await self._handle_command(station, msg)
        elif mtype == "apply_repeater":
            await self._handle_apply_repeater(station, msg)
        elif mtype == "scan_start":
            await self._handle_scan_start(station, msg)
        elif mtype == "scan_control":
            await self._handle_scan_control(station, msg)
        elif mtype == "ptt_keepalive":
            if await self._touch_if_holder(station):
                await self._relay(msg)
                # Refresh PTT dead-man and notify audio consumers.
                await self._audio_refresh_ptt(station)
                await self._bridge_gate(station)
            else:
                await self._error(msg.get("request_id"), "not_locked", "You do not hold the lock")
        elif mtype in ("subscribe", "unsubscribe"):
            # Any viewer may subscribe — control.view checked at connect. Ref-counted:
            # the agent's subscription is station-wide (see telemetry_subscriptions).
            await self._telemetry_change(station, msg, mtype)
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
        allowed, module_type = await self._authorize_command(station, msg)
        if not allowed:
            return
        await self._relay(msg)
        request_id = msg.get("request_id")
        self._remember_persist(request_id, msg, module_type)
        if request_id is not None:
            # A reused request_id must not orphan the previous timeout task —
            # cancel it first, else two timers fire for the same id and the
            # overwritten task can GC while pending ("Task was destroyed").
            existing = self.pending.pop(request_id, None)
            if existing is not None:
                existing.cancel()
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
        # Audio cross-plane glue (lazy import to avoid app-cycle at module load).
        if capability == "ptt":
            value = msg.get("value")
            if value:
                slot = msg.get("slot")
                module = msg.get("module", "")
                await self._audio_set_ptt(station, slot, module)
            else:
                await self._audio_clear_ptt(station)
            await self._bridge_gate(station)
        elif capability == "tx_route":
            value = msg.get("value")
            if isinstance(value, dict):
                await self._audio_set_tx_route(station, value.get("slot"), value.get("module", ""))
                await self._bridge_gate(station)
            elif value is None:
                await self._audio_clear_tx_route(station)
                await self._bridge_gate(station)
            # A malformed value (e.g. a bool/str) is ignored here — it was
            # already relayed to the agent; we just don't crash the socket or
            # corrupt the gate. No bridge broadcast on a no-op.

    async def _handle_apply_repeater(self, station, msg):
        """One click in the repeater panel (spec §5.3): resolve the repeater server-side
        into a single agent ``apply_preset`` for the chosen slot. Same gates as setting
        the caps by hand (lock + per-capability write); the agent refuses while PTT is
        active. The aggregated ``result`` comes back through ``control_result``."""
        request_id = msg.get("request_id")
        if not await self._touch_if_holder(station):
            await self._error(request_id, "not_locked", "You do not hold the lock")
            return
        try:
            resolved = await self._resolve_repeater(
                station, msg.get("repeater_id"), msg.get("slot"), msg.get("module")
            )
        except Exception:
            logger.exception("control: resolving apply_repeater failed")
            resolved = ("not_applicable", None)
        code, plan = resolved
        if code is not None:
            await self._error(request_id, code, gettext("Repeater cannot be applied here"))
            return
        try:
            denied = [
                cap
                for cap in plan["values"]
                if not await self._may_write(cap, plan["module_type"])
            ]
        except Exception:
            logger.exception("control: apply_repeater permission check failed; denying")
            denied = list(plan["values"])
        if denied:
            await self._audit(
                station,
                "control_command",
                f"{self.user.username} apply repeater {_clip(plan['callsign'])} "
                f"denied ({', '.join(denied)})",
            )
            await self.send(
                text_data=json.dumps(
                    {
                        "type": "error",
                        "code": "forbidden",
                        "message": gettext("Repeater could not be authorized"),
                        "request_id": request_id,
                    }
                )
            )
            return
        await self._relay(
            {
                "v": constants.PROTOCOL_VERSION,
                "type": "apply_preset",
                "request_id": request_id,
                "slot": plan["slot"],
                "module": plan["module"],
                "values": plan["values"],
            }
        )
        if isinstance(request_id, str) or type(request_id) is int:
            existing = self.pending.pop(request_id, None)
            if existing is not None:
                existing.cancel()
            # Frequency/tone are not persist-policy caps: a stale entry must not persist.
            self._persist_pending.pop(request_id, None)
            timeout = getattr(django.conf.settings, "CONTROL_PRESET_TIMEOUT_SECONDS", 30)
            self.pending[request_id] = asyncio.create_task(
                self._command_timeout(request_id, timeout)
            )
        await self._audit(
            station,
            "control_command",
            f"{self.user.username} applied repeater {_clip(plan['callsign'])} "
            f"on {_clip(plan['slot'])}/{_clip(plan['module'])}",
        )

    async def _handle_scan_start(self, station, msg):
        """Start a scan (spec §7.1): the server builds the effective list and hands it to
        the agent, which runs the loop. Refused (no agent frame) without the lock, without
        write on the frequency/tone capabilities, for a radius without station coordinates,
        or when the effective list is empty."""
        request_id = msg.get("request_id")
        if not await self._touch_if_holder(station):
            await self._error(request_id, "not_locked", "You do not hold the lock")
            return
        try:
            code, frame = await self._start_scan(station, msg)
        except Exception:
            logger.exception("control: starting scan failed")
            code, frame = "internal_error", None
        if code is not None:
            await self._error(request_id, code, _SCAN_ERRORS.get(code, code))
            return
        await self._relay(frame)

    async def _handle_scan_control(self, station, msg):
        """stop/skip/hold/resume for the station's running scan (lock holder only)."""
        request_id = msg.get("request_id")
        action = msg.get("action")
        if not await self._touch_if_holder(station):
            await self._error(request_id, "not_locked", "You do not hold the lock")
            return
        if action not in scan.SCAN_ACTIONS:
            await self._error(request_id, "invalid_action", "Unknown scan action")
            return
        if action == "stop":
            # Server-side stop: the row goes now (an unreachable agent can't strand it).
            scan_id = await self._stop_scan(station, "operator")
        else:
            scan_id = await database_sync_to_async(scan.current_scan_id)(station)
            if scan_id is not None:
                await self._relay(
                    {
                        "v": constants.PROTOCOL_VERSION,
                        "type": "scan_control",
                        "scan_id": scan_id,
                        "action": action,
                    }
                )
        if scan_id is None:
            await self._error(request_id, "no_active_scan", "No scan is running")

    async def _authorize_command(self, station, msg):
        """Server-side capability gate (``control.<policy verb>``, spec §3.4) — the real
        authz check; the read-only UI render is cosmetic. Every capability command a
        browser can reach the agent with passes through here (``_handle_command`` is the
        only relay of ``type: command``).

        Any op other than ``get`` is a write. Fail closed: any exception while resolving
        the module type or the user's level denies the command. Returns
        ``(allowed, module_type)``.
        """
        capability = msg.get("capability")
        if msg.get("op") == "get":
            return True, None
        module_type = None
        try:
            module_type = await self._lookup_module_type(
                station, msg.get("slot"), msg.get("module")
            )
            allowed = await self._may_write(capability, module_type)
        except Exception:
            logger.exception("control: capability permission check failed; denying command")
            allowed = False
        if allowed is True:
            return True, module_type
        verb = capability_policy.policy_for(capability, module_type).verb
        try:
            await self._audit(
                station,
                "control_command",
                f"{self.user.username} {_clip(msg.get('op'))} {_clip(capability)} "
                f"on {_clip(msg.get('slot'))}/{_clip(msg.get('module'))} "
                f"denied (requires control.{verb})",
            )
        except Exception:
            logger.exception("control: audit of denied command failed")
        request_id = msg.get("request_id")
        # Standard forbidden frame + PERMISSION_DENIED audit. If the user does hold the
        # verb (the denial came from a failed resolution), still deny — fail closed.
        try:
            send_generic = await self.area_allows(verb, request_id=request_id)
        except Exception:
            logger.exception("control: denial notification failed")
            send_generic = True
        if send_generic:
            frame = {
                "type": "error",
                "code": "forbidden",
                "message": "Command could not be authorized",
            }
            if request_id is not None:
                frame["request_id"] = request_id
            await self.send(text_data=json.dumps(frame))
        return False, module_type

    def _remember_persist(self, request_id, msg, module_type):
        """Track an in-flight ``set`` on a persist-policy capability so ``control_result``
        can persist it once the agent reports success (not on send)."""
        if not (isinstance(request_id, str) or type(request_id) is int):
            return
        # A reused request_id must not inherit a stale pending persist.
        self._persist_pending.pop(request_id, None)
        capability = msg.get("capability")
        policy = capability_policy.policy_for(capability, module_type)
        if msg.get("op") == "set" and policy.persist:
            self._persist_pending[request_id] = (
                msg.get("slot"),
                msg.get("module"),
                capability,
                msg.get("value"),
            )

    async def _command_timeout(self, request_id, timeout=None):
        """Fire a timeout error to the browser if no result arrives in time.

        Reads the timeout from settings at call-time (not the module-level
        constant) so the Task 7 settings fixture can override it without
        an importlib reload.
        """
        try:
            if timeout is None:
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

    # -- telemetry subscriptions (ref-counted across viewers) ------------------

    def _telemetry_budget_ok(self) -> bool:
        """Per-connection frame budget for subscribe/unsubscribe (flood guard)."""
        now = asyncio.get_running_loop().time()
        window = self._telemetry_frames
        while window and now - window[0] > telemetry_subscriptions.FRAME_WINDOW_SECONDS:
            window.popleft()
        if len(window) >= telemetry_subscriptions.FRAME_BUDGET:
            return False
        window.append(now)
        return True

    async def _telemetry_change(self, station, msg, op):
        addr = telemetry_subscriptions.parse_addr(msg)
        if addr is None or not self._telemetry_budget_ok():
            return
        slot, module, caps = addr
        if op == "subscribe":
            frames = await database_sync_to_async(telemetry_subscriptions.subscribe)(
                station, self.channel_name, slot, module, caps, msg.get("interval_ms")
            )
        else:
            frames = await database_sync_to_async(telemetry_subscriptions.unsubscribe)(
                station, self.channel_name, slot, module, caps
            )
        for frame in frames:
            await self._relay(frame)

    async def on_periodic_recheck(self, user):
        """Still allowed to watch (control.view). A holder who lost control.use is
        revoked here too: a silent holder sends no frames, so the per-frame check
        alone would let them keep the lock until the idle sweep. Then stamp this
        connection's telemetry rows as live; a failed stamp must not count as a
        failed permission re-check (would close the feed)."""
        station = self.station
        if station is None:
            return
        if not can(user, "control", "use"):
            await self._revoke_if_holder(station)
        try:
            await database_sync_to_async(telemetry_subscriptions.touch)(station, self.channel_name)
        except Exception:
            logger.exception("control: telemetry liveness stamp failed")

    async def _telemetry_drop_channel(self, station):
        try:
            frames = await database_sync_to_async(telemetry_subscriptions.drop_channel)(
                station, self.channel_name
            )
            for frame in frames:
                await self._relay(frame)
        except Exception:
            logger.exception("control: dropping telemetry subscriptions failed")

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
        acquired = await self._acquire(station)
        if acquired:
            await self._audit(
                station, "control_lock_acquired", f"{self.user.username} acquired control"
            )
            await lock_hooks.on_lock_acquired(
                station, self.user, "acquired", previous_holder_lost=False
            )
        if acquired is lock.DRAIN_REFUSED:
            await self._send_drain_refusal()
        await self._broadcast_lock(station)

    async def _revoke_if_holder(self, station):
        """Release the caller's lock after they lost control.use; kill PTT + TX route.

        No-op unless the caller is the current holder."""
        if not await self._release(station):
            return
        await self._audit(
            station,
            "control_lock_released",
            f"{self.user.username} lost control (requires control.use)",
        )
        await lock_hooks.on_lock_released(station, "revoked", clear_tx_route=True)
        await self._broadcast_lock(station)

    async def _lock_release(self, station):
        released = await self._release(station)
        if released:
            await self._audit(
                station, "control_lock_released", f"{self.user.username} released control"
            )
            # Lock loss -> scan, PTT and the audio gate (see lock_hooks).
            await lock_hooks.on_lock_released(station, "released")
        await self._broadcast_lock(station)

    async def _lock_request(self, station):
        holder = await self._request(station)
        if holder is lock.DRAIN_REFUSED:
            await self._send_drain_refusal()
            return
        if holder:
            await self.channel_layer.group_send(
                self.group_name,
                {
                    "type": "control.control_requested",
                    "holder_id": holder.holder_id,
                    "requester": {"id": self.user.id, "username": self.user.username},
                },
            )

    async def _lock_transfer(self, station, to_user_id):
        transferred = to_user_id is not None and await self._transfer(station, to_user_id)
        if transferred:
            await self._audit(
                station,
                "control_lock_transferred",
                f"{self.user.username} -> user {to_user_id}",
            )
            # Holder changed -> old holder can no longer key TX nor scan.
            to_user = await self._get_user(to_user_id)
            await lock_hooks.on_lock_acquired(
                station, to_user, "transferred", previous_holder_lost=True
            )
        if transferred is lock.DRAIN_REFUSED:
            await self._send_drain_refusal()
        await self._broadcast_lock(station)

    async def _lock_preempt(self, station):
        if not await self._can_override(station):
            # Override is an assignment rule, not an area right: same forbidden frame
            # shape, but no PERMISSION_DENIED audit (require() is deliberately not used).
            message = gettext(
                "Keine Berechtigung — Übernehmen der Steuerung erfordert %(manage)s oder "
                "eine Station-Admin-/Region-Manager-Zuweisung (mit Bedien-Recht)."
            ) % {"manage": level_label(required_level("control", "manage"))}
            await self.send(
                text_data=json.dumps({"type": "error", "code": "forbidden", "message": message})
            )
            return
        if await self._preempt(station) is lock.DRAIN_REFUSED:
            # A free lock while draining: preempting it would be a new grant.
            await self._send_drain_refusal()
            await self._send_lock_status(station)
            return
        await self._audit(
            station, "control_lock_preempted", f"{self.user.username} preempted control"
        )
        # Holder changed by preempt -> PTT and the previous holder's scan die.
        await lock_hooks.on_lock_acquired(
            station, self.user, "preempted", previous_holder_lost=True
        )
        await self._broadcast_lock(station)

    async def _send_drain_refusal(self):
        await self.send(
            text_data=json.dumps(
                {
                    "type": "error",
                    "code": "firmware_update_pending",
                    "message": lock.FIRMWARE_DRAIN_MESSAGE,
                }
            )
        )

    async def _broadcast_lock(self, station):
        await self._group_send_lock(await self._lock_status(station))

    async def _group_send_lock(self, status):
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

    async def control_scan_state(self, event):
        await self.send(text_data=json.dumps(event["msg"]))

    async def control_passive(self, event):
        await self.send(text_data=json.dumps(event["msg"]))

    async def control_result(self, event):
        msg = event["msg"]
        rid = msg.get("request_id")
        task = self.pending.pop(rid, None)
        if task is not None:
            task.cancel()
        entry = (
            self._persist_pending.pop(rid, None)
            if isinstance(rid, str) or type(rid) is int
            else None
        )
        await self.send(text_data=json.dumps(msg))
        if entry is not None and msg.get("ok") is True:
            await self._persist_result(*entry)

    async def control_event(self, event):
        await self.send(text_data=json.dumps(event["msg"]))

    async def control_lock(self, event):
        # Lost (or never held) the lock → this consumer's in-flight results must never
        # persist: a new holder may reuse the same request_id.
        if not (self.user and event["lock"].get("holder_id") == self.user.id):
            self._persist_pending.clear()
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

    async def _cached_station(self):
        """Return the Station, fetching once and caching on the instance.

        The station is fixed for the connection's life, so re-querying it on
        every incoming browser frame (incl. high-frequency ptt_keepalive) is
        wasted work. Falls back to a DB fetch if the cache is somehow unset.
        """
        if self.station is None:
            self.station = await self._get_station()
        return self.station

    @database_sync_to_async
    def _get_station(self):
        from apps.stations.models import Station

        try:
            return Station.objects.get(pk=self.station_id)
        except Station.DoesNotExist:
            return None

    @database_sync_to_async
    def _fresh_can_use(self):
        """control.use for the fresh DB row — a silent check (no denial audit)."""
        user = _fresh_user(self.user)
        return user is not None and can(user, "control", "use")

    @database_sync_to_async
    def _can_override(self, station):
        user = _fresh_user(self.user)
        return user is not None and can_override_station(user, station)

    @database_sync_to_async
    def _lookup_module_type(self, station, slot, module):
        """Registered type of the addressed module, or ``None`` if unknown/malformed.
        ``None`` still resolves the capability-name policy, so an unknown module id
        cannot bypass the gate."""
        if not isinstance(module, str) or not (isinstance(slot, str) or type(slot) is int):
            return None
        return (
            StationModule.objects.filter(station=station, slot=str(slot), module_id=module)
            .values_list("type", flat=True)
            .first()
        )

    @database_sync_to_async
    def _resolve_repeater(self, station, repeater_id, slot, module_id):
        """``(error_code, None)`` or ``(None, plan)`` for an ``apply_repeater`` frame."""
        return _resolve_repeater(station, repeater_id, slot, module_id)

    @database_sync_to_async
    def _may_write(self, capability, module_type):
        user = _fresh_user(self.user)
        if user is None:
            return False
        return capability_policy.can_write(user, capability, module_type)

    async def _persist_result(self, slot, module, capability, value):
        try:
            station = await self._cached_station()
            if station is None:
                return
            if await self._persist(station, slot, module, capability, value):
                await self._audit(
                    station,
                    "control_command",
                    f"{self.user.username} persisted {capability}={value!r}",
                )
        except Exception:
            logger.exception("control: persisting %s failed", capability)

    @database_sync_to_async
    def _persist(self, station, slot, module, capability, value):
        # Re-check at persist time (the lock broadcast may not have arrived yet): only the
        # current holder, still allowed to write this capability, may persist.
        if _lock_with_holder(station).holder_id != self.user.id:
            return False
        user = _fresh_user(self.user)
        if user is None or not capability_policy.can_write(user, capability, None):
            return False
        return persistence.persist_if_valid(station, slot, module, capability, value, self.user)

    @database_sync_to_async
    def _snapshot(self, station):
        """Build the connect-time inventory in the SAME slots-grouped shape as
        the agent's live ``inventory`` frame, so D5 wires one handler for
        ``type:"inventory"``. Adds a per-module ``online`` flag (additive) so
        offline modules can still render from persisted descriptors/last_state.
        """
        from . import serializers

        return serializers.snapshot(station)

    @database_sync_to_async
    def _start_scan(self, station, msg):
        user = _fresh_user(self.user)
        if user is None:
            return "forbidden", None
        return scan.start_scan(station, user, msg)

    async def _stop_scan(self, station, reason, unless_user=None):
        """Stop the station's scan (row + audit + agent stop); returns its scan_id."""
        try:
            return await database_sync_to_async(scan.stop_active_scan)(
                station, reason, unless_user=unless_user
            )
        except Exception:
            logger.exception("control: stopping scan failed")
            return None

    @database_sync_to_async
    def _get_user(self, user_id):
        from apps.accounts.models import User

        return User.objects.filter(pk=user_id).first()

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
        return _lock_status_with_drain(station)

    @database_sync_to_async
    def _audit(self, station, event_type, message):
        from apps.stations.models import StationAuditLog

        StationAuditLog.log(
            station=station, event_type=event_type, message=message, user=self.user
        )

    # -- audio cross-plane glue (lazy import to avoid circular app dependency) --

    async def _bridge_gate(self, station):
        """Broadcast audio.gate to browser and agent audio groups.

        Broadcasts the msgpack-safe wire state (no datetime) so the payload
        round-trips through the prod channels_redis (msgpack) layer.
        """
        from apps.audio import constants as audio_constants

        state = await self._get_gate_state(station)
        payload = {"type": "audio.gate", "state": state}
        await self.channel_layer.group_send(audio_constants.browser_group(station.pk), payload)
        await self.channel_layer.group_send(audio_constants.agent_group(station.pk), payload)

    @database_sync_to_async
    def _get_gate_state(self, station):
        from apps.audio import gate as audio_gate

        return audio_gate.get_wire_state(station)

    @database_sync_to_async
    def _audio_set_ptt(self, station, slot, module):
        from apps.audio import gate as audio_gate

        audio_gate.set_ptt(station, slot, module)

    @database_sync_to_async
    def _audio_clear_ptt(self, station):
        from apps.audio import gate as audio_gate

        audio_gate.clear_ptt(station)

    @database_sync_to_async
    def _audio_refresh_ptt(self, station):
        from apps.audio import gate as audio_gate

        audio_gate.refresh_ptt(station)

    @database_sync_to_async
    def _audio_set_tx_route(self, station, slot, module):
        from apps.audio import gate as audio_gate

        audio_gate.set_tx_route(station, slot, module)

    @database_sync_to_async
    def _audio_clear_tx_route(self, station):
        from apps.audio import gate as audio_gate

        audio_gate.clear_tx_route(station)
