# FM-Modul „offline" Fix + Audio/Control-Flag-Removal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ein einmaliges Discovery-Race darf ein Modul nie dauerhaft offline lassen; Control läuft immer, Audio wird auto-erkannt, und die `audio_enabled`/`control_enabled`-Flags verschwinden.

**Architecture:** Agent-seitig wird `probe_slot` gegen zerrissene `MODULE-LIST`-Antworten robust (Retry + Framing) und `control_client` re-discovered periodisch/bei leerem Inventory und re-emittet. Server-seitig fällt `control_enabled` (hardcoded) und `audio_enabled` (DB-Feld) weg; Control ist immer an, Audio wird zur Laufzeit aus einem vorhandenen Audio-Pfad (udev-getaggte ALSA-Karte) abgeleitet.

**Tech Stack:** Django 6.0, Python 3.14, pyserial, websockets, pytest. `station_agent` als Python-Paket (SRCREV-gepinnt ins linux-image).

**Spec:** `docs/superpowers/specs/2026-09-09-fm-module-offline-fix.md`

## Global Constraints

- NUR `station-manager` anfassen. Kein `linux-image`-SRCREV-Bump in diesem PR (nur im PR-Text erwähnen). FW-Repo nicht anfassen.
- EIN Feature-Branch, EIN PR am Ende. Keine Doc-only-Zwischen-PRs. Squash-Merge-Konvention, aber **nicht autonom mergen** (DB-Migration → User merged nach Review).
- Immer neueste stabile Versionen; keine alten Defaults.
- Django-Template-Regel: kein multi-line `{# … #}` (hier nicht relevant, aber Guard bleibt aktiv).
- Tests laufen mit `pytest` aus dem Repo-Root. Agent-Tests importieren `station_agent.*`, Server-Tests brauchen `@pytest.mark.django_db`.
- Serial-Boundary-Ehrlichkeit: „gefixt" erst wenn `selftest serial --slot 3` auf echter CM4 grün ist (HIL-Schritt am Ende).

---

### Task 1: `probe_slot` — Retry + Framing-Härtung (Agent)

Macht `probe_slot` robust gegen eine `MODULE-LIST`-Antwort, die durch eine async Log-/Status-Zeile zerrissen wurde: der `module list`-Befehl wird 2–3× mit kurzem Settle wiederholt, und vor jedem Retry wird die Leitung neu gedrained, damit periodischer Async-Traffic absorbiert wird.

**Files:**
- Modify: `station_agent/slot_discovery.py`
- Test: `tests/test_slot_discovery.py`

**Interfaces:**
- Consumes: nichts Neues.
- Produces: `probe_slot(control_path, timeout=3.0, trace=False, list_retries=3)` — neuer Keyword-Parameter `list_retries` (Default 3). Rückgabetyp unverändert (`list[dict] | None`). Neue Modulkonstante `_LIST_RETRY_SETTLE = 0.3` (Sekunden zwischen Retries).

- [ ] **Step 1: Failing-Test — zerrissene erste MODULE-LIST, Retry rettet**

In `tests/test_slot_discovery.py` eine neue Fake-Firmware-Variante ergänzen, die auf das ERSTE `module list` eine durch eine Log-Zeile zerrissene Antwort schickt und erst auf das ZWEITE eine saubere. Am Dateiende einfügen:

```python
def _fake_firmware_flaky_list(master_fd, stop, describe, fail_first=1):
    """Answers the first `fail_first` `module list` commands with a MODULE-LIST line
    that has an async log line spliced INTO its JSON payload (so json.loads fails on
    that line), then answers cleanly. `module <id> describe` always answers cleanly."""
    os.set_blocking(master_fd, False)

    def w(s: str):
        try:
            os.write(master_fd, s.encode())
        except (BlockingIOError, OSError):
            pass

    seen = 0
    buf = b""
    while not stop.is_set():
        try:
            chunk = os.read(master_fd, 1024)
        except BlockingIOError:
            time.sleep(0.005)
            continue
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if _LIST_RE.search(line):
                nonlocal_seen = seen
                if nonlocal_seen < fail_first:
                    seen += 1
                    # A Zephyr log line spliced into the middle of the MODULE-LIST payload:
                    w('MODULE-LIST {"mod')
                    w("[00:00:01.234] <inf> fm: periodic status\r\n")
                    w('ules":["fm"]}\r\n')
                    w("fm> ")
                else:
                    w('MODULE-LIST {"modules":["fm"]}\r\n')
                    w("fm> ")
                continue
            m = _DESCRIBE_RE.search(line)
            if m:
                w("MODULE-DESCRIBE " + json.dumps(describe) + "\r\n")
                w("fm> ")


def test_probe_slot_retries_after_corrupted_list(tmp_path):
    master_fd, slave_fd = os.openpty()
    stop = threading.Event()
    t = threading.Thread(
        target=_fake_firmware_flaky_list, args=(master_fd, stop, FM_DESCRIBE, 1), daemon=True
    )
    t.start()
    try:
        link = tmp_path / "control"
        link.symlink_to(os.ttyname(slave_fd))
        modules = slot_discovery.probe_slot(str(link), timeout=3.0, list_retries=3)
    finally:
        stop.set()
        os.close(master_fd)
        os.close(slave_fd)
        t.join(timeout=1)
    assert modules is not None
    assert [m["id"] for m in modules] == ["fm"]
```

