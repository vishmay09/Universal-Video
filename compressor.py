"""
Core video/image compression engine - no web framework dependency.

Everything here is plain, synchronous Python: ffmpeg/PIL calls, Cloudinary
uploads, batch workers. It's used by app.py (the FastAPI backend), and has
no knowledge of HTTP, jobs, or any UI. This split exists because the actual
compression logic has been solid and heavily tested throughout development;
the problems were always in the web-framework layer on top of it (Gradio's
dependency churn, its frozen progress bars, its API-schema crashes) - so
this module is kept completely framework-agnostic on purpose.
"""

import os
import io
import json
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path

from PIL import Image

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import cloudinary
import cloudinary.uploader
import cloudinary.api

# ==============================
# FFMPEG / FFPROBE LOCATION
# ==============================
# Prefer a bundled copy in ./ffmpeg_bin (used when ffmpeg isn't installed
# system-wide / isn't on PATH) or an FFMPEG_BIN_DIR env var pointing at one.
# Falls back to plain "ffmpeg"/"ffprobe" so a system-wide install still
# works unchanged.

def _resolve_binary(name):
    env_dir = os.environ.get("FFMPEG_BIN_DIR")
    candidates = []

    if env_dir:
        candidates.append(Path(env_dir) / f"{name}.exe")
        candidates.append(Path(env_dir) / name)

    local_bin = Path(__file__).resolve().parent / "ffmpeg_bin"
    candidates.append(local_bin / f"{name}.exe")
    candidates.append(local_bin / name)

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    found = shutil.which(name)
    if found:
        return found

    return name


FFMPEG_BIN = _resolve_binary("ffmpeg")
FFPROBE_BIN = _resolve_binary("ffprobe")
NULL_DEVICE = "NUL" if os.name == "nt" else "/dev/null"

# subprocess.run(..., text=True) with no encoding falls back to the OS's
# default codepage (cp1252 on Windows), which raises UnicodeDecodeError on
# some bytes ffmpeg/ffprobe can emit. Always decode as UTF-8 and drop
# anything that still doesn't fit, instead of crashing.
SUBPROCESS_TEXT_KWARGS = {"text": True, "encoding": "utf-8", "errors": "ignore"}

# ==============================
# FOLDERS
# ==============================
BASE_DIR = Path("video_processing_workspace")
COMPRESSED_VIDEO_FOLDER = BASE_DIR / "Compressed_Videos"
COMPRESSED_IMAGE_FOLDER = BASE_DIR / "Compressed_Images"
LOGS_FOLDER = BASE_DIR / "Logs"
UPLOAD_FOLDER = BASE_DIR / "Uploads"

for folder in [COMPRESSED_VIDEO_FOLDER, COMPRESSED_IMAGE_FOLDER, LOGS_FOLDER, UPLOAD_FOLDER]:
    folder.mkdir(parents=True, exist_ok=True)

# ==============================
# CONFIGURATION
# ==============================

# x264 "slow"/"medium" give better quality-per-bit but use meaningfully
# more memory (larger lookahead/reference-frame buffers) and CPU than
# "veryfast" - on a free-tier host with a hard ~512MB RAM ceiling shared by
# the OS, Python, and the ffmpeg subprocess together, that's the difference
# between "completes" and "the whole container gets OOM-killed mid-encode"
# (confirmed happening even with only ONE encode running at a time - this
# host's ceiling is lower than "faster" alone left room for). Quality loss
# going to "veryfast" is real but reliability on a genuinely
# memory-constrained host is not something quality can trade for.
VIDEO_ENCODE_PRESET = "veryfast"

# Explicit caps on top of the preset: fewer reference frames and no
# B-frames means less frame-buffer memory held at once, independent of
# whatever the preset's own defaults would pick.
VIDEO_ENCODE_EXTRA_ARGS = ["-refs", "1", "-bf", "0", "-g", "50"]

# Independent of the bitrate-driven output resolution below: decoding a
# very large source (e.g. 4K phone video) is itself memory-heavy regardless
# of what resolution it gets encoded back out at, so very large sources are
# always pre-capped before any encoding is attempted. Lowered from 1920 to
# 1280 after 1920 still wasn't enough to avoid OOM kills on this host for a
# single, non-concurrent encode.
MAX_SAFE_DECODE_HEIGHT = 1280

