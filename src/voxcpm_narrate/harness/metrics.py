"""Acoustic and text-alignment metrics for narration quality."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field


@dataclass
class SegmentScore:
    segment_id: str
    text: str
    duration_sec: float
    chars_per_sec: float
    duration_score: float
    edge_score: float
    energy_score: float
    asr_transcript: str | None = None
    cer: float | None = None
    asr_score: float | None = None
    llm_score: float | None = None
    llm_reason: str | None = None
    overall: float = 0.0
    awkward: bool = False
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _normalize_ja(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.lower()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[、。！？!?.,・「」『』（）()【】\[\]…・−-]", "", text)
    return text


def char_error_rate(reference: str, hypothesis: str) -> float:
    """Levenshtein CER over normalized Japanese/ASCII characters."""
    ref = _normalize_ja(reference)
    hyp = _normalize_ja(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0

    # classic DP
    prev = list(range(len(hyp) + 1))
    for i, rc in enumerate(ref, start=1):
        curr = [i]
        for j, hc in enumerate(hyp, start=1):
            ins = curr[j - 1] + 1
            delete = prev[j] + 1
            sub = prev[j - 1] + (0 if rc == hc else 1)
            curr.append(min(ins, delete, sub))
        prev = curr
    return prev[-1] / max(1, len(ref))


def score_acoustic(wav, sample_rate: int) -> tuple[float, float, float, list[str]]:
    """Return duration helpers are separate; here edge/energy scores + reasons."""
    import numpy as np

    reasons: list[str] = []
    if wav.size == 0:
        return 0.0, 0.0, 0.0, ["empty_audio"]

    x = np.asarray(wav, dtype=np.float32).reshape(-1)
    abs_x = np.abs(x)
    # leading/trailing silence ratio
    thr = max(1e-4, float(np.percentile(abs_x, 60)) * 0.15)
    nonzero = np.where(abs_x > thr)[0]
    if nonzero.size == 0:
        reasons.append("mostly_silence")
        return 0.0, 0.0, 0.0, reasons

    lead = nonzero[0] / sample_rate
    trail = (len(x) - 1 - nonzero[-1]) / sample_rate
    total = len(x) / sample_rate
    lead_ratio = lead / max(total, 1e-6)
    trail_ratio = trail / max(total, 1e-6)

    edge_score = 1.0
    if lead > 0.8 or lead_ratio > 0.25:
        edge_score -= 0.35
        reasons.append("long_leading_silence")
    if trail > 0.8 or trail_ratio > 0.25:
        edge_score -= 0.25
        reasons.append("long_trailing_silence")
    if lead < 0.01 and abs_x[: max(1, int(0.02 * sample_rate))].max() > 0.95:
        edge_score -= 0.2
        reasons.append("hard_attack_clipping")
    edge_score = float(max(0.0, min(1.0, edge_score)))

    # frame RMS stability
    frame = max(1, int(0.02 * sample_rate))
    hop = frame
    rms = []
    for i in range(0, len(x) - frame + 1, hop):
        frame_x = x[i : i + frame]
        rms.append(float(np.sqrt(np.mean(frame_x * frame_x) + 1e-12)))
    rms_arr = np.asarray(rms, dtype=np.float32)
    mean = float(rms_arr.mean()) if rms_arr.size else 0.0
    std = float(rms_arr.std()) if rms_arr.size else 0.0
    cv = std / max(mean, 1e-6)

    energy_score = 1.0
    if mean < 0.005:
        energy_score -= 0.5
        reasons.append("very_low_energy")
    if cv > 1.8:
        energy_score -= 0.35
        reasons.append("unstable_energy")
    if cv < 0.08 and total > 2.0:
        energy_score -= 0.25
        reasons.append("flat_monotonic_energy")
    peak = float(abs_x.max())
    if peak > 0.99:
        energy_score -= 0.2
        reasons.append("clipping")
    energy_score = float(max(0.0, min(1.0, energy_score)))

    return edge_score, energy_score, cv, reasons


def score_duration(text: str, duration_sec: float) -> tuple[float, float, list[str]]:
    """Japanese presentation speech is typically ~4.5–9.5 chars/sec."""
    reasons: list[str] = []
    chars = max(1, len(re.sub(r"\s+", "", text)))
    cps = chars / max(duration_sec, 1e-6)

    # soft trapezoid score
    if 5.0 <= cps <= 8.5:
        score = 1.0
    elif 4.0 <= cps < 5.0:
        score = 0.7 + 0.3 * (cps - 4.0) / 1.0
    elif 8.5 < cps <= 10.5:
        score = 0.7 + 0.3 * (10.5 - cps) / 2.0
    elif 3.0 <= cps < 4.0 or 10.5 < cps <= 12.5:
        score = 0.35
        reasons.append("duration_borderline")
    else:
        score = 0.1
        reasons.append("duration_outlier")

    if duration_sec < 0.6 and chars > 8:
        score = min(score, 0.2)
        reasons.append("too_short_for_text")

    return float(score), float(cps), reasons


def combine_scores(
    *,
    segment_id: str,
    text: str,
    duration_sec: float,
    duration_score: float,
    chars_per_sec: float,
    edge_score: float,
    energy_score: float,
    acoustic_reasons: list[str],
    asr_transcript: str | None,
    cer: float | None,
    llm_score: float | None,
    llm_reason: str | None,
    awkward_threshold: float,
) -> SegmentScore:
    reasons = list(acoustic_reasons)
    asr_score = None
    if cer is not None:
        asr_score = float(max(0.0, min(1.0, 1.0 - cer)))
        if cer >= 0.35:
            reasons.append(f"high_cer:{cer:.2f}")
        elif cer >= 0.18:
            reasons.append(f"moderate_cer:{cer:.2f}")

    if llm_score is not None and llm_score < 0.55:
        reasons.append("llm_flagged_intonation")

    weights = {
        "duration": 0.25,
        "edge": 0.15,
        "energy": 0.20,
    }
    overall = (
        duration_score * weights["duration"]
        + edge_score * weights["edge"]
        + energy_score * weights["energy"]
    )
    weight_sum = sum(weights.values())

    if asr_score is not None:
        overall += asr_score * 0.30
        weight_sum += 0.30
    if llm_score is not None:
        overall += llm_score * 0.25
        weight_sum += 0.25

    overall = float(overall / weight_sum)

    awkward = overall < awkward_threshold
    if cer is not None and cer >= 0.30:
        awkward = True
    if duration_score <= 0.15:
        awkward = True

    return SegmentScore(
        segment_id=segment_id,
        text=text,
        duration_sec=duration_sec,
        chars_per_sec=chars_per_sec,
        duration_score=duration_score,
        edge_score=edge_score,
        energy_score=energy_score,
        asr_transcript=asr_transcript,
        cer=cer,
        asr_score=asr_score,
        llm_score=llm_score,
        llm_reason=llm_reason,
        overall=overall,
        awkward=awkward,
        reasons=reasons,
    )
