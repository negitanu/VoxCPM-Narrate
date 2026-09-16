"""Text extraction and segmentation for narration TTS."""

from __future__ import annotations

import re
from pathlib import Path

SECTION_HEADER_RE = re.compile(r"^##\s+")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?])\s*|(?<=\.)(?!\d)\s+")
DEFAULT_PAUSE_MARKERS = (
    "一つ目",
    "二つ目",
    "三つ目",
    "四つ目",
    "五つ目",
    "First",
    "Second",
    "Third",
)


def extract_markdown_blocks(
    markdown: str,
    *,
    narration_heading: str = "読み上げ本文",
) -> list[dict[str, str]]:
    """Extract narration blocks under headings matching *narration_heading*.

    Tracks the nearest preceding ``##`` heading as the block title.
    """
    heading_re = re.compile(rf"^###\s+{re.escape(narration_heading)}\s*$")
    lines = markdown.splitlines()
    blocks: list[dict[str, str]] = []
    current_section = "untitled"
    in_narration = False
    buf: list[str] = []

    def flush() -> None:
        nonlocal buf
        text = "\n".join(buf).strip()
        if text:
            blocks.append({"section": current_section, "text": text})
        buf = []

    for line in lines:
        if SECTION_HEADER_RE.match(line):
            if in_narration:
                flush()
                in_narration = False
            current_section = line.lstrip("#").strip()
            continue

        if heading_re.match(line):
            if in_narration:
                flush()
            in_narration = True
            continue

        if in_narration:
            if line.startswith("#"):
                flush()
                in_narration = False
                continue
            buf.append(line)

    if in_narration:
        flush()

    return blocks


def load_input_blocks(
    path: Path,
    *,
    mode: str,
    narration_heading: str,
) -> list[dict[str, str]]:
    """Load narration blocks from markdown, plain text, or line-oriented text."""
    text = path.read_text(encoding="utf-8")
    if mode == "markdown":
        blocks = extract_markdown_blocks(text, narration_heading=narration_heading)
        if not blocks:
            raise ValueError(
                f"No narration sections found for heading '### {narration_heading}' in {path}"
            )
        return blocks

    if mode == "plain":
        body = text.strip()
        if not body:
            raise ValueError(f"Empty input: {path}")
        return [{"section": path.stem, "text": body}]

    if mode == "lines":
        lines = [
            ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")
        ]
        if not lines:
            raise ValueError(f"No non-empty lines in: {path}")
        return [{"section": path.stem, "text": "\n\n".join(lines)}]

    if mode == "ssml":
        raise ValueError("SSML mode should use load_jobs(), not load_input_blocks()")

    raise ValueError(f"Unknown input mode: {mode}")


def detect_input_mode(path: Path, explicit_mode: str | None = None) -> str:
    if explicit_mode and explicit_mode != "auto":
        return explicit_mode
    suffix = path.suffix.lower()
    if suffix in {".ssml", ".xml"}:
        return "ssml"
    if suffix in {".md", ".markdown"}:
        return "markdown"
    if suffix in {".txt", ".text"}:
        return "plain"
    return "markdown"


def load_jobs(
    path: Path,
    *,
    mode: str,
    narration_heading: str,
    max_chars: int,
    base_control: str | None = None,
) -> list[dict[str, object]]:
    """Load synthesis jobs from any supported input mode."""
    resolved = detect_input_mode(path, mode)
    if resolved == "ssml":
        from voxcpm_narrate.ssml import parse_ssml, ssml_chunks_to_jobs

        chunks = parse_ssml(path.read_text(encoding="utf-8"))
        return ssml_chunks_to_jobs(chunks, max_chars=max_chars, base_control=base_control)

    blocks = load_input_blocks(path, mode=resolved, narration_heading=narration_heading)
    return build_jobs(blocks, max_chars=max_chars)


def split_into_segments(text: str, *, max_chars: int) -> list[str]:
    """Split narration into short segments suitable for long-form VoxCPM2."""
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    segments: list[str] = []

    for para in paragraphs:
        para = re.sub(r"\s*\n\s*", "", para)
        if len(para) <= max_chars:
            segments.append(para)
            continue

        sentences = [s.strip() for s in SENTENCE_SPLIT_RE.split(para) if s.strip()]
        chunk = ""
        for sentence in sentences:
            candidate = f"{chunk}{sentence}" if chunk else sentence
            if chunk and len(candidate) > max_chars:
                segments.append(chunk)
                chunk = sentence
            else:
                chunk = candidate
        if chunk:
            segments.append(chunk)

    bounded: list[str] = []
    for segment in segments:
        while len(segment) > max_chars:
            prefix = segment[:max_chars]
            boundaries = list(re.finditer(r"[、,;；：:\s]", prefix))
            # Avoid a tiny fragment when the only comma is near the beginning.
            useful = [b for b in boundaries if b.end() >= max_chars // 2]
            cut = useful[-1].end() if useful else max_chars
            bounded.append(segment[:cut])
            segment = segment[cut:]
        if segment:
            bounded.append(segment)
    return bounded


def build_jobs(
    blocks: list[dict[str, str]],
    *,
    max_chars: int,
    pause_markers: tuple[str, ...] = DEFAULT_PAUSE_MARKERS,
    default_pause_sec: float = 0.18,
    marker_pause_sec: float = 0.55,
) -> list[dict[str, object]]:
    """Build synthesis jobs with optional longer pauses before list markers."""
    marker_re = re.compile(r"^(" + "|".join(re.escape(m) for m in pause_markers) + r")")
    jobs: list[dict[str, object]] = []
    for block_idx, block in enumerate(blocks, start=1):
        for seg_idx, segment in enumerate(
            split_into_segments(block["text"], max_chars=max_chars),
            start=1,
        ):
            pause_before = marker_pause_sec if marker_re.match(segment) else default_pause_sec
            jobs.append(
                {
                    "id": f"{block_idx:02d}_{seg_idx:03d}",
                    "section": block["section"],
                    "text": segment,
                    "pause_before_sec": pause_before,
                }
            )
    return jobs
