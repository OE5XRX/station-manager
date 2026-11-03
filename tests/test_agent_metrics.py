"""Tests for station_agent.metrics (generic metrics collector, spec §2/§4)."""

import os
import threading
import time
from types import SimpleNamespace

import pytest

from station_agent import metrics as m
from station_agent.metrics import MetricsCollector
from station_agent.metrics_contract import clean_descriptor, descriptors_hash, is_valid_key

# ---------------------------------------------------------------------------
# Fake procfs/sysfs helpers
# ---------------------------------------------------------------------------


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _proc_stat(root, user, system, idle):
    # cpu user nice system idle iowait irq softirq steal guest guest_nice
    _write(
        root / "proc" / "stat",
        f"cpu  {user} 0 {system} {idle} 0 0 0 0 0 0\ncpu0 1 0 1 1 0 0 0 0 0 0\n",
    )


def _dev_base(root):
    return root / "sys" / "devices" / "platform" / "soc" / "i2c-1"


def _ina(root, hwmon, addr, mv=None, ma=None, uw=None, name="ina226"):
    dev = _dev_base(root) / addr
    dev.mkdir(parents=True, exist_ok=True)
    hw = root / "sys" / "class" / "hwmon" / hwmon
    hw.mkdir(parents=True, exist_ok=True)
    os.symlink(dev, hw / "device")
    _write(hw / "name", name + "\n")
    if mv is not None:
        _write(hw / "in1_input", f"{mv}\n")
    if ma is not None:
        _write(hw / "curr1_input", f"{ma}\n")
    if uw is not None:
        _write(hw / "power1_input", f"{uw}\n")
    return hw


def _set_rail(hw, mv=None, ma=None, uw=None):
    if mv is not None:
        _write(hw / "in1_input", f"{mv}\n")
    if ma is not None:
        _write(hw / "curr1_input", f"{ma}\n")
    if uw is not None:
        _write(hw / "power1_input", f"{uw}\n")


def _net_dev(root, rows):
    """rows: {iface: (rx_bytes, rx_errs, rx_drop, tx_bytes, tx_errs, tx_drop)}"""
    lines = [
        "Inter-|   Receive                                                |  Transmit",
        " face |bytes    packets errs drop fifo frame compressed multicast"
        "|bytes    packets errs drop fifo colls carrier compressed",
    ]
    for iface, (rxb, rxe, rxd, txb, txe, txd) in rows.items():
        lines.append(f"{iface:>6}: {rxb} 10 {rxe} {rxd} 0 0 0 0 {txb} 10 {txe} {txd} 0 0 0 0")
    _write(root / "proc" / "net" / "dev", "\n".join(lines) + "\n")


def _half_full(_path):
    return SimpleNamespace(f_blocks=100, f_frsize=1, f_bavail=50)


def _collector(root, **kw):
    kw.setdefault("disk_mounts", ())
    return MetricsCollector(root=str(root), **kw)


# ---------------------------------------------------------------------------
# Basics
# ---------------------------------------------------------------------------


def test_no_sources_yields_empty_snapshot(tmp_path):
    c = _collector(tmp_path)
    c._sample_once()
    values, descs = c.snapshot()
    assert values == {}
    assert descs == []


def test_missing_root_entirely_does_not_raise(tmp_path):
    c = _collector(tmp_path / "does-not-exist")
    c._sample_once()
    assert c.snapshot() == ({}, [])


# ---------------------------------------------------------------------------
# PowerBoard / INA226 via hwmon
# ---------------------------------------------------------------------------


def test_hwmon_maps_by_device_path_and_converts_units(tmp_path):
    _ina(tmp_path, "hwmon3", "1-0041", mv=12100, ma=1500, uw=18150000)
    _ina(tmp_path, "hwmon2", "1-0040", mv=5050, ma=-250, uw=1262500)
    c = _collector(tmp_path)
    c._sample_once()
    values, _ = c.snapshot()
    assert values["pwr.12v.voltage_v"] == {"min": 12.1, "max": 12.1, "avg": 12.1}
    assert values["pwr.12v.current_a"]["avg"] == 1.5
    assert values["pwr.12v.power_w"]["avg"] == 18.15
    assert values["pwr.5v.voltage_v"]["avg"] == 5.05
    assert values["pwr.5v.current_a"]["avg"] == -0.25


