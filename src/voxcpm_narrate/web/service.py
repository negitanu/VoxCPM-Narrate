"""Production operations. SQLite points only to complete immutable audio artifacts."""

from __future__ import annotations

import copy
import hashlib
import json
import secrets
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path
from contextlib import nullcontext

from voxcpm_narrate.artifacts import file_hash, write_json, write_wav
from voxcpm_narrate.extract import load_jobs
from voxcpm_narrate.harness.audio_judge import AUDIO_JUDGE_SYSTEM, judge_audio_with_openrouter
from voxcpm_narrate.harness.asr import AsrTranscriber
from voxcpm_narrate.harness.content_gate import GATE_VERSION, inspect_audio
from voxcpm_narrate.harness.judge import LlmJudge, default_llm_api_key
from voxcpm_narrate.harness.loop import evaluate_segment
from voxcpm_narrate.harness.strategies import GenParams, build_strategies
from voxcpm_narrate.pronunciation import (
    apply_dictionary, dictionary_pattern, find_unknown_words, validate_reading,
)
from voxcpm_narrate.synthesize import assemble_full_wav, generate_wav, load_model
from voxcpm_narrate.web.jobs import (
    ACTIVE,
    Cancelled,
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
        self._content_asr = AsrTranscriber(device="cpu")
        self._content_lock = threading.Lock()

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
        learned = self.manager.lexicon()
        job["dictionary"] = learned["entries"]
        job["dictionary_origin_revision"] = learned["revision"]
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

    def pronunciation_candidates(self, jid: str, script: str | None = None,
                                 include_registered: bool = False) -> list[dict]:
        job = self.manager.get(jid)
        if script is None:
            sources = [(s["id"], s["revision"], s["draft"]["reading"] or s["draft"]["text"])
                       for s in job["segments"]]
        else:
            sources = [(None, None, item["text"]) for item in parse_script(
                script, job["mode"], job["config"]["max_chars"], job["config"]["control"])]
        candidates = {}
        learned = self.manager.lexicon()["entries"]
        for sid, revision, text in sources:
            dictionary = job.get("dictionary", {})
            items = find_unknown_words(text, dictionary)
            pattern = dictionary_pattern(dictionary)
            if include_registered and pattern:
                for match in pattern.finditer(text):
                    items.append({"term": match[0], "reading": dictionary[match[0]],
                                  "script": "登録済み", "status": "known", "occurrences": 1,
                                  "context": text[max(0, match.start() - 30):match.end() + 30]})
            for item in items:
                term = item["term"]
                if term in candidates:
                    candidates[term]["occurrences"] += item["occurrences"]
                    continue
                candidates[term] = {**item, "segment_id": sid, "segment_revision": revision,
                                    "reading": item["reading"] or learned.get(term, ""),
                                    "reading_source": "この制作" if term in dictionary else
                                        "共通辞書" if term in learned else None}
        return list(candidates.values())

    def import_lexicon(self, jid):
        def change(job):
            check_idle(job)
            combined = {**self.manager.lexicon()["entries"], **job["dictionary"]}
            if len(combined) > 200:
                raise ValueError("辞書を取り込むと200件を超えます。不要な用語を整理してください")
            job["dictionary"] = combined
        return self.manager.mutate(jid, change)

    def validate_pronunciation_preview(self, job, sid, revision, term, reading):
        validate_reading(term, reading)
        seg = segment(job, sid)
        check_revision(seg, revision)
        source = seg["draft"]["reading"] or seg["draft"]["text"]
        if not dictionary_pattern({term: reading}).search(source):
            raise Conflict("対象の語が本文から変更されています。候補を再取得してください")

    def preview_pronunciation(self, jid, sid, revision, term, reading):
        job = self.manager.get(jid)
        self.validate_pronunciation_preview(job, sid, revision, term, reading)
        seg = segment(job, sid)
        cfg = job["config"]
        spoken = self._spoken(seg["draft"], {**job["dictionary"], term: reading})
        self.manager.update(jid, message=f"「{term}」の読みを文中で試聴生成しています")
        model = self.model(cfg)
        self.manager.checkpoint(jid)
        metadata = {}
        wav = generate_wav(model, text=spoken, control=seg["draft"]["control"],
                           reference_audio=self._reference_audio(jid, cfg),
                           reference_transcript=cfg.get("reference_transcript"),
                           number_reading_style=seg['draft'].get('number_reading_style') or cfg.get('number_reading_style', 'hiragana'),
                           cfg_value=cfg["cfg_value"], inference_timesteps=cfg["timesteps"],
                           normalize=self._convert_numbers(seg['draft'], cfg), seed=cfg["seed"], input_metadata=metadata)
        preview_id = uuid.uuid4().hex
        path = self.manager.job_dir(jid) / "pronunciation" / f"{preview_id}.wav"
        write_wav(path, wav, int(model.tts_model.sample_rate))
        preview = {"id": preview_id, "term": term, "reading": reading, "segment_id": sid,
                   "segment_revision": revision, "text": spoken, **metadata}
        write_json(path.with_suffix(".json"), preview)
        self.manager.update(jid, pronunciation_preview=preview)

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
        return apply_dictionary(text, dictionary)

    @staticmethod
    def _convert_numbers(draft, config):
        override = draft.get('convert_numbers')
        return config.get('convert_numbers', True) if override is None else override

    def _generate(self, jid, sid, *, seed=None, overrides=None):
        job = self.manager.get(jid)
        seg = segment(job, sid)
        cfg = job["config"]
        draft = copy.deepcopy(seg["draft"])
        params = dict(
            control=draft["control"],
            cfg_value=cfg["cfg_value"],
            inference_timesteps=cfg["timesteps"],
            normalize=self._convert_numbers(draft, cfg),
            number_reading_style=draft.get('number_reading_style') or cfg.get('number_reading_style', 'hiragana'),
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
                           reference_transcript=cfg.get("reference_transcript"),
                           input_metadata=input_metadata, retry_badcase=False, **params)
        sr = int(model.tts_model.sample_rate)
        vid = uuid.uuid4().hex[:16]
        path = self.manager.job_dir(jid) / "versions" / sid / f"{vid}.wav"
        pace = None
        if cfg.get("pace_mode", "off") != "off":
            from voxcpm_narrate.speech_rate import adjust_rate
            write_wav(path.with_suffix(".raw.wav"), wav, sr)
            wav, pace = adjust_rate(wav, sr, input_metadata.get("prepared_reading", spoken),
                                    cfg["target_mora_rate"])
            pace["raw_sha256"] = file_hash(path.with_suffix(".raw.wav"))
        write_wav(path, wav, sr)
        v = dict(
            speech_rate=pace,
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

    def verify_content(self, jid, sid, vid):
        v = version(segment(self.manager.get(jid), sid), vid)
        path = self.audio_path(jid, sid, vid)
        digest = file_hash(path)
        cached = v.get("content_check")
        if (cached and cached.get("version") == GATE_VERSION
                and cached.get("sha256") == digest and not cached.get("unavailable")):
            return cached
        self.manager.update(jid, message=f"{sid} の原稿一致・日本語を検査しています")
        try:
            with self._content_lock:
                report = inspect_audio(path, [v["spoken_text"], v.get("prepared_reading", "")],
                                       self._content_asr,
                                       lambda: self.manager.checkpoint(jid))
        except Cancelled:
            raise
        except Exception as exc:
            # Infrastructure failures must neither pass nor trigger costly TTS retries.
            from voxcpm_narrate.console import log_error
            log_error(f"Content verification failed: {exc}")
            report = dict(version=GATE_VERSION, passed=False, unavailable=True,
                          reasons=["音声検査を実行できませんでした。ASRの設定を確認してください"],
                          windows=[])
        report = {**report, "sha256": digest}
        pace = v.get("speech_rate")
        if pace and (not pace["passed"] or pace.get("warning")):
            report["warnings"] = [*report.get("warnings", []), "話速（参考）: " + pace["reason"]]
        self.manager.mutate(jid, lambda j: version(segment(j, sid), vid).update(content_check=report))
        return report

    def _checked_generate(self, jid, sid, **kwargs):
        for attempt in range(3):  # Initial generation plus at most two content retries.
            self.manager.checkpoint(jid)
            v = self._generate(jid, sid, **kwargs)
            report = self.verify_content(jid, sid, v["id"])
            if report["passed"]:
                return v
            if report.get("unavailable"):
                break
            kwargs["seed"] = secrets.randbelow(2**31)
            if attempt < 2:
                self.manager.update(jid, message=f"{sid}: 原稿との不一致により再生成 ({attempt + 1}/2)")
        message = "音声検査に合格しませんでした。候補の検査結果を確認してください"
        self.manager.mutate(jid, lambda j: segment(j, sid).update(
            status="ready" if segment(j, sid)["accepted"] else "pending"))
        self.manager.update(jid, message=message)
        raise ValueError(message)

    def _require_content(self, jid, sid, vid):
        report = self.verify_content(jid, sid, vid)
        if not report["passed"]:
            raise ValueError("音声検査が未合格のため採用・書き出しできません: "
                             + " / ".join(report["reasons"]))
        return report

    def _build_export(self, job):
        if not all(s["accepted"] for s in job["segments"]):
            return None
        for seg in job["segments"]:
            version(seg)["content_check"] = self._require_content(
                job["id"], seg["id"], seg["accepted"])
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
        self._require_content(jid, sid, vid)
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
            draft={key: v.get(key) for key in ("text", "reading", "control", "pause_before_sec", "convert_numbers", "number_reading_style")},
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
            v = self._checked_generate(jid, sid)
            # Existing audio is never replaced just by pressing Generate.
            if not seg["accepted"]:
                self.adopt(jid, sid, v["id"])
                newly_generated.append(sid)
            self.manager.checkpoint(jid)
        if job["config"].get("improve"):
            self.improve(jid, newly_generated, api_key)
        current = self.manager.get(jid)
        self.manager.update(jid, export=self._build_export(current))

    def regenerate_all(self, jid):
        ids = [s["id"] for s in self.manager.get(jid)["segments"]]
        candidates = {}
        for index, sid in enumerate(ids, 1):
            self.manager.checkpoint(jid)
            self.manager.update(jid, regeneration_progress={"current": index - 1, "total": len(ids)})
            candidates[sid] = self._checked_generate(jid, sid, seed=secrets.randbelow(2**31))
            self.manager.update(jid, regeneration_progress={"current": index, "total": len(ids)})
        self.manager.checkpoint(jid)
        job = self.manager.get(jid)
        for seg in job["segments"]:
            v = candidates[seg["id"]]
            if seg["accepted"]:
                seg["history"].append(seg["accepted"])
            seg.update(accepted=v["id"], status="ready", revision=seg["revision"] + 1,
                       error=None, feedback=None,
                       draft={key: v.get(key) for key in ("text", "reading", "control", "pause_before_sec", "convert_numbers", "number_reading_style")})
        export = self._build_export(job)
        # Publish all accepted versions together; never leave a partially replaced production.
        with self.manager._lock:
            self.manager.checkpoint(jid)
            self.manager.update(jid, segments=job["segments"], export=export)

    def regenerate(self, jid, sid):
        self.manager.mutate(
            jid, lambda job: segment(job, sid).update(status="regenerating", error=None)
        )
        self._checked_generate(jid, sid, seed=secrets.randbelow(2**31))
        self.manager.update(jid, message="候補を保存しました。試聴して採用してください")

    def evaluate(self, jid, sid, vid, asr=None, judge=None):
        import soundfile as sf

        seg = segment(self.manager.get(jid), sid)
        v = version(seg, vid)
        path = self.audio_path(jid, sid, vid)
        wav, sr = sf.read(path, dtype="float32")
        with self._content_lock if asr is not None else nullcontext():
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
        asr = self._content_asr if cfg.get("improve_asr") else None
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
                if not self.verify_content(jid, sid, v["id"])["passed"]:
                    continue
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
        for seg in job["segments"]:
            self._require_content(jid, seg["id"], seg["accepted"])
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
