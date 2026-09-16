"""Reproducible local A/B corpus generation; no subjective scores are fabricated."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import random
import time
from pathlib import Path

import numpy as np

from voxcpm_narrate.artifacts import file_hash, write_json, write_wav
from voxcpm_narrate.audio_quality import measure
from voxcpm_narrate.synthesize import assemble_full_wav, generate_wav, load_model, wrap_control
from voxcpm_narrate.text_input import _normalizer, prepare_input
from voxcpm_narrate.japanese import DEFAULT_CONTROL


def main():
    parser = argparse.ArgumentParser(description="日本語の入力正規化を固定条件で A/B 比較")
    parser.add_argument("corpus", type=Path)
    parser.add_argument("output", type=Path, help="新規の出力ディレクトリ")
    parser.add_argument("--seeds", type=int, nargs="+", default=[42, 59, 73])
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--device", default="cpu", choices=["cpu", "mps", "cuda", "auto"])
    parser.add_argument("--model-id", default="openbmb/VoxCPM2")
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--prepare-only", action="store_true", help="モデルをロードせず入力変換だけ比較")
    parser.add_argument("--allow-download", action="store_true", help="未取得のモデルのダウンロードを許可")
    args = parser.parse_args()
    if not args.allow_download:
        os.environ["HF_HUB_OFFLINE"] = "1"
    if args.limit < 1 or args.output.exists():
        parser.error("limit は正数、output は存在しないディレクトリを指定してください")
    corpus = json.loads(args.corpus.read_text())[:args.limit]
    if not corpus or len({case["id"] for case in corpus}) != len(corpus):
        parser.error("評価台本は空でなく、id は一意である必要があります")
    # IDs become filenames; only controlled corpus identifiers are accepted.
    import re

    if any(not re.fullmatch(r"[a-zA-Z0-9_-]+", case["id"]) for case in corpus):
        parser.error("評価台本の id は英数字・ハイフン・アンダースコアにしてください")
    args.output.mkdir(parents=True)
    control = DEFAULT_CONTROL
    inputs = []
    for case in corpus:
        legacy = _normalizer().normalize(wrap_control(case["text"], control))
        current = prepare_input(case["text"], control, True)
        inputs.append({**case, "legacy": legacy, "preserved": current["model_input"]})
    write_json(args.output / "input-comparison.json", inputs)
    environment = {"python": platform.python_version(), "platform": platform.platform(),
                   "device_requested": args.device, "model_id": args.model_id,
                   "corpus_sha256": file_hash(args.corpus), "seeds": args.seeds,
                   "reference_sha256": file_hash(args.reference) if args.reference else None,
                   "versions": {name: importlib.metadata.version(name)
                                for name in ["voxcpm", "torch", "numpy", "soundfile"]},
                   "cfg_value": 2, "inference_timesteps": 10, "control": control}
    write_json(args.output / "environment.json", environment)
    if args.prepare_only:
        return
    import torch

    if args.device == "cpu":
        torch.set_num_threads(4)
    model = load_model(args.model_id, device=args.device, optimize=False)
    sr = int(model.tts_model.sample_rate)
    environment.update(model_revision=getattr(model, "revision", None), sample_rate=sr,
                       runtime=getattr(model, "narrate_provenance", None),
                       model_device=str(getattr(model.tts_model, "device", "unknown")),
                       torch_threads=torch.get_num_threads())
    write_json(args.output / "environment.json", environment)
    results, blind_key = [], []
    rng = random.Random(20260916)
    for seed in args.seeds:
        manifests = {variant: [] for variant in ("legacy", "preserved")}
        for case in inputs:
            pair = {}
            for variant in manifests:
                folder = args.output / f"{variant}_{seed}"
                metadata = {}
                started = time.monotonic()
                wav = generate_wav(model, text=case[variant], control=None,
                                   reference_audio=str(args.reference) if args.reference else None,
                                   cfg_value=2, inference_timesteps=10, normalize=False,
                                   seed=seed, input_metadata=metadata, output_language="auto")
                path = folder / "segments" / f"{case['id']}.wav"
                write_wav(path, wav, sr)
                pair[variant] = wav
                result = {"id": case["id"], "seed": seed, "variant": variant,
                          "text": case["text"], "check": case["check"], **metadata,
                          "sha256": file_hash(path), "generation_sec": time.monotonic() - started,
                          "duration_sec": len(wav) / sr, "subjective_evaluation": None}
                try:
                    result["acoustic"] = measure(path)
                except ValueError as exc:
                    result["measurement_error"] = str(exc)
                results.append(result)
                write_json(args.output / "results.json", results)
                manifests[variant].append({"id": case["id"], "text": case["text"],
                                           "section": "品質評価", "pause_before_sec": .3})
            # RMS-matched audition files are separate from untouched raw output.
            # This is only a comparison aid, not a LUFS-normalized deliverable.
            levels = {key: float(np.sqrt(np.mean(wave.astype(float) ** 2)))
                      for key, wave in pair.items()}
            target = min(min(levels.values()), *[
                .9 * levels[key] / max(float(np.max(np.abs(wave))), 1e-12)
                for key, wave in pair.items()
            ])
            labels = list(pair)
            rng.shuffle(labels)
            for label, variant in zip(["A", "B"], labels):
                wave = pair[variant]
                gain = min(1, target / max(levels[variant], 1e-12),
                           .9 / max(float(np.max(np.abs(wave))), 1e-12))
                path = args.output / "listening" / f"{case['id']}_{seed}_{label}.wav"
                write_wav(path, wave * gain, sr)
                blind_key.append({"file": path.name, "variant": variant, "gain": gain})
        for variant, manifest in manifests.items():
            folder = args.output / f"{variant}_{seed}"
            write_json(folder / "manifest.json", manifest)
            assemble_full_wav(manifest, folder / "segments", sample_rate=sr,
                              output_path=folder / "full.wav")
    write_json(args.output / "blind-key.json", blind_key)


if __name__ == "__main__":
    main()
