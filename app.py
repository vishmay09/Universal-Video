"""
FastAPI backend for the video/image compressor.

Replaces the earlier Gradio UI: most of the problems in this project's
history were Gradio's own machinery (dependency churn breaking on every
redeploy, progress bars frozen for the whole duration of a pass, its
internal API-schema generation crashing outright) rather than the actual
compression logic, which is unchanged here and already proven correct.
This is a small, dependency-light backend with a plain HTML/JS frontend
in static/, so there's a lot less moving machinery to break.

Jobs run in background threads and report progress into an in-memory
dict; the frontend polls GET /api/jobs/{id} every second. Polling was
chosen over WebSockets/SSE specifically because this runs behind Render's
proxy, and plain HTTP polling has zero proxy/timeout edge cases to debug -
unlike the multiple Gradio-specific container/proxy issues hit earlier in
this project's history.
"""

import json
import os
import shutil
import threading
import urllib.parse
import uuid
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import compressor as c

app = FastAPI(title="Video & Image Compressor")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==============================
# JOB TRACKING (in-memory + disk fallback)
# ==============================
# A job dict per compression run: {status, progress, desc, result, error}.
# status is one of: "running", "done", "failed".
#
# Kept in memory for speed, but also mirrored to a small JSON file per job.
# Reason: if the process restarts mid-job (e.g. an OOM kill from Render's
# free-tier ~512MB RAM limit during a memory-heavy video encode - one
# realistic cause of exactly that), the in-memory dict is wiped and every
# poll afterward gets a bare 404 "Job not found", which looks like a
# mystery bug rather than what actually happened. The on-disk copy lets a
# poll after a restart still find the job and report a real status instead.

JOBS = {}
JOBS_LOCK = threading.Lock()
JOBS_FOLDER = c.BASE_DIR / "Jobs"
JOBS_FOLDER.mkdir(parents=True, exist_ok=True)


def _job_file(job_id):
    return JOBS_FOLDER / f"{job_id}.json"


def _persist_job(job_id, job):
    try:
        with open(_job_file(job_id), "w", encoding="utf-8") as f:
            json.dump(job, f)
    except Exception:
        pass  # persistence is a best-effort fallback, never block on it


def new_job():
    job_id = uuid.uuid4().hex
    job = {"status": "running", "progress": 0.0, "desc": "Starting...", "result": None, "error": None}
    with JOBS_LOCK:
        JOBS[job_id] = job
    _persist_job(job_id, job)
    return job_id


def update_job(job_id, progress=None, desc=None):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        if progress is not None:
            job["progress"] = progress
        if desc is not None:
            job["desc"] = desc
        snapshot = dict(job)
    _persist_job(job_id, snapshot)


def finish_job(job_id, result):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job:
            job["status"] = "done"
            job["progress"] = 1.0
            job["result"] = result
            snapshot = dict(job)
        else:
            snapshot = None
    if snapshot:
        _persist_job(job_id, snapshot)


def fail_job(job_id, error):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job:
            job["status"] = "failed"
            job["error"] = error
            snapshot = dict(job)
        else:
            snapshot = None
    if snapshot:
        _persist_job(job_id, snapshot)


