"""Production operations. SQLite points only to complete immutable audio artifacts."""

from __future__ import annotations

import copy
import hashlib
import json
import secrets
import shutil
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

from voxcpm_narrate.artifacts import file_hash, write_json, write_wav
from voxcpm_narrate.extract import load_jobs
from voxcpm_narrate.harness.audio_judge import AUDIO_JUDGE_SYSTEM, judge_audio_with_openrouter
from voxcpm_narrate.harness.asr import AsrTranscriber
from voxcpm_narrate.harness.judge import LlmJudge, default_llm_api_key
from voxcpm_narrate.harness.loop import evaluate_segment
from voxcpm_narrate.harness.strategies import GenParams, build_strategies
from voxcpm_narrate.pronunciation import find_unknown_words
from voxcpm_narrate.synthesize import assemble_full_wav, generate_wav, load_model
from voxcpm_narrate.web.jobs import (
    ACTIVE,
    Conflict,
    JobManager,
    new_job,
    now,
    safe_job_snapshot,
)


def parse_script(script: str, mode: str, max_chars: int, control: str) -> list[dict]:
    if not script.strip():
        raise ValueError("台本を入力してください")
    suffix = {"ssml": ".ssml", "markdown": ".md", "plain": ".txt", "lines": ".txt"}[mode]
    with tempfile.TemporaryDirectory(prefix="narration-parse-") as tmp:
        path = Path(tmp) / ("script" + suffix)
        path.write_text(script, encoding="utf-8")
        jobs = load_jobs(
            path,
            mode=mode,
            narration_heading="読み上げ本文",
            max_chars=max_chars,
            base_control=control,
        )
    if len(jobs) > 2000:
        raise ValueError("1制作は2000セグメントまでです。台本を分けてください")
    return jobs


def segment(job: dict, segment_id: str) -> dict:
    found = next((s for s in job["segments"] if s["id"] == segment_id), None)
    if found is None:
        raise KeyError("セグメントが見つかりません")
    return found


def version(seg: dict, version_id: str | None = None) -> dict:
    vid = version_id or seg.get("accepted")
    found = next((v for v in seg["versions"] if v["id"] == vid), None)
    if found is None:
        raise KeyError("音声の版が見つかりません")
    return found


def check_idle(job: dict) -> None:
    if job["status"] in ACTIVE:
        raise Conflict("処理中は編集を保存できません。中断するか、完了を待ってください")


def check_revision(seg: dict, expected: int) -> None:
    if seg["revision"] != expected:
        raise Conflict("別の操作で内容が変更されました。最新状態を読み込んでください")


