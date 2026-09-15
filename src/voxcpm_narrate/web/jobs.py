"""Background job store for the web UI."""

from __future__ import annotations

import threading
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class JobState:
    id: str
    status: str = "queued"  # queued|running|improving|done|error
    phase: str = "queued"
    message: str = "Waiting…"
    current: int = 0
    total: int = 0
    percent: float = 0.0
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    error: str | None = None
    run_dir: str | None = None
    download_url: str | None = None
    duration_sec: float | None = None
    segment_count: int | None = None
    improve_enabled: bool = False
    improve_summary: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class JobManager:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, JobState] = {}
        self._lock = threading.Lock()
        self._worker_lock = threading.Lock()

    def create(self, *, improve_enabled: bool) -> JobState:
        job_id = uuid.uuid4().hex[:12]
        state = JobState(id=job_id, improve_enabled=improve_enabled)
        with self._lock:
            self._jobs[job_id] = state
        return state

    def get(self, job_id: str) -> JobState | None:
        with self._lock:
            return self._jobs.get(job_id)

    def update(self, job_id: str, **kwargs: Any) -> None:
        with self._lock:
            job = self._jobs[job_id]
            for key, value in kwargs.items():
                setattr(job, key, value)
            job.updated_at = datetime.now().isoformat(timespec="seconds")
            if job.total:
                job.percent = round(100.0 * job.current / max(job.total, 1), 1)

    def job_dir(self, job_id: str) -> Path:
        path = self.root / job_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def run_in_background(self, job_id: str, target, *args, **kwargs) -> None:
        def _runner() -> None:
            # Serialize heavy GPU/MPS jobs one at a time
            with self._worker_lock:
                try:
                    self.update(job_id, status="running", phase="starting", message="Starting…")
                    target(job_id, *args, **kwargs)
                except Exception as exc:  # noqa: BLE001
                    tb = traceback.format_exc()
                    print(tb)
                    self.update(
                        job_id,
                        status="error",
                        phase="error",
                        message=str(exc),
                        error=str(exc),
                    )

        threading.Thread(target=_runner, daemon=True, name=f"job-{job_id}").start()