# Bounds ffmpeg's own worker-thread count. Left unset, ffmpeg sizes its
# thread pool off the container's *reported* CPU count, which on a shared
# host can be higher than what it's actually allotted - more threads each
# holding their own encode buffers is more peak memory, not just more CPU.
FFMPEG_THREAD_LIMIT = "1"

# Below this many MB of available system memory, don't even attempt an
# encode - fail with a clear, immediate message instead of risking an
# OOM kill that takes down the entire container (losing ALL in-flight
# jobs, not just this one, and showing up to users as an opaque "Job not
# found" after the fact instead of a real reason). Linux-only (reads
# /proc/meminfo, what the container actually runs on); silently skipped
# elsewhere since there's nothing equivalent to check cheaply.
MIN_SAFE_MEMORY_MB = 150


def get_available_memory_mb():
    """Returns available system memory in MB, or None if it can't be
    determined (non-Linux, or /proc/meminfo unreadable) - callers must
    treat None as "unknown", not "zero"."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024
    except Exception:
        pass
    return None

VIDEO_SIZE_PRESETS = {
    "10mb": 10,
    "5mb": 5,
}

IMAGE_SIZE_PRESETS = {
    "500kb": 500,
    "200kb": 200,
}

# Batch processing runs files through a bounded worker pool rather than
# launching all of them at once - true unlimited parallelism (e.g. 100
# simultaneous 2-pass ffmpeg encodes) would thrash CPU/RAM on any machine
# and risk failures. Image compression is light (pure PIL, no subprocess)
# so it gets a real pool; video is capped at 1 (see VIDEO_ENCODE_SEMAPHORE
# below) regardless of CPU count, since it's memory, not just CPU, that's
# the constraint on a free-tier host.
BATCH_VIDEO_MAX_WORKERS = 1
BATCH_IMAGE_MAX_WORKERS = max(1, min(8, (os.cpu_count() or 4) * 2))
MAX_BATCH_FILES = 100

# Hard global cap: only one ffmpeg 2-pass video encode runs at a time,
# process-wide, regardless of which code path asked for it (the single-
# video API, or a batch worker thread). This is the actual reason a
# hand-written thread-per-request backend can be less reliable than a
# Gradio app on the exact same host and hardware: Gradio's own queue
# serializes heavy requests by default, so it was never running more than
# one encode at once even without anyone asking for that - our own
# threading had no such limit, so multiple concurrent requests (a retry,
# a double-click, a batch job overlapping a single-video one) could stack
# up several ffmpeg processes at once, each with its own decode/encode
# buffers - on a host with a hard ~512MB ceiling, that's a reliable way to
# get OOM-killed even when any *one* of them alone would have fit.
VIDEO_ENCODE_SEMAPHORE = threading.Semaphore(1)

# ==============================
# CLOUDINARY (PERSISTENT STORAGE)
# ==============================
# Render's (and most container hosts') filesystem is ephemeral - anything
# written locally is wiped on every restart or redeploy. Cloudinary gives
# every compressed file a permanent, shareable URL that survives that, and
# doubles as the "database" the Cloud Library reads back from.
# Credentials come from environment variables only - never hardcode them.

CLOUDINARY_CLOUD_NAME = os.environ.get("CLOUDINARY_CLOUD_NAME", "")
CLOUDINARY_API_KEY = os.environ.get("CLOUDINARY_API_KEY", "")
CLOUDINARY_API_SECRET = os.environ.get("CLOUDINARY_API_SECRET", "")

CLOUDINARY_CONFIGURED = bool(
    CLOUDINARY_CLOUD_NAME and CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET
)

if CLOUDINARY_CONFIGURED:
    cloudinary.config(
        cloud_name=CLOUDINARY_CLOUD_NAME,
        api_key=CLOUDINARY_API_KEY,
        api_secret=CLOUDINARY_API_SECRET,
        secure=True,
    )

CLOUDINARY_VIDEO_FOLDER = "video_image_compressor/videos"
CLOUDINARY_IMAGE_FOLDER = "video_image_compressor/images"


def upload_to_cloudinary(file_path, resource_type, folder):
    """
    Uploads a compressed file to Cloudinary for permanent storage. Returns
    {success, url, public_id, error}. Never raises - a failed/absent upload
    just means the file stays local-only for this session, it never blocks
    the compression result itself.
    """
    if not CLOUDINARY_CONFIGURED:
        return {"success": False, "url": None, "public_id": None,
                "error": "Cloudinary not configured (missing env vars)."}

    if not file_path or not os.path.exists(file_path):
        return {"success": False, "url": None, "public_id": None, "error": "File not found."}

    try:
        if resource_type == "video":
            result = cloudinary.uploader.upload_large(
                str(file_path), resource_type="video", folder=folder,
                use_filename=True, unique_filename=True, overwrite=False,
            )
        else:
            result = cloudinary.uploader.upload(
                str(file_path), resource_type="image", folder=folder,
                use_filename=True, unique_filename=True, overwrite=False,
            )
        return {
            "success": True,
            "url": result.get("secure_url"),
            "public_id": result.get("public_id"),
            "error": "",
        }
    except Exception as e:
        return {"success": False, "url": None, "public_id": None, "error": str(e)}


def fetch_cloud_library(max_results=30):
    """
    Lists the most recently uploaded videos and images from Cloudinary.
    Returns a list of {type, filename, size_bytes, created_at, url} dicts,
    newest first. Never raises - returns [] with nothing on any failure
    (including "not configured"); callers check CLOUDINARY_CONFIGURED
    separately for a more specific message.
    """
    if not CLOUDINARY_CONFIGURED:
        return []

    rows = []
    try:
        for resource_type, folder, label in [
            ("video", CLOUDINARY_VIDEO_FOLDER, "video"),
            ("image", CLOUDINARY_IMAGE_FOLDER, "image"),
        ]:
            result = cloudinary.api.resources(
                resource_type=resource_type,
                type="upload",
                prefix=folder,
                max_results=max_results,
                direction="desc",
            )
            for res in result.get("resources", []):
                rows.append({
                    "type": label,
                    "filename": Path(res.get("public_id", "")).name,
                    "size_bytes": res.get("bytes", 0),
                    "created_at": res.get("created_at", ""),
                    "url": res.get("secure_url", ""),
                })
    except Exception:
        return []

    rows.sort(key=lambda r: r["created_at"], reverse=True)
    return rows


# ==============================
# HELPERS
# ==============================

def safe_log(message, log_type="INFO"):
    """Safely log messages to file with UTF-8 encoding"""
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_file = LOGS_FOLDER / f"process_{datetime.now().strftime('%Y%m%d')}.log"
        with open(log_file, "a", encoding="utf-8") as f:
            clean_message = message.encode("utf-8", "ignore").decode("utf-8")
            f.write(f"[{timestamp}] [{log_type}] {clean_message}\n")
    except Exception as e:
        print(f"Logging error: {str(e)}")


def get_size_mb(path):
    if path and os.path.exists(path):
        return os.path.getsize(path) / (1024 * 1024)
    return 0


def get_size_kb(path):
    if path and os.path.exists(path):
        return os.path.getsize(path) / 1024
    return 0


def format_time(seconds):
    mins = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{mins:02d}:{secs:02d}"


def unique_path(folder, filename):
    """Prevent accidental overwrite by appending _1, _2, ... if needed."""
    path = Path(folder) / filename
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    counter = 1
    while True:
        candidate = Path(folder) / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def clean_stem(path):
    name = Path(path).stem.replace(" ", "_")
    cleaned = "".join(c for c in name if c.isalnum() or c in ("-", "_"))
    return cleaned or "file"


def create_zip(zip_name, file_paths):
    """Zips the given files into the system temp folder and returns its path.
    Missing/None paths are skipped so one bad entry can't break the whole zip."""
    zip_path = Path(tempfile.gettempdir()) / zip_name
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        used_names = set()
        for file_path in file_paths:
            if not file_path or not os.path.exists(file_path):
                continue
            arcname = Path(file_path).name
            base_arcname = arcname
            counter = 1
            while arcname in used_names:
                stem = Path(base_arcname).stem
                suffix = Path(base_arcname).suffix
                arcname = f"{stem}_{counter}{suffix}"
                counter += 1
            used_names.add(arcname)
            zf.write(file_path, arcname=arcname)
    return str(zip_path)