- [ ] **Step 2: Test ausführen, Fehlschlag bestätigen**

Run: `pytest tests/test_slot_discovery.py::test_probe_slot_retries_after_corrupted_list -v`
Expected: FAIL — ohne Retry liefert `probe_slot` `None` (bzw. `TypeError` weil `list_retries` noch kein Parameter ist).

- [ ] **Step 3: Retry in `probe_slot` implementieren**

In `station_agent/slot_discovery.py` die Konstante ergänzen (bei den anderen `_BOOT_*`-Konstanten):

```python
# Between `module list` retries: let the module's async log/status burst pass, then re-drain.
_LIST_RETRY_SETTLE = 0.3
```

Signatur erweitern und die List-Command-Sequenz in eine Retry-Schleife wickeln. `probe_slot` so anpassen:

```python
def probe_slot(
    control_path: str, timeout: float = 3.0, trace: bool = False, list_retries: int = 3
) -> list[dict] | None:
```

und den Block ab `deadline = ...` bis inkl. `if not isinstance(ids, list): return None` ersetzen durch:

```python
        deadline = time.monotonic() + timeout

        # `module list` can arrive corrupted when an async Zephyr log/status line is spliced
        # into its MODULE-LIST payload (the JSON on that line then fails to parse). Retry a
        # few times, re-draining between attempts to absorb the periodic status burst — a
        # single race must never leave a present module undiscovered.
        listing = None
        for attempt in range(max(1, list_retries)):
            if attempt > 0:
                time.sleep(_LIST_RETRY_SETTLE)
                _drain_until_quiet(ser, _BOOT_QUIET, _BOOT_MAX)
            listing = _command(ser, _LIST_CMD, _LIST_PREFIX, deadline, control_path, trace=trace)
            if listing is not None:
                break
            if time.monotonic() >= deadline:
                break
        if listing is None:
            logger.debug("slot probe: no MODULE-LIST from %s", control_path)
            return None
        # Fail closed if `modules` is missing or not a list; a present empty list
        # legitimately means "firmware responded, no modules".
        ids = listing.get("modules")
        if not isinstance(ids, list):
            return None
```

- [ ] **Step 4: Test ausführen, Erfolg bestätigen**

Run: `pytest tests/test_slot_discovery.py::test_probe_slot_retries_after_corrupted_list -v`
Expected: PASS

- [ ] **Step 5: Framing-Regression absichern — async Status-Zeile wird ignoriert**

Test ergänzen, der beweist, dass eine periodische Status-Zeile VOR der sauberen MODULE-LIST ignoriert wird (Framing). Am Dateiende einfügen:

```python
def test_probe_slot_ignores_async_status_lines(tmp_path):
    # Firmware emits a periodic status line, THEN the MODULE-LIST on the same read.
    def fw(master_fd, stop):
        os.set_blocking(master_fd, False)
        buf = b""
        while not stop.is_set():
            try:
                chunk = os.read(master_fd, 1024)
            except BlockingIOError:
                time.sleep(0.005); continue
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if _LIST_RE.search(line.strip()):
                    os.write(master_fd, b"03 Status - Power: ON, PTT: OFF, SQL: CLOSED\r\n")
                    os.write(master_fd, b'MODULE-LIST {"modules":["fm"]}\r\n')
                elif _DESCRIBE_RE.search(line.strip()):
                    os.write(master_fd, ("MODULE-DESCRIBE " + json.dumps(FM_DESCRIBE) + "\r\n").encode())

    master_fd, slave_fd = os.openpty()
    stop = threading.Event()
    t = threading.Thread(target=fw, args=(master_fd, stop), daemon=True)
    t.start()
    try:
        link = tmp_path / "control"
        link.symlink_to(os.ttyname(slave_fd))
        modules = slot_discovery.probe_slot(str(link), timeout=2.0)
    finally:
        stop.set(); os.close(master_fd); os.close(slave_fd); t.join(timeout=1)
    assert [m["id"] for m in modules] == ["fm"]
```

- [ ] **Step 6: Framing-Test ausführen**

Run: `pytest tests/test_slot_discovery.py::test_probe_slot_ignores_async_status_lines -v`
Expected: PASS (Framing existiert bereits; dieser Test verankert es als Regression-Guard).

- [ ] **Step 7: Volle Discovery-Suite grün**

Run: `pytest tests/test_slot_discovery.py -v`
Expected: alle PASS (bestehende + neue Tests).

