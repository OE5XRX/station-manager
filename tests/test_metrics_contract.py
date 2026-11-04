# tests/test_metrics_contract.py
import hashlib
import json

import pytest

from station_agent import metrics_contract as mc


def test_constants():
    assert mc.MAX_KEY_LEN == 64
    assert mc.MAX_KEYS == 200
    assert mc.GROUPS == ("system", "disk", "network", "power", "module", "other")
    assert mc.KEY_RE.pattern == r"^[a-z0-9_]+(\.[a-z0-9_]+){1,3}$"


@pytest.mark.parametrize(
    ("mount", "expected"),
    [
        ("/", "root"),
        ("/boot", "boot"),
        ("/data/log", "data_log"),
        ("/Data-X", "data_x"),
    ],
)
def test_mount_to_instance(mount, expected):
    assert mc.mount_to_instance(mount) == expected


@pytest.mark.parametrize(
    ("key", "ok"),
    [
        ("cpu.temp_c", True),
        ("pwr.12v.voltage_v", True),
        ("a.b.c.d", True),
        ("a.b.c.d.e", False),
        ("cpu", False),
        ("CPU.x", False),
        ("cpu..x", False),
        ("cpu.x\n", False),
        ("a." + "b" * 63, False),
        ("a." + "b" * 62, True),
        (None, False),
        (5, False),
    ],
)
def test_is_valid_key(key, ok):
    assert mc.is_valid_key(key) is ok


def test_normalize_value_scalar():
    assert mc.normalize_value(5) == {"min": 5.0, "max": 5.0, "avg": 5.0}
    out = mc.normalize_value(1.5)
    assert out == {"min": 1.5, "max": 1.5, "avg": 1.5}
    assert all(isinstance(v, float) for v in out.values())


def test_normalize_value_dict():
    out = mc.normalize_value({"min": 1, "max": 3, "avg": 2})
    assert out == {"min": 1.0, "max": 3.0, "avg": 2.0}
    assert all(isinstance(v, float) for v in out.values())


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        "5",
        None,
        float("nan"),
        float("inf"),
        float("-inf"),
        {"min": 1},
        {"min": 1, "max": "x", "avg": 2},
        {"min": 1, "max": True, "avg": 2},
        {"min": 1, "max": float("nan"), "avg": 2},
        {"min": 1, "max": 2, "avg": {"x": 1}},
        [1, 2, 3],
    ],
)
def test_normalize_value_rejects(value):
    assert mc.normalize_value(value) is None


