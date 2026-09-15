"""FastAPI application for Material Design narration UI."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from voxcpm_narrate.extract import load_jobs
from voxcpm_narrate.harness.loop import run_improve_loop
from voxcpm_narrate.synthesize import prepare_reference_wav, synthesize
from voxcpm_narrate.web.jobs import JobManager

WEB_DIR = Path(__file__).resolve().parent
STATIC_DIR = WEB_DIR / "static"
DEFAULT_OUT = Path(os.environ.get("VOXCPM_WEB_OUT", "output/voxcpm2/web_jobs")).resolve()

manager = JobManager(DEFAULT_OUT)

app = FastAPI(title="VoxCPM Narrate", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "service": "voxcpm-narrate-web"}


def _save_upload(upload: UploadFile, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    data = upload.file.read()
    if not data:
        raise HTTPException(status_code=400, detail=f"Empty upload: {upload.filename}")
    dest.write_bytes(data)
    return dest


def _execute_job(
    job_id: str,
    *,
    ssml_path: Path,
    reference_path: Path | None,
    device: str,
    control: str,
    max_chars: int,
    cfg_value: float,
    timesteps: int,
    seed: int,
    improve: bool,
    improve_asr: bool,
    improve_rounds: int,
    model_id: str,
) -> None:
    job_dir = manager.job_dir(job_id)
    run_dir = job_dir / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)

    def on_progress(**payload):
        manager.update(
            job_id,
            status="running",
            phase=str(payload.get("phase", "synthesize")),
            message=str(payload.get("message", "")),
            current=int(payload.get("current", 0)),
            total=int(payload.get("total", 0)),
        )

    manager.update(job_id, phase="parse", message="Parsing SSML…", current=0, total=0)
    jobs = load_jobs(
        ssml_path,
        mode="ssml",
        narration_heading="読み上げ本文",
        max_chars=max_chars,
        base_control=control or None,
    )
    if not jobs:
        raise RuntimeError("No speakable segments found in SSML")

    manager.update(
        job_id,
        segment_count=len(jobs),
        total=len(jobs),
        message=f"Parsed {len(jobs)} segments",
    )

    reference_audio = None
    if reference_path is not None:
        prepared = prepare_reference_wav(reference_path, run_dir / "reference.wav")
        reference_audio = str(prepared)

    full_path = synthesize(
        jobs,
        output_dir=run_dir,
        model_id=model_id,
        device=device,
        optimize=False,
        cfg_value=cfg_value,
        inference_timesteps=timesteps,
        normalize=True,
        control=control or None,
        reference_audio=reference_audio,
        seed=seed,
        dry_run=False,
        progress_callback=on_progress,
    )

    duration_sec = None
    try:
        import soundfile as sf

        info = sf.info(str(full_path))
        duration_sec = float(info.duration)
    except Exception:  # noqa: BLE001
        duration_sec = None

    improve_summary = None
    if improve:
        manager.update(
            job_id,
            status="improving",
            phase="improve",
            message="Running self-improvement loop…",
            current=0,
            total=0,
            percent=95.0,
        )

        def improve_progress(**payload):
            # harness doesn't call this yet; keep phase visible
            manager.update(
                job_id,
                status="improving",
                phase="improve",
                message=str(payload.get("message", "Improving…")),
            )

        _ = improve_progress
        report = run_improve_loop(
            run_dir=run_dir,
            model_id=model_id,
            device=device,
            optimize=False,
            reference_audio=reference_audio,
            base_control=control or None,
            base_cfg=cfg_value,
            base_timesteps=timesteps,
            base_normalize=True,
            base_seed=seed,
            max_rounds=improve_rounds,
            awkward_threshold=0.62,
            only_awkward=True,
            limit=None,
            segment_ids=None,
            use_asr=improve_asr,
            asr_device="cpu",
            use_llm=False,
            llm_base_url="http://127.0.0.1:11434/v1",
            llm_model="llama3.2",
            llm_api_key="ollama",
        )
        improve_summary = report.get("summary")
        full_path = run_dir / "full.wav"
        try:
            import soundfile as sf

            duration_sec = float(sf.info(str(full_path)).duration)
        except Exception:  # noqa: BLE001
            pass

    # Convenience symlink inside job dir
    latest = job_dir / "latest"
    if latest.exists() or latest.is_symlink():
        latest.unlink()
    latest.symlink_to(run_dir.resolve())

    manager.update(
        job_id,
        status="done",
        phase="done",
        message="Completed",
        current=len(jobs),
        total=len(jobs),
        percent=100.0,
        run_dir=str(run_dir),
        download_url=f"/api/jobs/{job_id}/download",
        duration_sec=duration_sec,
        improve_summary=improve_summary,
    )


@app.post("/api/jobs")
async def create_job(
    reference: UploadFile = File(..., description="Reference voice audio"),
    ssml: UploadFile = File(..., description="SSML talk script"),
    device: str = Form("auto"),
    control: str = Form("ややゆっくり、落ち着いたプレゼン説明調"),
    max_chars: int = Form(120),
    cfg_value: float = Form(2.0),
    timesteps: int = Form(10),
    seed: int = Form(42),
    improve: str = Form("false"),
    improve_asr: str = Form("true"),
    improve_rounds: int = Form(3),
    model_id: str = Form("openbmb/VoxCPM2"),
) -> dict:
    if device not in {"auto", "cpu", "mps", "cuda"}:
        raise HTTPException(status_code=400, detail="Invalid device")

    def as_bool(value: str) -> bool:
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    improve_flag = as_bool(improve)
    improve_asr_flag = as_bool(improve_asr)

    state = manager.create(improve_enabled=improve_flag)
    job_dir = manager.job_dir(state.id)

    ref_name = Path(reference.filename or "source.wav").name
    ssml_name = Path(ssml.filename or "script.ssml").name
    if not ssml_name.lower().endswith((".ssml", ".xml", ".txt")):
        ssml_name = "script.ssml"

    reference_path = _save_upload(reference, job_dir / "uploads" / ref_name)
    ssml_path = _save_upload(ssml, job_dir / "uploads" / ssml_name)

    manager.run_in_background(
        state.id,
        _execute_job,
        ssml_path=ssml_path,
        reference_path=reference_path,
        device=device,
        control=control.strip(),
        max_chars=max_chars,
        cfg_value=cfg_value,
        timesteps=timesteps,
        seed=seed,
        improve=improve_flag,
        improve_asr=improve_asr_flag,
        improve_rounds=improve_rounds,
        model_id=model_id,
    )
    return state.to_dict()


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.to_dict()


@app.get("/api/jobs/{job_id}/download")
def download_job(job_id: str) -> FileResponse:
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "done" or not job.run_dir:
        raise HTTPException(status_code=409, detail="Job not ready")
    wav = Path(job.run_dir) / "full.wav"
    if not wav.is_file():
        raise HTTPException(status_code=404, detail="full.wav not found")
    return FileResponse(
        wav,
        media_type="audio/wav",
        filename=f"narration_{job_id}.wav",
    )


def main() -> None:
    import uvicorn

    host = os.environ.get("VOXCPM_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("VOXCPM_WEB_PORT", "7860"))
    uvicorn.run("voxcpm_narrate.web.app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
