"""TX DSP policy → gst-launch argv fragments (spec 2026-10-03 §3.2, §4).

Chain (inserted between audioresample and pipewiresink, all in F32):
    band-pass (HPF+LPF) → gate (expander) → compressor → makeup (volume) → limiter

``audiodynamic`` is a memoryless per-sample curve (no attack/release/hang — verified with
gst-inspect). Hence: the gate is a low-threshold soft-knee expander that only touches
near-silence; the compressor is a static soft-knee "speech processor"; the limiter is a
hard-knee, very-high-ratio curve = a clipper at the ceiling. The SA818's LPF (ON by
default) band-limits the clip products. The limiter is ALWAYS the last stage so nothing
downstream re-introduces peaks (spec §5).

Degraded (any element missing): pass-through ``volume`` at ``degraded_gain`` (1.0 — no
makeup without a limiter; spec §4 fail-safe toward under-deviation).
"""

from __future__ import annotations

import dataclasses

from station_agent.audio.tx_settings import clamp_ceiling

DSP_ELEMENTS = ("audiocheblimit", "audiodynamic", "volume")


def db_to_linear(db: float) -> float:
    return 10.0 ** (db / 20.0)


@dataclasses.dataclass(frozen=True)
class TxDspPolicy:
    hpf_hz: int = 300
    lpf_hz: int = 3000
    filter_poles: int = 4
    gate_threshold_dbfs: float = -45.0
    gate_ratio: float = 2.0
    comp_threshold_dbfs: float = -30.0
    comp_ratio: float = 3.0
    nominal_compressed_peak_dbfs: float = -24.0
    headroom_db: float = 3.0
    limiter_ratio: float = 1000.0
    degraded_gain: float = 1.0


@dataclasses.dataclass(frozen=True)
class TxDspConfig:
    ceiling_dbfs: float
    enabled: bool = True
    policy: TxDspPolicy = dataclasses.field(default_factory=TxDspPolicy)

    def __post_init__(self):
        # Defence in depth: whatever the caller passes, the ceiling is clamped here.
        object.__setattr__(self, "ceiling_dbfs", clamp_ceiling(self.ceiling_dbfs))

    @property
    def limiter_threshold(self) -> float:
        return db_to_linear(self.ceiling_dbfs)

    @property
    def makeup_db(self) -> float:
        p = self.policy
        return self.ceiling_dbfs - p.headroom_db - p.nominal_compressed_peak_dbfs


def pre_limiter_fragment(cfg: TxDspConfig) -> list[str]:
    p = cfg.policy
    if not cfg.enabled:
        return ["!", "volume", f"volume={p.degraded_gain}"]
    return [
        *(
            "!",
            "audiocheblimit",
            "mode=high-pass",
            f"cutoff={p.hpf_hz}",
            f"poles={p.filter_poles}",
        ),
        *("!", "audiocheblimit", "mode=low-pass", f"cutoff={p.lpf_hz}", f"poles={p.filter_poles}"),
        *("!", "audiodynamic", "mode=expander", "characteristics=soft-knee"),
        f"ratio={p.gate_ratio}",
        f"threshold={db_to_linear(p.gate_threshold_dbfs):.6f}",
        *("!", "audiodynamic", "mode=compressor", "characteristics=soft-knee"),
        f"ratio={p.comp_ratio}",
        f"threshold={db_to_linear(p.comp_threshold_dbfs):.6f}",
        *("!", "volume", f"volume={db_to_linear(cfg.makeup_db):.6f}"),
    ]


def limiter_fragment(cfg: TxDspConfig) -> list[str]:
    if not cfg.enabled:
        return []
    return [
        *("!", "audiodynamic", "mode=compressor", "characteristics=hard-knee"),
        f"ratio={cfg.policy.limiter_ratio}",
        f"threshold={cfg.limiter_threshold:.6f}",
    ]
