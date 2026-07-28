# Fast-Dev-Loop (station-manager) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Agent- und Server-seitige Bausteine des Fast-Dev-Loops: Serial-Contract-Selftest, Serial-Trace, termios-treuer Simulator, Ein-Kommando-Dev-Server-Setup, Dev-Config und justfile.

**Architecture:** Der Agent bekommt ein CLI-Frontend (`selftest`) das die *echten* Produktions-Serialpfade (`probe_slot`, `execute`) aufruft — keine Zweit-Implementierung. Ein `trace_serial`-Schalter hexdumpt den Rohbyte-Verkehr. Der Simulator (`tests/fake_fw.py`) wird termios-treu, damit file-open-artige Bugs auch im Sim rot werden. Server-seitig legt ein Management-Command Dev-Station + statischen Key an; der Dev-Server (docker-compose runserver) existiert bereits.

**Tech Stack:** Python 3.13, pyserial, argparse, Django 6.0 management commands, pytest / pytest-django (`config.settings.test`), just.

## Global Constraints

- Python 3.13 target, ruff line-length 99 (`pyproject.toml [tool.ruff]`).
- Tests laufen unter `DJANGO_SETTINGS_MODULE=config.settings.test`; Test-Dateien in `tests/`, Namensmuster `test_*.py`.
- Serial-Öffnung wird **niemals** neu implementiert — immer die bestehenden `slot_discovery.probe_slot()` / `slot_control.execute()` aufrufen.
- `seed_dev_station` darf **nur** unter `config.settings.dev` laufen (hart abgesichert), nie in Prod.
- Commit-Messages enden mit `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- Branch: `feat/fast-dev-loop` (existiert bereits, Spec ist committed).

---

### Task 1: `trace_serial`-Config-Flag + Rohbyte-Hexdump

**Files:**
- Modify: `station_agent/config.py:15-38` (AgentConfig: neues Feld)
- Modify: `station_agent/slot_discovery.py:65-193` (Hexdump in `_command` + `_drain_until_quiet`)
- Modify: `station_agent/slot_control.py:87-119` (Hexdump in `_converse`)
- Create: `station_agent/serial_trace.py` (kleiner Hexdump-Helfer)
- Test: `tests/test_serial_trace.py`

**Interfaces:**
- Produces: `serial_trace.hexdump(direction: str, data: bytes) -> str` (formatiert eine Zeile, z.B. `"TX 12 bytes: 6d6f64756c65..."`), und `serial_trace.log_io(logger, direction: str, data: bytes, enabled: bool) -> None` (loggt via `logger.debug` nur wenn `enabled`).
- Consumes: `AgentConfig.trace_serial: bool`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_serial_trace.py
import logging
from station_agent import serial_trace


def test_hexdump_formats_direction_length_and_hex():
    line = serial_trace.hexdump("TX", b"abc")
    assert "TX" in line
    assert "3" in line          # length
    assert "616263" in line     # hex of "abc"


def test_log_io_emits_only_when_enabled(caplog):
    log = logging.getLogger("station_agent.test")
    with caplog.at_level(logging.DEBUG, logger="station_agent.test"):
        serial_trace.log_io(log, "RX", b"xy", enabled=False)
        assert not caplog.records
        serial_trace.log_io(log, "RX", b"xy", enabled=True)
        assert any("7879" in r.getMessage() for r in caplog.records)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_serial_trace.py -v`
Expected: FAIL (`ModuleNotFoundError: station_agent.serial_trace`).

- [ ] **Step 3: Write minimal implementation**

```python
# station_agent/serial_trace.py
"""Rohbyte-Hexdump für Serial-I/O-Debugging (Fast-Dev-Loop, --trace-serial)."""
import logging


def hexdump(direction: str, data: bytes) -> str:
    return f"{direction} {len(data)} bytes: {data.hex()}"


def log_io(logger: logging.Logger, direction: str, data: bytes, enabled: bool) -> None:
    if enabled and data:
        logger.debug("serial %s", hexdump(direction, data))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_serial_trace.py -v`
Expected: PASS.

- [ ] **Step 5: Add the config field**

In `station_agent/config.py`, im `AgentConfig`-Dataclass (nach `slot_dev_base`):

```python
    trace_serial: bool = False
```

- [ ] **Step 6: Wire the trace into the two production serial paths**