class ProductionService:
    def __init__(self, manager: JobManager):
        self.manager = manager
        self._model = None
        self._model_key = None

    def create(self, title, script, mode, config, parsed=None) -> dict:
        jobs = parsed or parse_script(script, mode, config["max_chars"], config["control"])
        segments = []
        for item in jobs:
            draft = dict(
                text=item["text"],
                reading="",
                control=item.get("control", config["control"]),
                pause_before_sec=item.get("pause_before_sec", 0.18),
            )
            segments.append(
                dict(
                    id=item["id"],
                    section=item["section"],
                    original_text=item["text"],
                    draft=draft,
                    revision=0,
                    status="pending",
                    accepted=None,
                    versions=[],
                    history=[],
                    error=None,
                    feedback=None,
                )
            )
        job = new_job(title, script, mode, config, segments)
        self.manager.job_dir(job["id"])
        return self.manager.save(job)

    def _reference_audio(self, jid: str, config: dict) -> str | None:
        reference = config.get("reference_audio")
        if not reference:
            return None
        root = self.manager.job_dir(jid).resolve()
        path = (root / reference).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return None
        return str(path)

    def edit(self, jid: str, sid: str, expected: int, draft: dict):
        def change(job):
            check_idle(job)
            seg = segment(job, sid)
            check_revision(seg, expected)
            seg.update(draft=draft, revision=seg["revision"] + 1, error=None)

        return self.manager.mutate(jid, change)

    def pronunciation_candidates(self, jid: str, script: str | None = None) -> list[dict]:
        job = self.manager.get(jid)
        source = script if script is not None else job["script"]
        return find_unknown_words(source, job.get("dictionary", {}))

    def model(self, config):
        key = (config["model_id"], config["device"])
        if key != self._model_key:
            self._model = None
            self._model_key = None
            import gc

            gc.collect()
            self._model = load_model(key[0], device=key[1], optimize=False)
            self._model_key = key
        return self._model

    def audio_path(self, jid, sid, vid=None):
        seg = segment(self.manager.get(jid), sid)
        v = version(seg, vid)
        return self.manager.job_dir(jid) / "versions" / sid / (v["id"] + ".wav")

    def _spoken(self, draft, dictionary):
        text = draft["reading"] or draft["text"]
        # One pass: replacements cannot recursively expand other dictionary entries.
        if dictionary:
            import re

            pattern = "|".join(
                re.escape(term) for term in sorted(dictionary, key=len, reverse=True)
            )
            text = re.sub(pattern, lambda match: dictionary[match.group()], text)
        return text

    def _generate(self, jid, sid, *, seed=None, overrides=None):
        job = self.manager.get(jid)
        seg = segment(job, sid)
        cfg = job["config"]
        draft = copy.deepcopy(seg["draft"])
        params = dict(
            control=draft["control"],
            cfg_value=cfg["cfg_value"],
            inference_timesteps=cfg["timesteps"],
            normalize=True,
            seed=cfg["seed"] if seed is None else seed,
        )
        params.update(overrides or {})
        draft["control"] = params["control"] or ""
        self.manager.update(jid, phase="loading_model", message="モデルを準備しています")
        started = time.monotonic()
        model = self.model(cfg)
        load_seconds = time.monotonic() - started
        self.manager.checkpoint(jid)
        self.manager.update(jid, phase="synthesize", message=f"{sid} を生成しています")
        spoken = self._spoken(draft, job["dictionary"])
        started = time.monotonic()
        input_metadata = {}
        wav = generate_wav(model, text=spoken, reference_audio=self._reference_audio(jid, cfg),
                           input_metadata=input_metadata, **params)
        sr = int(model.tts_model.sample_rate)
        vid = uuid.uuid4().hex[:16]
        path = self.manager.job_dir(jid) / "versions" / sid / f"{vid}.wav"
        write_wav(path, wav, sr)
        v = dict(
            id=vid,
            created_at=now(),
            **draft,
            spoken_text=spoken,
            **input_metadata,
            params=params,
            sample_rate=sr,
            duration_sec=len(wav) / sr,
            sha256=file_hash(path),
            model_id=cfg["model_id"],
            model_revision=getattr(model, "revision", None),
            reference_sha256=cfg.get("reference_sha256"),
            generation_sec=round(time.monotonic() - started, 3),
            load_sec=round(load_seconds, 3),
            evaluation=None,
        )

        def record(current):
            s = segment(current, sid)
            s["versions"].append(v)
            s["status"] = "ready" if s["accepted"] else "pending"
            current["timings"].append(
                dict(
                    segment_id=sid,
                    version_id=vid,
                    generation_sec=v["generation_sec"],
                    load_sec=v["load_sec"],
                )
            )

        self.manager.mutate(jid, record)
        return v

    def _build_export(self, job):
        if not all(s["accepted"] for s in job["segments"]):
            return None
        signature = [(s["id"], s["accepted"]) for s in job["segments"]]
        digest = hashlib.sha256(json.dumps(signature).encode()).hexdigest()[:20]
        dest = self.manager.job_dir(job["id"]) / "exports" / digest
        marker = dest / "complete.json"
        if marker.is_file():
            return json.loads(marker.read_text())
        # The directory is unpublished until both files and marker are complete.
        dest.mkdir(parents=True, exist_ok=True)
        segments_dir = dest / "segments"
        segments_dir.mkdir(exist_ok=True)
        manifest = []
        sr = None
        for seg in job["segments"]:
            v = version(seg)
            if sr is not None and sr != v["sample_rate"]:
                raise ValueError("音声のサンプルレートが一致しません")
            sr = v["sample_rate"]
            src = self.manager.job_dir(job["id"]) / "versions" / seg["id"] / (v["id"] + ".wav")
            shutil.copy2(src, segments_dir / (seg["id"] + ".wav"))
            manifest.append(
                dict(
                    id=seg["id"],
                    section=seg["section"],
                    text=v["text"],
                    control=v["control"],
                    pause_before_sec=v["pause_before_sec"],
                    version_id=v["id"],
                    spoken_text=v["spoken_text"],
                )
            )
        write_json(dest / "manifest.json", manifest)
        assemble_full_wav(manifest, segments_dir, sample_rate=sr, output_path=dest / "full.wav")
        import soundfile as sf

        result = dict(
            id=digest,
            duration_sec=sf.info(str(dest / "full.wav")).duration,
            versions=signature,
            created_at=now(),
        )
        write_json(marker, result)
        return result

    def adopt(self, jid, sid, vid):
        job = self.manager.get(jid)
        seg = segment(job, sid)
        v = version(seg, vid)
        if seg["accepted"] == vid:
            return
        if seg["accepted"]:
            seg["history"].append(seg["accepted"])
        seg.update(
            accepted=vid,
            status="ready",
            revision=seg["revision"] + 1,
            error=None,
            draft={key: v[key] for key in ("text", "reading", "control", "pause_before_sec")},
            feedback=None,
        )
        # Build first; a crash leaves the previous accepted snapshot untouched.
        export = self._build_export(job)
        self.manager.update(jid, segments=job["segments"], export=export)

    def generate(self, jid, selected=None, api_key=""):
        job = self.manager.get(jid)
        ids = (
            selected
            if selected is not None
            else [s["id"] for s in job["segments"] if not s["accepted"]]
        )
        newly_generated = []
        for sid in ids:
            self.manager.checkpoint(jid)
            seg = segment(self.manager.get(jid), sid)
            self.manager.mutate(jid, lambda j: segment(j, sid).update(status="running", error=None))
            v = self._generate(jid, sid)
            # Existing audio is never replaced just by pressing Generate.
            if not seg["accepted"]:
                self.adopt(jid, sid, v["id"])
                newly_generated.append(sid)
            self.manager.checkpoint(jid)
        if job["config"].get("improve"):
            self.improve(jid, newly_generated, api_key)
        current = self.manager.get(jid)
        self.manager.update(jid, export=self._build_export(current))

    def regenerate(self, jid, sid):
        self.manager.mutate(
            jid, lambda job: segment(job, sid).update(status="regenerating", error=None)
        )
        self._generate(jid, sid, seed=secrets.randbelow(2**31))
        self.manager.update(jid, message="候補を保存しました。試聴して採用してください")

    def evaluate(self, jid, sid, vid, asr=None, judge=None):
        import soundfile as sf

        seg = segment(self.manager.get(jid), sid)
        v = version(seg, vid)
        path = self.audio_path(jid, sid, vid)
        wav, sr = sf.read(path, dtype="float32")
        result = evaluate_segment(
            segment_id=sid,
            text=v["spoken_text"],
            wav=wav,
            sample_rate=sr,
            wav_path=path,
            asr=asr,
            judge=judge,
            awkward_threshold=0.62,
        )
        self.manager.mutate(
            jid, lambda job: version(segment(job, sid), vid).update(evaluation=result.to_dict())
        )
        return result

    def improve(self, jid, ids, api_key=""):
        job = self.manager.get(jid)
        cfg = job["config"]
        asr = AsrTranscriber(device="cpu") if cfg.get("improve_asr") else None
        judge = (
            LlmJudge(model=cfg["llm_model"], api_key=api_key or default_llm_api_key())
            if cfg.get("improve_llm")
            else None
        )
        for sid in ids:
            self.manager.checkpoint(jid)
            seg = segment(self.manager.get(jid), sid)
            if not seg["accepted"]:
                continue
            self.manager.update(jid, phase="improve", message=f"{sid} を評価しています")
            baseline = self.evaluate(jid, sid, seg["accepted"], asr, judge)
            if not baseline.awkward:
                continue
            base = GenParams(
                seg["draft"]["control"], cfg["cfg_value"], cfg["timesteps"], True, cfg["seed"]
            )
            for _, params in build_strategies(base, max_rounds=cfg["improve_rounds"]):
                self.manager.checkpoint(jid)
                v = self._generate(jid, sid, overrides=params.to_dict())
                score = self.evaluate(jid, sid, v["id"], asr, judge)
                comparable = judge is None or (
                    score.llm_score is not None and baseline.llm_score is not None
                )
                if comparable and not score.awkward and score.overall > baseline.overall + 0.01:
                    self.adopt(jid, sid, v["id"])
                    break

    def audio_judge(self, jid, sid, model, api_key):
        job = self.manager.get(jid)
        seg = segment(job, sid)
        v = version(seg)
        key = hashlib.sha256(
            (v["sha256"] + v["text"] + model + AUDIO_JUDGE_SYSTEM).encode()
        ).hexdigest()
        cache = self.manager.job_dir(jid) / "evaluations" / (key + ".json")
        if cache.is_file():
            result = json.loads(cache.read_text())
        else:
            if job["evaluation_count"] >= 50:
                raise ValueError("この制作の音声評価上限（50回）に達しました")
            self.manager.update(
                jid,
                evaluation_count=job["evaluation_count"] + 1,
                phase="audio_judge",
                message="音声の改善提案を取得しています",
            )
            result = judge_audio_with_openrouter(
                audio_path=self.audio_path(jid, sid),
                original_text=v["text"],
                model=model,
                api_key=api_key or default_llm_api_key(),
            ).to_dict()
            if not result.get("parse_fallback"):
                write_json(cache, result)
        result["version_id"] = v["id"]
        self.manager.mutate(jid, lambda j: segment(j, sid).update(feedback=result))

    def export_audio(self, jid, normalize=False):
        job = self.manager.get(jid)
        if not job["export"]:
            raise ValueError("全セグメントを生成・採用してから書き出してください")
        folder = self.manager.job_dir(jid) / "exports" / job["export"]["id"]
        source = folder / "full.wav"
        if not normalize:
            return source
        from voxcpm_narrate.audio_quality import PROCESSING_VERSION, finish

        result = folder / f"full_{PROCESSING_VERSION}.wav"
        marker = result.with_suffix(".quality.json")
        if result.is_file() and marker.is_file():
            report = json.loads(marker.read_text())
            if (report.get("source_sha256") == file_hash(source)
                    and report.get("output_sha256") == file_hash(result)):
                return result
        finish(source, result, manifest=json.loads((folder / "manifest.json").read_text()),
               segments_dir=folder / "segments")
        return result

    def export_zip(self, jid, normalize=False):
        job = self.manager.get(jid)
        if not job["export"]:
            raise ValueError("全セグメントを生成・採用してから書き出してください")
        folder = self.manager.job_dir(jid) / "exports" / job["export"]["id"]
        source = self.export_audio(jid, normalize=normalize)
        quality = None
        if normalize:
            import soundfile as sf

            quality = json.loads(source.with_suffix(".quality.json").read_text())
            finished, sample_rate = sf.read(source, dtype="float32")
        # A unique ZIP avoids races between simultaneous downloads.
        path = folder / (uuid.uuid4().hex + ".zip")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(source, "full.wav")
            if quality:
                archive.write(source.with_suffix(".quality.json"), "quality.json")
                archive.write(folder / "full.wav", "originals/full.wav")
            archive.write(folder / "manifest.json", "manifest.json")
            archive.writestr("script.txt", job["script"])
            archive.writestr(
                "production.json",
                json.dumps(safe_job_snapshot(job), ensure_ascii=False, indent=2),
            )
            reference = self._reference_audio(jid, job["config"])
            if reference:
                archive.write(reference, "reference.wav")
            chapters = {}
            for item in json.loads((folder / "manifest.json").read_text()):
                chapters.setdefault(item["section"], []).append(item)
            for index, (title, items) in enumerate(chapters.items(), 1):
                chapter = folder / ("finished_chapters" if normalize else "chapters") / f"{index:02d}.wav"
                if quality:
                    import numpy as np

                    bounds = {a["id"]: a for a in quality["preparation"]["segments"]}
                    parts = [finished[bounds[item["id"]]["start_sample"]:
                                      bounds[item["id"]]["end_sample"]] for item in items]
                    write_wav(chapter, np.concatenate(parts), sample_rate, subtype="PCM_24")
                elif not chapter.is_file():
                    assemble_full_wav(items, folder / "segments",
                                      sample_rate=version(job["segments"][0])["sample_rate"],
                                      output_path=chapter)
                archive.write(chapter, f"chapters/{index:02d}.wav")
            archive.writestr("chapters/index.json", json.dumps(list(chapters), ensure_ascii=False))
            for wav in sorted((folder / "segments").glob("*.wav")):
                archive.write(wav, ("originals/segments/" if normalize else "segments/") + wav.name)
        return path
