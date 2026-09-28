"""Shared validation for production settings and segment edits."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from voxcpm_narrate.harness.judge import (
    default_audio_judge_model, default_llm_model, default_llm_provider,
)


class Validated(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Config(Validated):
    number_reading_style: Literal['hiragana', 'kanji'] = 'kanji'
    convert_numbers: bool = True
    device: Literal["auto", "cpu", "mps", "cuda"] = "auto"
    control: str = Field(default="日本語、明瞭な声、自然な抑揚、会話に近いテンポ", max_length=1000)
    max_chars: int = Field(default=120, ge=10, le=500)
    cfg_value: float = Field(default=2, ge=0.1, le=5)
    timesteps: int = Field(default=10, ge=1, le=100)
    seed: int = Field(default=42, ge=0, le=2**31 - 1)
    model_id: str = Field(default="openbmb/VoxCPM2", min_length=1, max_length=200)
    pace_mode: Literal["off", "reference", "fixed"] = "off"
    target_mora_rate: float = Field(default=7, ge=2, le=12)
    improve: bool = False
    improve_asr: bool = False
    improve_llm: bool = False
    improve_rounds: int = Field(default=3, ge=0, le=6)
    llm_provider: Literal["openrouter", "azure"] = Field(default_factory=default_llm_provider)
    llm_model: str = Field(default_factory=default_llm_model, max_length=200)
    audio_judge_model: str = Field(default_factory=default_audio_judge_model, max_length=200)

    @model_validator(mode="before")
    @classmethod
    def provider_defaults(cls, value):
        if isinstance(value, dict):
            value = dict(value)
            provider = value.get("llm_provider") or default_llm_provider()
            if provider in {"openrouter", "azure"}:
                value.setdefault("llm_model", default_llm_model(provider))
                value.setdefault("audio_judge_model", default_audio_judge_model(provider))
        return value


class Draft(Validated):
    number_reading_style: Literal['hiragana', 'kanji'] | None = None
    convert_numbers: bool | None = None
    text: str = Field(min_length=1, max_length=2000)
    reading: str = Field(default="", max_length=2000)
    control: str = Field(default="", max_length=1000)
    pause_before_sec: float = Field(default=0.18, ge=0, le=10)


