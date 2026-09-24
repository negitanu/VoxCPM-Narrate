"""Pattern B: auditory multimodal judge via OpenRouter or Azure OpenAI."""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from voxcpm_narrate.harness.judge import (
    default_audio_judge_model,
    default_llm_api_key,
    default_llm_provider,
    fetch_openrouter_models,
    chat_completion_request,
)

AUDIO_JUDGE_SYSTEM = """あなたは日本語ナレーション音声の聴覚品質レビューアです。
添付された音声を聴き、元テキストとの対応関係を踏まえてイントネーション・アクセント・間の取り方を評価してください。

必ず次の JSON オブジェクトだけを出力してください。説明文やコードフェンスは禁止です。
{
  "feedback": "不自然に感じた具体的な箇所とその理由（日本語）",
  "revised_text": "句読点の追加やひらがな化でアクセントと間合いを調整したテキスト",
  "voice_design_prompt": "VoxCPM2用の英語音声デザインプロンプト（例: Clear articulation, natural pitch, steady pace）"
}
"""

AUDIO_EXTENSIONS = {".wav": "wav", ".mp3": "mp3"}


class AudioJudgeError(ValueError):
    """User-facing / API validation error for Pattern B."""


@dataclass
class AudioJudgeResult:
    feedback: str
    revised_text: str
    voice_design_prompt: str
    model: str
    raw_content: str | None = None
    parse_fallback: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def detect_audio_format(path: Path) -> str:
    ext = path.suffix.lower()
    if ext not in AUDIO_EXTENSIONS:
        raise AudioJudgeError(f"Unsupported audio format: {ext} (wav/mp3 only)")
    return AUDIO_EXTENSIONS[ext]


def encode_audio_base64(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise AudioJudgeError(f"Failed to read audio file: {exc}") from exc
    if not data:
        raise AudioJudgeError("Audio file is empty")
    try:
        return base64.b64encode(data).decode("ascii")
    except Exception as exc:  # noqa: BLE001
        raise AudioJudgeError(f"Base64 encoding failed: {exc}") from exc


def _extract_message_content(body: dict) -> str:
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AudioJudgeError(f"Unexpected chat-completion response shape: {exc}") from exc

    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(p for p in parts if p).strip()
    return str(content or "").strip()


def parse_audio_judge_json(content: str, *, original_text: str) -> AudioJudgeResult:
    """Parse model JSON; fall back to safe defaults when malformed."""
    match = re.search(r"\{.*\}", content, flags=re.DOTALL)
    if not match:
        return AudioJudgeResult(
            feedback=content.strip() or "モデル応答を JSON として解釈できませんでした。",
            revised_text=original_text,
            voice_design_prompt="Clear articulation, natural pitch contour, steady conversational pace",
            model="",
            raw_content=content,
            parse_fallback=True,
        )
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return AudioJudgeResult(
            feedback=content.strip() or "モデル応答の JSON パースに失敗しました。",
            revised_text=original_text,
            voice_design_prompt="Clear articulation, natural pitch contour, steady conversational pace",
            model="",
            raw_content=content,
            parse_fallback=True,
        )

    if not isinstance(data, dict):
        raise AudioJudgeError("モデル応答が JSON オブジェクトではありません")

    feedback = str(data.get("feedback") or "").strip()
    revised = str(data.get("revised_text") or "").strip() or original_text
    prompt = str(data.get("voice_design_prompt") or "").strip()
    if not prompt:
        prompt = "Clear articulation, natural pitch contour, steady conversational pace"
    if not feedback:
        feedback = "具体的なフィードバックを取得できませんでした。"

    return AudioJudgeResult(
        feedback=feedback,
        revised_text=revised,
        voice_design_prompt=prompt,
        model="",
        raw_content=content,
        parse_fallback=False,
    )


def judge_audio_with_openrouter(
    *,
    audio_path: Path,
    original_text: str,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    provider: str | None = None,
    timeout_sec: float = 120.0,
) -> AudioJudgeResult:
    """Send wav/mp3 audio + text to the selected provider's audio deployment."""
    resolved_provider = provider or default_llm_provider()
    resolved_model = (model or default_audio_judge_model(resolved_provider)).strip()
    if not resolved_model:
        raise AudioJudgeError("音声評価に使うモデルまたは Azure デプロイ名を指定してください")

    resolved_key = (api_key if api_key is not None else default_llm_api_key(resolved_provider)).strip()
    if not resolved_key:
        raise AudioJudgeError("音声評価には選択したプロバイダーの API キーが必要です")

    if resolved_provider == "openrouter":
        catalog = fetch_openrouter_models(api_key=resolved_key, base_url=base_url)
        if not any(m["id"] == resolved_model and m.get("supportsAudio") for m in catalog):
            raise AudioJudgeError(
                "選択したモデルの音声入力対応を確認できませんでした。モデル一覧を更新してください"
            )

    path = Path(audio_path)
    if not path.is_file():
        raise AudioJudgeError(f"Audio file not found: {path}")

    audio_format = detect_audio_format(path)
    if resolved_provider == "azure" and path.stat().st_size > 20 * 1024 * 1024:
        raise AudioJudgeError("Azure OpenAI の音声入力は 20 MB 以下にしてください")
    audio_b64 = encode_audio_base64(path)

    user_text = (
        f"元のテキスト: 「{original_text}」\n"
        "添付された音声を聴き、イントネーション・アクセント・間の取り方の不自然な点を評価してください。"
    )
    payload = {
        "model": resolved_model,
        "messages": [
            {"role": "system", "content": AUDIO_JUDGE_SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": audio_b64,
                            "format": audio_format,
                        },
                    },
                ],
            },
        ],
    }
    if resolved_provider == "openrouter":
        payload["temperature"] = 0.2
    try:
        req = chat_completion_request(
            provider=resolved_provider, base_url=base_url, api_key=resolved_key,
            payload=payload, app_title="voxcpm-narrate-pattern-b",
        )
    except ValueError as exc:
        raise AudioJudgeError(str(exc)) from exc
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise AudioJudgeError(f"{resolved_provider} HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise AudioJudgeError(f"{resolved_provider} unreachable: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise AudioJudgeError(f"{resolved_provider} returned non-JSON body: {exc}") from exc

    content = _extract_message_content(body)
    result = parse_audio_judge_json(content, original_text=original_text)
    result.model = resolved_model
    return result
