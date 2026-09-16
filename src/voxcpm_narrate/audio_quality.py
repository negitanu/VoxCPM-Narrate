"""Non-destructive narration finishing and measured loudness reports."""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

from voxcpm_narrate.artifacts import file_hash, write_json, write_wav

PROCESSING_VERSION = "narration-finish-v2"
TARGET_LUFS = -16.0
TRUE_PEAK_DBTP = -1.0


def _ffmpeg(source: Path, audio_filter: str, destination: Path | None = None,
            sample_rate: int | None = None) -> dict:
    executable = shutil.which("ffmpeg")
    if not executable:
        raise ValueError("音量調整には ffmpeg が必要です。原音 WAV はそのまま利用できます。")
    command = [executable, "-hide_banner", "-nostdin", "-y", "-i", str(source),
               "-af", audio_filter]
    if destination is None:
        command += ["-f", "null", "-"]
    else:
        command += ["-ar", str(sample_rate), "-ac", "1", "-c:a", "pcm_s24le",
                    str(destination)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired as exc:
        raise ValueError("音量測定・調整が制限時間を超えました。原音は保持されています。") from exc
    if result.returncode:
        raise ValueError("ffmpeg の音量処理に失敗しました: " + result.stderr[-800:])
    reports = re.findall(r'\{\s*"input_i"[\s\S]*?\}', result.stderr)
    if not reports:
        raise ValueError("ffmpeg から音量測定結果を取得できませんでした")
    raw = json.loads(reports[-1])
    parsed = {}
    for key, value in raw.items():
        try:
            number = float(value)
            parsed[key] = number if math.isfinite(number) else None
        except (ValueError, TypeError):
            parsed[key] = value
    return parsed


def measure(path: Path, *, target_lufs: float = TARGET_LUFS) -> dict:
    """Integrated loudness is null for insufficient/gated audio, never zero."""
    info = sf.info(str(path))
    result = _ffmpeg(path, f"loudnorm=I={target_lufs}:TP={TRUE_PEAK_DBTP}:LRA=11:print_format=json")
    return {
        "integrated_lufs": result["input_i"],
        "true_peak_dbtp": result["input_tp"],
        "loudness_range_lu": result["input_lra"],
        "duration_sec": info.duration,
        "sample_rate": info.samplerate,
        "channels": info.channels,
        "measurement_valid": result["input_i"] is not None and info.duration >= 3,
        "mono_playback": "native-mono (dual_mono=false)",
        "ffmpeg": result,
    }


def _speech_bounds(wav: np.ndarray, sr: int) -> tuple[int, int, float]:
    """Conservative 10 ms energy bounds, retaining breathing/consonant margins later."""
    frame = max(1, round(sr * .01))
    padded = np.pad(wav, (0, (-len(wav)) % frame))
    rms = np.sqrt(np.mean(padded.reshape(-1, frame).astype(np.float64) ** 2, axis=1))
    threshold = max(1e-5, float(np.max(rms)) * .01)
    active = np.flatnonzero(rms > threshold)
    if not len(active):
        raise ValueError("無音のセグメントがあります。再生成してから仕上げてください。")
    level = float(np.median(rms[active]))
    if level < 1e-4:
        raise ValueError("音量が極端に小さいセグメントがあります。過剰増幅を防ぐため再生成してください。")
    return int(active[0] * frame), min(len(wav), int((active[-1] + 1) * frame)), level


def prepare_segments(manifest: list[dict], directory: Path, destination: Path) -> dict:
    """Level phrases gently and account for retained silence in requested gaps."""
    clips, levels = [], []
    sample_rate = None
    for job in manifest:
        source = directory / f"{job['id']}.wav"
        wav, sr = sf.read(source, dtype="float32")
        if wav.ndim != 1 or not len(wav) or not np.isfinite(wav).all():
            raise ValueError("仕上げ対象は有限値を持つ空でないモノラル音声にしてください")
        if sample_rate is not None and sr != sample_rate:
            raise ValueError("セグメントのサンプルレートが一致しません")
        sample_rate = sr
        start, end, level = _speech_bounds(wav, sr)
        margin = round(.04 * sr)
        left, right = max(0, start - margin), min(len(wav), end + margin)
        clips.append((job, source, wav[left:right], start - left, right - end))
        levels.append(level)
    if not clips:
        raise ValueError("仕上げるセグメントがありません")
    median = float(np.median(levels))
    pieces, adjustments = [], []
    preceding_tail = 0
    cursor = 0
    for (job, source, wav, lead, tail), level in zip(clips, levels):
        gain_db = float(np.clip(20 * np.log10(median / level), 0, 3))
        pause = float(job.get("pause_before_sec", .18))
        if not math.isfinite(pause) or not 0 <= pause <= 60:
            raise ValueError("文間の指定が不正です")
        gap = max(0, round(pause * sample_rate) - lead - preceding_tail)
        pieces.extend([np.zeros(gap, dtype=np.float32), wav * 10 ** (gain_db / 20)])
        adjustments.append({"id": job["id"], "source_sha256": file_hash(source),
                            "gain_db": gain_db, "inserted_silence_sec": gap / sample_rate,
                            "start_sample": cursor, "end_sample": cursor + gap + len(wav),
                            "actual_gap_sec": (gap + lead + preceding_tail) / sample_rate})
        cursor += gap + len(wav)
        preceding_tail = tail
    pieces.append(np.zeros(max(0, round(.6 * sample_rate) - preceding_tail), dtype=np.float32))
    adjustments[-1]["end_sample"] += len(pieces[-1])
    write_wav(destination, np.concatenate(pieces), sample_rate)
    return {"segments": adjustments, "sample_rate": sample_rate}


def finish(source: Path, destination: Path, *, manifest: list[dict] | None = None,
           segments_dir: Path | None = None) -> dict:
    """Two-pass mono mastering, published only after output verification.

    Original waveforms stay untouched. The JSON sidecar is the completion marker.
    """
    if source.resolve() == destination.resolve():
        raise ValueError("仕上げ音声は原音と別のパスへ保存してください")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".finishing-", dir=destination.parent) as tmp:
        working = source
        preparation = None
        if manifest is not None:
            if segments_dir is None:
                raise ValueError("セグメント音声の保存先が必要です")
            working = Path(tmp) / "prepared.wav"
            preparation = prepare_segments(manifest, segments_dir, working)
        info = sf.info(str(working))
        if info.channels != 1:
            raise ValueError("仕上げ処理はモノラル音声に対応しています")
        data, _ = sf.read(working, dtype="float32")
        if not data.size or not np.isfinite(data).all() or np.max(np.abs(data)) < 1e-4:
            raise ValueError("無音または不正な音声は音量調整できません")
        before = measure(working)
        if not before["measurement_valid"]:
            raise ValueError("音量を安定して測定できません。3秒以上の音声で仕上げてください。")
        if TARGET_LUFS - before["integrated_lufs"] > 18:
            raise ValueError("音量が小さすぎます。過剰増幅を防ぐため再生成してください。")
        # Do not turn already-audible narration down just to meet a fixed target.
        target = min(-5.0, max(TARGET_LUFS, before["integrated_lufs"]))
        if target != TARGET_LUFS:
            before = measure(working, target_lufs=target)
        values = before["ffmpeg"]
        keys = {"measured_I": "input_i", "measured_TP": "input_tp",
                "measured_LRA": "input_lra", "measured_thresh": "input_thresh",
                "offset": "target_offset"}
        if any(values.get(key) is None for key in keys.values()):
            raise ValueError("音量測定値が不足しているため仕上げを中止しました")
        filters = f"loudnorm=I={target}:TP={TRUE_PEAK_DBTP}:LRA=11:linear=true"
        filters += "".join(f":{key}={values[value]}" for key, value in keys.items())
        filters += ":print_format=json"
        pending = Path(tmp) / "finished.wav"
        processing = _ffmpeg(working, filters, pending, info.samplerate)
        after = measure(pending)
        if (not after["measurement_valid"] or after["true_peak_dbtp"] is None
                or abs(after["integrated_lufs"] - target) > 1
                # Decoder/measurement rounding can report a tenth of a dB above the
                # requested ceiling; keep a small verification tolerance.
                or after["true_peak_dbtp"] > TRUE_PEAK_DBTP + 0.1):
            raise ValueError("仕上げ音声が音量・ピークの目標を満たしませんでした。原音は保持されています。")
        report = {"processing_version": PROCESSING_VERSION, "source_sha256": file_hash(source),
                  "output_sha256": file_hash(pending), "target_lufs": target,
                  "true_peak_limit_dbtp": TRUE_PEAK_DBTP, "before": before, "after": after,
                  "preparation": preparation, "processing": processing}
        # Both writes are atomic; a missing sidecar causes a retry, never a cache hit.
        from voxcpm_narrate.artifacts import atomic_bytes

        atomic_bytes(destination, pending.read_bytes())
        write_json(destination.with_suffix(".quality.json"), report)
        return report


def main():
    import argparse

    parser = argparse.ArgumentParser(description="原音を保持して音量を測定・調整します")
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, help="指定時は仕上げ WAV と測定 JSON を作成")
    parser.add_argument("--manifest", type=Path, help="文間・音量を調整する manifest.json")
    parser.add_argument("--segments-dir", type=Path)
    args = parser.parse_args()
    try:
        result = (finish(args.source, args.output,
                         manifest=json.loads(args.manifest.read_text()) if args.manifest else None,
                         segments_dir=args.segments_dir)
                  if args.output else measure(args.source))
    except ValueError as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