In `station_agent/slot_discovery.py` — `probe_slot` reicht `trace` an `_command`/`_drain_until_quiet` weiter. Minimal-invasiv: ein modul-globales Flag, das `probe_slot` setzt, ist unsauber; stattdessen `trace: bool = False` als Parameter durchreichen. In `_command` nach erfolgreichem `ser.write(cmd)`:

```python
        from station_agent import serial_trace
        serial_trace.log_io(logger, "TX", cmd, trace)
```

und nach `chunk = ser.read(4096)`:

```python
        serial_trace.log_io(logger, "RX", chunk, trace)
```

Signaturen erweitern: `_command(ser, cmd, prefix, deadline, control_path, trace=False)` und `probe_slot(control_path, timeout=3.0, trace=False)`. In `slot_control.py::_converse` analog nach `os.write(fd, cmd)` und `os.read(fd, 4096)` mit einem `trace`-Parameter auf `execute(..., trace=False)`.

- [ ] **Step 7: Write a test that probe_slot with trace=True emits hexdump**

```python
# tests/test_serial_trace.py  (append)
import os
from tests.fake_fw import FakeFirmware


def test_probe_slot_trace_emits_tx_rx(caplog):
    from station_agent import slot_discovery
    fw = FakeFirmware({"fm1": {"identity": {}, "capabilities": []}})
    fw.start()
    try:
        with caplog.at_level("DEBUG", logger="station_agent.slot_discovery"):
            slot_discovery.probe_slot(fw.control_path, timeout=3.0, trace=True)
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "serial TX" in msgs and "serial RX" in msgs
    finally:
        fw.stop()
```

- [ ] **Step 8: Run tests**

Run: `python -m pytest tests/test_serial_trace.py -v`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add station_agent/serial_trace.py station_agent/config.py station_agent/slot_discovery.py station_agent/slot_control.py tests/test_serial_trace.py
git commit -m "feat(agent): --trace-serial rohbyte-hexdump auf den serial-pfaden

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: `station_agent selftest serial` CLI

**Files:**
- Modify: `station_agent/__main__.py:1-11` (argparse-Frontend)
- Create: `station_agent/selftest.py` (Selftest-Logik)
- Test: `tests/test_selftest.py`

**Interfaces:**
- Consumes: `slot_discovery.probe_slot(control_path, timeout, trace=True)`, `config.load_config()`.
- Produces: `selftest.run_serial(control_path: str, *, timeout: float = 3.0) -> int` (Exit-Code: 0 = mind. ein Modul beschrieben, 1 = nichts/Timeout). `__main__` dispatcht `selftest serial [--slot N] [--base PATH]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_selftest.py
from station_agent import selftest
from tests.fake_fw import FakeFirmware, make_slot_tree


def test_run_serial_returns_zero_when_module_describes(tmp_path):
    fw = FakeFirmware({"fm1": {"identity": {"model": "SA818"}, "capabilities": ["ptt"]}})
    fw.start()
    try:
        rc = selftest.run_serial(fw.control_path, timeout=3.0)
        assert rc == 0
    finally:
        fw.stop()


def test_run_serial_returns_one_on_dead_path(tmp_path):
    dead = str(tmp_path / "nonexistent")
    assert selftest.run_serial(dead, timeout=0.5) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_selftest.py -v`
Expected: FAIL (`ModuleNotFoundError: station_agent.selftest`).

- [ ] **Step 3: Write minimal implementation**

```python
# station_agent/selftest.py
"""Serial-Contract-Selftest: öffnet das Slot-Control-Device über die echten
Produktionspfade und hexdumpt den Verkehr. Grün nur wenn ein Modul antwortet.
Ehrlichkeits-Regel: an dieser Grenze zählt nur ein grüner Lauf auf echtem CM4."""
import logging
import sys

from station_agent import slot_discovery

logger = logging.getLogger("station_agent.selftest")


def run_serial(control_path: str, *, timeout: float = 3.0) -> int:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logger.info("selftest serial: probing %s (trace on)", control_path)
    modules = slot_discovery.probe_slot(control_path, timeout=timeout, trace=True)
    if not modules:
        logger.error("selftest serial: FAIL — no module described on %s", control_path)
        return 1
    for m in modules:
        logger.info("selftest serial: OK — module %s identity=%s caps=%s",
                    m.get("id"), m.get("identity"), m.get("capabilities"))
    return 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_selftest.py -v`
Expected: PASS.

- [ ] **Step 5: Add the argparse frontend (backward-compatible)**

