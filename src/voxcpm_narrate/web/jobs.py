"""SQLite snapshots and a single, recoverable local worker queue."""

from __future__ import annotations

import copy
import json
import queue
import shutil
import sqlite3
import threading
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

from voxcpm_narrate.console import log_error

ACTIVE = {"queued", "running", "improving"}
PRIVATE_FIELDS = {"model_source", "reference_audio"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Conflict(ValueError):
    pass


class Cancelled(Exception):
    pass


def safe_job_snapshot(job: dict) -> dict:
    """Return production metadata without server paths or internal errors."""

    def scrub(value):
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if key in PRIVATE_FIELDS:
                    continue
                if key == "error" and item:
                    result[key] = "処理に失敗しました。サーバーのログを確認してください。"
                elif key == "model_id" and isinstance(item, str) and Path(item).is_absolute():
                    result[key] = "local-model"
                else:
                    result[key] = scrub(item)
            return result
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value

    return scrub(copy.deepcopy(job))


class JobManager:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._queue = queue.Queue()
        self._worker = None
        self._running_id = None
        self._db = sqlite3.connect(self.root / "productions.sqlite3", check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, body TEXT NOT NULL)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS pronunciation_lexicon "
            "(id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, body TEXT NOT NULL)"
        )
        self._db.execute("INSERT OR IGNORE INTO pronunciation_lexicon VALUES (1, 0, '{}')")
        self._db.commit()
        # Never silently restart costly operations after a process restart.
        for job in self.list():
            changed = False
            reference = job.get("config", {}).get("reference_audio")
            if reference and Path(reference).is_absolute():
                expected = self.root / job["id"] / "reference.wav"
                if Path(reference).resolve() == expected.resolve() and expected.is_file():
                    job["config"]["reference_audio"] = "reference.wav"
                else:
                    job["config"].pop("reference_audio", None)
                    job.setdefault("reference_warnings", []).append(
                        "以前の参照音声パスを削除しました。必要に応じて再登録してください"
                    )
                changed = True
            if job["status"] in ACTIVE:
                job.update(
                    status="interrupted",
                    phase="interrupted",
                    operation=None,
                    cancel_requested=False,
                    message="処理が中断されました。再開できます。",
                )
                for seg in job["segments"]:
                    if seg["status"] in {"running", "regenerating", "judging"}:
                        seg["status"] = "ready" if seg.get("accepted") else "pending"
                changed = True
            if changed:
                self.save(job)

    def save(self, job: dict) -> dict:
        with self._lock:
            snapshot = copy.deepcopy(job)
            snapshot["updated_at"] = now()
            with self._db:
                self._db.execute(
                    "INSERT OR REPLACE INTO jobs VALUES (?, ?)",
                    (snapshot["id"], json.dumps(snapshot, ensure_ascii=False)),
                )
            return copy.deepcopy(snapshot)

    def get(self, job_id: str, *, include_deleted=False) -> dict:
        with self._lock:
            row = self._db.execute("SELECT body FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise KeyError("制作が見つかりません")
        job = json.loads(row[0])
        if job.get("deleted_at") and not include_deleted:
            raise KeyError("制作はごみ箱にあります")
        return job

    def list(self, *, deleted=False) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT body FROM jobs").fetchall()
        return sorted(
            (job for row in rows if bool((job := json.loads(row[0])).get("deleted_at")) == deleted),
            key=lambda job: job["updated_at"], reverse=True
        )

    def delete(self, job_id):
        with self._lock:
            job = self.get(job_id, include_deleted=True)
            if job["status"] in ACTIVE or self._running_id == job_id:
                raise Conflict("処理中の制作は削除できません。中断して完了を待ってください")
            if not job.get("deleted_at"):
                job["deleted_at"] = now()
                self.save(job)

    def restore(self, job_id):
        with self._lock:
            job = self.get(job_id, include_deleted=True)
            if job.get("purging"):
                raise Conflict("完全削除が開始された制作は復元できません。ごみ箱を空にする操作を再実行してください")
            job.pop("deleted_at", None)
            return self.save(job)

    def empty_trash(self, expected_ids):
        """Purge only the confirmed snapshot; retain a tombstone on I/O failure."""
        with self._lock:
            jobs = self.list(deleted=True)
            if set(expected_ids) != {job["id"] for job in jobs}:
                raise Conflict("ごみ箱の内容が変更されました。一覧を更新して確認し直してください")
            for job in jobs:
                if job["status"] in ACTIVE or job["id"] == self._running_id:
                    raise Conflict("処理中の制作があるため完全削除できません")
                if not job["id"].isalnum():
                    raise ValueError("制作IDが不正です")
            deleted, failed = [], []
            for job in jobs:
                # Persist before touching files so a crash cannot offer partial audio for restore.
                job["purging"] = True
                self.save(job)
                path = self.root / job["id"]
                try:
                    if path.is_symlink():
                        path.unlink()
                    elif path.exists():
                        shutil.rmtree(path)
                    with self._db:
                        self._db.execute("DELETE FROM jobs WHERE id=?", (job["id"],))
                    deleted.append(job["id"])
                except (OSError, sqlite3.Error):
                    failed.append(job["id"])
            return {"deleted_ids": deleted, "failed_ids": failed}

    def mutate(self, job_id: str, fn) -> dict:
        with self._lock:
            job = self.get(job_id)
            fn(job)
            return self.save(job)

    def update(self, job_id: str, **kwargs) -> dict:
        return self.mutate(job_id, lambda job: job.update(kwargs))

    def lexicon(self) -> dict:
        with self._lock:
            revision, body = self._db.execute(
                "SELECT revision, body FROM pronunciation_lexicon WHERE id=1"
            ).fetchone()
            return {"revision": revision, "entries": json.loads(body)}

    def learn_reading(self, job_id: str, term: str, reading: str, *, shared: bool,
                     expected_revision: int) -> dict:
        """Commit the job dictionary and shared vocabulary in one transaction."""
        from voxcpm_narrate.pronunciation import validate_reading

        validate_reading(term, reading)
        with self._lock:
            job = self.get(job_id)
            if job["status"] in ACTIVE:
                raise Conflict("生成処理が完了してから読みを登録してください")
            lexicon = self.lexicon()
            if shared and expected_revision != lexicon["revision"]:
                raise Conflict("共通辞書が更新されています。候補を再取得して登録してください")
            job["dictionary"][term] = reading
            if len(job["dictionary"]) > 200:
                raise ValueError("この制作の読み辞書は200件までです")
            if shared:
                lexicon["entries"][term] = reading
                if len(lexicon["entries"]) > 200:
                    raise ValueError("共通読み辞書は200件までです")
            job["updated_at"] = now()
            with self._db:
                self._db.execute("UPDATE jobs SET body=? WHERE id=?",
                                 (json.dumps(job, ensure_ascii=False), job_id))
                if shared:
                    self._db.execute("UPDATE pronunciation_lexicon SET revision=?, body=? WHERE id=1",
                                     (lexicon["revision"] + 1, json.dumps(lexicon["entries"], ensure_ascii=False)))
            return job

    def job_dir(self, job_id: str) -> Path:
        if not job_id.isalnum():
            raise ValueError("Invalid job id")
        path = self.root / job_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def checkpoint(self, job_id: str) -> None:
        if self.get(job_id).get("cancel_requested"):
            raise Cancelled()

    def submit(self, job_id: str, kind: str, target, *, request_id: str, validate=None) -> dict:
        with self._lock:
            job = self.get(job_id)
            if request_id in job.get("requests", []):
                return job
            if job["status"] in ACTIVE:
                raise Conflict("別の処理が進行中です。完了または中断後に操作してください。")
            if validate:
                validate(job)
            job.update(
                status="queued",
                phase=kind,
                operation=kind,
                error=None,
                cancel_requested=False,
                message="キューで待機しています",
            )
            job["requests"] = (job.get("requests", []) + [request_id])[-100:]
            self.save(job)
            self._queue.put((job_id, target))
            if self._worker is None:
                self._worker = threading.Thread(
                    target=self._run, daemon=True, name="narration-worker"
                )
                self._worker.start()
            return self.get(job_id)

    def _run(self):
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            job_id, target = item
            with self._lock:
                self._running_id = job_id
            try:
                self.checkpoint(job_id)
                self.update(job_id, status="running")
                completion_message = target(job_id)
                self.update(
                    job_id,
                    status="done",
                    phase="done",
                    operation=None,
                    cancel_requested=False,
                    message=completion_message if isinstance(completion_message, str) else "保存しました",
                )
            except Cancelled:
                self.update(
                    job_id,
                    status="interrupted",
                    phase="interrupted",
                    operation=None,
                    cancel_requested=False,
                    message="中断しました。保存済みの部分から再開できます。",
                )
            except Exception as exc:
                # Persist the failure before logging: logging must never strand a job.
                self.update(
                    job_id,
                    status="error",
                    phase="error",
                    operation=None,
                    error=type(exc).__name__,
                    message="処理に失敗しました。保存済み音声は保持されています。",
                )
                try:
                    log_error(traceback.format_exc())
                except Exception:
                    pass
            finally:

                def settle(job):
                    for seg in job["segments"]:
                        if seg["status"] in {"running", "regenerating", "judging"}:
                            seg["status"] = "ready" if seg.get("accepted") else "pending"

                with self._lock:
                    self.mutate(job_id, settle)
                    self._running_id = None
                self._queue.task_done()

    def close(self):
        if self._worker:
            self._queue.put(None)
            self._worker.join(timeout=5)
            if self._worker.is_alive():
                return
        self._db.close()


def new_job(title: str, script: str, mode: str, config: dict, segments: list) -> dict:
    return dict(
        schema_version=1,
        id=uuid.uuid4().hex[:12],
        title=title,
        script=script,
        mode=mode,
        config=config,
        segments=segments,
        status="draft",
        phase="draft",
        message="生成する箇所を選んでください",
        error=None,
        created_at=now(),
        updated_at=now(),
        operation=None,
        cancel_requested=False,
        requests=[],
        export=None,
        timings=[],
        reference_warnings=[],
        dictionary={},
        evaluation_count=0,
    )
