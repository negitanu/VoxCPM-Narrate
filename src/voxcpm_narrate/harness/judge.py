"""Optional local-LLM intonation judge via OpenAI-compatible HTTP API."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass
class LlmJudgement:
    score: float
    reason: str
    awkward: bool


JUDGE_SYSTEM = """あなたは日本語ナレーション音声の品質レビューアです。
与えられた「台本」と「音声認識結果」を比較し、イントネーションや読みの違和感が疑われるかを判定してください。
音声そのものは聞けないため、認識ゆれ・語順崩れ・不自然な区切り・固有名詞の崩れから推定します。

必ず次の JSON だけを返してください:
{"score": 0.0から1.0, "awkward": true/false, "reason": "短い理由"}
score は高いほど自然。awkward=true は聞き手に違和感が出そうな場合。
"""


class LocalLlmJudge:
    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:11434/v1",
        model: str = "llama3.2",
        api_key: str = "ollama",
        timeout_sec: float = 60.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_sec = timeout_sec

    def judge(self, *, text: str, asr_transcript: str | None, metrics_summary: str) -> LlmJudgement:
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
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            return LlmJudgement(score=0.5, reason=f"llm_unreachable:{exc}", awkward=False)

        content = body["choices"][0]["message"]["content"]
        return _parse_judgement(content)


def _parse_judgement(content: str) -> LlmJudgement:
    match = re.search(r"\{.*\}", content, flags=re.DOTALL)
    if not match:
        return LlmJudgement(score=0.5, reason="llm_parse_failed", awkward=False)
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return LlmJudgement(score=0.5, reason="llm_invalid_json", awkward=False)

    score = float(data.get("score", 0.5))
    score = max(0.0, min(1.0, score))
    awkward = bool(data.get("awkward", score < 0.55))
    reason = str(data.get("reason", "")).strip() or "no_reason"
    return LlmJudgement(score=score, reason=reason, awkward=awkward)