- [ ] **Step 8: Commit**

```bash
git add station_agent/slot_discovery.py tests/test_slot_discovery.py
git commit -m "fix(agent): retry module-list probe against interleaved async console output"
```

---

### Task 2: `control_client` — periodische Re-Discovery + Re-Emit (Agent)

Sendet Inventory nicht nur einmal beim Connect: eine Hintergrund-Task re-discovered periodisch, und wenn sich das Inventory ändert (insb. leer → gefüllt), wird `set_inventory` + `emit_inventory` erneut aufgerufen. Cancel bei Disconnect.

**Files:**
- Modify: `station_agent/control_client.py`
- Modify: `station_agent/config.py` (neues Feld `control_rediscovery_interval`)
- Modify: `station_agent/config.example.yml` (dokumentiere neues Feld)
- Test: `tests/test_control_client.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `discover_slots(base, trace=)` (Task 1 unverändert), `Broker.set_inventory(list)`, `Broker.emit_inventory()`.
- Produces: `AgentConfig.control_rediscovery_interval: float = 30.0`. Neue private Methode `ControlClient._rediscovery_loop(broker, loop)` (async), gestartet als `asyncio.Task` in `_connect_and_serve`, gecancelt im `finally`.

- [ ] **Step 1: Config-Feld — Failing-Test**

In `tests/test_config.py` ergänzen:

```python
def test_control_rediscovery_interval_default_and_override(tmp_path, monkeypatch):
    from station_agent.config import CONFIG_PATH_ENV, load_config

    p = tmp_path / "c.yml"
    p.write_text(
        "server_url: http://x\nstation_id: 1\ned25519_key_path: /k.pem\n"
        "control_rediscovery_interval: 5.0\n"
    )
    monkeypatch.setenv(CONFIG_PATH_ENV, str(p))
    cfg = load_config()
    assert cfg.control_rediscovery_interval == 5.0
```

- [ ] **Step 2: Test ausführen, Fehlschlag bestätigen**

Run: `pytest tests/test_config.py::test_control_rediscovery_interval_default_and_override -v`
Expected: FAIL — `AttributeError: control_rediscovery_interval`.

- [ ] **Step 3: Config-Feld implementieren**

In `station_agent/config.py` im Dataclass-Block (nach `telemetry_min_floor_ms`) ergänzen:

```python
    # Control channel re-discovery: re-scan slots this often (s) and re-emit inventory if it
    # changed. A single startup race that yielded an empty inventory recovers on the next scan.
    control_rediscovery_interval: float = 30.0
```

Und im `load_config()`-Konstruktor (nach `telemetry_min_floor_ms=...`) ergänzen:

```python
        control_rediscovery_interval=float(data.get("control_rediscovery_interval", 30.0)),