```python
# station_agent/__main__.py
"""Entry point for the Station Agent.

Usage:
  python -m station_agent                     run the agent (default)
  python -m station_agent selftest serial [--slot N] [--base PATH]
"""
import argparse
import sys

from .agent import StationAgent


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="station_agent")
    sub = parser.add_subparsers(dest="cmd")
    st = sub.add_parser("selftest", help="run a self-test")
    st_sub = st.add_subparsers(dest="what")
    serial_p = st_sub.add_parser("serial", help="serial contract test against a slot")
    serial_p.add_argument("--slot", type=int, default=0)
    serial_p.add_argument("--base", default="/dev/oe5xrx")
    serial_p.add_argument("--timeout", type=float, default=3.0)

    args = parser.parse_args(argv)

    if args.cmd == "selftest" and args.what == "serial":
        from station_agent import selftest
        path = f"{args.base}/slot{args.slot}/control"
        return selftest.run_serial(path, timeout=args.timeout)

    # Default: run the agent.
    StationAgent().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: Test the CLI dispatch (dead path → exit 1)**

```python
# tests/test_selftest.py  (append)
from station_agent.__main__ import main


def test_cli_selftest_serial_dead_path_returns_one(tmp_path):
    rc = main(["selftest", "serial", "--base", str(tmp_path), "--slot", "9", "--timeout", "0.5"])
    assert rc == 1
