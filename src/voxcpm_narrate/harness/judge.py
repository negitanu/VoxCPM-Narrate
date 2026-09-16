"""Optional LLM intonation judge via OpenAI-compatible HTTP API (OpenRouter by default)."""

from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_OPENROUTER_MODEL = "openai/gpt-4o-mini"
DEFAULT_AUDIO_JUDGE_MODEL = "google/gemini-2.0-flash-001"

# Catalog for Web UI / Pattern A (text) / Pattern B (audio).
OPENROUTER_MODELS: tuple[dict[str, Any], ...] = (
    {
        "id": "google/gemini-2.0-flash-001",
        "name": "Gemini 2.0 Flash (高速・音声対応)",
        "supportsAudio": True,
    },
    {
        "id": "google/gemini-2.5-flash",
        "name": "Gemini 2.5 Flash (高精度・音声対応)",
        "supportsAudio": True,
    },
    {
        "id": "google/gemini-1.5-pro",
        "name": "Gemini 1.5 Pro (深層分析・音声対応)",
        "supportsAudio": True,
    },
    {
        "id": "openai/gpt-4o-audio-preview",
        "name": "GPT-4o Audio Preview (音声対応)",
        "supportsAudio": True,
    },
    {
        "id": "openai/gpt-4o-mini",
        "name": "GPT-4o Mini (テキスト専用)",
        "supportsAudio": False,
    },
    {
        "id": "anthropic/claude-3.5-haiku",
        "name": "Claude 3.5 Haiku (テキスト専用)",
        "supportsAudio": False,
    },
    {
        "id": "qwen/qwen-2.5-72b-instruct",
        "name": "Qwen 2.5 72B (テキスト専用)",
        "supportsAudio": False,
    },
    {
        "id": "meta-llama/llama-3.3-70b-instruct",
        "name": "Llama 3.3 70B (テキスト専用)",
        "supportsAudio": False,
    },
    {
        "id": "deepseek/deepseek-chat",
        "name": "DeepSeek Chat (テキスト専用)",
        "supportsAudio": False,
    },
)

# Backwards-compatible alias used by older call sites.
SUGGESTED_OPENROUTER_MODELS: tuple[tuple[str, str], ...] = tuple(
    (m["id"], m["name"]) for m in OPENROUTER_MODELS
)


def model_supports_audio(model_id: str) -> bool:
    mid = (model_id or "").strip()
    for item in OPENROUTER_MODELS:
        if item["id"] == mid:
            return bool(item["supportsAudio"])
    # Unknown custom ids: treat as non-audio unless caller opts in.
    return False


def audio_capable_models() -> list[dict[str, Any]]:
    return [dict(m) for m in OPENROUTER_MODELS if m["supportsAudio"]]


@dataclass
class LlmJudgement:
    score: float | None
    reason: str
    awkward: bool


JUDGE_SYSTEM = """あなたは日本語ナレーション音声の品質レビューアです。
与えられた「台本」と「音声認識結果」を比較し、イントネーションや読みの違和感が疑われるかを判定してください。
音声そのものは聞けないため、認識ゆれ・語順崩れ・不自然な区切り・固有名詞の崩れから推定します。

必ず次の JSON だけを返してください:
{"score": 0.0から1.0, "awkward": true/false, "reason": "短い理由"}
score は高いほど自然。awkward=true は聞き手に違和感が出そうな場合。
"""


def default_llm_base_url() -> str:
    return os.environ.get("OPENROUTER_BASE_URL") or os.environ.get(
        "VOXCPM_LLM_BASE_URL", OPENROUTER_BASE_URL
    )


def default_llm_model() -> str:
    return os.environ.get("OPENROUTER_MODEL") or os.environ.get(
        "VOXCPM_LLM_MODEL", DEFAULT_OPENROUTER_MODEL
    )


def default_audio_judge_model() -> str:
    return os.environ.get("OPENROUTER_AUDIO_MODEL") or os.environ.get(
        "VOXCPM_AUDIO_JUDGE_MODEL", DEFAULT_AUDIO_JUDGE_MODEL
    )


