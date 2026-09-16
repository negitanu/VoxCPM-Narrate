"""Audio helpers and VoxCPM2 synthesis."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from voxcpm_narrate.console import ensure_rich_tqdm, log, progress_session


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
    output_wav.parent.mkdir(parents=True, exist_ok=True)
    if reference.suffix.lower() == ".wav":
        if reference.resolve() != output_wav.resolve():
            shutil.copy2(reference, output_wav)
        return output_wav

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
    import importlib.metadata
    import platform

    from huggingface_hub import snapshot_download
    from voxcpm import VoxCPM

    # Patch after import: VoxCPM binds `from tqdm import tqdm` at module load.
    ensure_rich_tqdm()

    log(f"[bold]Loading model:[/bold] {model_id} (device={device}, optimize={optimize})")
    local_model = Path(model_id).is_dir()
    resolved = Path(model_id) if local_model else Path(snapshot_download(repo_id=model_id))
    model = VoxCPM.from_pretrained(
        str(resolved),
        load_denoiser=False,
        device=device,
        optimize=optimize,
    )
    model.revision = resolved.name if resolved.parent.name == "snapshots" else None
    model.narrate_provenance = {
        "model_revision": model.revision,
        "model_id": "local-model" if local_model else model_id,
        "device": str(getattr(model.tts_model, "device", device)),
        "python": platform.python_version(),
        "libraries": {name: importlib.metadata.version(name)
                      for name in ("voxcpm", "torch", "numpy", "soundfile")},
    }
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
    input_metadata: dict | None = None,
    output_language: str = "ja",
):
    ensure_rich_tqdm()
    import numpy as np

    from voxcpm_narrate.text_input import prepare_input

    prepared = prepare_input(text, control, normalize, language=output_language)
    if input_metadata is not None:
        input_metadata.update(prepared)
        input_metadata["runtime"] = getattr(model, "narrate_provenance", None)
    kwargs: dict[str, Any] = {
        "text": prepared["model_input"],
        "cfg_value": cfg_value,
        "inference_timesteps": inference_timesteps,
        "normalize": False,
        "retry_badcase": True,
    }
    if reference_audio:
        kwargs["reference_wav_path"] = reference_audio
    if seed is not None:
        kwargs["seed"] = seed

    try:
        wav = model.generate(**kwargs)
    except TypeError as exc:
        if "seed" not in str(exc) or "unexpected keyword" not in str(exc):
            raise
        import random
        import torch

        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
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
        pieces.append(silence(pause_before, sample_rate))
        pieces.append(wav)

    pieces.append(silence(0.6, sample_rate))
    full = np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.float32)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    from voxcpm_narrate.artifacts import write_wav

    write_wav(output_path, full, sample_rate)
    log(f"[green]Saved:[/green] {output_path} ([cyan]{len(full) / sample_rate:.1f}[/cyan] sec)")
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
    manifest_path.write_text(
        json.dumps(jobs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    lines_path = output_dir / "segments.txt"
    lines_path.write_text("\n".join(str(j["text"]) for j in jobs) + "\n", encoding="utf-8")

    if dry_run:
        log(f"[yellow]dry-run[/yellow] segments={len(jobs)} manifest={manifest_path}")
        log(f"[yellow]dry-run[/yellow] lines={lines_path}")
        return output_dir / "full.wav"

    if reference_audio:
        ref_path = Path(reference_audio)
        if not ref_path.is_file():
            raise FileNotFoundError(f"reference audio not found: {ref_path}")
        log(f"[bold]Reference voice:[/bold] {ref_path}")

    if progress_callback:
        progress_callback(
            phase="loading_model", current=0, total=len(jobs), message="Loading VoxCPM2…"
        )

    model = load_model(model_id, device=device, optimize=optimize)
    sample_rate = int(model.tts_model.sample_rate)

    with progress_session() as progress:
        task_id = progress.add_task("Synthesize", total=len(jobs))
        for i, job in enumerate(jobs, start=1):
            seg_path = segments_dir / f"{job['id']}.wav"
            preview = str(job["text"])
            if len(preview) > 72:
                preview = preview[:72] + "…"
            progress.update(
                task_id,
                description=f"[{i}/{len(jobs)}] {job['id']} · {preview}",
            )
            log(
                f"[cyan][[{i}/{len(jobs)}]][/cyan] {job['id']} "
                f"chars={len(str(job['text']))} section={job['section']}"
            )
            log(f"  text: {job['text']}")
            job_control = job.get("control")
            effective_control = str(job_control) if job_control else control
            if job.get("ssml_style"):
                log(f"  style: {job['ssml_style']}")

            if progress_callback:
                cb_preview = str(job["text"])
                if len(cb_preview) > 80:
                    cb_preview = cb_preview[:80] + "…"
                progress_callback(
                    phase="synthesize",
                    segment_status="running",
                    current=i,
                    total=len(jobs),
                    message=f"[{i}/{len(jobs)}] {cb_preview}",
                    segment_id=str(job["id"]),
                )

            input_metadata = {}
            wav = generate_wav(
                model,
                text=str(job["text"]),
                control=effective_control,
                reference_audio=reference_audio,
                cfg_value=cfg_value,
                inference_timesteps=inference_timesteps,
                normalize=normalize,
                seed=seed,
                input_metadata=input_metadata,
            )
            from voxcpm_narrate.artifacts import write_json, write_wav

            write_wav(seg_path, wav, sample_rate)
            write_json(seg_path.with_suffix(".input.json"), input_metadata)
            progress.advance(task_id)

            if progress_callback:
                progress_callback(
                    phase="synthesize",
                    segment_status="ready",
                    current=i,
                    total=len(jobs),
                    message=f"[{i}/{len(jobs)}] ready · {job['id']}",
                    segment_id=str(job["id"]),
                )

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
