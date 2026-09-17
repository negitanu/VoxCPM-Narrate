"""Local narration studio API."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask

from voxcpm_narrate import __version__
from voxcpm_narrate.artifacts import file_hash
from voxcpm_narrate.harness.judge import (
    OPENROUTER_MODELS,
    default_audio_judge_model,
    default_llm_api_key,
    default_llm_model,
    fetch_openrouter_models,
)
from voxcpm_narrate.synthesize import prepare_reference_wav
from voxcpm_narrate.web.jobs import ACTIVE, Conflict, JobManager, safe_job_snapshot
from voxcpm_narrate.web.service import (
    ProductionService,
    check_idle,
    check_revision,
    parse_script,
    segment,
    version,
)

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_OUT = Path(os.environ.get("VOXCPM_WEB_OUT", "output/voxcpm2/web_jobs")).resolve()
manager = JobManager(DEFAULT_OUT)
service = ProductionService(manager)
app = FastAPI(title="VoxCPM Narrate", version=__version__)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class Validated(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Config(Validated):
    number_reading_style: Literal['hiragana', 'kanji'] = 'kanji'
    convert_numbers: bool = True
    device: Literal["auto", "cpu", "mps", "cuda"] = "auto"
    control: str = Field(default="日本語、明瞭な声、自然な抑揚、会話に近いテンポ", max_length=1000)
    max_chars: int = Field(default=120, ge=10, le=500)
    cfg_value: float = Field(default=2, ge=0.1, le=5)
    timesteps: int = Field(default=10, ge=1, le=100)
    seed: int = Field(default=42, ge=0, le=2**31 - 1)
    model_id: str = Field(default="openbmb/VoxCPM2", min_length=1, max_length=200)
    pace_mode: Literal["off", "reference", "fixed"] = "off"
    target_mora_rate: float = Field(default=7, ge=2, le=12)
    improve: bool = False
    improve_asr: bool = False
    improve_llm: bool = False
    improve_rounds: int = Field(default=3, ge=0, le=6)
    llm_model: str = Field(default_factory=default_llm_model, min_length=1, max_length=200)


class PreviewBody(Validated):
    script: str = Field(min_length=1, max_length=200000)
    mode: Literal["ssml", "markdown", "plain", "lines"] = "plain"
    config: Config = Field(default_factory=Config)


class Draft(Validated):
    number_reading_style: Literal['hiragana', 'kanji'] | None = None
    convert_numbers: bool | None = None
    text: str = Field(min_length=1, max_length=2000)
    reading: str = Field(default="", max_length=2000)
    control: str = Field(default="", max_length=1000)
    pause_before_sec: float = Field(default=0.18, ge=0, le=10)


class RenameBody(Validated):
    title: str = Field(min_length=1, max_length=150)
    expected_title: str = Field(max_length=150)


class EditBody(Validated):
    expected_revision: int = Field(ge=0)
    draft: Draft


class Operation(Validated):
    request_id: str = Field(default_factory=lambda: uuid.uuid4().hex, min_length=1, max_length=100)
    segment_ids: list[str] | None = None
    api_key: str = Field(default="", max_length=1000)


class SegmentOperation(Operation):
    expected_revision: int = Field(ge=0)
    version_id: str | None = None
    model: str = Field(default_factory=default_audio_judge_model, max_length=200)


class DictionaryBody(Validated):
    entries: dict[str, str]


class PronunciationBody(Validated):
    script: str | None = Field(default=None, max_length=200000)
    include_registered: bool = False


class LearnReading(Validated):
    term: str = Field(min_length=1, max_length=100)
    reading: str = Field(min_length=1, max_length=200)
    shared: bool = False
    expected_revision: int = Field(default=0, ge=0)


class ReadingPreview(Operation):
    term: str = Field(min_length=1, max_length=100)
    reading: str = Field(min_length=1, max_length=200)
    segment_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=0)


class EmptyTrashBody(Validated):
    job_ids: list[str] = Field(max_length=10000)
    confirmed: bool = False


class ImportBody(Validated):
    run: str = Field(min_length=1, max_length=400)
    config: Config = Field(default_factory=Config)


@app.exception_handler(Conflict)
async def conflict_handler(_request, exc):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(KeyError)
async def missing_handler(_request, exc):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(ValueError)
async def invalid_handler(_request, exc):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True, "service": "voxcpm-narrate-web", "schema_version": 1}


@app.get("/api/llm/settings")
def settings():
    return dict(
        default_model=default_llm_model(),
        default_audio_model=default_audio_judge_model(),
        api_key_configured=bool(default_llm_api_key()),
        suggested_models=list(OPENROUTER_MODELS),
    )


@app.get("/api/llm/models")
def models(x_api_key: str | None = Header(default=None)):
    try:
        items = fetch_openrouter_models(api_key=x_api_key or default_llm_api_key())
    except Exception as exc:
        raise HTTPException(
            502, "モデル一覧を取得できませんでした。接続とキーを確認してください"
        ) from exc
    return {"models": items}


@app.post("/api/preview")
def preview(body: PreviewBody):
    from voxcpm_narrate.japanese import japanese_numbers
    segments = parse_script(body.script, body.mode, body.config.max_chars, body.config.control)
    for item in segments:
        item['prepared_reading'] = japanese_numbers(item['text'], style=body.config.number_reading_style) if body.config.convert_numbers else item['text']
    return {"segments": segments}


@app.get("/api/jobs")
def list_jobs(deleted: bool = False):
    return {
        "jobs": [
            dict(
                id=j["id"],
                title=j["title"],
                purging=bool(j.get("purging")),
                status=j["status"],
                updated_at=j["updated_at"],
                total=len(j["segments"]),
                ready=sum(bool(s["accepted"]) for s in j["segments"]),
            )
            for j in manager.list(deleted=deleted)
        ]
    }


@app.post("/api/reference-transcription")
def transcribe_reference(reference: UploadFile = File(...)):
    suffix = Path(reference.filename or "audio.wav").suffix.lower()
    if suffix not in {".wav", ".ogg", ".mp3", ".flac", ".m4a", ".webm", ".mp4"}:
        raise ValueError("対応する音声ファイルを選択してください")
    with tempfile.TemporaryDirectory(prefix="narrate-reference-") as tmp:
        source = Path(tmp) / ("upload" + suffix)
        size = 0
        with source.open("wb") as output:
            while chunk := reference.file.read(1024 * 1024):
                size += len(chunk)
                if size > 100 * 1024 * 1024:
                    raise ValueError("参照音声は100 MBまでです")
                output.write(chunk)
        try:
            audio = prepare_reference_wav(source, Path(tmp) / "reference.wav")
            import soundfile as sf
            info = sf.info(audio)
            if not 0 < info.duration <= 120:
                raise ValueError("参照音声は120秒以内にしてください")
            import numpy as np
            wav, sr = sf.read(audio, dtype="float32")
            if not np.isfinite(wav).all():
                raise ValueError("音声に不正な値が含まれています")
            if wav.ndim > 1:
                sf.write(audio, wav.mean(axis=1), sr, subtype="FLOAT")
            with service._content_lock:
                text = service._content_asr.transcribe(audio)
        except Exception as exc:
            raise ValueError("文字起こしできませんでした。音声を確認するか手動で入力してください") from exc
    return {"text": text}


@app.post("/api/jobs")
def create_job(
    script: str = Form(..., max_length=200000),
    title: str = Form("新しいナレーション", max_length=150),
    mode: Literal["ssml", "markdown", "plain", "lines"] = Form("plain"),
    config: str = Form("{}"),
    reference: UploadFile | None = File(None),
    reference_script: str = Form("", max_length=2000),
    reference_transcript: str = Form("", max_length=2000),
    transcript_confirmed: bool = Form(False),
):
    if reference_transcript.strip() and (reference is None or not transcript_confirmed):
        raise ValueError("参照音声の文字起こしを確認してください")
    if reference_script.strip() and reference is None:
        raise ValueError("読み上げスクリプトには参照音声が必要です")
    cfg = Config.model_validate_json(config).model_dump()
    if cfg["pace_mode"] == "reference" and not reference_transcript.strip():
        raise ValueError("参照音声の話速を使うには、録音と確認済みの文字起こしが必要です")
    parsed = parse_script(script, mode, cfg["max_chars"], cfg["control"])
    job = service.create(title.strip() or "新しいナレーション", script, mode, cfg, parsed)
    if reference:
        suffix = Path(reference.filename or "source.wav").suffix.lower()
        if suffix not in {".wav", ".ogg", ".mp3", ".flac", ".m4a", ".webm", ".mp4"}:
            raise ValueError("対応形式は WAV / OGG / MP3 / FLAC / M4A / WebM / MP4 です")
        path = manager.job_dir(job["id"]) / "uploads" / ("reference" + suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        try:
            with path.open("wb") as stream:
                while chunk := reference.file.read(1024 * 1024):
                    size += len(chunk)
                    if size > 100 * 1024 * 1024:
                        raise ValueError("参照音声は100 MBまでです")
                    stream.write(chunk)
            if not size:
                raise ValueError("参照音声が空です")
            prepared = prepare_reference_wav(path, manager.job_dir(job["id"]) / "reference.wav")
            import numpy as np
            import soundfile as sf

            info = sf.info(str(prepared))
            if info.frames == 0 or info.duration > 120:
                raise ValueError("参照音声は0秒より長く、120秒以内にしてください")
            wav, sr = sf.read(prepared, dtype="float32")
            warnings = []
            if info.duration < 5 or info.duration > 30:
                warnings.append("参照音声は5〜30秒を目安にしてください")
            if np.max(np.abs(wav)) >= 0.99:
                warnings.append("参照音声にクリップの可能性があります")
            if float(np.sqrt(np.mean(wav**2))) < 0.005:
                warnings.append("参照音声の音量が小さいか、無音の可能性があります")
            if wav.ndim > 1:
                from voxcpm_narrate.artifacts import write_wav

                write_wav(prepared, wav.mean(axis=1), sr)
                warnings.append("参照音声をモノラルに変換しました")
            cfg.update(reference_audio="reference.wav", reference_sha256=file_hash(prepared),
                       reference_script=reference_script.strip(),
                       reference_transcript=reference_transcript.strip())
            if cfg["pace_mode"] == "reference":
                from voxcpm_narrate.speech_rate import reference_rate
                cfg["target_mora_rate"] = reference_rate(prepared, reference_transcript.strip())
            job = manager.update(job["id"], config=cfg, reference_warnings=warnings)
        except Exception as exc:
            manager.update(
                job["id"],
                status="error",
                error=type(exc).__name__,
                message="参照音声を読み込めませんでした",
            )
            raise ValueError("参照音声を読み込めませんでした") from exc
    return public(job)


def public(job):
    result = safe_job_snapshot(job)
    result["total"] = len(job["segments"])
    result["current"] = sum(bool(s["accepted"]) for s in job["segments"])
    result["percent"] = round(100 * result["current"] / max(1, result["total"]), 1)
    result["download_url"] = f"/api/jobs/{job['id']}/download" if job["export"] else None
    result["duration_sec"] = job["export"]["duration_sec"] if job["export"] else None
    return result


@app.get("/api/jobs/{jid}")
def get_job(jid: str):
    return public(manager.get(jid))


@app.patch("/api/jobs/{jid}/title")
def rename_job(jid: str, body: RenameBody):
    title = body.title.strip()
    if not title:
        raise ValueError("制作名を入力してください")

    def change(job):
        if job["title"] != body.expected_title:
            raise Conflict("制作名が別の操作で変更されました。最新の名前を確認してください")
        job["title"] = title

    return public(manager.mutate(jid, change))


@app.patch("/api/jobs/{jid}/segments/{sid}")
def edit_segment(jid: str, sid: str, body: EditBody):
    if not body.draft.text.strip():
        raise ValueError("読み上げ本文を入力してください")
    return public(service.edit(jid, sid, body.expected_revision, body.draft.model_dump()))


@app.put("/api/jobs/{jid}/dictionary")
def dictionary(jid: str, body: DictionaryBody):
    if len(body.entries) > 200 or any(
        not k.strip() or not v.strip() or len(k) > 100 or len(v) > 200
        for k, v in body.entries.items()
    ):
        raise ValueError("読み辞書は200件まで。用語と読みを空欄にしないでください")

    def change(job):
        check_idle(job)
        job["dictionary"] = body.entries

    return public(manager.mutate(jid, change))


@app.post("/api/jobs/{jid}/pronunciation-candidates")
def pronunciation_candidates(jid: str, body: PronunciationBody):
    return {"candidates": service.pronunciation_candidates(jid, body.script, body.include_registered),
            "lexicon": manager.lexicon()}


@app.get("/api/pronunciation-dictionary")
def pronunciation_dictionary():
    return manager.lexicon()


@app.post("/api/jobs/{jid}/pronunciation-learn")
def pronunciation_learn(jid: str, body: LearnReading):
    return public(manager.learn_reading(jid, body.term, body.reading, shared=body.shared,
                                       expected_revision=body.expected_revision))


@app.post("/api/jobs/{jid}/pronunciation-import")
def pronunciation_import(jid: str):
    return public(service.import_lexicon(jid))


@app.post("/api/jobs/{jid}/pronunciation-preview")
def pronunciation_preview(jid: str, body: ReadingPreview):
    return public(manager.submit(jid, "pronunciation-preview",
        lambda key: service.preview_pronunciation(key, body.segment_id, body.expected_revision,
                                                 body.term, body.reading),
        request_id=body.request_id,
        validate=lambda job: service.validate_pronunciation_preview(job, body.segment_id,
                          body.expected_revision, body.term, body.reading)))


@app.get("/api/jobs/{jid}/pronunciation-preview/{preview_id}")
def pronunciation_preview_audio(jid: str, preview_id: str):
    # Serve only the published preview belonging to this production.
    preview = manager.get(jid).get("pronunciation_preview")
    if not preview or preview["id"] != preview_id:
        raise KeyError("試聴音声が見つかりません。最新の試聴を生成してください")
    path = manager.job_dir(jid) / "pronunciation" / f"{preview['id']}.wav"
    if not path.is_file():
        raise KeyError("試聴音声が見つかりません")
    return FileResponse(path, media_type="audio/wav")


@app.post("/api/jobs/{jid}/generate")
def generate(jid: str, body: Operation):
    def validate(job):
        if body.segment_ids is not None:
            if not body.segment_ids or len(set(body.segment_ids)) != len(body.segment_ids):
                raise ValueError("生成するセグメントを選択してください（重複不可）")
            for sid in body.segment_ids:
                segment(job, sid)
        if job["config"]["improve_llm"] and not (body.api_key or default_llm_api_key()):
            raise ValueError("LLM 評価には API キーが必要です")
        reference_path = job["config"].get("reference_audio")
        if reference_path and service._reference_audio(jid, job["config"]) is None:
            raise ValueError("保存された参照音声が見つかりません")

    job = manager.submit(
        jid,
        "generate",
        lambda key: service.generate(key, body.segment_ids, body.api_key),
        request_id=body.request_id,
        validate=validate,
    )
    return public(job)


@app.post("/api/jobs/{jid}/regenerate-all")
def regenerate_all(jid: str, body: Operation):
    def validate(job):
        if body.segment_ids is not None:
            raise ValueError("全て再生成ではセグメントの指定はできません")
        job["regeneration_progress"] = {"current": 0, "total": len(job["segments"])}
        if job["config"].get("reference_audio") and service._reference_audio(jid, job["config"]) is None:
            raise ValueError("保存された参照音声が見つかりません")
    return public(manager.submit(jid, "regenerate-all", service.regenerate_all,
                                 request_id=body.request_id, validate=validate))


@app.delete("/api/jobs/{jid}")
def delete_job(jid: str):
    manager.delete(jid)
    return {"deleted": True}


@app.post("/api/trash/empty")
def empty_trash(body: EmptyTrashBody):
    if not body.confirmed:
        raise ValueError("完全削除の確認が必要です")
    return manager.empty_trash(body.job_ids)


@app.post("/api/jobs/{jid}/restore")
def restore_job(jid: str):
    return public(manager.restore(jid))


@app.post("/api/jobs/{jid}/cancel")
def cancel(jid: str):
    def change(job):
        if job["status"] in ACTIVE:
            job.update(cancel_requested=True, message="現在の処理が終わり次第、中断します")

    return public(manager.mutate(jid, change))


def validate_segment(sid, body):
    def validate(job):
        check_revision(segment(job, sid), body.expected_revision)

    return validate


@app.post("/api/jobs/{jid}/segments/{sid}/regenerate")
def regenerate(jid: str, sid: str, body: SegmentOperation):
    return public(
        manager.submit(
            jid,
            "regenerate",
            lambda key: service.regenerate(key, sid),
            request_id=body.request_id,
            validate=validate_segment(sid, body),
        )
    )


@app.post("/api/jobs/{jid}/segments/{sid}/adopt")
def adopt(jid: str, sid: str, body: SegmentOperation):
    if not body.version_id:
        raise ValueError("採用する版を選択してください")

    def validate(job):
        validate_segment(sid, body)(job)
        version(segment(job, sid), body.version_id)

    return public(
        manager.submit(
            jid,
            "adopt",
            lambda key: service.adopt(key, sid, body.version_id),
            request_id=body.request_id,
            validate=validate,
        )
    )


@app.post("/api/jobs/{jid}/segments/{sid}/recheck")
def recheck(jid: str, sid: str, body: SegmentOperation):
    if not body.version_id:
        raise ValueError("再検査する版を選択してください")

    def validate(job):
        validate_segment(sid, body)(job)
        version(segment(job, sid), body.version_id)

    return public(manager.submit(jid, "recheck",
        lambda key: service.verify_content(key, sid, body.version_id),
        request_id=body.request_id, validate=validate))


@app.post("/api/jobs/{jid}/segments/{sid}/audio-judge")
def audio_judge(jid: str, sid: str, body: SegmentOperation):
    def validate(job):
        validate_segment(sid, body)(job)
        version(segment(job, sid))
        if not (body.api_key or default_llm_api_key()):
            raise ValueError("音声評価には OpenRouter API キーが必要です")

    return public(
        manager.submit(
            jid,
            "audio_judge",
            lambda key: service.audio_judge(key, sid, body.model, body.api_key),
            request_id=body.request_id,
            validate=validate,
        )
    )


@app.get("/api/jobs/{jid}/segments/{sid}")
def download_segment(jid: str, sid: str, version_id: str | None = None):
    wav = service.audio_path(jid, sid, version_id)
    if not wav.is_file():
        raise HTTPException(404, "音声ファイルが見つかりません")
    return FileResponse(wav, media_type="audio/wav", filename=f"{sid}.wav")


@app.get("/api/jobs/{jid}/download")
def download(jid: str, normalize: bool = False):
    wav = service.export_audio(jid, normalize=normalize)
    suffix = "_finished" if normalize else ""
    return FileResponse(wav, media_type="audio/wav", filename=f"narration_{jid}{suffix}.wav")


@app.get("/api/jobs/{jid}/archive")
def archive(jid: str, normalize: bool = False):
    path = service.export_zip(jid, normalize=normalize)
    return FileResponse(
        path,
        media_type="application/zip",
        filename=f"narration_{jid}.zip",
        background=BackgroundTask(path.unlink, missing_ok=True),
    )


@app.get("/api/legacy-runs")
def legacy_runs():
    root = DEFAULT_OUT.parent
    found = []
    for pattern in ("run_*/manifest.json", "web_jobs/*/run_*/manifest.json"):
        for manifest in root.glob(pattern):
            if manifest.resolve().is_relative_to(root):
                found.append(str(manifest.parent.relative_to(root)))
    return {"runs": sorted(found, reverse=True)}


@app.post("/api/import")
def import_run(body: ImportBody):
    root = DEFAULT_OUT.parent
    path = (root / body.run).resolve()
    if not path.is_relative_to(root) or not (path / "manifest.json").is_file():
        raise ValueError("出力ディレクトリ内の既存 run を指定してください")
    jobs = json.loads((path / "manifest.json").read_text())
    if not isinstance(jobs, list) or not jobs or len(jobs) > 2000:
        raise ValueError("manifest が不正です")
    ids = [str(item["id"]) for item in jobs]
    if len(ids) != len(set(ids)) or any(not sid.replace("_", "").isalnum() for sid in ids):
        raise ValueError("manifest のセグメント ID が不正です")
    cfg = body.config.model_dump()
    job = service.create(
        path.name + "（取り込み）", "\n".join(j["text"] for j in jobs), "plain", cfg, jobs
    )
    ref = path / "reference.wav"
    if ref.is_file():
        dst = prepare_reference_wav(ref, manager.job_dir(job["id"]) / "reference.wav")
        cfg.update(reference_audio="reference.wav", reference_sha256=file_hash(dst))
    import soundfile as sf
    from voxcpm_narrate.artifacts import write_wav
    from voxcpm_narrate.web.jobs import now

    for seg in job["segments"]:
        wav_path = path / "segments" / (seg["id"] + ".wav")
        if not wav_path.is_file():
            continue
        wav, sr = sf.read(wav_path, dtype="float32")
        vid = uuid.uuid4().hex[:16]
        dst = manager.job_dir(job["id"]) / "versions" / seg["id"] / (vid + ".wav")
        write_wav(dst, wav, sr)
        seg.update(accepted=vid, status="ready")
        seg["versions"].append(
            dict(
                id=vid,
                created_at=now(),
                **seg["draft"],
                spoken_text=seg["draft"]["text"],
                params=None,
                model_id="unknown (imported)",
                sample_rate=sr,
                duration_sec=len(wav) / sr,
                sha256=file_hash(dst),
                evaluation=None,
            )
        )
    job.update(config=cfg, status="done", message="元の run を保持して取り込みました")
    job["export"] = service._build_export(job)
    return public(manager.save(job))


def main():
    import ipaddress
    import uvicorn
    from voxcpm_narrate.console import ensure_rich_tqdm

    ensure_rich_tqdm()
    host = os.environ.get("VOXCPM_WEB_HOST", "127.0.0.1")
    try:
        loopback = host.lower() == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback and os.environ.get("VOXCPM_ALLOW_REMOTE") != "1":
        raise SystemExit(
            "認証のない Web UI は既定でローカル接続だけを許可します。"
            "外部公開を保護した場合のみ VOXCPM_ALLOW_REMOTE=1 を設定してください。"
        )
    uvicorn.run(
        "voxcpm_narrate.web.app:app",
        host=host,
        port=int(os.environ.get("VOXCPM_WEB_PORT", "7860")),
        reload=False,
    )


if __name__ == "__main__":
    main()
