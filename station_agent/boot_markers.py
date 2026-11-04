"""Boot-path markers from the kernel cmdline (slot-boot-stage2 spec §5).

The CM4 boot scripts (stage 1 on FAT, stage 2 in the A/B slot) report how the
boot went via two kernel args:

- ``oe5xrx.stage2=ok|missing|failed``
- ``oe5xrx.overlays=ok:<a>[,<b>...]|failed:<name>``

The OTA commit gate refuses to confirm a trial boot when stage 2 did not run
cleanly or an overlay failed. A cmdline *without* any marker is an older
FAT/stage 1 (legacy boot path) and never blocks — otherwise every station that
has not been reflashed yet would roll back every update. A marker that is
present but unintelligible fails closed: its presence proves the new boot path,
and an unknown value is not proof of "ok".

Single source for the parser: the pre-commit health check and the generic
metrics both use this module.
"""

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

CMDLINE_PATH = "/proc/cmdline"

STAGE2_KEY = "oe5xrx.stage2"
OVERLAYS_KEY = "oe5xrx.overlays"

_STAGE2_VALUES = ("ok", "missing", "failed")
_OVERLAY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
# Raw marker text quoted in a blocker message is clipped + made printable.
_QUOTE_MAX = 64


def _quote(value: str) -> str:
    clean = "".join(ch if ch.isprintable() else "?" for ch in value)
    if len(clean) > _QUOTE_MAX:
        clean = clean[:_QUOTE_MAX] + "..."
    return repr(clean)


def _parse_overlays(raw: str) -> tuple[str, tuple[str, ...]]:
    """Return ``(state, names)``; state is ``ok``, ``failed`` or ``malformed``."""
    if raw == "ok":
        return "ok", ()
    state, sep, rest = raw.partition(":")
    if not sep or state not in ("ok", "failed"):
        return "malformed", ()
    if not rest:
        # "ok:" = no overlays configured; "failed:" without a name is not parseable.
        return ("ok", ()) if state == "ok" else ("malformed", ())
    names = tuple(rest.split(","))
    if not all(_OVERLAY_NAME_RE.fullmatch(n) for n in names):
        return "malformed", ()
    return state, names


@dataclass(frozen=True)
class BootMarkers:
    """Parsed boot markers; ``None`` fields mean the marker is absent."""

    stage2: str | None = None  # raw value ("" for a bare flag)
    overlays: str | None = None  # "ok" | "failed" | "malformed"
    overlays_raw: str = ""
    overlay_names: tuple[str, ...] = ()  # applied overlays when overlays == "ok"
    failed_overlays: tuple[str, ...] = ()  # when overlays == "failed"

    @property
    def legacy(self) -> bool:
        """True when no marker is present (old FAT / stage 1 without markers)."""
        return self.stage2 is None and self.overlays is None

    def commit_blockers(self) -> list[str]:
        """Reasons why a trial boot must not be committed (empty = commit allowed)."""
        reasons = []
        if self.stage2 is not None and self.stage2 != "ok":
            if self.stage2 in _STAGE2_VALUES:
                reasons.append(f"{STAGE2_KEY}={self.stage2}")
            else:
                reasons.append(f"{STAGE2_KEY} has unknown value {_quote(self.stage2)}")
        if self.overlays == "failed":
            reasons.append(f"{OVERLAYS_KEY}=failed:{','.join(self.failed_overlays)}")
        elif self.overlays == "malformed":
            reasons.append(f"{OVERLAYS_KEY} is malformed: {_quote(self.overlays_raw)}")
        return reasons

    def metric_values(self) -> dict[str, float]:
        """Generic-metrics values; a key is only reported when its marker is present."""
        out: dict[str, float] = {}
        if self.stage2 is not None:
            out["boot.stage2_ok"] = 1.0 if self.stage2 == "ok" else 0.0
        if self.overlays is not None:
            if self.overlays == "failed":
                out["boot.overlays_failed"] = float(len(self.failed_overlays))
            elif self.overlays == "malformed":
                # Unparseable marker: at least one overlay state is unknown -> count 1.
                out["boot.overlays_failed"] = 1.0
            else:
                out["boot.overlays_failed"] = 0.0
        return out