def build_links_manifest(results):
    """
    Writes a plain-text manifest of each batch file's permanent Cloudinary
    URL (when it has one) to a temp file, to be bundled into the ZIP.
    Returns None if there's nothing to write.
    """
    lines = [f"{r['name']} -> {r['cloud_url']}" for r in results if r.get("cloud_url")]
    if not lines:
        return None

    manifest_path = Path(tempfile.gettempdir()) / f"cloudinary_links_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(manifest_path)


def get_video_duration(video_path):
    """Get video duration in seconds via ffprobe"""
    try:
        cmd = [
            FFPROBE_BIN, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video_path)
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=10, **SUBPROCESS_TEXT_KWARGS)
        duration = float(result.stdout.strip()) if result.stdout.strip() else 0
        return duration
    except Exception as e:
        safe_log(f"Get video duration error: {str(e)}", "ERROR")
        return 0


def get_video_resolution(video_path):
    """Get video width/height via ffprobe. Returns (0, 0) on failure."""
    try:
        cmd = [
            FFPROBE_BIN, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "json",
            str(video_path)
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=10, **SUBPROCESS_TEXT_KWARGS)
        data = json.loads(result.stdout)
        stream = data["streams"][0]
        return int(stream["width"]), int(stream["height"])
    except Exception as e:
        safe_log(f"Get video resolution error: {str(e)}", "ERROR")
        return 0, 0


