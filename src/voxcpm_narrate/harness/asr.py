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

        from voxcpm_narrate.console import log

        log(f"[bold]Loading ASR:[/bold] {self.model_id} (device={self.device})")
        # SenseVoiceSmall works well for multilingual short clips
        model_source = self.model_id
        if model_source == "iic/SenseVoiceSmall":
            cached = Path.home() / ".cache/modelscope/models/iic--SenseVoiceSmall/snapshots/master"
            if (cached / "config.yaml").is_file() and (cached / "model.pt").is_file():
                model_source = str(cached)
        self._model = AutoModel(
            model=model_source,
            vad_model=None,
            punc_model=None,
            device=self.device,
            disable_update=True,
        )

    def recognize(self, audio) -> str:
        """Preserve auto-detected language tags for the content gate; input is 16 kHz."""
        self._ensure()
        result = self._model.generate(input=audio, language="auto", use_itn=True, fs=16000)
        if not result:
            return ""
        item = result[0]
        return str(item.get("text") or item.get("preds") or "") if isinstance(item, dict) else str(item)

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