def load_job(job_id):
    """Checks memory first (fast path), falls back to the on-disk copy so a
    job survives a process restart (e.g. an OOM kill mid-encode) instead of
    every poll afterward getting a bare, unexplained 404."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job:
            return dict(job)

    try:
        with open(_job_file(job_id), "r", encoding="utf-8") as f:
            job = json.load(f)
        if job.get("status") == "running":
            # The job was mid-flight when this process (re)started - the
            # thread that was updating it is gone, so it will never finish.
            job["status"] = "failed"
            job["error"] = (
                "The server restarted while this job was running (most likely an "
                "out-of-memory restart on this host during a memory-heavy encode). "
                "Please try again - a shorter clip or a smaller target size uses "
                "less memory and is less likely to hit this."
            )
        return job
    except FileNotFoundError:
        return None
    except Exception:
        return None


def save_upload(upload_file: UploadFile) -> Path:
    """Saves an uploaded file to disk under a unique name and returns its path."""
    suffix = Path(upload_file.filename or "upload").suffix
    dest = c.UPLOAD_FOLDER / f"{uuid.uuid4().hex}{suffix}"
    with open(dest, "wb") as f:
        shutil.copyfileobj(upload_file.file, f)
    return dest


def make_download_url(path) -> str:
    """Builds a /api/download URL with the path properly query-encoded, so
    filenames with spaces or special characters don't produce a broken URL."""
    return f"/api/download?path={urllib.parse.quote(str(path))}"


def resolve_target(size_choice: str, custom_value: float | None, presets: dict, label: str):
    if size_choice == "custom":
        if custom_value is None or custom_value <= 0:
            raise HTTPException(400, f"Enter a valid custom size in {label}.")
        return float(custom_value)
    value = presets.get(size_choice)
    if value is None:
        raise HTTPException(400, "Unknown target size option.")
    return value


# ==============================
# SINGLE VIDEO
# ==============================

@app.post("/api/compress/video")
async def compress_video(file: UploadFile = File(...), size_choice: str = Form(...), custom_mb: float | None = Form(None)):
    target_mb = resolve_target(size_choice, custom_mb, c.VIDEO_SIZE_PRESETS, "MB")
    input_path = save_upload(file)
    job_id = new_job()

    def run():
        try:
            original_size = c.get_size_mb(input_path)
            duration = c.get_video_duration(input_path)
            size_tag = str(int(target_mb)) if target_mb == int(target_mb) else str(target_mb).replace(".", "_")
            output_path = c.unique_path(c.COMPRESSED_VIDEO_FOLDER, f"{c.clean_stem(input_path)}_under{size_tag}MB.mp4")

            def on_progress(fraction, desc=""):
                update_job(job_id, progress=fraction, desc=desc)

            update_job(job_id, progress=0.02, desc="Analyzing video...")
            result = c.compress_video_to_target_size(input_path, output_path, target_mb, progress_callback=on_progress)

            if not result.get("success"):
                fail_job(job_id, result.get("error", "Unknown error"))
                return

            update_job(job_id, progress=0.95, desc="Uploading to Cloudinary...")
            upload = c.upload_to_cloudinary(output_path, "video", c.CLOUDINARY_VIDEO_FOLDER)

            reduction = ((original_size - result["size_mb"]) / original_size * 100) if original_size > 0 else 0

            finish_job(job_id, {
                "filename": output_path.name,
                "download_url": make_download_url(output_path),
                "original_size_mb": round(original_size, 2),
                "final_size_mb": result["size_mb"],
                "duration": c.format_time(duration),
                "reduction_pct": round(reduction, 1),
                "video_kbps": result["video_kbps"],
                "audio_kbps": result["audio_kbps"],
                "original_resolution": result["original_resolution"],
                "output_resolution": result["output_resolution"],
                "attempts": result["attempts"],
                "cloud_url": upload.get("url"),
                "cloud_error": None if upload.get("success") else upload.get("error"),
            })
        except Exception as e:
            fail_job(job_id, str(e))
        finally:
            try:
                input_path.unlink(missing_ok=True)
            except Exception:
                pass

    threading.Thread(target=run, daemon=True).start()
    return {"job_id": job_id}


# ==============================
# SINGLE IMAGE
# ==============================

