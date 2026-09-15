"""Optional ASR transcription for CER-based quality checks."""

from __future__ import annotations

from pathlib import Path


class AsrTranscriber:
    """Lazy SenseVoice / FunASR wrapper (already pulled in by voxcpm)."""

    def __init__(self, model_id: str = "iic/SenseVoiceSmall", device: str = "cpu"):
        self.model_id = model_id
        self.device = device
        self._model = None

    def _ensure(self):
        if self._model is not None:
            return
        try:
            from funasr import AutoModel
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "funasr is required for --asr. It usually comes with voxcpm."
            ) from exc

        print(f"Loading ASR: {self.model_id} (device={self.device})")
        # SenseVoiceSmall works well for multilingual short clips
        self._model = AutoModel(
            model=self.model_id,
            vad_model=None,
            punc_model=None,
            device=self.device,
            disable_update=True,
        )

    def transcribe(self, wav_path: Path) -> str:
        self._ensure()
        assert self._model is not None
        result = self._model.generate(input=str(wav_path), language="ja", use_itn=True)
        if not result:
            return ""
        item = result[0]
        if isinstance(item, dict):
            text = item.get("text") or item.get("preds") or ""
        else:
            text = str(item)
        # SenseVoice may prefix language/emotion tags like <|ja|>
        import re

        text = re.sub(r"<\|[^|]*\|>", "", text)
        return text.strip()