def test_hwmon_renumbering_still_maps_to_rail(tmp_path):
    hw = _ina(tmp_path, "hwmon3", "1-0041", mv=12000)
    c = _collector(tmp_path)
    c._sample_once()
    assert c.snapshot()[0]["pwr.12v.voltage_v"]["avg"] == 12.0
    # Reboot-style renumbering: hwmon3 vanishes, the same device shows up as hwmon7.
    (hw / "device").unlink()
    for f in hw.iterdir():
        f.unlink()
    hw.rmdir()
    hw7 = tmp_path / "sys" / "class" / "hwmon" / "hwmon7"
    hw7.mkdir()
    os.symlink(_dev_base(tmp_path) / "1-0041", hw7 / "device")
    _write(hw7 / "name", "ina226\n")
    _write(hw7 / "in1_input", "11900\n")
    c._sample_once()
    assert c.snapshot()[0]["pwr.12v.voltage_v"]["avg"] == 11.9


def test_hwmon_appearing_late_is_picked_up_on_rescan(tmp_path, monkeypatch):
    c = _collector(tmp_path)
    c._sample_once()
    assert c.snapshot()[0] == {}
    _ina(tmp_path, "hwmon4", "1-0040", mv=5000)
    # Not rescanned until the rescan interval elapsed.
    c._sample_once()
    assert "pwr.5v.voltage_v" not in c.snapshot()[0]
    monkeypatch.setattr(c, "_next_hwmon_scan", 0.0)
    c._sample_once()
    assert c.snapshot()[0]["pwr.5v.voltage_v"]["avg"] == 5.0


def test_hwmon_wrong_name_or_address_ignored(tmp_path):
    _ina(tmp_path, "hwmon0", "1-0041", mv=12000, name="cpu_thermal")
    _ina(tmp_path, "hwmon1", "1-0045", mv=12000)
    c = _collector(tmp_path)
    c._sample_once()
    assert c.snapshot()[0] == {}


def test_hwmon_ina2xx_name_accepted(tmp_path):
    _ina(tmp_path, "hwmon1", "1-0040", mv=5000, name="ina2xx")
    c = _collector(tmp_path)
    c._sample_once()
    assert "pwr.5v.voltage_v" in c.snapshot()[0]


def test_unreadable_in1_input_omits_voltage_keeps_current(tmp_path):
    hw = _ina(tmp_path, "hwmon3", "1-0041", mv=12000, ma=1500)
    # A directory where the file should be -> read raises IsADirectoryError (OSError).
    (hw / "in1_input").unlink()
    (hw / "in1_input").mkdir()
    c = _collector(tmp_path)
    c._sample_once()
    values, descs = c.snapshot()
    assert "pwr.12v.voltage_v" not in values
    assert values["pwr.12v.current_a"]["avg"] == 1.5
    assert {d["key"] for d in descs} == set(values)


def test_garbage_in1_input_omits_voltage(tmp_path):
    _ina(tmp_path, "hwmon3", "1-0041", ma=1500)
    _write(tmp_path / "sys" / "class" / "hwmon" / "hwmon3" / "in1_input", "nope\n")
    c = _collector(tmp_path)
    c._sample_once()
    values, _ = c.snapshot()
    assert "pwr.12v.voltage_v" not in values
    assert "pwr.12v.current_a" in values


# ---------------------------------------------------------------------------
# Window aggregation
# ---------------------------------------------------------------------------


def test_window_min_max_avg_over_three_ticks(tmp_path):
    hw = _ina(tmp_path, "hwmon3", "1-0041", mv=12000)
    c = _collector(tmp_path)
    for mv in (12000, 12300, 11700):
        _set_rail(hw, mv=mv)
        c._sample_once()
    values, _ = c.snapshot()
    assert values["pwr.12v.voltage_v"] == {"min": 11.7, "max": 12.3, "avg": 12.0}


def test_snapshot_resets_window(tmp_path):
    hw = _ina(tmp_path, "hwmon3", "1-0041", mv=12000)
    c = _collector(tmp_path)
    for mv in (11000, 13000):
        _set_rail(hw, mv=mv)
        c._sample_once()
    c.snapshot()
    _set_rail(hw, mv=12500)
    c._sample_once()
    values, _ = c.snapshot()
    assert values["pwr.12v.voltage_v"] == {"min": 12.5, "max": 12.5, "avg": 12.5}