@app.post("/api/compress/image")
async def compress_image(file: UploadFile = File(...), size_choice: str = Form(...), custom_kb: float | None = Form(None)):
    target_kb = resolve_target(size_choice, custom_kb, c.IMAGE_SIZE_PRESETS, "KB")
    input_path = save_upload(file)
    job_id = new_job()

    def run():
        try:
            original_size = c.get_size_kb(input_path)
            update_job(job_id, progress=0.2, desc="Compressing image...")

            size_tag = str(int(target_kb)) if target_kb == int(target_kb) else str(target_kb).replace(".", "_")
            out_format = c.detect_output_image_format(input_path)
            ext = ".webp" if out_format == "WEBP" else ".jpg"
            output_path = c.unique_path(c.COMPRESSED_IMAGE_FOLDER, f"{c.clean_stem(input_path)}_under{size_tag}KB{ext}")

            result = c.compress_image_to_target_size(input_path, output_path, target_kb)

            if not result.get("success"):
                fail_job(job_id, result.get("error", "Unknown error"))
                return

            update_job(job_id, progress=0.9, desc="Uploading to Cloudinary...")
            upload = c.upload_to_cloudinary(output_path, "image", c.CLOUDINARY_IMAGE_FOLDER)

            reduction = ((original_size - result["size_kb"]) / original_size * 100) if original_size > 0 else 0

            finish_job(job_id, {
                "filename": output_path.name,
                "download_url": make_download_url(output_path),
                "original_size_kb": round(original_size, 2),
                "final_size_kb": result["size_kb"],
                "reduction_pct": round(reduction, 1),
                "format": result["format"],
                "quality": result["quality"],
                "original_resolution": result["original_resolution"],
                "output_resolution": result["output_resolution"],
                "cloud_url": upload.get("url"),
                "cloud_error": None if upload.get("success") else upload.get("error"),
            })
        except Exception as e:
            fail_job(job_id, str(e))
        finally:
            try:
                input_path.unlink(missing_ok=True)
            except Exception:
                pass

    threading.Thread(target=run, daemon=True).start()
    return {"job_id": job_id}


# ==============================
# BATCH VIDEO / IMAGE
# ==============================

@app.post("/api/compress/video/batch")
async def compress_video_batch(files: list[UploadFile] = File(...), size_choice: str = Form(...), custom_mb: float | None = Form(None)):
    if len(files) > c.MAX_BATCH_FILES:
        raise HTTPException(400, f"Upload {c.MAX_BATCH_FILES} videos or fewer at a time (got {len(files)}).")
    target_mb = resolve_target(size_choice, custom_mb, c.VIDEO_SIZE_PRESETS, "MB")
    input_paths = [save_upload(f) for f in files]
    job_id = new_job()

    def run():
        from concurrent.futures import ThreadPoolExecutor, as_completed
        total = len(input_paths)
        results = [None] * total
        try:
            with ThreadPoolExecutor(max_workers=c.BATCH_VIDEO_MAX_WORKERS) as executor:
                future_to_index = {
                    executor.submit(c.batch_compress_one_video, p, target_mb): idx
                    for idx, p in enumerate(input_paths)
                }
                completed = 0
                for future in as_completed(future_to_index):
                    idx = future_to_index[future]
                    results[idx] = future.result()
                    completed += 1
                    update_job(job_id, progress=completed / total, desc=f"Processed {completed}/{total} videos")

            output_files = [r["output_path"] for r in results if r["success"]]
            zip_path = None
            if output_files:
                manifest_path = c.build_links_manifest(results)
                zip_path = c.create_zip(
                    f"batch_videos_{uuid.uuid4().hex[:8]}.zip",
                    output_files + ([manifest_path] if manifest_path else []),
                )

            finish_job(job_id, {
                "files": results,
                "success_count": sum(1 for r in results if r["success"]),
                "failed_count": sum(1 for r in results if not r["success"]),
                "zip_download_url": make_download_url(zip_path) if zip_path else None,
            })
        except Exception as e:
            fail_job(job_id, str(e))
        finally:
            for p in input_paths:
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    pass

    threading.Thread(target=run, daemon=True).start()
    return {"job_id": job_id}


