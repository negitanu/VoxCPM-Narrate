"""SQLite snapshots and a single, recoverable local worker queue."""

from __future__ import annotations

import copy
import json
import queue
import sqlite3
import threading
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

from voxcpm_narrate.console import log_error

ACTIVE = {"queued", "running", "improving"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Conflict(ValueError):
    pass


class Cancelled(Exception):
    pass


class JobManager:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._queue = queue.Queue()
        self._worker = None
        self._db = sqlite3.connect(self.root / "productions.sqlite3", check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, body TEXT NOT NULL)"
        )
        self._db.commit()
        # Never silently restart costly operations after a process restart.
        for job in self.list():
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

    def get(self, job_id: str) -> dict:
        with self._lock:
            row = self._db.execute("SELECT body FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise KeyError("制作が見つかりません")
        return json.loads(row[0])

    def list(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT body FROM jobs").fetchall()
        return sorted(
            (json.loads(row[0]) for row in rows), key=lambda job: job["updated_at"], reverse=True
        )

    def mutate(self, job_id: str, fn) -> dict:
        with self._lock:
            job = self.get(job_id)
            fn(job)
            return self.save(job)

    def update(self, job_id: str, **kwargs) -> dict:
        return self.mutate(job_id, lambda job: job.update(kwargs))

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
            try:
                self.checkpoint(job_id)
                self.update(job_id, status="running")
                target(job_id)
                self.update(
                    job_id,
                    status="done",
                    phase="done",
                    operation=None,
                    cancel_requested=False,
                    message="保存しました",
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
                    error=str(exc),
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

                self.mutate(job_id, settle)
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