```

- [ ] **Step 4: Test ausführen, Erfolg bestätigen**

Run: `pytest tests/test_config.py::test_control_rediscovery_interval_default_and_override -v`
Expected: PASS

- [ ] **Step 5: Re-Discovery — Failing-Test**

In `tests/test_control_client.py` neuen Test ergänzen. Er startet mit LEEREM Slot-Tree, verbindet, erwartet ein erstes (leeres) Inventory, legt DANN den Slot an und erwartet ein zweites Inventory mit `fm`. `_FakeConfig` um `control_rediscovery_interval` erweitern:

```python
def test_control_client_rediscovers_and_reemits_inventory(tmp_path):
    import os as _os

    fw = FakeFirmware({"fm": FM})
    fw.start()
    base = str(tmp_path / "oe5xrx")
    _os.makedirs(base, exist_ok=True)  # empty at connect time
    key_path = _gen_key(tmp_path)

    inventories = []
    second = asyncio.Event()

    async def server(ws):
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "inventory":
                inventories.append(msg)
                if len(inventories) == 1:
                    # After the first (empty) inventory, plug the module in.
                    slot_dir = _os.path.join(base, "slot1")
                    _os.makedirs(slot_dir, exist_ok=True)
                    _os.symlink(fw.control_path, _os.path.join(slot_dir, "control"))
                elif len(inventories) >= 2 and msg["slots"]:
                    second.set()

    async def scenario():
        async with websockets.serve(server, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            cfg = _FakeConfig(f"http://127.0.0.1:{port}", 1, key_path, base)
            cfg.control_rediscovery_interval = 0.5
            client = ControlClient(cfg)
            loop = asyncio.get_running_loop()
            t = loop.run_in_executor(None, client.run)
            try:
                await asyncio.wait_for(second.wait(), timeout=10.0)
            finally:
                client.stop()
                await asyncio.wait_for(t, timeout=5.0)

    try:
        asyncio.run(scenario())
    finally:
        fw.stop()

    assert inventories[0]["slots"] == []
    assert any(s["slot"] == 1 for s in inventories[-1]["slots"])
```

Außerdem `_FakeConfig.__init__` um ein Default-Attribut ergänzen (damit der bestehende Test unverändert bleibt):

```python
        self.control_rediscovery_interval = 30.0
```

- [ ] **Step 6: Test ausführen, Fehlschlag bestätigen**

Run: `pytest tests/test_control_client.py::test_control_client_rediscovers_and_reemits_inventory -v`
Expected: FAIL — es kommt nur EIN Inventory (kein Re-Emit).

- [ ] **Step 7: Re-Discovery-Loop implementieren**

In `station_agent/control_client.py` in `_connect_and_serve` nach `logger.info("Control: connected, inventory sent")` die Re-Discovery-Task starten und im `finally` canceln. Konkret den `try/except/finally`-Block um den `async for message` so umbauen:

```python
            rediscovery = loop.create_task(self._rediscovery_loop(broker, loop, discovered))
            try:
                async for message in ws:
                    if self._shutdown.is_set():
                        break
                    try:
                        parsed = parse_message(message)
                    except ProtocolError as exc:
                        logger.warning("Control: dropping malformed message: %s", exc)
                        continue
                    await broker.handle(parsed)
            except websockets.exceptions.ConnectionClosed as exc:
                logger.info("Control: WebSocket closed (code=%s)", exc.code)
            finally:
                rediscovery.cancel()
                try:
                    await rediscovery
                except asyncio.CancelledError:
                    pass
                await broker.on_disconnect()
                self._ws = None
```

Und die neue Methode auf der Klasse ergänzen (nach `_connect_and_serve`):

```python
    async def _rediscovery_loop(self, broker, loop, last_discovered) -> None:
        """Periodically re-scan slots; if the inventory changed, re-emit it.

        A single startup race can yield an empty inventory (the FM console interleaves async
        logs into the MODULE-LIST reply). Discovery must not be a one-shot: re-scan on an
        interval and push a fresh inventory whenever it differs from what we last sent, so a
        module that lost the race — or is hot-plugged — comes online without a reconnect.
        """
        interval = getattr(self._config, "control_rediscovery_interval", 30.0)
        if interval <= 0:
            return
        enabled = getattr(self._config, "slot_discovery_enabled", True)
        trace = getattr(self._config, "trace_serial", False)
        while not self._shutdown.is_set():
            await asyncio.sleep(interval)
            if self._shutdown.is_set() or self._ws is None:
                return
            if not enabled:
                continue
            try:
                discovered = await loop.run_in_executor(
                    None, lambda: discover_slots(self._config.slot_dev_base, trace=trace)
                )
            except Exception:  # noqa: BLE001 — re-discovery must never break the control link
                logger.exception("Control: re-discovery failed; keeping current inventory")
                continue
            if discovered == last_discovered:
                continue
            logger.info("Control: inventory changed on re-discovery; re-emitting")
            last_discovered = discovered
            broker.set_inventory(discovered)
            await broker.emit_inventory()
```

- [ ] **Step 8: Test ausführen, Erfolg bestätigen**

Run: `pytest tests/test_control_client.py::test_control_client_rediscovers_and_reemits_inventory -v`
Expected: PASS

- [ ] **Step 9: Beide Control-Client-Tests + Config grün**

Run: `pytest tests/test_control_client.py tests/test_config.py -v`
Expected: alle PASS (der bestehende `test_control_client_connects_...` bleibt grün).

- [ ] **Step 10: Beispiel-Config dokumentieren**

In `station_agent/config.example.yml` nach `telemetry_min_floor_ms: 200` ergänzen:

```yaml
# Control re-discovery: re-scan slots every N seconds and re-emit inventory if it changed.
# Recovers a module that lost the startup discovery race (or was hot-plugged). 0 disables.
control_rediscovery_interval: 30.0
```

- [ ] **Step 11: Commit**

```bash
git add station_agent/control_client.py station_agent/config.py station_agent/config.example.yml tests/test_control_client.py tests/test_config.py
git commit -m "fix(agent): periodic control re-discovery re-emits inventory after a lost race"
```

---

### Task 3: Control auto-on — `control_enabled` entfernen

Control-Channel läuft immer (server_url ist Pflicht). Das hardgecodete `control_enabled: true` im Provisioning/Seed, das Agent-Gate und das Config-Feld verschwinden.

**Files:**
- Modify: `station_agent/agent.py:475` (Gate entfernen)
- Modify: `station_agent/config.py` (Feld + Loader-Zeile entfernen)
- Modify: `station_agent/config.example.yml` (Key entfernen)
- Modify: `apps/provisioning/config_render.py` (Zeile entfernen)
- Modify: `apps/stations/management/commands/seed_dev_station.py:70` (Zeile entfernen)
- Test: `tests/test_config.py`, `tests/test_provisioning.py`

**Interfaces:**
- Consumes: `AgentConfig.server_url` (Pflicht), `audio_router_module` (aus Task 4-Kontext; hier bleibt die bestehende `config.audio_enabled`-Referenz vorerst unangetastet — Task 4 räumt sie auf).
- Produces: `render_config` emittiert **kein** `control_enabled` mehr. `AgentConfig` hat **kein** `control_enabled`-Attribut mehr. Control-Thread startet unbedingt (nur `server_url`-Gate über `validate`).

- [ ] **Step 1: Provisioning-Test anpassen — kein `control_enabled` mehr**

In `tests/test_provisioning.py` in `test_render_produces_expected_fields` die control-Assertion ersetzen:

```python
        # Control channel is always on now (no control_enabled flag); config.yml must NOT
        # carry the removed key.
        assert "control_enabled" not in yaml_text
```

Und in `test_render_config_round_trips_through_agent_loader` die Zeile
`assert cfg.control_enabled is True` **entfernen**.

- [ ] **Step 2: Config-Test anpassen — `control_enabled` weg**

In `tests/test_config.py`: den Test `test_control_enabled_from_yaml` **entfernen** und die Assertion `assert cfg.control_enabled is False` (um Zeile 26) **entfernen**. Neuen Test ergänzen, der beweist, dass ein alter `control_enabled`-Key im YAML ignoriert wird (Rückwärtskompatibilität für bestehende CONFFILEs):

```python
def test_legacy_control_enabled_key_is_ignored(tmp_path, monkeypatch):
    from station_agent.config import CONFIG_PATH_ENV, load_config

    p = tmp_path / "c.yml"
    p.write_text(
        "server_url: http://x\nstation_id: 1\ned25519_key_path: /k.pem\n"
        "control_enabled: true\n"
    )
    monkeypatch.setenv(CONFIG_PATH_ENV, str(p))
    cfg = load_config()
    assert not hasattr(cfg, "control_enabled")
```

- [ ] **Step 3: Tests ausführen, Fehlschlag bestätigen**

Run: `pytest tests/test_config.py tests/test_provisioning.py::TestConfigRender -v`
Expected: FAIL — `control_enabled` existiert noch in `render_config`-Output und auf `AgentConfig`.

- [ ] **Step 4: Config-Feld entfernen**

In `station_agent/config.py`:
- Dataclass-Zeile `control_enabled: bool = False` **entfernen**.
- Loader-Zeile `control_enabled=bool(data.get("control_enabled", False)),` **entfernen**.

- [ ] **Step 5: Agent-Gate entfernen**

In `station_agent/agent.py` den Block ab `if config.control_enabled:` (Zeile ~475) so umbauen, dass der Control-Thread unbedingt startet. Ersetze:

```python
        if config.control_enabled:
            # Local import inside the enabled branch: stations with the control
            # channel off never import ControlClient (and its websockets dep).
            from .control_client import ControlClient

            logger.info("Control channel enabled")
            virtual = [audio_router_module] if audio_router_module is not None else None
            control_client = ControlClient(config, virtual_modules=virtual)
            control_thread = threading.Thread(
                target=control_client.run, name="control-client", daemon=True
            )
            control_thread.start()
        else:
            logger.info("Control channel disabled")
            if config.audio_enabled:
                logger.warning(
                    "Audio enabled but control channel disabled — the audio-router will not "
                    "appear on the control-plane (Spec 0 §5.6 needs the control channel)."
                )
```

durch:

```python
        # Control channel always runs — server_url is a required config field, so a station
        # that talks to the server always exposes its control plane. (No control_enabled flag.)
        from .control_client import ControlClient

        logger.info("Control channel enabled")
        virtual = [audio_router_module] if audio_router_module is not None else None
        control_client = ControlClient(config, virtual_modules=virtual)
        control_thread = threading.Thread(
            target=control_client.run, name="control-client", daemon=True
        )
        control_thread.start()
```

- [ ] **Step 6: Provisioning-Render + Seed bereinigen**

In `apps/provisioning/config_render.py` die Zeile `control_enabled: true` aus dem `dedent`-Block **entfernen**.

In `apps/stations/management/commands/seed_dev_station.py` die Zeile
`self.stdout.write("control_enabled: true")` **entfernen**.

- [ ] **Step 7: Beispiel-Config bereinigen**

In `station_agent/config.example.yml` den Kommentar `# Persistent control channel (D3 agent broker). Off by default.` und die Zeile `control_enabled: false` **entfernen** (die Zeilen `control_dead_man_timeout`, `slot_command_timeout`, Telemetrie bleiben).

- [ ] **Step 8: Tests ausführen, Erfolg bestätigen**

Run: `pytest tests/test_config.py tests/test_provisioning.py::TestConfigRender -v`
Expected: alle PASS.

- [ ] **Step 9: Commit**

```bash
git add station_agent/agent.py station_agent/config.py station_agent/config.example.yml apps/provisioning/config_render.py apps/stations/management/commands/seed_dev_station.py tests/test_config.py tests/test_provisioning.py
git commit -m "feat: control channel always on; remove control_enabled flag"
```

---

### Task 4: Audio auto-detect — Agent-Gating auf vorhandenen Audio-Pfad

Der Agent liest nicht mehr `config.audio_enabled`, sondern erkennt zur Laufzeit, ob ein Audio-Pfad existiert (udev-getaggte ALSA-Karte via `PipeWireRouterBackend.list_audio_slots()`). Ist einer da → Audio-Router + Audio-Client starten.

**Files:**
- Create: `station_agent/audio/detect.py`
- Modify: `station_agent/agent.py` (drei `config.audio_enabled`-Gates ersetzen)
- Test: `tests/test_audio_detect.py`

**Interfaces:**
- Consumes: `PipeWireRouterBackend(sysfs_sound=...).list_audio_slots() -> list[int]`.
- Produces: `station_agent.audio.detect.audio_path_present(config, backend=None) -> bool` — `True` wenn `backend.list_audio_slots()` nicht leer ist; `backend` default `PipeWireRouterBackend(sysfs_sound=config.audio_sysfs_sound)`. Fail-closed: jede Exception → `False`.

- [ ] **Step 1: Detektor — Failing-Test**

`tests/test_audio_detect.py` neu anlegen:

```python
from station_agent.audio.detect import audio_path_present


class _Cfg:
    audio_sysfs_sound = "/sys/class/sound"


class _Backend:
    def __init__(self, slots):
        self._slots = slots

    def list_audio_slots(self):
        return self._slots


class _RaisingBackend:
    def list_audio_slots(self):
        raise OSError("no sysfs")


def test_audio_present_when_backend_lists_slots():
    assert audio_path_present(_Cfg(), backend=_Backend([3])) is True


def test_audio_absent_when_no_slots():
    assert audio_path_present(_Cfg(), backend=_Backend([])) is False


def test_audio_detect_fails_closed_on_error():
    assert audio_path_present(_Cfg(), backend=_RaisingBackend()) is False
```

- [ ] **Step 2: Test ausführen, Fehlschlag bestätigen**

Run: `pytest tests/test_audio_detect.py -v`
Expected: FAIL — `ModuleNotFoundError: station_agent.audio.detect`.

- [ ] **Step 3: Detektor implementieren**

`station_agent/audio/detect.py` neu anlegen:

```python
"""Runtime audio-path detection (interim, decoupled from firmware `describe`).

The agent enables audio when a discovered slot actually has an audio path, independent of
any config flag: `PipeWireRouterBackend.list_audio_slots()` enumerates ALSA cards tagged
`OE5XRX_SLOT` via udev. A non-empty list means at least one slot exposes audio hardware.

TODO(FW-describe): once the parallel FW session ships the `audio` capability in
`module describe`, switch this to gate on that capability from the discovered inventory
instead of probing ALSA/udev directly.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def audio_path_present(config, backend=None) -> bool:
    """True if any slot exposes an audio path. Fail-closed: any error → False (audio off)."""
    if backend is None:
        from station_agent.audio.router_backend import PipeWireRouterBackend

        backend = PipeWireRouterBackend(
            sysfs_sound=getattr(config, "audio_sysfs_sound", "/sys/class/sound")
        )
    try:
        slots = backend.list_audio_slots()
    except Exception:  # noqa: BLE001 — detection must never crash agent startup
        logger.debug("audio detect: backend.list_audio_slots() failed", exc_info=True)
        return False
    present = bool(slots)
    logger.info("audio detect: audio path %s (slots=%s)", "present" if present else "absent", slots)
    return present
```

- [ ] **Step 4: Test ausführen, Erfolg bestätigen**

Run: `pytest tests/test_audio_detect.py -v`
Expected: alle PASS.

- [ ] **Step 5: Agent auf Detektor umstellen**

In `station_agent/agent.py`:

(a) Am Anfang der Audio-Router-Sektion den lokalen Detektor auswerten. Ersetze die Zeile
`audio_router_module = None` + `if config.audio_enabled:` durch:

```python
        # Audio auto-on (interim): enable audio when a discovered slot exposes an audio path,
        # not from a config flag. TODO(FW-describe): switch to the `audio` capability later.
        from .audio.detect import audio_path_present

        audio_present = audio_path_present(config)

        audio_router_module = None
        if audio_present:
```

(b) Der `if config.control_enabled … else … if config.audio_enabled:` Zweig wurde in Task 3 bereits entfernt — nichts weiter zu tun.

(c) Beim Audio-Client-Start (Zeile ~498) `if config.audio_enabled:` durch `if audio_present:` ersetzen:

```python
        if audio_present:
            from .audio.bridge_factory import BridgeFactory
            from .audio.ws_client import AudioClient

            logger.info("Audio channel enabled")
```

und den `else:`-Zweig-Log `logger.info("Audio channel disabled")` beibehalten.

- [ ] **Step 6: Agent-Modul importiert sauber + Detekt-Tests grün**

Run: `python -c "import station_agent.agent"` (aus Repo-Root, venv aktiv)
Expected: kein `AttributeError`/Importfehler.
Run: `pytest tests/test_audio_detect.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add station_agent/audio/detect.py station_agent/agent.py tests/test_audio_detect.py
git commit -m "feat(agent): auto-enable audio from a present audio path, not a flag"
```

---

### Task 5: `audio_enabled`-Flag komplett entfernen (Server + Agent + Config)

Entfernt das DB-Feld, Admin-Referenz, Migration, `render_config`-Arg + Caller, Agent-Config-Feld und die config.yml/example-Keys.

**Files:**
- Modify: `apps/stations/models.py` (`audio_enabled` Feld entfernen)
- Create: `apps/stations/migrations/0018_remove_station_audio_enabled.py`
- Modify: `apps/stations/admin.py:63` (aus Hardware-Fieldset entfernen)
- Modify: `apps/provisioning/config_render.py` (Param + Key entfernen)
- Modify: `apps/provisioning/management/commands/run_background_jobs.py:273` (kwarg entfernen)
- Modify: `station_agent/config.py` (Feld + Loader-Zeile entfernen)
- Modify: `station_agent/config.example.yml` (Key entfernen)
- Test: `tests/test_provisioning.py`

**Interfaces:**
- Consumes: nichts Neues.
- Produces: `render_config(*, server_url, station_id) -> str` (kein `audio_enabled`-Param mehr). `Station` ohne `audio_enabled`. `AgentConfig` ohne `audio_enabled`. Migration `0018` (depends on `0017`), Reverse re-addiert das Feld.

- [ ] **Step 1: Provisioning-Tests anpassen**

In `tests/test_provisioning.py`:
- `test_render_audio_enabled_defaults_off` und `test_render_audio_enabled_can_be_turned_on` **entfernen**.
- In `test_render_config_round_trips_through_agent_loader`: den `@pytest.mark.parametrize("enabled", ...)`-Decorator + `enabled`-Param **entfernen**, den `audio_enabled=enabled`-Aufruf zu einem parameterlosen `render_config(server_url=..., station_id=station.id)` machen, und die Assertions `assert cfg.audio_enabled is enabled` **entfernen**. Neue Assertion ergänzen:

```python
        assert "audio_enabled" not in yaml_text
        assert not hasattr(cfg, "audio_enabled")
```

- [ ] **Step 2: Tests ausführen, Fehlschlag bestätigen**

Run: `pytest tests/test_provisioning.py::TestConfigRender -v`
Expected: FAIL — `render_config` akzeptiert noch `audio_enabled`, `yaml_text` enthält den Key.

- [ ] **Step 3: `render_config` bereinigen**

`apps/provisioning/config_render.py` komplett so schreiben (keine Audio-/Control-Reste):

```python
from textwrap import dedent


def render_config(*, server_url: str, station_id: int) -> str:
    # config.yml is a CONFFILE preserved across OTA, so existing stations pick up changes
    # to this template only via re-provisioning (or a manual on-device edit). The control
    # channel is always on (server_url is required) and audio is auto-detected on-device,
    # so neither has a flag here anymore.
    return dedent(
        f"""\
        server_url: {server_url}
        station_id: {station_id}
        ed25519_key_path: /etc/stationagent/device_key.pem
        heartbeat_interval: 60
        ota_check_interval: 5
        download_dir: /tmp/station-agent
        log_level: INFO
        terminal_enabled: true
        terminal_shell: /bin/sh
        bootloader: auto
        """
    )
```

- [ ] **Step 4: Caller bereinigen**

In `apps/provisioning/management/commands/run_background_jobs.py` (um Zeile 273) das kwarg `audio_enabled=job.station.audio_enabled,` aus dem `render_config(...)`-Aufruf **entfernen**, sodass nur `server_url=` + `station_id=` bleiben.

- [ ] **Step 5: Modell + Admin bereinigen**

In `apps/stations/models.py` das gesamte `audio_enabled = models.BooleanField(...)`-Feld (inkl. help_text-Block) **entfernen**.

In `apps/stations/admin.py` das Hardware-Fieldset-Tupel von `("hardware_revision", "audio_enabled")` auf `("hardware_revision",)` ändern.

- [ ] **Step 6: Migration erzeugen**

Run: `python manage.py makemigrations stations`
Expected: erzeugt `apps/stations/migrations/0018_remove_station_audio_enabled.py` mit einer `RemoveField`-Operation (depends on `0017`). Datei kurz prüfen.

- [ ] **Step 7: Agent-Config bereinigen**

In `station_agent/config.py`:
- Dataclass-Zeile `audio_enabled: bool = False` (+ zugehöriger Kommentarblock „Audio subsystem (Session B)…" nur soweit er sich auf `audio_enabled` bezieht) **entfernen**. Die übrigen `audio_*`-Felder (rx_rate, mic_rate, udp_port_base, dead_man_timeout, max_tx_seconds, router_slot, sysfs_sound) **bleiben** — sie parametrisieren die Audio-Engine weiterhin.
- Loader-Zeile `audio_enabled=bool(data.get("audio_enabled", False)),` **entfernen**.

- [ ] **Step 8: Beispiel-Config bereinigen**

In `station_agent/config.example.yml` die Zeile `audio_enabled: false` **entfernen** und den Kommentarblock so anpassen, dass er nicht mehr behauptet, Audio sei per Flag „off by default" (Audio wird jetzt auto-erkannt). Die übrigen `audio_*`-Keys bleiben.

- [ ] **Step 9: Migration + Django-Checks + Tests**

Run: `python manage.py makemigrations --check --dry-run` → Expected: „No changes detected".
Run: `python manage.py migrate` (auf Test-/Dev-DB) → Expected: `0018` läuft fehlerfrei.
Run: `pytest tests/test_provisioning.py::TestConfigRender tests/test_config.py -v`
Expected: alle PASS.

- [ ] **Step 10: Commit**

```bash
git add apps/stations/models.py apps/stations/admin.py apps/stations/migrations/0018_remove_station_audio_enabled.py apps/provisioning/config_render.py apps/provisioning/management/commands/run_background_jobs.py station_agent/config.py station_agent/config.example.yml tests/test_provisioning.py
git commit -m "refactor: remove Station.audio_enabled flag (audio is auto-detected)"
```

---

### Task 6: Volle Test-Suite + Lint + HIL-Validierung

Gesamtabsicherung: keine übersehenen Referenzen, Suite grün, und die Serial-Boundary-Ehrlichkeitsregel auf echter CM4.

**Files:** keine (Verifikation).

- [ ] **Step 1: Restreferenzen suchen**

Run: `grep -rn "audio_enabled\|control_enabled" apps/ station_agent/ tests/ --include="*.py" --include="*.yml"`
Expected: keine Treffer mehr außer bewusst dokumentierten (z.B. `test_legacy_control_enabled_key_is_ignored`, Migration `0018` Reverse). Jede unerwartete Fundstelle beheben.

- [ ] **Step 2: Volle Agent- + Server-Test-Suite**

Run: `pytest tests/ -q`
Expected: alle PASS (insb. `test_slot_discovery`, `test_control_client`, `test_config`, `test_provisioning`, `test_audio_detect`, `test_broker*`, `test_audio_*`).

- [ ] **Step 3: Lint/Format (falls konfiguriert)**

Run: `ruff check station_agent apps tests && ruff format --check station_agent apps tests` (falls `ruff` im Projekt; sonst überspringen und im PR vermerken).
Expected: sauber.

- [ ] **Step 4: HIL — echte CM4 (Serial-Boundary-Ehrlichkeitsregel)**

Auf der CM4 (`ssh root@192.168.88.211`, key-based; bei Host-Key-Wechsel `ssh-keygen -R 192.168.88.211` bzw. `-o StrictHostKeyChecking=accept-new`) den geänderten Agent testen (dev-launch / live-mount des `station_agent`):
1. Frischen Agent-Start auslösen.
2. Erwartung: FM-Modul geht im station-manager ONLINE (Inventory nicht leer; ggf. erst nach einem Re-Discovery-Intervall).
3. Run: `python -m station_agent selftest serial --slot 3` → Expected: grün (SA818-V, volle Caps).

Ist die CM4 nicht erreichbar, eine begründete HIL-Notiz in den PR-Text schreiben (was getestet wurde, was offen bleibt), statt Sim-grün als hinreichend zu behaupten.

- [ ] **Step 5: Kein Commit nötig** — reine Verifikation; Ergebnisse fließen in den PR-Text.

---

## Self-Review

**Spec-Coverage:**
- §1 Agent-Robustheit (probe retry + framing) → Task 1; control re-discovery → Task 2. ✓
- §2 Control auto-on (Flag + Gate weg) → Task 3. ✓
- §3 Audio auto (Detektor, TODO auf describe) → Task 4. ✓
- §4 Flags weg (`audio_enabled` Modell/Admin/Migration/render/Caller/Config/yml; `control_enabled` in Task 3) → Task 5 (+ Task 3). ✓
- Validierung (CM4 selftest) → Task 6. ✓

**Placeholder-Scan:** Keine „TBD"/„handle edge cases"; alle Code-Schritte tragen konkreten Code. ✓

**Typ-Konsistenz:** `audio_path_present(config, backend=None) -> bool` konsistent in Task 4 definiert und genutzt; `control_rediscovery_interval: float` konsistent (config.py + Loader + Tests); `render_config(*, server_url, station_id)` finale Signatur konsistent in Task 5 (Caller angepasst). ✓