@app.post("/api/compress/image/batch")
async def compress_image_batch(files: list[UploadFile] = File(...), size_choice: str = Form(...), custom_kb: float | None = Form(None)):
    if len(files) > c.MAX_BATCH_FILES:
        raise HTTPException(400, f"Upload {c.MAX_BATCH_FILES} images or fewer at a time (got {len(files)}).")
    target_kb = resolve_target(size_choice, custom_kb, c.IMAGE_SIZE_PRESETS, "KB")
    input_paths = [save_upload(f) for f in files]
    job_id = new_job()

    def run():
        from concurrent.futures import ThreadPoolExecutor, as_completed
        total = len(input_paths)
        results = [None] * total
        try:
            with ThreadPoolExecutor(max_workers=c.BATCH_IMAGE_MAX_WORKERS) as executor:
                future_to_index = {
                    executor.submit(c.batch_compress_one_image, p, target_kb): idx
                    for idx, p in enumerate(input_paths)
                }
                completed = 0
                for future in as_completed(future_to_index):
                    idx = future_to_index[future]
                    results[idx] = future.result()
                    completed += 1
                    update_job(job_id, progress=completed / total, desc=f"Processed {completed}/{total} images")

            output_files = [r["output_path"] for r in results if r["success"]]
            zip_path = None
            if output_files:
                manifest_path = c.build_links_manifest(results)
                zip_path = c.create_zip(
                    f"batch_images_{uuid.uuid4().hex[:8]}.zip",
                    output_files + ([manifest_path] if manifest_path else []),
                )

            finish_job(job_id, {
                "files": results,
                "success_count": sum(1 for r in results if r["success"]),
                "failed_count": sum(1 for r in results if not r["success"]),
                "zip_download_url": make_download_url(zip_path) if zip_path else None,
            })
        except Exception as e:
            fail_job(job_id, str(e))
        finally:
            for p in input_paths:
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    pass

    threading.Thread(target=run, daemon=True).start()
    return {"job_id": job_id}


# ==============================
# JOB STATUS / DOWNLOAD / LIBRARY
# ==============================

@app.get("/api/jobs/{job_id}")
async def get_job_route(job_id: str):
    job = load_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    return job


ALLOWED_DOWNLOAD_ROOTS = [
    str(c.COMPRESSED_VIDEO_FOLDER.resolve()),
    str(c.COMPRESSED_IMAGE_FOLDER.resolve()),
    os.path.realpath(__import__("tempfile").gettempdir()),
]


@app.get("/api/download")
async def download(path: str):
    resolved = str(Path(path).resolve())
    if not any(resolved.startswith(root) for root in ALLOWED_DOWNLOAD_ROOTS):
        raise HTTPException(403, "Not allowed.")
    if not os.path.exists(resolved):
        raise HTTPException(404, "File not found.")
    return FileResponse(resolved, filename=Path(resolved).name)


@app.get("/api/library")
async def library():
    return JSONResponse({
        "configured": c.CLOUDINARY_CONFIGURED,
        "items": c.fetch_cloud_library(),
    })


@app.get("/api/config")
async def config():
    return {
        "cloudinary_configured": c.CLOUDINARY_CONFIGURED,
        "max_batch_files": c.MAX_BATCH_FILES,
    }


# ==============================
# STATIC FRONTEND
# ==============================

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 7860))
    print("=" * 70)
    print("VIDEO & IMAGE COMPRESSOR (FastAPI) - STARTED")
    print("=" * 70)
    print(f"  Compressed Videos: {c.COMPRESSED_VIDEO_FOLDER}")
    print(f"  Compressed Images: {c.COMPRESSED_IMAGE_FOLDER}")
    print(f"  Cloudinary configured: {c.CLOUDINARY_CONFIGURED}")
    print(f"  Listening on 0.0.0.0:{port}")
    print("=" * 70)

    uvicorn.run(app, host="0.0.0.0", port=port)
