"""CLI entrypoint for voxcpm-narrate."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from voxcpm_narrate.console import ensure_rich_tqdm, log, log_error
from voxcpm_narrate.extract import detect_input_mode, load_jobs
from voxcpm_narrate.harness.judge import default_llm_api_key, default_llm_base_url, default_llm_model
from voxcpm_narrate.harness.loop import run_improve_loop
from voxcpm_narrate.synthesize import prepare_reference_wav, synthesize


def _add_common_synth_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--model-id", default="openbmb/VoxCPM2")
    p.add_argument("--device", default="auto", choices=["auto", "cpu", "mps", "cuda"])
    p.add_argument("--optimize", action="store_true", help="Enable torch.compile (mainly CUDA)")
    p.add_argument("--no-optimize", dest="optimize", action="store_false")
    p.set_defaults(optimize=False)
    p.add_argument("--cfg-value", type=float, default=2.0)
    p.add_argument("--inference-timesteps", type=int, default=10)
    p.add_argument("--normalize", action="store_true", default=True,
                   help="Expand numbers into Japanese readings (not audio gain)")
    p.add_argument("--no-normalize", dest="normalize", action="store_false")
    p.add_argument(
        "--control",
        default="日本語、明瞭な声、自然な抑揚、会話に近いテンポ",
        help="Style / voice-design control instruction",
    )
    p.add_argument("--no-control", action="store_true", help="Disable control prefix")
    p.add_argument(
        "--reference",
        "--reference-audio",
        dest="reference",
        type=Path,
        default=None,
        help="Reference voice for cloning (wav/ogg/mp3/...)",
    )
    p.add_argument("--seed", type=int, default=42, help="Random seed")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="voxcpm-narrate",
        description="Local VoxCPM2 narration TTS + intonation self-improvement harness",
    )
    sub = parser.add_subparsers(dest="command")

    # --- synthesize (also default when no subcommand for backward compat) ---
    syn = sub.add_parser("synthesize", help="Synthesize narration audio from a script")
    syn.add_argument(
        "--input",
        "--script",
        dest="input",
        type=Path,
        required=True,
        help="Input markdown / text / SSML file",
    )
    syn.add_argument(
        "--mode",
        choices=["auto", "markdown", "plain", "lines", "ssml"],
        default="auto",
        help="Input parsing mode (default: auto by file extension)",
    )
    syn.add_argument(
        "--narration-heading",
        default="読み上げ本文",
        help="Markdown ### heading that marks narration blocks",
    )
    syn.add_argument("--output-dir", type=Path, default=None)
    syn.add_argument("--out-root", type=Path, default=Path("output/voxcpm2"))
    syn.add_argument("--max-chars", type=int, default=120)
    syn.add_argument("--section", type=int, default=None)
    syn.add_argument("--slide", type=int, default=None, help="Alias of --section")
    syn.add_argument("--limit", type=int, default=None)
    syn.add_argument("--dry-run", action="store_true")
    _add_common_synth_args(syn)

    # --- improve ---
    imp = sub.add_parser(
        "improve",
        help="Detect awkward intonation segments and regenerate them in a loop",
    )
    imp.add_argument(
        "--run-dir",
        type=Path,
        default=Path("output/voxcpm2/latest"),
        help="Existing synthesis run directory (with manifest.json + segments/)",
    )
    imp.add_argument("--max-rounds", type=int, default=3, help="Max regeneration strategies per segment")
    imp.add_argument(
        "--threshold",
        type=float,
        default=0.62,
        help="Overall score below this is treated as awkward",
    )
    imp.add_argument(
        "--all",
        dest="only_awkward",
        action="store_false",
        help="Try improving all segments, not only awkward ones",
    )
    imp.set_defaults(only_awkward=True)
    imp.add_argument("--limit", type=int, default=None, help="Only first N selected segments")
    imp.add_argument(
        "--segment-id",
        action="append",
        default=None,
        help="Specific segment id to improve (repeatable)",
    )
    imp.add_argument("--asr", action="store_true", help="Enable SenseVoice ASR + CER scoring")
    imp.add_argument("--asr-device", default="cpu", help="ASR device (default: cpu)")
    imp.add_argument(
        "--llm-judge",
        action="store_true",
        help="Enable OpenRouter (OpenAI-compatible) LLM judge",
    )
    imp.add_argument(
        "--llm-base-url",
        default=default_llm_base_url(),
        help="OpenAI-compatible base URL (default: OpenRouter)",
    )
    imp.add_argument(
        "--llm-model",
        default=default_llm_model(),
        help="Model id (default: openai/gpt-4o-mini or OPENROUTER_MODEL)",
    )
    imp.add_argument(
        "--llm-api-key",
        default=default_llm_api_key(),
        help="API key (default: OPENROUTER_API_KEY)",
    )
    _add_common_synth_args(imp)

    return parser


def _prepare_reference(args: argparse.Namespace, output_dir: Path) -> str | None:
    if args.reference is None:
        return None
    if not args.reference.is_file():
        raise FileNotFoundError(f"reference voice not found: {args.reference}")
    prepared = prepare_reference_wav(args.reference, output_dir / "reference.wav")
    return str(prepared)


def cmd_synthesize(args: argparse.Namespace) -> int:
    if not args.input.is_file():
        log_error(f"input not found: {args.input}")
        return 1

    control = None if args.no_control else args.control
    mode = detect_input_mode(args.input, args.mode)

    try:
        jobs = load_jobs(
            args.input,
            mode=mode,
            narration_heading=args.narration_heading,
            max_chars=args.max_chars,
            base_control=control if mode == "ssml" else None,
        )
    except ValueError as exc:
        log_error(str(exc))
        return 1

    # Section filter: by 1-based section index among unique section names in order
    section = args.section if args.section is not None else args.slide
    if section is not None:
        ordered_sections: list[str] = []
        for job in jobs:
            sec = str(job["section"])
            if sec not in ordered_sections:
                ordered_sections.append(sec)
        if section < 1 or section > len(ordered_sections):
            log_error(f"--section out of range: 1..{len(ordered_sections)}")
            return 1
        target = ordered_sections[section - 1]
        jobs = [j for j in jobs if str(j["section"]) == target]

    if args.limit is not None:
        jobs = jobs[: args.limit]

    if not jobs:
        log_error("No synthesis jobs produced from input.")
        return 1

    if args.output_dir is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = args.out_root / f"run_{stamp}"
    else:
        output_dir = args.output_dir

    reference_audio = None
    if not args.dry_run and args.reference is not None:
        try:
            reference_audio = _prepare_reference(args, output_dir)
        except (RuntimeError, FileNotFoundError) as exc:
            log_error(str(exc))
            return 1

    log(f"[bold]==>[/bold] parse mode: {mode} ({len(jobs)} segments)")
    synthesize(
        jobs,
        output_dir=output_dir,
        model_id=args.model_id,
        device=args.device,
        optimize=args.optimize,
        cfg_value=args.cfg_value,
        inference_timesteps=args.inference_timesteps,
        normalize=args.normalize,
        control=control,
        reference_audio=reference_audio,
        seed=args.seed,
        dry_run=args.dry_run,
    )

    if args.output_dir is None and not args.dry_run:
        latest = args.out_root / "latest"
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        latest.symlink_to(output_dir.resolve())

    log("")
    log("[bold green]Done.[/bold green]")
    log(f"  segments : {output_dir / 'segments'}/")
    log(f"  manifest : {output_dir / 'manifest.json'}")
    log(f"  full wav : {output_dir / 'full.wav'}")
    if args.output_dir is None and not args.dry_run:
        log(f"  latest   : {args.out_root / 'latest'} -> {output_dir}")
    return 0


def cmd_improve(args: argparse.Namespace) -> int:
    run_dir = args.run_dir
    if run_dir.is_symlink():
        run_dir = run_dir.resolve()
    if not run_dir.is_dir():
        log_error(f"run dir not found: {args.run_dir}")
        return 1

    control = None if args.no_control else args.control
    reference_audio = None
    if args.reference is not None:
        try:
            reference_audio = _prepare_reference(args, run_dir / "harness")
        except (RuntimeError, FileNotFoundError) as exc:
            log_error(str(exc))
            return 1
    else:
        # Prefer existing converted reference in the run dir
        existing = run_dir / "reference.wav"
        if existing.is_file():
            reference_audio = str(existing)

    try:
        report = run_improve_loop(
            run_dir=run_dir,
            model_id=args.model_id,
            device=args.device,
            optimize=args.optimize,
            reference_audio=reference_audio,
            base_control=control,
            base_cfg=args.cfg_value,
            base_timesteps=args.inference_timesteps,
            base_normalize=args.normalize,
            base_seed=args.seed,
            max_rounds=args.max_rounds,
            awkward_threshold=args.threshold,
            only_awkward=args.only_awkward,
            limit=args.limit,
            segment_ids=args.segment_id,
            use_asr=args.asr,
            asr_device=args.asr_device,
            use_llm=args.llm_judge,
            llm_base_url=args.llm_base_url,
            llm_model=args.llm_model,
            llm_api_key=args.llm_api_key,
        )
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        log_error(f"improve failed: {exc}")
        return 1

    summary = report.get("summary", {})
    log("")
    log("[bold green]Improve done.[/bold green]")
    log(f"  checked  : {summary.get('checked')}")
    log(f"  awkward  : {summary.get('awkward_initial')}")
    log(f"  replaced : {summary.get('replaced')}")
    log(f"  report   : {run_dir / 'harness' / 'report.json'}")
    log(f"  full wav : {run_dir / 'full.wav'}")
    return 0


def _legacy_argv(argv: list[str]) -> list[str]:
    """Allow `voxcpm-narrate --input ...` without an explicit subcommand."""
    if not argv:
        return ["synthesize"]
    if argv[0] in {"synthesize", "improve", "-h", "--help"}:
        return argv
    return ["synthesize", *argv]


def main(argv: list[str] | None = None) -> int:
    ensure_rich_tqdm()
    raw = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(_legacy_argv(raw))

    if args.command == "improve":
        return cmd_improve(args)
    if args.command == "synthesize":
        return cmd_synthesize(args)

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