def test_empty_window_reemits_fresh_last_sample_for_concurrent_caller(tmp_path):
    # Two heartbeat paths (main + keepalive) share one collector: if one just
    # drained the window, the other still reports the latest fresh sample
    # rather than dropping the PowerBoard keys.
    _ina(tmp_path, "hwmon3", "1-0041", mv=12000)
    c = _collector(tmp_path)
    c._sample_once()
    c.snapshot()
    values, _ = c.snapshot()
    assert values["pwr.12v.voltage_v"] == 12.0


def test_stale_last_sample_not_reemitted(tmp_path, monkeypatch):
    _ina(tmp_path, "hwmon3", "1-0041", mv=12000)
    c = _collector(tmp_path)
    c._sample_once()
    c.snapshot()
    real = time.monotonic
    monkeypatch.setattr(m.time, "monotonic", lambda: real() + 3600)
    values, _ = c.snapshot()
    assert "pwr.12v.voltage_v" not in values


# ---------------------------------------------------------------------------
# CPU usage
# ---------------------------------------------------------------------------


def test_cpu_usage_from_two_proc_stat_snapshots(tmp_path):
    _proc_stat(tmp_path, user=100, system=100, idle=800)
    c = _collector(tmp_path)
    c._sample_once()  # baseline only
    assert "cpu.usage_pct" not in c.snapshot()[0]
    # +75 busy, +25 idle -> 75 %
    _proc_stat(tmp_path, user=150, system=125, idle=825)
    c._sample_once()
    values, _ = c.snapshot()
    assert values["cpu.usage_pct"]["avg"] == 75.0


def test_cpu_usage_zero_delta_skipped(tmp_path):
    _proc_stat(tmp_path, user=100, system=100, idle=800)
    c = _collector(tmp_path)
    c._sample_once()
    c._sample_once()
    assert "cpu.usage_pct" not in c.snapshot()[0]


# ---------------------------------------------------------------------------
# Sampler thread robustness
# ---------------------------------------------------------------------------


def test_failing_source_does_not_block_other_sources(tmp_path):
    _ina(tmp_path, "hwmon3", "1-0041", mv=12000)
    c = _collector(tmp_path)

    def boom():
        raise RuntimeError("source exploded")

    c._sampler_sources.insert(0, boom)
    c._sample_once()
    assert c.snapshot()[0]["pwr.12v.voltage_v"]["avg"] == 12.0


def test_sampler_thread_survives_tick_exception(tmp_path, monkeypatch):
    c = _collector(tmp_path, interval=0.01)
    calls = {"n": 0}
    reached = threading.Event()

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("tick failed")
        if calls["n"] >= 3:
            reached.set()

    monkeypatch.setattr(c, "_sample_once", flaky)
    c.start()
    try:
        assert reached.wait(5.0)
        assert c._thread.is_alive()
    finally:
        c.stop()
    assert not c._thread.is_alive()


def test_start_stop_idempotent(tmp_path):
    c = _collector(tmp_path, interval=0.01)
    c.start()
    c.start()
    c.stop()
    c.stop()


# ---------------------------------------------------------------------------
# Per-snapshot sources
# ---------------------------------------------------------------------------


def test_system_sources(tmp_path):
    _write(tmp_path / "sys" / "class" / "thermal" / "thermal_zone0" / "temp", "52123\n")
    _write(tmp_path.joinpath(*m._CPUFREQ_PATH), "1500000\n")
    _write(tmp_path / "proc" / "loadavg", "0.52 0.41 0.30 1/123 4567\n")
    _write(tmp_path / "proc" / "uptime", "3600.55 7000.00\n")
    _write(
        tmp_path / "proc" / "meminfo",
        "MemTotal:        1000000 kB\nMemFree:          100000 kB\nMemAvailable:     750000 kB\n"
        "SwapTotal:        200000 kB\nSwapFree:         150000 kB\n",
    )
    c = _collector(tmp_path)
    values, _ = c.snapshot()
    assert values["cpu.temp_c"] == 52.123
    assert values["cpu.freq_mhz"] == 1500.0
    assert values["sys.load1"] == 0.52
    assert values["sys.load5"] == 0.41
    assert values["sys.load15"] == 0.3
    assert values["sys.uptime_s"] == 3600.55
    assert values["mem.used_pct"] == 25.0
    assert values["swap.used_pct"] == 25.0


