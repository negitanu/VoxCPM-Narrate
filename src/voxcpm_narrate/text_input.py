"""Explicit text preparation; never run Japanese through a zh/en normalizer."""

import re
from functools import lru_cache

from voxcpm_narrate.japanese import DEFAULT_CONTROL, JAPANESE_VOICE, LEGACY_CONTROL, japanese_numbers

INPUT_VERSION = "japanese-reading-v5"
JAPANESE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uff66-\uff9f]")


@lru_cache(maxsize=1)
def _normalizer():
    from voxcpm.utils.text_normalize import TextNormalizer

    return TextNormalizer()


def prepare_input(text: str, control: str | None, normalize: bool, *, language: str = "ja",
                  voice_instruction: bool = True, number_reading_style: str = 'hiragana') -> dict:
    """Prepare Japanese readings and a supported English language/style instruction.

    ``auto`` is reserved for reproducible comparisons of already-prepared input.
    ``voice_instruction=False`` omits the parenthesised instruction entirely. Use it in
    continuation mode: VoxCPM concatenates ``prompt_text + target_text``, so an
    instruction placed there lands mid-sentence and is read aloud as English.
    """
    if language not in ("ja", "auto"):
        raise ValueError("Unsupported output language")
    mode = "japanese-readings" if normalize else "preserved"
    spoken = japanese_numbers(text, style=number_reading_style) if normalize else text
    instruction = (control or "").strip()
    if not voice_instruction:
        instruction = ""
    elif language == "auto":
        instruction = f"({instruction.strip('()')})" if instruction else ""
    elif instruction in (DEFAULT_CONTROL, LEGACY_CONTROL):
        instruction = JAPANESE_VOICE
        instruction = f"({instruction})"
    else:
        instruction = "A native Japanese speaker, speaking Japanese" + (
            ", " + instruction.strip("()") if instruction else "")
        instruction = f"({instruction})"
    return {
        "model_input": instruction + spoken,
        "voice_instruction": instruction or None,
        "prepared_reading": spoken,
        "number_reading_style_used": number_reading_style if normalize else None,
        "text_preparation": mode,
        "text_preparation_version": INPUT_VERSION,
        "output_language": language,
    }