```

- [ ] **Step 7: Run tests**

Run: `python -m pytest tests/test_selftest.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add station_agent/selftest.py station_agent/__main__.py tests/test_selftest.py
git commit -m "feat(agent): 'selftest serial' CLI über echte probe_slot()-produktionspfade

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Simulator termios-treu machen (Anti-„Sim lügt")

**Files:**
- Modify: `tests/fake_fw.py:22-44` (cfmakeraw auf den PTY-fds)
- Test: `tests/test_sim_fidelity.py`

**Interfaces:**
- Produces: `FakeFirmware` präsentiert das Slave-PTY im **raw** termios-Modus (kein Canonical, kein Echo, kein CR/LF-Mapping) — so verhält sich der Sim wie ein echter UART, und ein file-open-Zugriff, der termios ignoriert, scheitert im Sim genauso wie auf HW.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sim_fidelity.py
import termios
from tests.fake_fw import FakeFirmware


def test_sim_pty_is_raw_mode():
    fw = FakeFirmware({"fm1": {"identity": {}, "capabilities": []}})
    fw.start()
    try:
        attrs = termios.tcgetattr(fw._slave_fd)
        iflag, oflag, cflag, lflag = attrs[0], attrs[1], attrs[2], attrs[3]
        # Raw: canonical mode + echo OFF, output post-processing OFF.
        assert not (lflag & termios.ICANON), "PTY still in canonical mode — sim lies vs real UART"
        assert not (lflag & termios.ECHO), "PTY still echoes — sim lies vs real UART"
        assert not (oflag & termios.OPOST), "PTY still post-processes output"
    finally:
        fw.stop()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_sim_fidelity.py -v`
Expected: FAIL (default PTY is canonical/echoing).

- [ ] **Step 3: Make the sim PTY raw**

In `tests/fake_fw.py`, in `FakeFirmware.__init__` nach `self._master_fd, self._slave_fd = os.openpty()`:

```python
        import termios, tty
        # Real UARTs deliver raw bytes: no line discipline, no echo, no CR/LF
        # mapping. A bare openpty() slave is canonical+echoing, which hides
        # file-open bugs that a real UART would expose. Force raw on both ends.
        for fd in (self._master_fd, self._slave_fd):
            try:
                tty.setraw(fd)
            except termios.error:
                pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_sim_fidelity.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full existing serial suite to ensure no regression**

Run: `python -m pytest tests/test_slot_discovery.py tests/test_broker.py tests/test_selftest.py -v`
Expected: PASS (raw mode must not break discovery/command round-trips).

- [ ] **Step 6: Commit**

```bash
git add tests/fake_fw.py tests/test_sim_fidelity.py
git commit -m "test(sim): raw-termios PTY damit file-open-bugs auch im simulator rot werden

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: `seed_dev_station` Management-Command

**Files:**
- Create: `apps/stations/management/commands/seed_dev_station.py`
- Test: `tests/test_seed_dev_station.py`

**Interfaces:**
- Produces: `manage.py seed_dev_station [--name "Dev Station"]` — idempotent (get_or_create auf `name`), legt `Station` + `DeviceKey` an, schreibt Private-Key-PEM nach `--key-out` (default `./dev-device-key.pem`), druckt eine fertige Agent-`config.yml` auf stdout. Verweigert Ausführung wenn nicht `settings.DEBUG` / nicht Dev-Settings.
- Consumes: `Station.objects.get_or_create`, `DeviceKey.generate_keypair()` (staticmethod → `(private_pem_bytes, public_b64_str)`), `DeviceKey.objects.get_or_create(station=...)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_seed_dev_station.py
import io
import pytest
from django.core.management import call_command
from django.test import override_settings

from apps.stations.models import Station
from apps.api.models import DeviceKey


@pytest.mark.django_db
@override_settings(DEBUG=True)
def test_seed_dev_station_is_idempotent(tmp_path):
    key_out = str(tmp_path / "k.pem")
    out1 = io.StringIO()
    call_command("seed_dev_station", "--key-out", key_out, stdout=out1)
    out2 = io.StringIO()
    call_command("seed_dev_station", "--key-out", key_out, stdout=out2)

    assert Station.objects.filter(name="Dev Station").count() == 1
    assert DeviceKey.objects.filter(station__name="Dev Station").count() == 1
    assert "server_url:" in out2.getvalue()
    assert "station_id:" in out2.getvalue()


@pytest.mark.django_db
@override_settings(DEBUG=False)
def test_seed_dev_station_refuses_in_prod(tmp_path):
    from django.core.management.base import CommandError
    with pytest.raises(CommandError):
        call_command("seed_dev_station", "--key-out", str(tmp_path / "k.pem"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_seed_dev_station.py -v`
Expected: FAIL (`Unknown command: 'seed_dev_station'`).

- [ ] **Step 3: Write the command**

```python
# apps/stations/management/commands/seed_dev_station.py
"""Legt eine Dev-Station + statischen Device-Key an und druckt die Agent-Config.
NUR unter DEBUG/Dev-Settings — statische Keys dürfen nie in Prod (Spec §4)."""
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.api.models import DeviceKey
from apps.stations.models import Station


class Command(BaseCommand):
    help = "Seed a dev station + static device key and print the agent config.yml."

    def add_arguments(self, parser):
        parser.add_argument("--name", default="Dev Station")
        parser.add_argument("--callsign", default="OE5XRX")
        parser.add_argument("--server-url", default="http://10.0.2.2:8000")
        parser.add_argument("--key-out", default="./dev-device-key.pem")

    def handle(self, *args, name, callsign, server_url, key_out, **opts):
        if not settings.DEBUG:
            raise CommandError("seed_dev_station refuses to run without DEBUG (dev only).")

        station, _ = Station.objects.get_or_create(
            name=name, defaults={"callsign": callsign}
        )
        key = DeviceKey.objects.filter(station=station).first()
        if key is None:
            private_pem, public_b64 = DeviceKey.generate_keypair()
            key = DeviceKey.objects.create(station=station, current_public_key=public_b64)
            Path(key_out).write_bytes(private_pem)
            self.stderr.write(f"Wrote private key to {key_out}")
        else:
            self.stderr.write("DeviceKey already exists; reusing (private key not re-shown).")

        self.stdout.write("# --- agent config.yml (dev) ---")
        self.stdout.write(f"server_url: {server_url}")
        self.stdout.write(f"station_id: {station.id}")
        self.stdout.write(f"ed25519_key_path: {key_out}")
        self.stdout.write("log_level: DEBUG")
        self.stdout.write("trace_serial: true")
        self.stdout.write("slot_discovery_enabled: true")
        self.stdout.write("control_enabled: true")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_seed_dev_station.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/stations/management/commands/seed_dev_station.py tests/test_seed_dev_station.py
git commit -m "feat(dev): seed_dev_station management command (static key, DEBUG-only)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Dev-Config-Template + justfile + Ehrlichkeits-Regel in CLAUDE.md

**Files:**
- Create: `station_agent/config.dev.example.yml`
- Create: `justfile`
- Create: `scripts/dev-selftest.sh`
- Modify: `CLAUDE.md` (Ehrlichkeits-Regel)
- Modify: `.gitignore` (`.env`, `dev-device-key.pem`)

**Interfaces:**
- Produces: `just dev-server`, `just seed`, `just selftest [host=...]`, `just test`. Hosts/Secrets aus ungetrackter `.env` mit Env-Override.

- [ ] **Step 1: Create the dev config template**

```yaml
# station_agent/config.dev.example.yml
# Dev-Config: zeigt auf den lokalen docker-compose-Server.
# Prod-Smoke-Test = nur server_url + ed25519_key_path umbiegen.
server_url: http://10.0.2.2:8000   # Host aus QEMU-Sicht; für CM4 die LAN-IP des Dev-Rechners
station_id: 1
ed25519_key_path: /etc/stationagent/device_key.pem
log_level: DEBUG
trace_serial: true
heartbeat_interval: 15
slot_discovery_enabled: true
control_enabled: true
```

- [ ] **Step 2: Create the justfile (thin recipes, logic in scripts/)**

```make
# justfile — station-manager. `just --list` zeigt alle Recipes.
# Hosts/Secrets aus ungetrackter .env (Env-Override möglich).
set dotenv-load := true

cm4_host := env_var_or_default("CM4_HOST", "cm4-dev.local")

# Dev-Server hoch + Dev-Station seeden
dev-server:
    docker compose up -d web
    docker compose exec web python manage.py seed_dev_station

# Nur Dev-Station seeden (Config drucken)
seed:
    docker compose exec web python manage.py seed_dev_station

# Serial-Contract-Test gegen ein Ziel (CM4 default)
selftest host=cm4_host:
    scripts/dev-selftest.sh {{host}}

# Test-Suite
test:
    python -m pytest -q
```

- [ ] **Step 3: Create the selftest helper script**

```bash
# scripts/dev-selftest.sh
#!/usr/bin/env bash
# Serial-Contract-Test auf einem Ziel-Host ausführen (Ehrlichkeits-Regel:
# an der Serial-Grenze zählt nur ein grüner Lauf auf echtem CM4).
set -euo pipefail
host="${1:?usage: dev-selftest.sh <host> [slot]}"
slot="${2:-0}"
exec ssh "root@${host}" "python -m station_agent selftest serial --slot ${slot}"
```

Dann: `chmod +x scripts/dev-selftest.sh`.

- [ ] **Step 4: Add the honesty rule to CLAUDE.md**

Unter dem Abschnitt zum station-agent in `CLAUDE.md` diesen Absatz ergänzen:

```markdown
### Serial-Boundary Ehrlichkeits-Regel
Ein Bug am Serial-/Modul-Boundary (Modul nicht gefunden/lesbar, Control-Knopf fehlt)
gilt erst als gefixt, wenn `python -m station_agent selftest serial` auf **echtem CM4**
grün ist. Sim-grün (QEMU/native_sim) ist notwendig, nicht hinreichend — der Simulator
kann Timing-/termios-Effekte des echten UART nicht vollständig nachbilden.
```

- [ ] **Step 5: Update .gitignore**

An `.gitignore` anhängen (falls nicht vorhanden):

```
.env
dev-device-key.pem
```

- [ ] **Step 6: Verify the justfile parses**

Run: `just --list`
Expected: zeigt `dev-server`, `seed`, `selftest`, `test`.

- [ ] **Step 7: Commit**

```bash
git add station_agent/config.dev.example.yml justfile scripts/dev-selftest.sh CLAUDE.md .gitignore
git commit -m "feat(dev): justfile + dev-config-template + serial ehrlichkeits-regel

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- `selftest serial` → Task 2 ✓ (ruft echte `probe_slot()` auf, keine Zweit-Impl.)
- `--trace-serial` → Task 1 ✓
- Sim-PTY termios-treu → Task 3 ✓
- `seed_dev_station` (DEBUG-only, statischer Key) → Task 4 ✓
- Dev-Config + Prod-Switch → Task 5 (config.dev.example.yml) ✓
- Dev-Server Auto-Reload → bereits vorhanden (docker-compose runserver), keine Task nötig ✓
- justfile → Task 5 ✓
- Ehrlichkeits-Regel → Task 5 (CLAUDE.md) ✓

**Placeholder scan:** keine TBD/TODO; alle Code-Steps enthalten echten Code.

**Type consistency:** `probe_slot(control_path, timeout, trace)` konsistent in Task 1/2; `run_serial(control_path, *, timeout)` in Task 2; `generate_keypair()` liefert `(private_pem_bytes, public_b64_str)` (staticmethod) konsistent in Task 4.