# ==============================
# VIDEO COMPRESSION (2-PASS, TARGET SIZE)
# ==============================
# Two-pass VBR encoding hits a target file size far more accurately than a
# single CRF pass, because the bitrate is derived directly from the target
# size and duration instead of guessed.

def estimate_target_bitrates(duration, target_size_mb):
    """Work out video/audio bitrates (kbps) that should land the encode
    under target_size_mb for the given duration (seconds)."""
    total_kbps = (target_size_mb * 8192 * 0.96) / max(duration, 0.1)

    if target_size_mb <= 3:
        audio_kbps = 48
    elif target_size_mb <= 6:
        audio_kbps = 64
    elif target_size_mb <= 12:
        audio_kbps = 96
    else:
        audio_kbps = 128

    video_kbps = int(total_kbps - audio_kbps)
    video_kbps = max(video_kbps, 80)
    return video_kbps, audio_kbps


def pick_max_height(video_kbps):
    """Cap resolution based on available bitrate so quality stays watchable
    instead of a high-res video turning into a blocky mess at a low bitrate."""
    if video_kbps >= 1200:
        return None
    elif video_kbps >= 700:
        return 720
    elif video_kbps >= 400:
        return 480
    elif video_kbps >= 200:
        return 360
    else:
        return 240


def cleanup_passlog(passlog_base):
    """Remove ffmpeg's 2-pass log files (not needed after encoding)."""
    for suffix in ["-0.log", "-0.log.mbtree", "-0.log.temp", "-0.log.mbtree.temp"]:
        p = Path(str(passlog_base) + suffix)
        try:
            if p.exists():
                p.unlink()
        except Exception:
            pass