def _kernel_args(cmdline: str):
    """Yield ``(key, value)`` like the kernel's ``next_arg()``.

    Whitespace inside double quotes does not split; a leading quote on the
    token and on the value plus a trailing quote are stripped
    (``"k=v"`` / ``k="v"`` -> ``k``, ``v``). An unquoted bare ``--`` ends the
    kernel args (the rest belongs to init).
    """
    tokens: list[tuple[str, bool]] = []  # (raw token, contained a quote)
    buf: list[str] = []
    in_quote = quoted = False
    for ch in cmdline:
        if ch == '"':
            in_quote = not in_quote
            quoted = True
        if ch.isspace() and not in_quote:
            if buf:
                tokens.append(("".join(buf), quoted))
            buf, quoted = [], False
            continue
        buf.append(ch)
    if buf:
        tokens.append(("".join(buf), quoted))
    for token, had_quote in tokens:
        if token == "--" and not had_quote:
            return
        if token.startswith('"'):
            token = token[1:]
        key, _, value = token.partition("=")
        if value.startswith('"'):
            value = value[1:]
        if token.endswith('"'):
            # The closing quote ends whichever part is last.
            if value:
                value = value[:-1]
            elif key.endswith('"'):
                key = key[:-1]
        yield key, value


def parse_boot_markers(cmdline: str) -> BootMarkers:
    """Parse the markers out of a kernel cmdline string.

    Tokenised like the kernel (double quotes group, see ``_kernel_args``); if a
    marker occurs more than once the last occurrence wins (kernel semantics —
    stage 2 appends after stage 1). Args after a bare ``--`` belong to init,
    not the kernel, and are ignored.
    """
    stage2: str | None = None
    overlays_raw: str | None = None
    for key, value in _kernel_args(cmdline):
        if key == STAGE2_KEY:
            stage2 = value
        elif key == OVERLAYS_KEY:
            overlays_raw = value
    if overlays_raw is None:
        return BootMarkers(stage2=stage2)
    state, names = _parse_overlays(overlays_raw)
    return BootMarkers(
        stage2=stage2,
        overlays=state,
        overlays_raw=overlays_raw,
        overlay_names=names if state == "ok" else (),
        failed_overlays=names if state == "failed" else (),
    )


def read_boot_markers(path: str | None = None) -> BootMarkers | None:
    """Read and parse ``path`` (default ``/proc/cmdline``); ``None`` if unreadable."""
    try:
        return _read(path or CMDLINE_PATH)
    except OSError:
        return None


def _read(path: str) -> BootMarkers:
    with open(path, encoding="utf-8", errors="replace") as fh:
        return parse_boot_markers(fh.read())


def check_boot_markers(path: str | None = None) -> tuple[bool, str]:
    """Pre-commit health check: ``(passed, message)`` in the health_check style.

    A failing message contains ``FAIL`` so the caller's rolled_back reason
    carries it.
    """
    path = path or CMDLINE_PATH
    try:
        markers = _read(path)
    except FileNotFoundError:
        # No procfs cmdline at all (container/sim): indistinguishable from the
        # legacy path, so don't gate.
        logger.warning("%s not found; not gating the commit on boot markers", path)
        return True, f"Boot SKIP: {path} not found, boot markers unknown"
    except OSError as exc:
        # The file exists but can't be read (e.g. a hardened unit hiding /proc):
        # fail closed rather than silently switching the gate off.
        return False, f"Boot FAIL: cannot read {path}: {type(exc).__name__}"
    if markers.legacy:
        return True, "Boot OK: no boot markers (legacy boot path)"
    blockers = markers.commit_blockers()
    if blockers:
        return False, "Boot FAIL: " + "; ".join(blockers)
    return True, "Boot OK: " + ", ".join(
        f"{k}={v}"
        for k, v in (
            (STAGE2_KEY, markers.stage2),
            (OVERLAYS_KEY, markers.overlays_raw if markers.overlays else None),
        )
        if v is not None
    )
