"""Retry strategies for awkward intonation segments."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GenParams:
    control: str | None
    cfg_value: float
    inference_timesteps: int
    normalize: bool
    seed: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "control": self.control,
            "cfg_value": self.cfg_value,
            "inference_timesteps": self.inference_timesteps,
            "normalize": self.normalize,
            "seed": self.seed,
        }


def build_strategies(base: GenParams, *, max_rounds: int) -> list[tuple[str, GenParams]]:
    """Ordered improvement attempts (excluding the baseline already generated)."""
    control = base.control or "落ち着いたプレゼン説明調"
    variants: list[tuple[str, GenParams]] = [
        (
            "seed_jitter",
            GenParams(
                control=base.control,
                cfg_value=base.cfg_value,
                inference_timesteps=base.inference_timesteps,
                normalize=base.normalize,
                seed=base.seed + 17,
            ),
        ),
        (
            "lower_cfg",
            GenParams(
                control=base.control,
                cfg_value=max(1.4, base.cfg_value - 0.4),
                inference_timesteps=base.inference_timesteps,
                normalize=base.normalize,
                seed=base.seed + 31,
            ),
        ),
        (
            "slower_natural",
            GenParams(
                control=f"{control}、ややゆっくり、句読点で自然に間を取る、一本調子を避ける",
                cfg_value=base.cfg_value,
                inference_timesteps=base.inference_timesteps,
                normalize=base.normalize,
                seed=base.seed + 47,
            ),
        ),
        (
            "clearer_phrasing",
            GenParams(
                control=f"{control}、意味の区切りで抑揚をつけて、丁寧に読み上げる",
                cfg_value=max(1.5, base.cfg_value - 0.2),
                inference_timesteps=min(20, base.inference_timesteps + 4),
                normalize=base.normalize,
                seed=base.seed + 67,
            ),
        ),
        (
            "more_steps",
            GenParams(
                control=base.control,
                cfg_value=base.cfg_value,
                inference_timesteps=min(24, base.inference_timesteps + 6),
                normalize=base.normalize,
                seed=base.seed + 89,
            ),
        ),
        (
            "normalize_toggle",
            GenParams(
                control=f"{control}、落ち着いて明瞭に",
                cfg_value=max(1.5, base.cfg_value - 0.3),
                inference_timesteps=base.inference_timesteps,
                normalize=not base.normalize,
                seed=base.seed + 101,
            ),
        ),
    ]
    return variants[: max(0, max_rounds)]
