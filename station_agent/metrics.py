"""Generic heartbeat metrics collector (spec 2026-11-03 §2, §2.1, §4).

A daemon sampler thread (~1 Hz) aggregates fast-moving values (CPU usage,
PowerBoard INA226 rails via hwmon) into a min/max/avg window; everything else
is read once per ``snapshot()``. Every source is feature-detected: a missing or
unreadable source only omits its key, it never raises.

Never touches the slot control devices (``/dev/oe5xrx/slot*/control``) - all
reads here are procfs/sysfs/statvfs only.
"""

import logging
import math
import os
import threading
import time

from station_agent.boot_markers import read_boot_markers
from station_agent.metrics_contract import (
    MAX_KEYS,
    clean_descriptor,
    is_valid_key,
    key_matches_pattern,
    mount_to_instance,
)

logger = logging.getLogger(__name__)

# PowerBoard INA226 I2C addresses on i2c-1 (spec §1): mapped via the device
# path, never via the hwmonN number (that changes between boots).
_INA_RAILS = {"1-0040": "5v", "1-0041": "12v"}
_INA_NAME_PREFIXES = ("ina226", "ina2xx")
# (hwmon file, metric name, divisor to SI unit)
_INA_CHANNELS = (
    ("in1_input", "voltage_v", 1000.0),  # mV
    ("curr1_input", "current_a", 1000.0),  # mA
    ("power1_input", "power_w", 1_000_000.0),  # uW
)
_HWMON_RESCAN_S = 60.0
_CPUFREQ_PATH = ("sys", "devices", "system", "cpu", "cpu0", "cpufreq", "scaling_cur_freq")

_NET_EXCLUDE_PREFIXES = ("veth", "docker", "br-")

# A drained window re-emits the latest sample only while it is this fresh.
_STALE_FACTOR = 3.0
_STALE_MIN_S = 5.0

_ROUND = 4

# Static per-pattern descriptor table: pattern -> (label template, unit, group,
# precision, min, max). ``{i}`` in the label is the wildcard instance segment.
_DESCRIPTORS = (
    ("cpu.usage_pct", "CPU usage", "%", "system", 1, 0, 100),
    ("cpu.temp_c", "CPU temperature", "°C", "system", 1, -40, 125),
    ("cpu.freq_mhz", "CPU frequency", "MHz", "system", 0, 0, 10000),
    ("sys.load1", "Load average (1 min)", "", "system", 2, 0, 1000),
    ("sys.load5", "Load average (5 min)", "", "system", 2, 0, 1000),
    ("sys.load15", "Load average (15 min)", "", "system", 2, 0, 1000),
    ("sys.uptime_s", "Uptime", "s", "system", 0, 0, None),
    ("mem.used_pct", "Memory used", "%", "system", 1, 0, 100),
    ("swap.used_pct", "Swap used", "%", "system", 1, 0, 100),
    ("disk.*.used_pct", "Disk used ({i})", "%", "disk", 1, 0, 100),
    ("net.server_rtt_ms", "Server round-trip time", "ms", "network", 0, 0, 600000),
    ("net.*.rx_kbps", "{i} receive rate", "kbit/s", "network", 1, 0, 10_000_000),
    ("net.*.tx_kbps", "{i} transmit rate", "kbit/s", "network", 1, 0, 10_000_000),
    ("net.*.errors", "{i} errors", "", "network", 0, 0, None),
    ("net.*.drops", "{i} drops", "", "network", 0, 0, None),
    ("pwr.12v.voltage_v", "12 V rail voltage", "V", "power", 2, 0, 30),
    ("pwr.12v.current_a", "12 V rail current", "A", "power", 3, -10, 10),
    ("pwr.12v.power_w", "12 V rail power", "W", "power", 2, -300, 300),
    ("pwr.5v.voltage_v", "5 V rail voltage", "V", "power", 2, 0, 30),
    ("pwr.5v.current_a", "5 V rail current", "A", "power", 3, -10, 10),
    ("pwr.5v.power_w", "5 V rail power", "W", "power", 2, -300, 300),
    ("boot.stage2_ok", "Boot stage 2 OK", "", "system", 0, 0, 1),
    ("boot.overlays_failed", "Failed DT overlays", "", "system", 0, 0, None),
)


