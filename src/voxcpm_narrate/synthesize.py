"""Audio helpers and VoxCPM2 synthesis."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


def silence(seconds: float, sample_rate: int):
    import numpy as np

    n = max(0, int(round(seconds * sample_rate)))
    return np.zeros(n, dtype=np.float32)


def wrap_control(text: str, control: str | None) -> str:
    if not control:
        return text
    control = control.strip()
    if not control:
        return text
    if not control.startswith("("):
        control = f"({control})"
    return f"{control}{text}"


def prepare_reference_wav(reference: Path, output_wav: Path) -> Path:
    """Return a WAV path usable by VoxCPM2 (convert non-WAV via ffmpeg when needed)."""
    if reference.suffix.lower() == ".wav":
        return reference

    if not shutil.which("ffmpeg"):
        raise RuntimeError(
            f"ffmpeg is required to convert {reference}. "
            "Install ffmpeg or pass a .wav reference file."
        )

    output_wav.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(reference),
        "-ac",
        "1",
        "-ar",
        "16000",
        str(output_wav),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return output_wav


def load_model(model_id: str, *, device: str, optimize: bool):
    from voxcpm import VoxCPM

    print(f"Loading model: {model_id} (device={device}, optimize={optimize})")
    model = VoxCPM.from_pretrained(
        model_id,
        load_denoiser=False,
        device=device,
        optimize=optimize,
    )
    return model


def generate_wav(
    model,
    *,
    text: str,
    control: str | None,
    reference_audio: str | None,
    cfg_value: float,
    inference_timesteps: int,
    normalize: bool,
    seed: int | None,
):
    import numpy as np

    kwargs: dict[str, Any] = {
        "text": wrap_control(text, control),
        "cfg_value": cfg_value,
        "inference_timesteps": inference_timesteps,
        "normalize": normalize,
        "retry_badcase": True,
    }
    if reference_audio:
        kwargs["reference_wav_path"] = reference_audio
    if seed is not None:
        kwargs["seed"] = seed

    try:
        wav = model.generate(**kwargs)
    except TypeError:
        kwargs.pop("seed", None)
        wav = model.generate(**kwargs)

    return np.asarray(wav, dtype=np.float32).reshape(-1)


def assemble_full_wav(
    jobs: list[dict[str, object]],
    segments_dir: Path,
    *,
    sample_rate: int,
    output_path: Path,
) -> Path:
    import numpy as np
    import soundfile as sf

    pieces = []
    for i, job in enumerate(jobs):
        seg_path = segments_dir / f"{job['id']}.wav"
        if not seg_path.is_file():
            raise FileNotFoundError(f"missing segment wav: {seg_path}")
        wav, sr = sf.read(seg_path, dtype="float32")
        if sr != sample_rate:
            raise ValueError(f"sample rate mismatch in {seg_path}: {sr} != {sample_rate}")
        wav = np.asarray(wav, dtype=np.float32).reshape(-1)
        pause_before = float(job.get("pause_before_sec", 0.18))
        if i == 0:
            pieces.append(silence(0.25, sample_rate))
        else:
            pieces.append(silence(pause_before, sample_rate))
        pieces.append(wav)

    pieces.append(silence(0.6, sample_rate))
    full = np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.float32)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(output_path, full, sample_rate)
    print(f"Saved: {output_path} ({len(full) / sample_rate:.1f} sec)")
    return output_path


def synthesize(
    jobs: list[dict[str, object]],
    *,
    output_dir: Path,
    model_id: str,
    device: str,
    optimize: bool,
    cfg_value: float,
    inference_timesteps: int,
    normalize: bool,
    control: str | None,
    reference_audio: str | None,
    seed: int | None,
    dry_run: bool,
    progress_callback=None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    segments_dir = output_dir / "segments"
    segments_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(jobs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines_path = output_dir / "segments.txt"
    lines_path.write_text("\n".join(str(j["text"]) for j in jobs) + "\n", encoding="utf-8")

    if dry_run:
        print(f"[dry-run] segments={len(jobs)} manifest={manifest_path}")
        print(f"[dry-run] lines={lines_path}")
        return output_dir / "full.wav"

    import soundfile as sf

    if reference_audio:
        ref_path = Path(reference_audio)
        if not ref_path.is_file():
            raise FileNotFoundError(f"reference audio not found: {ref_path}")
        print(f"Reference voice: {ref_path}")

    if progress_callback:
        progress_callback(phase="loading_model", current=0, total=len(jobs), message="Loading VoxCPM2…")

    model = load_model(model_id, device=device, optimize=optimize)
    sample_rate = int(model.tts_model.sample_rate)

    for i, job in enumerate(jobs, start=1):
        seg_path = segments_dir / f"{job['id']}.wav"
        print(
            f"[{i}/{len(jobs)}] {job['id']} chars={len(str(job['text']))} section={job['section']}"
        )
        print(f"  text: {job['text']}")
        job_control = job.get("control")
        effective_control = str(job_control) if job_control else control
        if job.get("ssml_style"):
            print(f"  style: {job['ssml_style']}")

        if progress_callback:
            preview = str(job["text"])
            if len(preview) > 80:
                preview = preview[:80] + "…"
            progress_callback(
                phase="synthesize",
                current=i,
                total=len(jobs),
                message=f"[{i}/{len(jobs)}] {preview}",
                segment_id=str(job["id"]),
            )

        wav = generate_wav(
            model,
            text=str(job["text"]),
            control=effective_control,
            reference_audio=reference_audio,
            cfg_value=cfg_value,
            inference_timesteps=inference_timesteps,
            normalize=normalize,
            seed=seed,
        )
        sf.write(seg_path, wav, sample_rate)

    if progress_callback:
        progress_callback(
            phase="assemble",
            current=len(jobs),
            total=len(jobs),
            message="Assembling full.wav…",
        )

    return assemble_full_wav(
        jobs,
        segments_dir,
        sample_rate=sample_rate,
        output_path=output_dir / "full.wav",
    )