def test_swap_absent_no_key(tmp_path):
    _write(
        tmp_path / "proc" / "meminfo",
        "MemTotal: 1000000 kB\nMemAvailable: 500000 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n",
    )
    c = _collector(tmp_path)
    values, _ = c.snapshot()
    assert values["mem.used_pct"] == 50.0
    assert "swap.used_pct" not in values


def test_disk_used_pct_via_statvfs(tmp_path, monkeypatch):
    def fake_statvfs(path):
        if path == "/boot":
            raise OSError("not mounted")
        return SimpleNamespace(f_blocks=1000, f_frsize=4096, f_bavail=250)

    monkeypatch.setattr(m.os, "statvfs", fake_statvfs)
    c = MetricsCollector(root=str(tmp_path))
    values, _ = c.snapshot()
    assert values["disk.root.used_pct"] == 75.0
    assert "disk.boot.used_pct" not in values


def test_disk_mount_instance_mapping(tmp_path, monkeypatch):
    monkeypatch.setattr(m.os, "statvfs", _half_full)
    c = MetricsCollector(root=str(tmp_path), disk_mounts=("/data/log",))
    values, _ = c.snapshot()
    assert values == {"disk.data_log.used_pct": 50.0}


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------


def test_net_first_snapshot_no_rates_then_deltas(tmp_path, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: clock["t"])
    _net_dev(
        tmp_path,
        {
            "lo": (1, 0, 0, 1, 0, 0),
            "eth0": (1000, 1, 2, 2000, 3, 4),
            "veth12ab": (5, 0, 0, 5, 0, 0),
            "docker0": (5, 0, 0, 5, 0, 0),
            "br-1234": (5, 0, 0, 5, 0, 0),
        },
    )
    c = _collector(tmp_path)
    assert not any(k.startswith("net.") for k in c.snapshot()[0])

    clock["t"] += 10.0
    _net_dev(
        tmp_path,
        {
            "lo": (99999, 0, 0, 99999, 0, 0),
            "eth0": (1000 + 12500, 2, 2, 2000 + 25000, 5, 7),
            "veth12ab": (500, 0, 0, 500, 0, 0),
            "docker0": (500, 0, 0, 500, 0, 0),
            "br-1234": (500, 0, 0, 500, 0, 0),
        },
    )
    values, _ = c.snapshot()
    net = {k: v for k, v in values.items() if k.startswith("net.")}
    # 12500 B in 10 s = 10 kbit/s; 25000 B = 20 kbit/s
    assert net == {
        "net.eth0.rx_kbps": 10.0,
        "net.eth0.tx_kbps": 20.0,
        "net.eth0.errors": 3.0,  # rx +1, tx +2
        "net.eth0.drops": 3.0,  # rx +0, tx +3
    }


def test_net_counter_decrease_skips_interval(tmp_path, monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: clock["t"])
    _net_dev(tmp_path, {"eth0": (100000, 5, 5, 100000, 5, 5)})
    c = _collector(tmp_path)
    c.snapshot()
    clock["t"] += 10
    _net_dev(tmp_path, {"eth0": (10, 0, 0, 10, 0, 0)})  # driver reset
    assert not any(k.startswith("net.") for k in c.snapshot()[0])
    clock["t"] += 10
    _net_dev(tmp_path, {"eth0": (1260, 0, 0, 10, 0, 0)})
    values, _ = c.snapshot()
    assert values["net.eth0.rx_kbps"] == 1.0


def test_server_rtt(tmp_path):
    c = _collector(tmp_path)
    assert "net.server_rtt_ms" not in c.snapshot()[0]
    c.record_server_rtt(42.7)
    assert c.snapshot()[0]["net.server_rtt_ms"] == 42.7
    c.record_server_rtt(float("nan"))
    assert c.snapshot()[0]["net.server_rtt_ms"] == 42.7


# ---------------------------------------------------------------------------
# Descriptors
# ---------------------------------------------------------------------------


