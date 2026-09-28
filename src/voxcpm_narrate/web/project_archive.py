"""Portable project snapshots, validated and staged before publishing to SQLite."""

from __future__ import annotations

import copy
import json
import re
import shutil
import stat
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from voxcpm_narrate.artifacts import file_hash, write_json
from voxcpm_narrate.web.jobs import ACTIVE, Conflict, now, safe_job_snapshot
from voxcpm_narrate.web.schemas import Config, Draft

MAX_ARCHIVE_BYTES = 2 * 1024**3
MAX_METADATA_BYTES = 16 * 1024**2
FORMAT = "voxcpm-narrate-project"
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9_\-]{1,100}$")]


class Metadata(BaseModel):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)


class ArchiveConfig(Config):
    reference_audio: Literal["reference.wav"] | None = None
    reference_sha256: str | None = None
    reference_script: str = Field(default="", max_length=2000)
    reference_transcript: str = Field(default="", max_length=2000)


class AudioVersion(Metadata):
    id: Identifier
    text: str
    reading: str = ""
    control: str
    pause_before_sec: float = Field(ge=0, le=10)
    spoken_text: str
    created_at: str
    sample_rate: int = Field(gt=0, le=384000)
    duration_sec: float = Field(gt=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    evaluation: dict | None = None
    content_check: dict | None = None
    speech_rate: dict | None = None


class Segment(Metadata):
    id: Identifier
    section: str
    original_text: str
    draft: Draft
    revision: int = Field(ge=0)
    status: str
    accepted: Identifier | None
    versions: list[AudioVersion]
    history: list[Identifier]
    error: str | None = None
    feedback: dict | None = None


class Export(Metadata):
    id: Identifier
    duration_sec: float = Field(gt=0)


class Preview(Metadata):
    id: Identifier
    term: str
    reading: str
    segment_id: Identifier


class Project(Metadata):
    schema_version: Literal[1]
    title: str = Field(min_length=1, max_length=150)
    script: str = Field(max_length=200000)
    mode: Literal["plain", "lines", "markdown", "ssml"]
    config: ArchiveConfig
    segments: list[Segment] = Field(min_length=1, max_length=2000)
    dictionary: dict[str, str] = Field(max_length=200)
    export: Export | None = None
    pronunciation_preview: Preview | None = None
    timings: list[dict] = Field(default_factory=list)
    reference_warnings: list[str] = Field(default_factory=list)
    evaluation_count: int = Field(default=0, ge=0)


def validate_project(raw):
    try:
        job = Project.model_validate(raw).model_dump()
    except ValidationError as exc:
        raise ValueError("プロジェクトのデータ形式が不正です") from exc
    ids = [s["id"] for s in job["segments"]]
    if len(ids) != len(set(ids)):
        raise ValueError("セグメント ID が重複しています")
    for seg in job["segments"]:
        vids = [v["id"] for v in seg["versions"]]
        if (len(vids) != len(set(vids)) or
                (seg["accepted"] is not None and seg["accepted"] not in vids) or
                any(v not in vids for v in seg["history"])):
            raise ValueError("音声の候補・採用履歴が不正です")
    if job["export"] and not all(s["accepted"] for s in job["segments"]):
        raise ValueError("結合音声と採用状態が一致しません")
    if job["pronunciation_preview"] and job["pronunciation_preview"]["segment_id"] not in ids:
        raise ValueError("読みの試聴対象が不正です")
    if any(not k.strip() or not v.strip() or len(k) > 100 or len(v) > 200
           for k, v in job["dictionary"].items()):
        raise ValueError("読み辞書が不正です")
    # Local model paths are not portable and must never be read from an imported ZIP.
    model = job["config"]["model_id"]
    if (model == "local-model" or not re.fullmatch(r"[\w.-]+/[\w.-]+", model) or
            any(part in {".", ".."} for part in model.split("/"))):
        job["config"]["model_id"] = "openbmb/VoxCPM2"
        job["reference_warnings"].append("ローカルモデルは同梱されないため既定のモデルを使用します")
    return job


def project_files(job):
    """Return required and optional paths, derived only from validated identifiers."""
    required, optional = set(), set()
    if job["config"].get("reference_audio"):
        required.add("reference.wav")
    for seg in job["segments"]:
        for v in seg["versions"]:
            base = f"versions/{seg['id']}/{v['id']}"
            required.add(base + ".wav")
            optional.add(base + ".raw.wav")
    if job.get("pronunciation_preview"):
        base = f"pronunciation/{job['pronunciation_preview']['id']}"
        required.add(base + ".wav")
        optional.add(base + ".json")
    if job.get("export"):
        base = f"exports/{job['export']['id']}/"
        required.update(base + name for name in ("full.wav", "manifest.json", "complete.json"))
        required.update(base + f"segments/{s['id']}.wav" for s in job["segments"])
    return required, optional


def export_project(manager, jid):
    # Hold the same lock used by edits/submission/deletion for a consistent snapshot.
    with manager._lock:
        source = manager.get(jid)
        if source["status"] in ACTIVE or manager._running_id == jid:
            raise Conflict("処理が完了するか、中断してからプロジェクトを書き出してください")
        snapshot = safe_job_snapshot(source)
        if source["config"].get("reference_audio"):
            snapshot["config"]["reference_audio"] = "reference.wav"
        job = validate_project(snapshot)
        required, optional = project_files(job)
        root = manager.job_dir(jid).resolve()
        files = required | {p for p in optional if (root / p).is_file()}
        hashes = {}
        for name in sorted(files):
            path = root / name
            if not path.resolve().is_relative_to(root) or not path.is_file():
                raise ValueError("保存された音声ファイルが見つかりません")
            hashes[name] = file_hash(path)
        metadata = json.dumps(dict(format=FORMAT, version=1, production=job, files=hashes),
                              ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(metadata) > MAX_METADATA_BYTES:
            raise ValueError("プロジェクトのメタデータが大きすぎます")
        if sum((root / name).stat().st_size for name in files) + len(metadata) > MAX_ARCHIVE_BYTES:
            raise ValueError("プロジェクトは展開後2 GBまでです")
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as temp:
            path = Path(temp.name)
        try:
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("project.json", metadata)
                for name in sorted(files):
                    archive.write(root / name, name)
            return path
        except BaseException:
            path.unlink(missing_ok=True)
            raise


def import_project(manager, upload):
    """Never extract user-controlled paths; publish only after all checks succeed."""
    with tempfile.TemporaryDirectory(prefix="narrate-project-", dir=manager.root) as tmp:
        staging = Path(tmp)
        try:
            with zipfile.ZipFile(upload) as archive:
                infos = archive.infolist()
                names = [i.filename for i in infos]
                if len(infos) > 20000 or len(names) != len(set(names)):
                    raise ValueError("ZIP 内のファイル数または重複した名前が不正です")
                if sum(i.file_size for i in infos) > MAX_ARCHIVE_BYTES:
                    raise ValueError("プロジェクトは展開後2 GBまでです")
                for info in infos:
                    name = info.filename
                    if ("\\" in name or PurePosixPath(name).is_absolute() or
                            ".." in PurePosixPath(name).parts or
                            str(PurePosixPath(name)) != name.rstrip("/") or
                            stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1):
                        raise ValueError("ZIP 内のパスまたはファイル形式が不正です")
                if "project.json" not in names:
                    raise ValueError("「プロジェクト ZIP」で書き出したファイルを選択してください")
                if archive.getinfo("project.json").file_size > MAX_METADATA_BYTES:
                    raise ValueError("プロジェクトのメタデータが大きすぎます")
                data = json.loads(archive.read("project.json"))
                if not isinstance(data, dict) or data.get("format") != FORMAT or data.get("version") != 1:
                    raise ValueError("対応していないプロジェクト ZIP 形式です")
                job = validate_project(data.get("production"))
                required, optional = project_files(job)
                hashes = data.get("files")
                if (not isinstance(hashes, dict) or not required <= hashes.keys() or
                        not hashes.keys() <= required | optional or
                        set(names) != {"project.json", *hashes}):
                    raise ValueError("ZIP 内のプロジェクトファイルが不足しているか不正です")
                for name, digest in hashes.items():
                    target = staging / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(name) as source, target.open("wb") as dest:
                        shutil.copyfileobj(source, dest, 1024 * 1024)
                    if file_hash(target) != digest:
                        raise ValueError("プロジェクト内のファイルが破損しています")
            _validate_audio(job, staging)
        except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, UnicodeError,
                json.JSONDecodeError, RuntimeError, NotImplementedError) as exc:
            raise ValueError("プロジェクト ZIP を読み込めませんでした。ファイルを確認してください") from exc
        job = copy.deepcopy(job)
        job.update(id=uuid.uuid4().hex[:12], created_at=now(), updated_at=now(),
                   status="done" if job["export"] else "draft", phase="imported",
                   message="プロジェクト ZIP を読み込みました", error=None,
                   requests=[], operation=None, cancel_requested=False)
        for field in ("deleted_at", "purging", "regeneration_progress"):
            job.pop(field, None)
        for seg in job["segments"]:
            seg["status"] = "ready" if seg["accepted"] else "pending"
        destination = manager.root / job["id"]
        with manager._lock:
            if destination.exists():
                raise Conflict("制作 ID が重複しました。もう一度取り込んでください")
            staging.rename(destination)
            try:
                return manager.save(job)
            except BaseException:
                shutil.rmtree(destination)
                raise


def _validate_audio(job, root):
    import soundfile as sf

    try:
        for seg in job["segments"]:
            for v in seg["versions"]:
                path = root / "versions" / seg["id"] / (v["id"] + ".wav")
                info = sf.info(path)
                if (info.frames <= 0 or info.samplerate != v["sample_rate"] or
                        abs(info.duration - v["duration_sec"]) > 0.01 or
                        file_hash(path) != v["sha256"]):
                    raise ValueError("音声ファイルと候補の情報が一致しません")
        if job["config"].get("reference_audio"):
            path = root / "reference.wav"
            if not 0 < sf.info(path).duration <= 120:
                raise ValueError("参照音声は120秒以内にしてください")
            job["config"]["reference_sha256"] = file_hash(path)
        if job.get("pronunciation_preview"):
            path = root / "pronunciation" / (job["pronunciation_preview"]["id"] + ".wav")
            if sf.info(path).frames <= 0:
                raise ValueError("読みの試聴音声が空です")
        if job["export"]:
            folder = root / "exports" / job["export"]["id"]
            info = sf.info(folder / "full.wav")
            if info.frames <= 0 or abs(info.duration - job["export"]["duration_sec"]) > 0.01:
                raise ValueError("結合音声の情報が一致しません")
            manifest = []
            for seg in job["segments"]:
                v = next(v for v in seg["versions"] if v["id"] == seg["accepted"])
                if file_hash(folder / "segments" / (seg["id"] + ".wav")) != v["sha256"]:
                    raise ValueError("結合音声のセグメントと採用状態が一致しません")
                manifest.append(dict(id=seg["id"], section=seg["section"],
                                     version_id=v["id"], **{key: v[key] for key in
                                     ("text", "control", "pause_before_sec", "spoken_text")}))
            # Recreate cache metadata from validated values instead of trusting ZIP JSON paths.
            job["export"] = dict(id=job["export"]["id"], duration_sec=info.duration,
                                 versions=[(s["id"], s["accepted"]) for s in job["segments"]],
                                 created_at=now())
            write_json(folder / "manifest.json", manifest)
            write_json(folder / "complete.json", job["export"])
    except (RuntimeError, sf.LibsndfileError) as exc:
        raise ValueError("プロジェクト内の音声を読み込めませんでした") from exc
