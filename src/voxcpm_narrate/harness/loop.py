"""Self-improvement loop: detect awkward intonation and regenerate."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from voxcpm_narrate.console import ensure_rich_tqdm, log, progress_session
from voxcpm_narrate.harness.asr import AsrTranscriber
from voxcpm_narrate.harness.judge import LlmJudge
from voxcpm_narrate.harness.metrics import (
    SegmentScore,
    char_error_rate,
    combine_scores,
    score_acoustic,
    score_duration,
)
from voxcpm_narrate.harness.strategies import GenParams, build_strategies
from voxcpm_narrate.ssml import merge_control
from voxcpm_narrate.synthesize import assemble_full_wav, generate_wav, load_model


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def evaluate_segment(
    *,
    segment_id: str,
    text: str,
    wav,
    sample_rate: int,
    wav_path: Path | None,
    asr: AsrTranscriber | None,
    judge: LlmJudge | None,
    awkward_threshold: float,
) -> SegmentScore:
    duration_sec = float(len(wav) / sample_rate) if sample_rate else 0.0
    duration_score, cps, dur_reasons = score_duration(text, duration_sec)
    edge_score, energy_score, _cv, ac_reasons = score_acoustic(wav, sample_rate)

    asr_transcript = None
    cer = None
    if asr is not None and wav_path is not None:
        asr_transcript = asr.transcribe(wav_path)
        cer = char_error_rate(text, asr_transcript)

    llm_score = None
    llm_reason = None
    if judge is not None:
        summary = (
            f"duration={duration_sec:.2f}s cps={cps:.2f} "
            f"duration_score={duration_score:.2f} edge={edge_score:.2f} "
            f"energy={energy_score:.2f} cer={cer}"
        )
        judgement = judge.judge(text=text, asr_transcript=asr_transcript, metrics_summary=summary)
        llm_score = judgement.score
        llm_reason = judgement.reason
        if judgement.awkward and llm_score is not None:
            # combine_scores also uses threshold; keep reason visible
            pass

    return combine_scores(
        segment_id=segment_id,
        text=text,
        duration_sec=duration_sec,
        duration_score=duration_score,
        chars_per_sec=cps,
        edge_score=edge_score,
        energy_score=energy_score,
        acoustic_reasons=dur_reasons + ac_reasons,
        asr_transcript=asr_transcript,
        cer=cer,
        llm_score=llm_score,
        llm_reason=llm_reason,
        awkward_threshold=awkward_threshold,
    )


def run_improve_loop(
    *,
    run_dir: Path,
    model_id: str,
    device: str,
    optimize: bool,
    reference_audio: str | None,
    base_control: str | None,
    base_cfg: float,
    base_timesteps: int,
    base_normalize: bool,
    base_seed: int,
    max_rounds: int,
    awkward_threshold: float,
    only_awkward: bool,
    limit: int | None,
    segment_ids: list[str] | None,
    use_asr: bool,
    asr_device: str,
    use_llm: bool,
    llm_base_url: str,
    llm_model: str,
    llm_api_key: str,
) -> dict[str, Any]:
    import soundfile as sf
    from voxcpm_narrate.artifacts import write_wav

    run_dir = run_dir.resolve()
    manifest_path = run_dir / "manifest.json"
    segments_dir = run_dir / "segments"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"manifest.json not found in {run_dir}")
    if not segments_dir.is_dir():
        raise FileNotFoundError(f"segments/ not found in {run_dir}")

    jobs: list[dict[str, object]] = _load_json(manifest_path)
    work_dir = run_dir / "harness"
    candidates_dir = work_dir / "candidates"
    work_dir.mkdir(parents=True, exist_ok=True)
    candidates_dir.mkdir(parents=True, exist_ok=True)

    asr = AsrTranscriber(device=asr_device) if use_asr else None
    judge = (
        LlmJudge(base_url=llm_base_url, model=llm_model, api_key=llm_api_key) if use_llm else None
    )

    # Probe sample rate from first existing wav
    sample_rate = None
    for job in jobs:
        p = segments_dir / f"{job['id']}.wav"
        if p.is_file():
            _, sample_rate = sf.read(p, dtype="float32")
            break
    if sample_rate is None:
        raise FileNotFoundError(
            f"no segment wav files found under {segments_dir}. "
            "Point --run-dir at a completed synthesis run (not a --dry-run)."
        )

    selected_jobs = jobs
    if segment_ids:
        idset = set(segment_ids)
        selected_jobs = [j for j in jobs if str(j["id"]) in idset]
    if limit is not None:
        selected_jobs = selected_jobs[:limit]

    base = GenParams(
        control=base_control,
        cfg_value=base_cfg,
        inference_timesteps=base_timesteps,
        normalize=base_normalize,
        seed=base_seed,
    )

    report: dict[str, Any] = {
        "run_dir": str(run_dir),
        "awkward_threshold": awkward_threshold,
        "max_rounds": max_rounds,
        "use_asr": use_asr,
        "use_llm": use_llm,
        "segments": [],
    }

    model = None
    improved_count = 0
    awkward_count = 0

    ensure_rich_tqdm()
    with progress_session() as progress:
        task_id = progress.add_task("Improve", total=len(selected_jobs))
        for idx, job in enumerate(selected_jobs, start=1):
            seg_id = str(job["id"])
            text = str(job["text"])
            seg_path = segments_dir / f"{seg_id}.wav"
            progress.update(task_id, description=f"[[{idx}/{len(selected_jobs)}]] {seg_id}")
            if not seg_path.is_file():
                log(f"[yellow]skip[/yellow] missing wav: {seg_path}")
                progress.advance(task_id)
                continue

            wav, sr = sf.read(seg_path, dtype="float32")
            wav = wav.reshape(-1)
            score = evaluate_segment(
                segment_id=seg_id,
                text=text,
                wav=wav,
                sample_rate=sr,
                wav_path=seg_path,
                asr=asr,
                judge=judge,
                awkward_threshold=awkward_threshold,
            )
            awkward_style = "red" if score.awkward else "green"
            log(
                f"[cyan][[{idx}/{len(selected_jobs)}]][/cyan] {seg_id} "
                f"overall=[bold]{score.overall:.3f}[/bold] "
                f"awkward=[{awkward_style}]{score.awkward}[/{awkward_style}] "
                f"reasons={score.reasons}"
            )

            entry: dict[str, Any] = {
                "id": seg_id,
                "text": text,
                "initial": score.to_dict(),
                "attempts": [],
                "replaced": False,
                "final": score.to_dict(),
            }

            if score.awkward:
                awkward_count += 1

            should_retry = score.awkward or not only_awkward
            if max_rounds <= 0 or not should_retry:
                report["segments"].append(entry)
                progress.advance(task_id)
                continue

            if model is None:
                model = load_model(model_id, device=device, optimize=optimize)

            best_score = score
            best_wav = wav
            strategies = build_strategies(base, max_rounds=max_rounds)

            for strategy_name, params in strategies:
                log(f"  -> retry [magenta]{strategy_name}[/magenta]: {params.to_dict()}")
                retry_control = params.control
                if job.get("ssml_style"):
                    retry_control = merge_control(params.control, str(job["ssml_style"]))
                elif job.get("control") and strategy_name in {
                    "seed_jitter",
                    "lower_cfg",
                    "more_steps",
                }:
                    retry_control = str(job["control"])

                input_metadata = {}
                cand_wav = generate_wav(
                    model,
                    text=text,
                    control=retry_control,
                    reference_audio=reference_audio,
                    cfg_value=params.cfg_value,
                    inference_timesteps=params.inference_timesteps,
                    normalize=params.normalize,
                    seed=params.seed,
                    input_metadata=input_metadata,
                )
                cand_path = candidates_dir / f"{seg_id}__{strategy_name}.wav"
                write_wav(cand_path, cand_wav, sample_rate)

                cand_score = evaluate_segment(
                    segment_id=seg_id,
                    text=text,
                    wav=cand_wav,
                    sample_rate=sample_rate,
                    wav_path=cand_path,
                    asr=asr,
                    judge=judge,
                    awkward_threshold=awkward_threshold,
                )
                attempt = {
                    **input_metadata,
                    "strategy": strategy_name,
                    "params": params.to_dict(),
                    "score": cand_score.to_dict(),
                    "path": str(cand_path),
                }
                entry["attempts"].append(attempt)
                log(
                    f"     score={cand_score.overall:.3f} awkward={cand_score.awkward} "
                    f"reasons={cand_score.reasons}"
                )

                comparable = judge is None or (
                    cand_score.llm_score is not None and score.llm_score is not None
                )
                if (
                    comparable
                    and not cand_score.awkward
                    and cand_score.overall > best_score.overall
                ):
                    best_score = cand_score
                    best_wav = cand_wav

                # Early stop if no longer awkward and improved
                if (
                    comparable
                    and not cand_score.awkward
                    and cand_score.overall > score.overall + 0.01
                ):
                    best_score = cand_score
                    best_wav = cand_wav
                    break

            if best_score.overall > score.overall + 0.01:
                # backup original once
                backup = work_dir / "originals" / f"{seg_id}.wav"
                backup.parent.mkdir(parents=True, exist_ok=True)
                if not backup.exists():
                    shutil.copy2(seg_path, backup)
                write_wav(seg_path, best_wav, sample_rate)
                entry["replaced"] = True
                entry["final"] = best_score.to_dict()
                improved_count += 1
                log(
                    f"  [green]✓ replaced[/green] {seg_id}: "
                    f"{score.overall:.3f} -> {best_score.overall:.3f}"
                )
            else:
                entry["final"] = score.to_dict()
                log(f"  · keep original {seg_id} (no better candidate)")

            report["segments"].append(entry)
            progress.advance(task_id)

    report["summary"] = {
        "checked": len(report["segments"]),
        "awkward_initial": awkward_count,
        "replaced": improved_count,
    }

    report_path = work_dir / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    log(f"[green]Wrote harness report:[/green] {report_path}")

    # Reassemble full.wav with possibly updated segments
    assemble_full_wav(
        jobs,
        segments_dir,
        sample_rate=sample_rate,
        output_path=run_dir / "full.wav",
    )

    # human-readable summary
    summary_path = work_dir / "summary.md"
    lines = [
        "# Intonation improvement summary",
        "",
        f"- checked: {report['summary']['checked']}",
        f"- awkward(initial): {report['summary']['awkward_initial']}",
        f"- replaced: {report['summary']['replaced']}",
        "",
        "| id | initial | final | replaced | reasons |",
        "|---|---:|---:|:---:|---|",
    ]
    for seg in report["segments"]:
        lines.append(
            f"| {seg['id']} | {seg['initial']['overall']:.3f} | {seg['final']['overall']:.3f} | "
            f"{'yes' if seg['replaced'] else 'no'} | "
            f"{','.join(seg['final'].get('reasons') or [])} |"
        )
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"[green]Wrote summary:[/green] {summary_path}")
    return report