def _full_tree(tmp_path):
    _proc_stat(tmp_path, 100, 100, 800)
    _ina(tmp_path, "hwmon3", "1-0041", mv=12100, ma=1500, uw=18150000)
    _ina(tmp_path, "hwmon2", "1-0040", mv=5000, ma=500, uw=2500000)
    _write(tmp_path / "sys" / "class" / "thermal" / "thermal_zone0" / "temp", "52123\n")
    _write(tmp_path.joinpath(*m._CPUFREQ_PATH), "1500000\n")
    _write(tmp_path / "proc" / "loadavg", "0.52 0.41 0.30 1/123 4567\n")
    _write(tmp_path / "proc" / "uptime", "3600.55 7000.00\n")
    _write(
        tmp_path / "proc" / "meminfo",
        "MemTotal: 1000 kB\nMemAvailable: 750 kB\nSwapTotal: 200 kB\nSwapFree: 150 kB\n",
    )
    _net_dev(tmp_path, {"eth0": (0, 0, 0, 0, 0, 0), "wlan0": (0, 0, 0, 0, 0, 0)})


def test_descriptors_cover_exactly_emitted_keys_and_are_clean(tmp_path, monkeypatch):
    _full_tree(tmp_path)
    monkeypatch.setattr(m.os, "statvfs", _half_full)
    c = MetricsCollector(root=str(tmp_path))
    c.record_server_rtt(12.0)
    c._sample_once()
    c.snapshot()  # net baseline
    _proc_stat(tmp_path, 150, 125, 825)
    c._sample_once()
    _net_dev(tmp_path, {"eth0": (100, 0, 0, 100, 0, 0), "wlan0": (100, 0, 0, 100, 0, 0)})
    values, descs = c.snapshot()

    assert {d["key"] for d in descs} == set(values)
    assert len(descs) == len(values)
    assert all(is_valid_key(k) for k in values)
    # Already canonical: the server re-hashes its cleaned copy, so 0 vs 0.0 would differ.
    assert [clean_descriptor(d) for d in descs] == descs
    assert descriptors_hash([clean_descriptor(d) for d in descs]) == descriptors_hash(descs)
    # Spot-check content.
    by_key = {d["key"]: d for d in descs}
    assert by_key["pwr.12v.voltage_v"]["group"] == "power"
    assert by_key["pwr.12v.voltage_v"]["unit"] == "V"
    assert by_key["pwr.12v.current_a"]["min"] < 0
    assert by_key["disk.root.used_pct"]["group"] == "disk"
    assert by_key["net.wlan0.rx_kbps"]["group"] == "network"
    assert by_key["cpu.usage_pct"]["max"] == 100.0
    assert all(len(d["label"]) <= 64 for d in descs)
    expected = {
        "cpu.usage_pct",
        "cpu.temp_c",
        "cpu.freq_mhz",
        "sys.load1",
        "sys.load5",
        "sys.load15",
        "sys.uptime_s",
        "mem.used_pct",
        "swap.used_pct",
        "disk.root.used_pct",
        "disk.boot.used_pct",
        "net.server_rtt_ms",
    }
    for rail in ("12v", "5v"):
        expected |= {f"pwr.{rail}.voltage_v", f"pwr.{rail}.current_a", f"pwr.{rail}.power_w"}
    for iface in ("eth0", "wlan0"):
        expected |= {f"net.{iface}.{n}" for n in ("rx_kbps", "tx_kbps", "errors", "drops")}
    assert set(values) == expected


def test_interface_name_sanitised_to_valid_key(tmp_path, monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: clock["t"])
    _net_dev(tmp_path, {"Eth.Weird": (0, 0, 0, 0, 0, 0)})
    c = _collector(tmp_path)
    c.snapshot()
    clock["t"] += 1
    _net_dev(tmp_path, {"Eth.Weird": (10, 0, 0, 10, 0, 0)})
    values, descs = c.snapshot()
    assert values["net.eth_weird.rx_kbps"] == 0.08
    assert all(is_valid_key(k) for k in values)
    assert {d["key"] for d in descs} == set(values)


def test_concurrent_snapshots_are_thread_safe(tmp_path):
    _full_tree(tmp_path)
    c = _collector(tmp_path)
    errors = []

    def worker():
        try:
            for _ in range(50):
                c._sample_once()
                values, descs = c.snapshot()
                assert {d["key"] for d in descs} == set(values)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


@pytest.mark.parametrize("value", [float("inf"), float("nan")])
def test_non_finite_source_values_omitted(tmp_path, value):
    _write(tmp_path / "proc" / "loadavg", f"{value} 0.1 0.1 1/1 1\n")
    c = _collector(tmp_path)
    values, _ = c.snapshot()
    assert "sys.load1" not in values
    assert values["sys.load5"] == 0.1