def default_llm_api_key() -> str:
    return os.environ.get("OPENROUTER_API_KEY") or os.environ.get("VOXCPM_LLM_API_KEY") or ""


class LlmJudge:
    """OpenAI-compatible chat-completions judge (OpenRouter / any compatible endpoint)."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        timeout_sec: float = 60.0,
        http_referer: str | None = None,
        app_title: str = "voxcpm-narrate",
    ):
        self.base_url = (base_url or default_llm_base_url()).rstrip("/")
        self.model = model or default_llm_model()
        self.api_key = api_key if api_key is not None else default_llm_api_key()
        self.timeout_sec = timeout_sec
        self.http_referer = http_referer or os.environ.get(
            "OPENROUTER_HTTP_REFERER", "https://github.com/negitanu/VoxCPM-Narrate"
        )
        self.app_title = app_title

    def judge(self, *, text: str, asr_transcript: str | None, metrics_summary: str) -> LlmJudgement:
        if not self.api_key:
            return LlmJudgement(
                score=None,
                reason="llm_missing_api_key",
                awkward=False,
            )

        user = (
            f"台本:\n{text}\n\n"
            f"音声認識結果:\n{asr_transcript or '(なし)'}\n\n"
            f"音響メトリクス:\n{metrics_summary}\n"
        )
        payload = {
            "model": self.model,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": JUDGE_SYSTEM},
                {"role": "user", "content": user},
            ],
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        if "openrouter.ai" in self.base_url:
            headers["HTTP-Referer"] = self.http_referer
            headers["X-Title"] = self.app_title

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            return LlmJudgement(
                score=None,
                reason=f"llm_http_{exc.code}:{detail}",
                awkward=False,
            )
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            return LlmJudgement(score=None, reason=f"llm_unreachable:{exc}", awkward=False)

        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return LlmJudgement(score=None, reason="llm_invalid_response", awkward=False)
        return _parse_judgement(content)


# Backwards-compatible alias
LocalLlmJudge = LlmJudge


def _parse_judgement(content: str) -> LlmJudgement:
    if not isinstance(content, str):
        return LlmJudgement(score=None, reason="llm_invalid_content", awkward=False)
    match = re.search(r"\{.*\}", content, flags=re.DOTALL)
    if not match:
        return LlmJudgement(score=None, reason="llm_parse_failed", awkward=False)
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return LlmJudgement(score=None, reason="llm_invalid_json", awkward=False)

    if not isinstance(data, dict):
        return LlmJudgement(score=None, reason="llm_invalid_json", awkward=False)
    try:
        score = float(data["score"])
    except (KeyError, TypeError, ValueError):
        return LlmJudgement(score=None, reason="llm_invalid_score", awkward=False)
    if not math.isfinite(score):
        return LlmJudgement(score=None, reason="llm_invalid_score", awkward=False)
    score = max(0.0, min(1.0, score))
    awkward = bool(data.get("awkward", score < 0.55))
    reason = str(data.get("reason", "")).strip() or "no_reason"
    return LlmJudgement(score=score, reason=reason, awkward=awkward)


def fetch_openrouter_models(*, api_key: str, base_url: str | None = None) -> list[dict]:
    """Return a compact model list from OpenRouter (id + name)."""
    root = (base_url or default_llm_base_url()).rstrip("/")
    req = urllib.request.Request(
        f"{root}/models",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": os.environ.get(
                "OPENROUTER_HTTP_REFERER", "https://github.com/negitanu/VoxCPM-Narrate"
            ),
            "X-Title": "voxcpm-narrate",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    items = body.get("data") or []
    out: list[dict] = []
    for item in items:
        model_id = str(item.get("id") or "").strip()
        if not model_id:
            continue
        name = str(item.get("name") or model_id).strip()
        modalities = (item.get("architecture") or {}).get("input_modalities") or []
        out.append({"id": model_id, "name": name, "supportsAudio": "audio" in modalities})
    out.sort(key=lambda m: m["id"].lower())
    return out
