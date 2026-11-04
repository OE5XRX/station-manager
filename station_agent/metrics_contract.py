"""Shared generic-metrics contract (agent + server).

Pure stdlib, no side effects: imported by the station agent to build the
heartbeat ``metrics`` / ``metric_descriptors`` blocks and by the server to
validate and store them. Both sides MUST use these functions so key naming,
value shape and the descriptor hash can never drift apart.
"""

import hashlib
import json
import math
import re

KEY_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+){1,3}$")
MAX_KEY_LEN = 64
MAX_KEYS = 200
GROUPS = ("system", "disk", "network", "power", "module", "other")

MAX_LABEL_LEN = 64
MAX_UNIT_LEN = 16
DEFAULT_PRECISION = 1
MAX_PRECISION = 6

_INSTANCE_BAD_CHARS = re.compile(r"[^a-z0-9_]")
_WILDCARD = "*"


def is_valid_key(key: object) -> bool:
    """True if ``key`` is a str matching ``KEY_RE`` and at most ``MAX_KEY_LEN`` chars."""
    # fullmatch: ``$`` alone would accept a trailing newline.
    return isinstance(key, str) and len(key) <= MAX_KEY_LEN and KEY_RE.fullmatch(key) is not None


def mount_to_instance(mount: str) -> str:
    """Map a mount point to a stable key instance segment (``/`` -> ``root``)."""
    if mount == "/":
        return "root"
    instance = mount.lstrip("/").replace("/", "_").lower()
    return _INSTANCE_BAD_CHARS.sub("_", instance)


def _finite_number(value: object) -> float | None:
    # bool is an int subclass; True/False are never a measurement.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        out = float(value)
    except OverflowError:
        return None
    return out if math.isfinite(out) else None


def normalize_value(value: object) -> dict[str, float] | None:
    """Normalise a metric value to ``{"min", "max", "avg"}`` floats.

    Scalars become min=max=avg. Returns None for bools, non-finite numbers,
    strings and dicts lacking (or holding non-numeric) min/max/avg.
    """
    if isinstance(value, dict):
        out = {}
        for field in ("min", "max", "avg"):
            num = _finite_number(value.get(field))
            if num is None:
                return None
            out[field] = num
        return out
    num = _finite_number(value)
    if num is None:
        return None
    return {"min": num, "max": num, "avg": num}


def _precision(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return DEFAULT_PRECISION
    return max(0, min(MAX_PRECISION, value))


def _printable(value: object, max_len: int) -> str:
    # Strip control/format chars (CR/LF/tab, NUL, ESC, zero-width...) before
    # truncating: labels/units end up in logs, UI and alert messages.
    if not isinstance(value, str):
        return ""
    return "".join(ch for ch in value if ch.isprintable())[:max_len]


def clean_descriptor(d: object) -> dict | None:
    """Coerce an agent-sent descriptor into the canonical shape, or None if unusable."""
    if not isinstance(d, dict) or not is_valid_key(d.get("key")):
        return None
    group = d.get("group")
    lo = _finite_number(d.get("min"))
    hi = _finite_number(d.get("max"))
    if lo is not None and hi is not None and lo > hi:
        # An inverted range would reject every value — treat it as unbounded.
        lo = hi = None
    return {
        "key": d["key"],
        "label": _printable(d.get("label"), MAX_LABEL_LEN),
        "unit": _printable(d.get("unit"), MAX_UNIT_LEN),
        "group": group if group in GROUPS else "other",
        "precision": _precision(d.get("precision")),
        "min": lo,
        "max": hi,
    }


def descriptors_hash(descriptors: list[dict]) -> str:
    """SHA-256 hex over the canonical descriptor serialisation (spec §2.1)."""
    canonical = json.dumps(
        sorted(descriptors, key=lambda d: d["key"]),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def is_valid_pattern(pattern: str) -> bool:
    """True for a valid key, or a key-shaped pattern with exactly one ``*`` segment."""
    if not isinstance(pattern, str):
        return False
    segments = pattern.split(".")
    wildcards = segments.count(_WILDCARD)
    if wildcards == 0:
        return is_valid_key(pattern)
    if wildcards != 1:
        return False
    # Substitute a placeholder segment so the regular key rules apply to the rest.
    probe = ".".join("x" if s == _WILDCARD else s for s in segments)
    return is_valid_key(probe) and len(pattern) <= MAX_KEY_LEN


def pattern_matcher(pattern: str):
    """Validate/split ``pattern`` once; return a ``key -> bool`` matcher.

    Same semantics as :func:`key_matches_pattern`, for matching many keys
    against one pattern without re-validating it per key.
    """
    if not is_valid_pattern(pattern):
        return lambda key: False
    pattern_segments = pattern.split(".")
    if _WILDCARD not in pattern_segments:
        return lambda key: key == pattern

    def match(key: str) -> bool:
        key_segments = key.split(".")
        if len(key_segments) != len(pattern_segments):
            return False
        return all(
            p in (_WILDCARD, k) for k, p in zip(key_segments, pattern_segments, strict=True)
        )

    return match


def key_matches_pattern(key: str, pattern: str) -> bool:
    """Match ``key`` against ``pattern``; a ``*`` segment matches exactly one segment."""
    return pattern_matcher(pattern)(key)