def _reference_hash(ds):
    return hashlib.sha256(
        json.dumps(
            sorted(ds, key=lambda d: d["key"]),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


def test_descriptors_hash_matches_spec_and_is_order_independent():
    ds = [
        {"key": "pwr.12v.voltage_v", "label": "12 V rail – voltage", "unit": "V"},
        {"key": "cpu.temp_c", "label": "CPU temperature", "unit": "°C"},
    ]
    h = mc.descriptors_hash(ds)
    assert h == _reference_hash(ds)
    assert h == mc.descriptors_hash(list(reversed(ds)))
    assert len(h) == 64


def test_descriptors_hash_empty():
    assert mc.descriptors_hash([]) == hashlib.sha256(b"[]").hexdigest()


def test_clean_descriptor_coerces():
    out = mc.clean_descriptor({"key": "x.y", "label": "L" * 80, "group": "weird"})
    assert out is not None
    assert len(out["label"]) == 64
    assert out["group"] == "other"
    assert out["key"] == "x.y"
    assert out["precision"] == 1
    assert out["min"] is None and out["max"] is None
    assert set(out) == {"key", "label", "unit", "group", "precision", "min", "max"}


def test_clean_descriptor_full():
    d = {
        "key": "pwr.12v.voltage_v",
        "label": "12 V rail voltage",
        "unit": "V",
        "group": "power",
        "precision": 2,
        "min": 0,
        "max": 30,
    }
    assert mc.clean_descriptor(d) == {**d, "min": 0.0, "max": 30.0}


def test_clean_descriptor_field_bounds():
    out = mc.clean_descriptor(
        {
            "key": "a.b",
            "unit": "u" * 20,
            "precision": 9,
            "min": "x",
            "max": float("inf"),
        }
    )
    assert len(out["unit"]) == 16
    assert out["precision"] == 6
    assert out["min"] is None and out["max"] is None
    assert mc.clean_descriptor({"key": "a.b", "precision": -3})["precision"] == 0
    assert mc.clean_descriptor({"key": "a.b", "precision": True})["precision"] == 1
    assert mc.clean_descriptor({"key": "a.b", "precision": "2"})["precision"] == 1


@pytest.mark.parametrize("d", [{"key": "bad"}, {"label": "x"}, "x.y", None, {"key": "A.b"}])
def test_clean_descriptor_invalid(d):
    assert mc.clean_descriptor(d) is None


@pytest.mark.parametrize(
    ("key", "pattern", "ok"),
    [
        ("disk.root.used_pct", "disk.*.used_pct", True),
        ("disk.a.b.used_pct", "disk.*.used_pct", False),
        ("disk.root.used_pct", "disk.root.used_pct", True),
        ("disk.root.used_pct", "disk.boot.used_pct", False),
        ("cpu.temp_c", "*.temp_c", True),
        ("disk.root.used_pct", "disk.*.*", False),
    ],
)
def test_key_matches_pattern(key, pattern, ok):
    assert mc.key_matches_pattern(key, pattern) is ok


@pytest.mark.parametrize(
    ("pattern", "ok"),
    [
        ("disk.*.used_pct", True),
        ("*.x", True),
        ("cpu.temp_c", True),
        ("disk.*.*", False),
        ("disk.r*.used_pct", False),
        ("*", False),
        ("cpu", False),
        (None, False),
    ],
)
def test_is_valid_pattern(pattern, ok):
    assert mc.is_valid_pattern(pattern) is ok


def test_clean_descriptor_strips_non_printable():
    raw = {"key": "x.y", "label": "12 V\r\nrail\tvolt\x00age\x1b", "unit": "V\n\u200b"}
    out = mc.clean_descriptor(raw)
    assert out["label"] == "12 Vrailvoltage"
    assert out["unit"] == "V"


def test_clean_descriptor_strips_before_truncating():
    raw = {"key": "x.y", "label": "\n" * 70 + "Label", "unit": "\t" * 20 + "kV"}
    out = mc.clean_descriptor(raw)
    assert out["label"] == "Label"
    assert out["unit"] == "kV"


def test_clean_descriptor_inverted_range_dropped():
    out = mc.clean_descriptor({"key": "a.b", "min": 30, "max": 0})
    assert out["min"] is None and out["max"] is None
    # one-sided and equal bounds survive
    assert mc.clean_descriptor({"key": "a.b", "min": 5})["min"] == 5.0
    eq = mc.clean_descriptor({"key": "a.b", "min": 5, "max": 5})
    assert eq["min"] == 5.0 and eq["max"] == 5.0


def test_clean_descriptor_keeps_nbsp_and_narrow_nbsp():
    raw = {"key": "x.y", "label": "12 V rail", "unit": " kV​"}
    out = mc.clean_descriptor(raw)
    assert out["label"] == "12 Vrail"
    assert out["unit"] == " kV"


def test_printable_predicate_matches_server_validator():
    from django.core.exceptions import ValidationError

    from apps.monitoring.models import validate_printable

    for ch in ["a", " ", " ", " ", "°", "\n", "\t", "\x1b", "​", " ", " "]:
        kept = mc.clean_descriptor({"key": "x.y", "label": ch})["label"] == ch
        try:
            validate_printable(ch)
            accepted = True
        except ValidationError:
            accepted = False
        assert kept is accepted, repr(ch)