def _finite(value: float) -> float | None:
    return value if isinstance(value, (int, float)) and math.isfinite(value) else None


def describe(key: str) -> dict | None:
    """Return the canonical (``clean_descriptor``-ed) descriptor for ``key``."""
    for pattern, label, unit, group, precision, lo, hi in _DESCRIPTORS:
        if not key_matches_pattern(key, pattern):
            continue
        segments = pattern.split(".")
        instance = key.split(".")[segments.index("*")] if "*" in segments else ""
        return clean_descriptor(
            {
                "key": key,
                "label": label.format(i=instance),
                "unit": unit,
                "group": group,
                "precision": precision,
                "min": lo,
                "max": hi,
            }
        )
    return None


class _Window:
    __slots__ = ("count", "max", "min", "total")

    def __init__(self, value: float):
        self.min = self.max = self.total = value
        self.count = 1

    def add(self, value: float) -> None:
        self.min = min(self.min, value)
        self.max = max(self.max, value)
        self.total += value
        self.count += 1

    def as_value(self) -> dict:
        return {
            "min": round(self.min, _ROUND),
            "max": round(self.max, _ROUND),
            "avg": round(self.total / self.count, _ROUND),
        }


class MetricsCollector:
    """Collects the heartbeat ``metrics`` block and its descriptors.

    ``root`` prefixes every procfs/sysfs path (tests use a fake tree);
    ``disk_mounts`` are real mount points passed to ``os.statvfs``.
    ``snapshot()`` is safe to call from several heartbeat threads at once.
    """

    def __init__(self, root: str = "/", interval: float = 1.0, disk_mounts=("/", "/boot")):
        self._root = root
        self._interval = interval
        self._disk_mounts = tuple(disk_mounts)

        self._lock = threading.Lock()  # window + last samples + rtt
        self._snap_lock = threading.Lock()  # net baseline (per-snapshot deltas)
        self._window: dict[str, _Window] = {}
        self._last_sample: dict[str, tuple[float, float]] = {}  # key -> (value, monotonic)
        self._rtt_ms: float | None = None

        # Sampler-thread-only state.
        self._prev_cpu: tuple[int, int] | None = None
        self._hwmon: dict[str, str] = {}  # rail -> hwmon dir
        self._next_hwmon_scan = 0.0
        self._sampler_sources = [self._sample_cpu, self._sample_power]

        self._prev_net: tuple[float, dict[str, tuple[int, ...]]] | None = None
        # The kernel cmdline is fixed for the whole boot: read once, then cache.
        self._boot_values: dict[str, float] | None = None

        self._logged: set[str] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ API

    def start(self) -> None:
        """Start the ~1 Hz sampler thread (no-op if already running)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="metrics-sampler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the sampler thread and wait for it to exit."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def record_server_rtt(self, ms: float) -> None:
        """Remember the last heartbeat POST duration (reported next snapshot)."""
        value = _finite(ms)
        if value is None or value < 0:
            return
        with self._lock:
            self._rtt_ms = value

    def snapshot(self) -> tuple[dict[str, float | dict], list[dict]]:
        """Return ``(values, descriptors)`` and reset the sampler window.

        Descriptors cover exactly the keys in ``values`` and are already in
        canonical ``clean_descriptor`` form (the server re-hashes them).
        """
        values: dict[str, float | dict] = {}
        for label, fn in (
            ("cpu_temp", self._read_cpu_temp),
            ("cpu_freq", self._read_cpu_freq),
            ("loadavg", self._read_loadavg),
            ("uptime", self._read_uptime),
            ("meminfo", self._read_meminfo),
            ("disk", self._read_disks),
            ("boot", self._read_boot_markers),
        ):
            values.update(self._safe(label, fn) or {})
        with self._snap_lock:
            values.update(self._safe("net", self._read_net) or {})

        now = time.monotonic()
        max_age = max(self._interval * _STALE_FACTOR, _STALE_MIN_S)
        with self._lock:
            window, self._window = self._window, {}
            for key, agg in window.items():
                values[key] = agg.as_value()
            for key, (value, ts) in self._last_sample.items():
                if key not in values and now - ts <= max_age:
                    values[key] = value
            if self._rtt_ms is not None:
                values["net.server_rtt_ms"] = round(self._rtt_ms, _ROUND)

        out: dict[str, float | dict] = {}
        descriptors: list[dict] = []
        for key in sorted(values):
            if len(out) >= MAX_KEYS:
                self._log_once("max_keys", "metrics: more than %d keys, dropping rest", MAX_KEYS)
                break
            desc = describe(key) if is_valid_key(key) else None
            if desc is None:
                self._log_once(f"key:{key}", "metrics: dropping undescribable key %r", key)
                continue
            out[key] = values[key]
            descriptors.append(desc)
        return out, descriptors

    # -------------------------------------------------------------- sampler

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._sample_once()
            except Exception:  # noqa: BLE001 - the sampler must never die
                logger.debug("metrics sampler tick failed", exc_info=True)
            self._stop.wait(self._interval)

    def _sample_once(self) -> None:
        samples: dict[str, float] = {}
        for fn in self._sampler_sources:
            samples.update(self._safe(getattr(fn, "__name__", "source"), fn) or {})
        if not samples:
            return
        now = time.monotonic()
        with self._lock:
            for key, value in samples.items():
                agg = self._window.get(key)
                if agg is None:
                    self._window[key] = _Window(value)
                else:
                    agg.add(value)
                self._last_sample[key] = (round(value, _ROUND), now)

    def _sample_cpu(self) -> dict[str, float]:
        with open(self._path("proc", "stat")) as fh:
            fields = fh.readline().split()
        if not fields or fields[0] != "cpu":
            return {}
        # user nice system idle iowait irq softirq steal (guest is inside user)
        counters = [int(x) for x in fields[1:9]]
        total = sum(counters)
        idle = counters[3] + (counters[4] if len(counters) > 4 else 0)
        prev, self._prev_cpu = self._prev_cpu, (total, idle)
        if prev is None:
            return {}
        d_total, d_idle = total - prev[0], idle - prev[1]
        if d_total <= 0 or d_idle < 0:
            return {}
        return {"cpu.usage_pct": (d_total - d_idle) / d_total * 100.0}

    def _scan_hwmon(self) -> None:
        mapping: dict[str, str] = {}
        base = self._path("sys", "class", "hwmon")
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            entries = []
        for entry in entries:
            if not entry.startswith("hwmon"):
                continue
            hw = os.path.join(base, entry)
            try:
                device = os.path.realpath(os.path.join(hw, "device"))
                rail = _INA_RAILS.get(os.path.basename(device))
                if rail is None:
                    continue
                with open(os.path.join(hw, "name")) as fh:
                    name = fh.read().strip()
            except OSError:
                continue
            if name.startswith(_INA_NAME_PREFIXES):
                mapping[rail] = hw
        if mapping != self._hwmon:
            logger.info("metrics: PowerBoard hwmon mapping %s", mapping or "none")
        self._hwmon = mapping

    def _sample_power(self) -> dict[str, float]:
        now = time.monotonic()
        vanished = any(not os.path.isdir(hw) for hw in self._hwmon.values())
        if now >= self._next_hwmon_scan or vanished:
            self._next_hwmon_scan = now + _HWMON_RESCAN_S
            self._scan_hwmon()
        out: dict[str, float] = {}
        for rail, hw in self._hwmon.items():
            for filename, metric, divisor in _INA_CHANNELS:
                path = os.path.join(hw, filename)
                try:
                    with open(path) as fh:
                        raw = int(fh.read().strip())
                except (OSError, ValueError) as exc:
                    self._log_once(path, "metrics: cannot read %s: %s", path, exc)
                    continue
                out[f"pwr.{rail}.{metric}"] = raw / divisor
        return out

    # --------------------------------------------------- per-snapshot reads

    def _read_cpu_temp(self) -> dict:
        raw = self._read_number("sys", "class", "thermal", "thermal_zone0", "temp")
        return {} if raw is None else {"cpu.temp_c": raw / 1000.0}

    def _read_cpu_freq(self) -> dict:
        raw = self._read_number(*_CPUFREQ_PATH)
        return {} if raw is None else {"cpu.freq_mhz": raw / 1000.0}

    def _read_loadavg(self) -> dict:
        with open(self._path("proc", "loadavg")) as fh:
            parts = fh.read().split()
        out = {}
        for key, raw in zip(("sys.load1", "sys.load5", "sys.load15"), parts[:3], strict=False):
            value = _finite(float(raw))
            if value is not None:
                out[key] = value
        return out

    def _read_uptime(self) -> dict:
        with open(self._path("proc", "uptime")) as fh:
            value = _finite(float(fh.read().split()[0]))
        return {} if value is None else {"sys.uptime_s": value}

    def _read_meminfo(self) -> dict:
        info: dict[str, int] = {}
        with open(self._path("proc", "meminfo")) as fh:
            for line in fh:
                name, _, rest = line.partition(":")
                parts = rest.split()
                if parts:
                    try:
                        info[name.strip()] = int(parts[0])
                    except ValueError:
                        continue
        out = {}
        total, avail = info.get("MemTotal"), info.get("MemAvailable")
        if total and avail is not None:
            out["mem.used_pct"] = round((total - avail) / total * 100.0, 2)
        swap_total, swap_free = info.get("SwapTotal"), info.get("SwapFree")
        if swap_total and swap_free is not None:
            out["swap.used_pct"] = round((swap_total - swap_free) / swap_total * 100.0, 2)
        return out

    def _read_disks(self) -> dict:
        out = {}
        for mount in self._disk_mounts:
            try:
                st = os.statvfs(mount)
            except OSError:
                continue
            total = st.f_blocks * st.f_frsize
            if total <= 0:
                continue
            free = st.f_bavail * st.f_frsize
            out[f"disk.{mount_to_instance(mount)}.used_pct"] = round((1 - free / total) * 100.0, 2)
        return out

    def _read_boot_markers(self) -> dict:
        """``boot.*`` from the stage-2/overlay cmdline markers (slot-boot-stage2 §5)."""
        if self._boot_values is None:
            markers = read_boot_markers(self._path("proc", "cmdline"))
            if markers is None:
                return {}  # unreadable: retry next snapshot
            self._boot_values = markers.metric_values()
        return dict(self._boot_values)

    def _read_net(self) -> dict:
        """Per-interval rates/deltas from /proc/net/dev (caller holds _snap_lock)."""
        counters: dict[str, tuple[int, ...]] = {}
        with open(self._path("proc", "net", "dev")) as fh:
            for line in fh:
                iface, sep, rest = line.partition(":")
                iface = iface.strip()
                if not sep or iface == "lo" or iface.startswith(_NET_EXCLUDE_PREFIXES):
                    continue
                fields = rest.split()
                if len(fields) < 12:
                    continue
                try:
                    # rx_bytes rx_errs rx_drop tx_bytes tx_errs tx_drop
                    counters[iface] = tuple(int(fields[i]) for i in (0, 2, 3, 8, 10, 11))
                except ValueError:
                    continue
        now = time.monotonic()
        prev, self._prev_net = self._prev_net, (now, counters)
        if prev is None:
            return {}
        dt = now - prev[0]
        if dt <= 0:
            return {}
        out = {}
        for iface, cur in counters.items():
            old = prev[1].get(iface)
            if old is None:
                continue
            delta = [c - o for c, o in zip(cur, old, strict=True)]
            if any(d < 0 for d in delta):
                continue  # counter reset / wrap: skip this interval
            rx_b, rx_e, rx_d, tx_b, tx_e, tx_d = delta
            inst = mount_to_instance(iface)  # same sanitising as mounts (spec §2)
            out[f"net.{inst}.rx_kbps"] = round(rx_b * 8 / 1000.0 / dt, 2)
            out[f"net.{inst}.tx_kbps"] = round(tx_b * 8 / 1000.0 / dt, 2)
            out[f"net.{inst}.errors"] = float(rx_e + tx_e)
            out[f"net.{inst}.drops"] = float(rx_d + tx_d)
        return out

    # -------------------------------------------------------------- helpers

    def _path(self, *parts: str) -> str:
        return os.path.join(self._root, *parts)

    def _read_number(self, *parts: str) -> float | None:
        with open(self._path(*parts)) as fh:
            return _finite(float(fh.read().strip()))

    def _safe(self, label: str, fn):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - a source failure only omits its keys
            self._log_once(f"src:{label}", "metrics source %s unavailable: %s", label, exc)
            return None

    def _log_once(self, tag: str, msg: str, *args) -> None:
        if tag in self._logged:
            return
        self._logged.add(tag)
        logger.debug(msg, *args)
