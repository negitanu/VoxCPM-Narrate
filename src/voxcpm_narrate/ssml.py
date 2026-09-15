"""SSML talk-script parsing for VoxCPM narration.

VoxCPM2 does not consume SSML natively, so this module converts a practical
SSML subset into plain-text segments with pause / style metadata.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

BREAK_STRENGTH_SEC = {
    "none": 0.0,
    "x-weak": 0.12,
    "weak": 0.25,
    "medium": 0.5,
    "strong": 0.85,
    "x-strong": 1.2,
}

RATE_HINTS = {
    "x-slow": "かなりゆっくり",
    "slow": "ややゆっくり",
    "medium": "",
    "fast": "やや速めに",
    "x-fast": "速めに",
}

PITCH_HINTS = {
    "x-low": "低めの抑揚で",
    "low": "やや低めに",
    "medium": "",
    "high": "やや高めに",
    "x-high": "高めの抑揚で",
}

EMPHASIS_HINTS = {
    "reduced": "控えめに",
    "moderate": "少し強調して",
    "strong": "はっきり強調して",
}


@dataclass
class SsmlChunk:
    text: str
    pause_before_sec: float = 0.18
    style_hints: list[str] = field(default_factory=list)
    section: str = "ssml"

    @property
    def control_suffix(self) -> str | None:
        hints = [h for h in self.style_hints if h]
        if not hints:
            return None
        # unique preserve order
        seen: set[str] = set()
        ordered: list[str] = []
        for h in hints:
            if h not in seen:
                seen.add(h)
                ordered.append(h)
        return "、".join(ordered)


def _local(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _parse_time_to_sec(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip().lower()
    if value in BREAK_STRENGTH_SEC:
        return BREAK_STRENGTH_SEC[value]
    m = re.fullmatch(r"([0-9]*\.?[0-9]+)\s*(ms|s)?", value)
    if not m:
        return None
    amount = float(m.group(1))
    unit = m.group(2) or "s"
    return amount / 1000.0 if unit == "ms" else amount


def _rate_hint(rate: str | None) -> str | None:
    if not rate:
        return None
    rate = rate.strip().lower()
    if rate in RATE_HINTS:
        return RATE_HINTS[rate] or None
    m = re.fullmatch(r"([+-]?[0-9]*\.?[0-9]+)%", rate)
    if m:
        pct = float(m.group(1))
        if pct <= -20:
            return "かなりゆっくり"
        if pct < 0:
            return "ややゆっくり"
        if pct >= 20:
            return "速めに"
        if pct > 0:
            return "やや速めに"
        return None
    m = re.fullmatch(r"([0-9]*\.?[0-9]+)", rate)
    if m:
        # SSML relative rate multiplier, 1.0 = medium
        mult = float(m.group(1))
        if mult <= 0.8:
            return "ややゆっくり"
        if mult >= 1.2:
            return "やや速めに"
    return None


def _pitch_hint(pitch: str | None) -> str | None:
    if not pitch:
        return None
    pitch = pitch.strip().lower()
    if pitch in PITCH_HINTS:
        return PITCH_HINTS[pitch] or None
    return None


def merge_control(base: str | None, suffix: str | None) -> str | None:
    parts = [p for p in [base, suffix] if p]
    if not parts:
        return None
    return "、".join(parts)


def parse_ssml(ssml_text: str) -> list[SsmlChunk]:
    """Parse SSML into ordered speech chunks."""
    raw = ssml_text.strip()
    if not raw:
        raise ValueError("Empty SSML input")

    # Allow files that omit the root <speak>
    if "<speak" not in raw.lower():
        raw = f"<speak>{raw}</speak>"

    # ElementTree is strict about ampersands; unescape common HTML entities first
    # after protecting already-escaped XML. Using html.unescape on content-only is hard;
    # wrap with a tolerant parse.
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        # Retry after unescaping then re-escaping critical chars inside text is messy;
        # fall back to stripping XML declaration issues / BOM.
        cleaned = raw.lstrip("\ufeff")
        cleaned = re.sub(r"<\?xml[^?]*\?>", "", cleaned, count=1).strip()
        if "<speak" not in cleaned.lower():
            cleaned = f"<speak>{cleaned}</speak>"
        try:
            root = ET.fromstring(cleaned)
        except ET.ParseError as exc:
            raise ValueError(f"Invalid SSML/XML: {exc}") from exc

    chunks: list[SsmlChunk] = []
    pending_break = 0.25
    style_stack: list[list[str]] = [[]]
    section_stack: list[str] = ["ssml"]
    paragraph_idx = 0

    def current_style() -> list[str]:
        merged: list[str] = []
        for layer in style_stack:
            merged.extend(layer)
        return merged

    def emit_text(text: str | None) -> None:
        nonlocal pending_break
        if text is None:
            return
        # Collapse whitespace but keep Japanese text intact
        normalized = html.unescape(text)
        normalized = re.sub(r"[ \t\r\f\v]+", " ", normalized)
        normalized = re.sub(r"\n+", "", normalized).strip()
        if not normalized:
            return

        style = current_style()
        section = section_stack[-1]
        # Merge inline fragments (e.g. text + <sub> + text) unless a break was requested
        if (
            chunks
            and pending_break <= 0.18
            and chunks[-1].section == section
            and chunks[-1].style_hints == style
        ):
            prev = chunks[-1]
            joiner = "" if prev.text.endswith(("。", "、", "！", "？", " ")) else ""
            # Prefer no space for Japanese continuity
            prev.text = f"{prev.text}{joiner}{normalized}"
            pending_break = 0.18
            return

        chunks.append(
            SsmlChunk(
                text=normalized,
                pause_before_sec=pending_break,
                style_hints=style,
                section=section,
            )
        )
        pending_break = 0.18

    def walk(node: ET.Element) -> None:
        nonlocal pending_break, paragraph_idx
        name = _local(node.tag).lower()

        if name == "break":
            strength = node.attrib.get("strength")
            time_v = node.attrib.get("time")
            sec = _parse_time_to_sec(time_v)
            if sec is None:
                sec = BREAK_STRENGTH_SEC.get((strength or "medium").lower(), 0.5)
            pending_break = max(pending_break, sec)
            return

        if name == "sub":
            alias = node.attrib.get("alias")
            if alias:
                emit_text(alias)
                return
            # fall through to children/text if no alias

        hints: list[str] = []
        if name == "prosody":
            rh = _rate_hint(node.attrib.get("rate"))
            ph = _pitch_hint(node.attrib.get("pitch"))
            if rh:
                hints.append(rh)
            if ph:
                hints.append(ph)
            vol = (node.attrib.get("volume") or "").strip().lower()
            if vol in {"soft", "x-soft", "silent"}:
                hints.append("小さめの声で")
            elif vol in {"loud", "x-loud"}:
                hints.append("はっきり大きめに")
        elif name == "emphasis":
            level = (node.attrib.get("level") or "moderate").lower()
            eh = EMPHASIS_HINTS.get(level)
            if eh:
                hints.append(eh)
        elif name in {"emotion", "mstts:express-as"} or name.endswith("express-as"):
            style = node.attrib.get("style") or node.attrib.get("name")
            if style:
                hints.append(f"{style}の感情で")

        pushed_style = bool(hints)
        pushed_section = False
        if name == "p":
            paragraph_idx += 1
            section_stack.append(f"p{paragraph_idx}")
            pushed_section = True
            if chunks:
                pending_break = max(pending_break, 0.45)
        elif name == "s":
            if chunks:
                pending_break = max(pending_break, 0.28)

        if pushed_style:
            style_stack.append(hints)

        # text before first child
        emit_text(node.text)

        for child in list(node):
            walk(child)
            emit_text(child.tail)

        if pushed_style:
            style_stack.pop()
        if pushed_section:
            section_stack.pop()
            pending_break = max(pending_break, 0.35)

        # Ignored-but-accepted tags: speak, voice, lang, say-as, audio(skip), mark, w, phoneme(text)
        if name == "audio":
            # skip embedded audio; optional break
            pending_break = max(pending_break, 0.2)

    if _local(root.tag).lower() != "speak":
        # unexpected root — still try to walk
        pass
    walk(root)

    if not chunks:
        raise ValueError("No speakable text found in SSML")

    return chunks


def ssml_chunks_to_jobs(
    chunks: list[SsmlChunk],
    *,
    max_chars: int,
    base_control: str | None = None,
) -> list[dict[str, object]]:
    """Convert SSML chunks into synthesis jobs, splitting long text as needed."""
    from voxcpm_narrate.extract import split_into_segments

    jobs: list[dict[str, object]] = []
    for chunk_idx, chunk in enumerate(chunks, start=1):
        parts = split_into_segments(chunk.text, max_chars=max_chars)
        control = merge_control(base_control, chunk.control_suffix)
        for part_idx, part in enumerate(parts, start=1):
            pause = chunk.pause_before_sec if part_idx == 1 else 0.12
            job: dict[str, object] = {
                "id": f"{chunk_idx:02d}_{part_idx:03d}",
                "section": chunk.section,
                "text": part,
                "pause_before_sec": pause,
            }
            if control:
                job["control"] = control
            if chunk.control_suffix:
                job["ssml_style"] = chunk.control_suffix
            jobs.append(job)
    return jobs