def run_ffmpeg_with_progress(cmd, duration, progress_start, progress_span, progress_callback, desc_prefix,
                              stall_timeout=180, hard_timeout=1800):
    """
    Runs an ffmpeg command (which must include "-progress pipe:1") and
    reports live sub-progress with elapsed time. stderr is merged into the
    same pipe as stdout (not read separately) to avoid a classic subprocess
    deadlock: ffmpeg writes a lot of its own log output to stderr, and if
    that pipe fills up while we're only draining stdout, ffmpeg blocks
    trying to write to it - which would also silently stop the progress
    lines we're waiting for.

    Two independent safety nets, because "for line in process.stdout" has
    NO timeout of its own:
      - stall_timeout: killed if out_time (real encoding progress) hasn't
        actually advanced for this long. This deliberately does NOT reset
        on every line - ffmpeg can keep emitting periodic "-progress"
        heartbeat lines even while genuinely stuck (e.g. a filter/codec
        issue with a specific input file), which would otherwise mask a
        real hang forever behind an elapsed counter that looks alive.
      - hard_timeout: killed if the whole pass runs longer than this
        regardless of whether it's still progressing.

    Returns (returncode, last ~4000 chars of output for error reporting).
    A returncode of -1 means this function killed the process itself.
    """
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="ignore",
        bufsize=1,
    )

    output_tail = []
    out_time_seconds = 0.0
    last_progress_out_time = -1.0
    start_time = time.monotonic()
    stall_state = {"killed": False}

    def kill_for_stall():
        stall_state["killed"] = True
        try:
            process.kill()
        except Exception:
            pass

    watchdog = threading.Timer(stall_timeout, kill_for_stall)
    watchdog.daemon = True
    watchdog.start()

    hard_timeout_hit = False

    try:
        for line in process.stdout:
            output_tail.append(line)
            if len(output_tail) > 200:
                output_tail.pop(0)

            stripped = line.strip()
            if stripped.startswith("out_time_ms="):
                try:
                    out_time_seconds = int(stripped.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    pass

            # Only reset the stall watchdog when out_time has genuinely
            # advanced, not on every heartbeat line - see docstring.
            if out_time_seconds > last_progress_out_time:
                last_progress_out_time = out_time_seconds
                watchdog.cancel()
                watchdog = threading.Timer(stall_timeout, kill_for_stall)
                watchdog.daemon = True
                watchdog.start()

            elapsed = time.monotonic() - start_time

            if progress_callback and duration > 0:
                frac = min(out_time_seconds / duration, 1.0)
                try:
                    progress_callback(
                        progress_start + progress_span * frac,
                        desc=f"{desc_prefix} ({frac * 100:.1f}%, {int(elapsed)}s elapsed / ~{int(duration)}s total)",
                    )
                except Exception:
                    pass

            if elapsed > hard_timeout:
                hard_timeout_hit = True
                try:
                    process.kill()
                except Exception:
                    pass
                break
    finally:
        watchdog.cancel()

    process.wait(timeout=30)
    tail_text = "".join(output_tail)[-4000:]

    if stall_state["killed"]:
        return -1, f"ffmpeg produced no progress for {stall_timeout}s and was stopped (stalled). {tail_text}"
    if hard_timeout_hit:
        return -1, f"ffmpeg exceeded the {hard_timeout}s time limit and was stopped. {tail_text}"

    return process.returncode, tail_text


def compress_video_to_target_size(input_path, output_path, target_size_mb, progress_callback=None):
    """
    Compresses input_path to output_path so the final file lands at or
    under target_size_mb, using 2-pass H.264 encoding for the best quality
    the size budget allows. Automatically downscales resolution when the
    available bitrate is too low for the source resolution, and retries
    with a tighter bitrate if the first attempt overshoots the target.

    Returns a dict: {success, size_mb, video_kbps, audio_kbps, attempts,
    original_resolution, output_resolution, error}.
    """
    input_path = Path(input_path)
    output_path = Path(output_path)

    duration = get_video_duration(input_path)
    if duration <= 0:
        return {"success": False, "error": "Could not read video duration."}

    orig_w, orig_h = get_video_resolution(input_path)

    # Wait for exclusive access to the encoder before doing anything heavy -
    # see VIDEO_ENCODE_SEMAPHORE for why this matters. If another encode is
    # already running, this blocks here (no ffmpeg process exists yet, so
    # nothing to time out) rather than starting a second one alongside it.
    if progress_callback and VIDEO_ENCODE_SEMAPHORE._value < 1:
        try:
            progress_callback(0.0, desc="Waiting for another video to finish encoding first...")
        except Exception:
            pass

    with VIDEO_ENCODE_SEMAPHORE:
        # Checked here (after acquiring the lock, right before actually
        # starting ffmpeg) rather than before waiting, since memory can free
        # up while queued behind another job. A None result means "couldn't
        # determine" (e.g. not running on Linux) - proceed as before rather
        # than blocking on an unknown.
        available_mb = get_available_memory_mb()
        if available_mb is not None and available_mb < MIN_SAFE_MEMORY_MB:
            safe_log(
                f"Refusing to start video encode: only {available_mb:.0f}MB available "
                f"(need at least {MIN_SAFE_MEMORY_MB}MB)",
                "ERROR",
            )
            return {
                "success": False,
                "error": (
                    f"Not enough server memory available right now ({available_mb:.0f}MB free) "
                    "to safely start encoding - this avoids risking a full server restart. "
                    "Please try again in a moment."
                ),
            }

        passlog_base = Path(os.environ.get("TEMP", "/tmp")) / f"ff2pass_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"

        attempt_target = target_size_mb
        max_attempts = 3

        try:
            for attempt in range(1, max_attempts + 1):
                video_kbps, audio_kbps = estimate_target_bitrates(duration, attempt_target)

                max_h = pick_max_height(video_kbps)
                # Whichever cap is smaller wins - the bitrate-driven one
                # (quality reasoning) or the hard decode-memory safety cap
                # (reliability reasoning, independent of bitrate).
                if orig_h and orig_h > MAX_SAFE_DECODE_HEIGHT:
                    max_h = min(max_h, MAX_SAFE_DECODE_HEIGHT) if max_h else MAX_SAFE_DECODE_HEIGHT

                vf_parts = []
                if max_h and orig_h and max_h < orig_h:
                    vf_parts.append(f"scale=-2:{max_h}")
                vf_parts.append("format=yuv420p")
                vf_filter = ",".join(vf_parts)

                attempt_start = 0.30 * (attempt - 1)

                pass1_cmd = [
                    FFMPEG_BIN, "-y", "-progress", "pipe:1", "-nostats",
                    "-threads", FFMPEG_THREAD_LIMIT,
                    "-i", str(input_path),
                    "-vf", vf_filter,
                    "-c:v", "libx264", "-preset", VIDEO_ENCODE_PRESET,
                    *VIDEO_ENCODE_EXTRA_ARGS,
                    "-b:v", f"{video_kbps}k",
                    "-pass", "1", "-passlogfile", str(passlog_base),
                    "-an", "-f", "null", NULL_DEVICE,
                ]
                code1, tail1 = run_ffmpeg_with_progress(
                    pass1_cmd, duration, attempt_start + 0.02, 0.13, progress_callback,
                    f"Pass 1/2 (attempt {attempt})",
                )

                if code1 != 0:
                    return {"success": False, "error": f"Pass 1 failed: {tail1}"}

                pass2_cmd = [
                    FFMPEG_BIN, "-y", "-progress", "pipe:1", "-nostats",
                    "-threads", FFMPEG_THREAD_LIMIT,
                    "-i", str(input_path),
                    "-vf", vf_filter,
                    "-c:v", "libx264", "-preset", VIDEO_ENCODE_PRESET,
                    *VIDEO_ENCODE_EXTRA_ARGS,
                    "-b:v", f"{video_kbps}k",
                    "-pass", "2", "-passlogfile", str(passlog_base),
                    "-c:a", "aac", "-b:a", f"{audio_kbps}k", "-ar", "48000",
                    "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart",
                    str(output_path),
                ]
                code2, tail2 = run_ffmpeg_with_progress(
                    pass2_cmd, duration, attempt_start + 0.16, 0.13, progress_callback,
                    f"Pass 2/2 (attempt {attempt})",
                )

                if code2 != 0 or not output_path.exists():
                    return {"success": False, "error": f"Pass 2 failed: {tail2}"}

                final_size = get_size_mb(output_path)

                if final_size <= target_size_mb * 1.02 or attempt == max_attempts:
                    out_w, out_h = get_video_resolution(output_path)
                    safe_log(
                        f"Compressed {input_path.name} -> {final_size:.2f} MB "
                        f"(target {target_size_mb} MB, attempt {attempt})",
                        "SUCCESS",
                    )
                    return {
                        "success": True,
                        "size_mb": round(final_size, 2),
                        "video_kbps": video_kbps,
                        "audio_kbps": audio_kbps,
                        "attempts": attempt,
                        "original_resolution": f"{orig_w}x{orig_h}" if orig_w else "unknown",
                        "output_resolution": f"{out_w}x{out_h}" if out_w else "unknown",
                    }

                safe_log(
                    f"Compression attempt {attempt} overshot target "
                    f"({final_size:.2f} MB > {target_size_mb} MB), retrying tighter",
                    "INFO",
                )
                attempt_target = attempt_target * (target_size_mb / final_size) * 0.95

            return {"success": False, "error": "Could not converge under the target size."}

        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Compression timed out."}
        except Exception as e:
            safe_log(f"Compression error: {str(e)}", "ERROR")
            return {"success": False, "error": str(e)}
        finally:
            cleanup_passlog(passlog_base)


# ==============================
# IMAGE COMPRESSION (TARGET SIZE)
# ==============================
# Binary-searches the JPEG/WEBP quality setting to find the highest quality
# that still fits under the target size, and only downscales resolution as
# a last resort if even minimum quality can't hit the target at full size.

def detect_output_image_format(input_path):
    """Returns 'WEBP' if the image has transparency (to preserve alpha),
    otherwise 'JPEG'."""
    try:
        with Image.open(input_path) as img:
            has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
            return "WEBP" if has_alpha else "JPEG"
    except Exception:
        return "JPEG"


def compress_image_to_target_size(input_path, output_path, target_size_kb):
    """
    Compresses input_path to output_path so the final file lands at or
    under target_size_kb. Uses WEBP for images with transparency, JPEG
    otherwise. Returns a dict: {success, size_kb, quality, format,
    original_resolution, output_resolution, error}.
    """
    try:
        input_path = Path(input_path)
        img = Image.open(input_path)
        img.load()

        has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
        out_format = "WEBP" if has_alpha else "JPEG"

        if out_format == "JPEG" and img.mode != "RGB":
            img = img.convert("RGB")
        elif out_format == "WEBP" and img.mode not in ("RGBA", "RGB"):
            img = img.convert("RGBA")

        target_bytes = int(target_size_kb * 1024)
        original_w, original_h = img.size

        working_img = img
        best_data = None
        best_quality = None

        for _ in range(6):
            lo, hi = 5, 95
            found = None

            while lo <= hi:
                mid = (lo + hi) // 2
                buf = io.BytesIO()
                save_kwargs = {"quality": mid, "optimize": True}
                if out_format == "WEBP":
                    save_kwargs["method"] = 6
                working_img.save(buf, format=out_format, **save_kwargs)
                size = buf.tell()

                if size <= target_bytes:
                    found = (mid, buf.getvalue())
                    lo = mid + 1
                else:
                    hi = mid - 1

            if found:
                best_quality, best_data = found
                break

            w, h = working_img.size
            new_w, new_h = max(int(w * 0.85), 32), max(int(h * 0.85), 32)
            if (new_w, new_h) == (w, h):
                break
            working_img = working_img.resize((new_w, new_h), Image.LANCZOS)

        if best_data is None:
            buf = io.BytesIO()
            working_img.save(buf, format=out_format, quality=5, optimize=True)
            best_data = buf.getvalue()
            best_quality = 5

        with open(output_path, "wb") as f:
            f.write(best_data)

        safe_log(
            f"Compressed image {input_path.name} -> {len(best_data)/1024:.2f} KB "
            f"(target {target_size_kb} KB, quality {best_quality})",
            "SUCCESS",
        )

        return {
            "success": True,
            "size_kb": round(len(best_data) / 1024, 2),
            "quality": best_quality,
            "format": out_format,
            "original_resolution": f"{original_w}x{original_h}",
            "output_resolution": f"{working_img.size[0]}x{working_img.size[1]}",
        }

    except Exception as e:
        safe_log(f"Image compression error: {str(e)}", "ERROR")
        return {"success": False, "error": str(e)}


# ==============================
# BATCH WORKERS (single file, safe to run in a thread pool)
# ==============================

def batch_compress_one_video(input_path, target_mb):
    """
    Compresses one video for a batch job, or - if it's already at/under the
    target size - copies it through unchanged instead of re-encoding it.
    Never raises: any failure is captured and returned in the result dict.
    """
    input_path = Path(input_path)
    name = input_path.name

    try:
        original_size = get_size_mb(input_path)

        if original_size <= target_mb:
            out_path = unique_path(COMPRESSED_VIDEO_FOLDER, f"{clean_stem(input_path)}{input_path.suffix}")
            shutil.copy2(input_path, out_path)
            upload = upload_to_cloudinary(out_path, "video", CLOUDINARY_VIDEO_FOLDER)
            return {
                "name": name, "success": True, "action": "copied (already under target)",
                "original_size_mb": round(original_size, 2), "final_size_mb": round(original_size, 2),
                "output_path": str(out_path), "cloud_url": upload.get("url"), "error": "",
            }

        size_tag = str(int(target_mb)) if target_mb == int(target_mb) else str(target_mb).replace(".", "_")
        out_path = unique_path(COMPRESSED_VIDEO_FOLDER, f"{clean_stem(input_path)}_under{size_tag}MB.mp4")
        result = compress_video_to_target_size(input_path, out_path, target_mb)

        if result.get("success"):
            upload = upload_to_cloudinary(out_path, "video", CLOUDINARY_VIDEO_FOLDER)
            return {
                "name": name, "success": True, "action": "compressed",
                "original_size_mb": round(original_size, 2), "final_size_mb": result["size_mb"],
                "output_path": str(out_path), "cloud_url": upload.get("url"), "error": "",
            }

        return {
            "name": name, "success": False, "action": "failed",
            "original_size_mb": round(original_size, 2), "final_size_mb": None,
            "output_path": None, "cloud_url": None, "error": result.get("error", "Unknown error"),
        }

    except Exception as e:
        safe_log(f"Batch video error ({name}): {str(e)}", "ERROR")
        return {
            "name": name, "success": False, "action": "failed",
            "original_size_mb": 0, "final_size_mb": None, "output_path": None,
            "cloud_url": None, "error": str(e),
        }


def batch_compress_one_image(input_path, target_kb):
    """
    Compresses one image for a batch job, or - if it's already at/under the
    target size - copies it through unchanged instead of re-encoding it.
    Never raises: any failure is captured and returned in the result dict.
    """
    input_path = Path(input_path)
    name = input_path.name

    try:
        original_size = get_size_kb(input_path)

        if original_size <= target_kb:
            out_path = unique_path(COMPRESSED_IMAGE_FOLDER, f"{clean_stem(input_path)}{input_path.suffix}")
            shutil.copy2(input_path, out_path)
            upload = upload_to_cloudinary(out_path, "image", CLOUDINARY_IMAGE_FOLDER)
            return {
                "name": name, "success": True, "action": "copied (already under target)",
                "original_size_kb": round(original_size, 2), "final_size_kb": round(original_size, 2),
                "output_path": str(out_path), "cloud_url": upload.get("url"), "error": "",
            }

        size_tag = str(int(target_kb)) if target_kb == int(target_kb) else str(target_kb).replace(".", "_")
        out_format = detect_output_image_format(input_path)
        ext = ".webp" if out_format == "WEBP" else ".jpg"
        out_path = unique_path(COMPRESSED_IMAGE_FOLDER, f"{clean_stem(input_path)}_under{size_tag}KB{ext}")
        result = compress_image_to_target_size(input_path, out_path, target_kb)

        if result.get("success"):
            upload = upload_to_cloudinary(out_path, "image", CLOUDINARY_IMAGE_FOLDER)
            return {
                "name": name, "success": True, "action": "compressed",
                "original_size_kb": round(original_size, 2), "final_size_kb": result["size_kb"],
                "output_path": str(out_path), "cloud_url": upload.get("url"), "error": "",
            }

        return {
            "name": name, "success": False, "action": "failed",
            "original_size_kb": round(original_size, 2), "final_size_kb": None,
            "output_path": None, "cloud_url": None, "error": result.get("error", "Unknown error"),
        }

    except Exception as e:
        safe_log(f"Batch image error ({name}): {str(e)}", "ERROR")
        return {
            "name": name, "success": False, "action": "failed",
            "original_size_kb": 0, "final_size_kb": None, "output_path": None,
            "cloud_url": None, "error": str(e),
        }
