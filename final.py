import os
import io
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import gradio as gr
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

for folder in [COMPRESSED_VIDEO_FOLDER, COMPRESSED_IMAGE_FOLDER, LOGS_FOLDER]:
    folder.mkdir(parents=True, exist_ok=True)

# ==============================
# CONFIGURATION
# ==============================
VIDEO_SIZE_PRESETS = {
    "Under 10 MB": 10,
    "Under 5 MB": 5,
    "Custom (MB)": None,
}

IMAGE_SIZE_PRESETS = {
    "Under 500 KB": 500,
    "Under 200 KB": 200,
    "Custom (KB)": None,
}

# Batch processing runs files through a bounded worker pool rather than
# launching all of them at once - true unlimited parallelism (e.g. 100
# simultaneous 2-pass ffmpeg encodes) would thrash CPU/RAM on any machine
# and risk failures, defeating "without any error". Video encoding is CPU
# heavy so it gets a small pool; image compression is light so it gets more.
BATCH_VIDEO_MAX_WORKERS = max(1, min(3, os.cpu_count() or 2))
BATCH_IMAGE_MAX_WORKERS = max(1, min(8, (os.cpu_count() or 4) * 2))
MAX_BATCH_FILES = 100

# ==============================
# CLOUDINARY (PERSISTENT STORAGE)
# ==============================
# Render's (and most container hosts') filesystem is ephemeral - anything
# written locally is wiped on every restart or redeploy. Cloudinary gives
# every compressed file a permanent, shareable URL that survives that, and
# doubles as the "database" the Cloud Library tab reads back from.
# Credentials come from environment variables only - never hardcode them.
# Locally, put them in a .env file (loaded above); on Render, set them
# under the service's Environment tab as secrets.

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
    """Get file size in MB"""
    if path and os.path.exists(path):
        return os.path.getsize(path) / (1024 * 1024)
    return 0


def get_size_kb(path):
    """Get file size in KB"""
    if path and os.path.exists(path):
        return os.path.getsize(path) / 1024
    return 0


def format_time(seconds):
    """Format seconds to MM:SS format"""
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
            # Avoid collisions inside the zip if two outputs share a filename.
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
    Returns None if there's nothing to write (Cloudinary not configured,
    or every upload failed) so callers can skip it cleanly.
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
        import json
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
# size and duration instead of guessed. Pass 1 analyzes the video to build
# a bitrate distribution plan; pass 2 spends that bitrate where it's most
# visually useful, which is why 2-pass looks noticeably better than 1-pass
# at the same output size.

def estimate_target_bitrates(duration, target_size_mb):
    """Work out video/audio bitrates (kbps) that should land the encode
    under target_size_mb for the given duration (seconds)."""
    # 1 MB = 8192 kbit (binary). Reserve ~4% for container/muxing overhead
    # and encoder variance so the real output lands under the target
    # instead of right at (or just over) it.
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
    video_kbps = max(video_kbps, 80)  # floor - below this, video is unwatchable anyway
    return video_kbps, audio_kbps


def pick_max_height(video_kbps):
    """Cap resolution based on available bitrate so quality stays watchable
    instead of a high-res video turning into a blocky mess at a low bitrate."""
    if video_kbps >= 1200:
        return None  # no forced downscale
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

    passlog_base = Path(os.environ.get("TEMP", "/tmp")) / f"ff2pass_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"

    attempt_target = target_size_mb
    max_attempts = 3

    try:
        for attempt in range(1, max_attempts + 1):
            video_kbps, audio_kbps = estimate_target_bitrates(duration, attempt_target)

            vf_parts = []
            max_h = pick_max_height(video_kbps)
            if max_h and orig_h and max_h < orig_h:
                vf_parts.append(f"scale=-2:{max_h}")
            vf_parts.append("format=yuv420p")
            vf_filter = ",".join(vf_parts)

            if progress_callback:
                progress_callback(0.10 + 0.30 * (attempt - 1), desc=f"Pass 1/2 (attempt {attempt})")

            pass1_cmd = [
                FFMPEG_BIN, "-y",
                "-i", str(input_path),
                "-vf", vf_filter,
                "-c:v", "libx264", "-preset", "slow",
                "-b:v", f"{video_kbps}k",
                "-pass", "1", "-passlogfile", str(passlog_base),
                "-an", "-f", "null", NULL_DEVICE,
            ]
            r1 = subprocess.run(pass1_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 timeout=3600, **SUBPROCESS_TEXT_KWARGS)

            if r1.returncode != 0:
                return {"success": False, "error": f"Pass 1 failed: {r1.stderr[-800:]}"}

            if progress_callback:
                progress_callback(0.30 + 0.30 * (attempt - 1), desc=f"Pass 2/2 (attempt {attempt})")

            pass2_cmd = [
                FFMPEG_BIN, "-y",
                "-i", str(input_path),
                "-vf", vf_filter,
                "-c:v", "libx264", "-preset", "slow",
                "-b:v", f"{video_kbps}k",
                "-pass", "2", "-passlogfile", str(passlog_base),
                "-c:a", "aac", "-b:a", f"{audio_kbps}k", "-ar", "48000",
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                str(output_path),
            ]
            r2 = subprocess.run(pass2_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 timeout=3600, **SUBPROCESS_TEXT_KWARGS)

            if r2.returncode != 0 or not output_path.exists():
                return {"success": False, "error": f"Pass 2 failed: {r2.stderr[-800:]}"}

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

            # Overshot the target - tighten the bitrate proportionally and retry.
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
# that still fits under the target size (best quality for the size budget,
# same philosophy as the video compressor's 2-pass approach), and only
# downscales resolution as a last resort if even minimum quality can't hit
# the target at full size.

def detect_output_image_format(input_path):
    """Returns 'WEBP' if the image has transparency (to preserve alpha),
    otherwise 'JPEG'. Cheap check used to name the output file up front."""
    try:
        with Image.open(input_path) as img:
            has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
            return "WEBP" if has_alpha else "JPEG"
    except Exception:
        return "JPEG"


def compress_image_to_target_size(input_path, output_path, target_size_kb):
    """
    Compresses input_path to output_path so the final file lands at or
    under target_size_kb. Uses WEBP for images with transparency (to keep
    the alpha channel), JPEG otherwise. Returns a dict: {success, size_kb,
    quality, format, original_resolution, output_resolution, error}.
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

        for _ in range(6):  # resolution-shrink attempts (only if quality alone can't fit)
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
                    lo = mid + 1  # try for higher quality that still fits
                else:
                    hi = mid - 1

            if found:
                best_quality, best_data = found
                break

            # Even the lowest quality is too big at this resolution - shrink
            # dimensions ~15% and try the quality search again.
            w, h = working_img.size
            new_w, new_h = max(int(w * 0.85), 32), max(int(h * 0.85), 32)
            if (new_w, new_h) == (w, h):
                break
            working_img = working_img.resize((new_w, new_h), Image.LANCZOS)

        if best_data is None:
            # Absolute fallback: smallest quality at the smallest attempted size.
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


# ==============================
# GRADIO HANDLERS
# ==============================

def process_compress_video_ui(video_path, size_choice, custom_mb, progress=gr.Progress(track_tqdm=False)):
    """Click handler for the Video Compressor tab."""
    if video_path is None:
        return "ERROR: Please upload a video.", None, None

    if size_choice == "Custom (MB)":
        try:
            target_mb = float(custom_mb)
        except (TypeError, ValueError):
            return "ERROR: Enter a valid custom size in MB.", None, None
        if target_mb <= 0:
            return "ERROR: Custom size must be greater than 0.", None, None
    else:
        target_mb = VIDEO_SIZE_PRESETS.get(size_choice)
        if target_mb is None:
            return "ERROR: Unknown target size option.", None, None

    input_path = Path(video_path)
    original_size = get_size_mb(input_path)
    duration = get_video_duration(input_path)

    size_tag = str(int(target_mb)) if target_mb == int(target_mb) else str(target_mb).replace(".", "_")
    output_path = unique_path(COMPRESSED_VIDEO_FOLDER, f"{clean_stem(input_path)}_under{size_tag}MB.mp4")

    status_text = "=== VIDEO COMPRESSION ===\n"
    status_text += f"Input: {input_path.name}\n"
    status_text += f"Original Size: {original_size:.2f} MB\n"
    status_text += f"Duration: {format_time(duration)}\n"
    status_text += f"Target: Under {target_mb} MB\n"
    status_text += "-" * 40 + "\n"

    def on_progress(fraction, desc=""):
        try:
            progress(fraction, desc=desc)
        except Exception:
            pass

    on_progress(0.05, "Analyzing video...")
    result = compress_video_to_target_size(input_path, output_path, target_mb, progress_callback=on_progress)

    if not result.get("success"):
        status_text += f"[FAILED] {result.get('error', 'Unknown error')}\n"
        return status_text, None, None

    on_progress(1.0, "Done")

    final_size = result["size_mb"]
    reduction = ((original_size - final_size) / original_size * 100) if original_size > 0 else 0

    status_text += "[OK] Compression complete\n\n"
    status_text += f"Output File: {output_path.name}\n"
    status_text += f"Final Size: {final_size} MB (target: under {target_mb} MB)\n"
    status_text += f"Size Reduction: {reduction:.1f}%\n"
    status_text += f"Video Bitrate: {result['video_kbps']} kbps\n"
    status_text += f"Audio Bitrate: {result['audio_kbps']} kbps\n"
    status_text += f"Original Resolution: {result['original_resolution']}\n"
    status_text += f"Output Resolution: {result['output_resolution']}"
    status_text += " (downscaled to fit the size budget)\n" if result["output_resolution"] != result["original_resolution"] else " (unchanged)\n"
    status_text += f"Encoding Passes Used: {result['attempts']}\n"
    status_text += f"Location: {output_path}\n"

    on_progress(1.0, "Uploading to Cloudinary...")
    upload = upload_to_cloudinary(output_path, "video", CLOUDINARY_VIDEO_FOLDER)
    if upload["success"]:
        status_text += f"\nCloudinary URL (permanent): {upload['url']}\n"
    else:
        status_text += f"\nCloudinary upload skipped: {upload['error']}\n"

    return status_text, str(output_path), str(output_path)


def process_compress_image_ui(image_path, size_choice, custom_kb, progress=gr.Progress(track_tqdm=False)):
    """Click handler for the Image Compressor tab."""
    if image_path is None:
        return "ERROR: Please upload an image.", None, None

    if size_choice == "Custom (KB)":
        try:
            target_kb = float(custom_kb)
        except (TypeError, ValueError):
            return "ERROR: Enter a valid custom size in KB.", None, None
        if target_kb <= 0:
            return "ERROR: Custom size must be greater than 0.", None, None
    else:
        target_kb = IMAGE_SIZE_PRESETS.get(size_choice)
        if target_kb is None:
            return "ERROR: Unknown target size option.", None, None

    input_path = Path(image_path)
    original_size_kb = get_size_kb(input_path)

    try:
        progress(0.2, desc="Compressing image...")
    except Exception:
        pass

    size_tag = str(int(target_kb)) if target_kb == int(target_kb) else str(target_kb).replace(".", "_")
    out_format = detect_output_image_format(input_path)
    ext = ".webp" if out_format == "WEBP" else ".jpg"
    output_path = unique_path(COMPRESSED_IMAGE_FOLDER, f"{clean_stem(input_path)}_under{size_tag}KB{ext}")

    result = compress_image_to_target_size(input_path, output_path, target_kb)

    status_text = "=== IMAGE COMPRESSION ===\n"
    status_text += f"Input: {input_path.name}\n"
    status_text += f"Original Size: {original_size_kb:.2f} KB\n"
    status_text += f"Target: Under {target_kb} KB\n"
    status_text += "-" * 40 + "\n"

    if not result.get("success"):
        status_text += f"[FAILED] {result.get('error', 'Unknown error')}\n"
        return status_text, None, None

    try:
        progress(1.0, desc="Done")
    except Exception:
        pass

    final_size = result["size_kb"]
    reduction = ((original_size_kb - final_size) / original_size_kb * 100) if original_size_kb > 0 else 0

    status_text += "[OK] Compression complete\n\n"
    status_text += f"Output File: {output_path.name}\n"
    status_text += f"Format: {result['format']}\n"
    status_text += f"Final Size: {final_size} KB (target: under {target_kb} KB)\n"
    status_text += f"Size Reduction: {reduction:.1f}%\n"
    status_text += f"JPEG/WEBP Quality Used: {result['quality']}\n"
    status_text += f"Original Resolution: {result['original_resolution']}\n"
    status_text += f"Output Resolution: {result['output_resolution']}"
    status_text += " (downscaled to fit the size budget)\n" if result["output_resolution"] != result["original_resolution"] else " (unchanged)\n"
    status_text += f"Location: {output_path}\n"

    try:
        progress(1.0, desc="Uploading to Cloudinary...")
    except Exception:
        pass
    upload = upload_to_cloudinary(output_path, "image", CLOUDINARY_IMAGE_FOLDER)
    if upload["success"]:
        status_text += f"\nCloudinary URL (permanent): {upload['url']}\n"
    else:
        status_text += f"\nCloudinary upload skipped: {upload['error']}\n"

    return status_text, str(output_path), str(output_path)


def _resolve_target(size_choice, custom_value, presets, unit_label):
    """Shared validation for the batch tabs' size-choice + custom-value inputs."""
    if size_choice == f"Custom ({unit_label})":
        try:
            value = float(custom_value)
        except (TypeError, ValueError):
            return None, f"ERROR: Enter a valid custom size in {unit_label}."
        if value <= 0:
            return None, "ERROR: Custom size must be greater than 0."
        return value, None

    value = presets.get(size_choice)
    if value is None:
        return None, "ERROR: Unknown target size option."
    return value, None


def process_batch_videos_ui(video_files, size_choice, custom_mb, progress=gr.Progress(track_tqdm=False)):
    """Click handler for the Batch Video Compressor tab."""
    if not video_files:
        return "ERROR: Please upload at least one video.", None

    if len(video_files) > MAX_BATCH_FILES:
        return f"ERROR: Please upload {MAX_BATCH_FILES} videos or fewer at a time (got {len(video_files)}).", None

    target_mb, err = _resolve_target(size_choice, custom_mb, VIDEO_SIZE_PRESETS, "MB")
    if err:
        return err, None

    total = len(video_files)
    results = [None] * total

    status_text = "=== BATCH VIDEO COMPRESSION ===\n"
    status_text += f"Total Videos: {total}\n"
    status_text += f"Target: Under {target_mb} MB each (already-small videos are copied through unchanged)\n"
    status_text += f"Parallel Workers: {BATCH_VIDEO_MAX_WORKERS}\n"
    status_text += "=" * 60 + "\n\n"

    try:
        progress(0.0, desc=f"Starting batch of {total} videos...")
    except Exception:
        pass

    with ThreadPoolExecutor(max_workers=BATCH_VIDEO_MAX_WORKERS) as executor:
        future_to_index = {
            executor.submit(batch_compress_one_video, f, target_mb): idx
            for idx, f in enumerate(video_files)
        }
        completed = 0
        for future in as_completed(future_to_index):
            idx = future_to_index[future]
            results[idx] = future.result()
            completed += 1
            try:
                progress(completed / total, desc=f"Processed {completed}/{total} videos")
            except Exception:
                pass

    success_count = sum(1 for r in results if r["success"])
    failed_count = total - success_count
    copied_count = sum(1 for r in results if r["success"] and r["action"].startswith("copied"))
    compressed_count = success_count - copied_count

    for idx, r in enumerate(results, 1):
        status_text += f"[{idx}/{total}] {r['name']}\n"
        if r["success"]:
            status_text += f"    {r['action'].upper()} - {r['original_size_mb']} MB -> {r['final_size_mb']} MB\n"
            if r.get("cloud_url"):
                status_text += f"    Cloudinary: {r['cloud_url']}\n"
        else:
            status_text += f"    FAILED - {r['error']}\n"

    status_text += "\n" + "=" * 60 + "\n"
    status_text += "BATCH COMPLETE\n"
    status_text += "=" * 60 + "\n"
    status_text += f"Compressed: {compressed_count} | Copied (already small): {copied_count} | Failed: {failed_count}\n"
    if CLOUDINARY_CONFIGURED:
        uploaded_count = sum(1 for r in results if r.get("cloud_url"))
        status_text += f"Uploaded to Cloudinary (permanent storage): {uploaded_count}/{success_count}\n"
    else:
        status_text += "Cloudinary not configured - files only saved to local (ephemeral) disk.\n"

    output_files = [r["output_path"] for r in results if r["success"]]
    zip_path = None
    if output_files:
        manifest_path = build_links_manifest(results)
        zip_path = create_zip(
            f"batch_videos_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
            output_files + ([manifest_path] if manifest_path else []),
        )
        status_text += f"\nZIP ready for download ({len(output_files)} files).\n"
    else:
        status_text += "\nNo files were successfully processed - nothing to zip.\n"

    return status_text, zip_path


def process_batch_images_ui(image_files, size_choice, custom_kb, progress=gr.Progress(track_tqdm=False)):
    """Click handler for the Batch Image Compressor tab."""
    if not image_files:
        return "ERROR: Please upload at least one image.", None

    if len(image_files) > MAX_BATCH_FILES:
        return f"ERROR: Please upload {MAX_BATCH_FILES} images or fewer at a time (got {len(image_files)}).", None

    target_kb, err = _resolve_target(size_choice, custom_kb, IMAGE_SIZE_PRESETS, "KB")
    if err:
        return err, None

    total = len(image_files)
    results = [None] * total

    status_text = "=== BATCH IMAGE COMPRESSION ===\n"
    status_text += f"Total Images: {total}\n"
    status_text += f"Target: Under {target_kb} KB each (already-small images are copied through unchanged)\n"
    status_text += f"Parallel Workers: {BATCH_IMAGE_MAX_WORKERS}\n"
    status_text += "=" * 60 + "\n\n"

    try:
        progress(0.0, desc=f"Starting batch of {total} images...")
    except Exception:
        pass

    with ThreadPoolExecutor(max_workers=BATCH_IMAGE_MAX_WORKERS) as executor:
        future_to_index = {
            executor.submit(batch_compress_one_image, f, target_kb): idx
            for idx, f in enumerate(image_files)
        }
        completed = 0
        for future in as_completed(future_to_index):
            idx = future_to_index[future]
            results[idx] = future.result()
            completed += 1
            try:
                progress(completed / total, desc=f"Processed {completed}/{total} images")
            except Exception:
                pass

    success_count = sum(1 for r in results if r["success"])
    failed_count = total - success_count
    copied_count = sum(1 for r in results if r["success"] and r["action"].startswith("copied"))
    compressed_count = success_count - copied_count

    for idx, r in enumerate(results, 1):
        status_text += f"[{idx}/{total}] {r['name']}\n"
        if r["success"]:
            status_text += f"    {r['action'].upper()} - {r['original_size_kb']} KB -> {r['final_size_kb']} KB\n"
            if r.get("cloud_url"):
                status_text += f"    Cloudinary: {r['cloud_url']}\n"
        else:
            status_text += f"    FAILED - {r['error']}\n"

    status_text += "\n" + "=" * 60 + "\n"
    status_text += "BATCH COMPLETE\n"
    status_text += "=" * 60 + "\n"
    status_text += f"Compressed: {compressed_count} | Copied (already small): {copied_count} | Failed: {failed_count}\n"
    if CLOUDINARY_CONFIGURED:
        uploaded_count = sum(1 for r in results if r.get("cloud_url"))
        status_text += f"Uploaded to Cloudinary (permanent storage): {uploaded_count}/{success_count}\n"
    else:
        status_text += "Cloudinary not configured - files only saved to local (ephemeral) disk.\n"

    output_files = [r["output_path"] for r in results if r["success"]]
    zip_path = None
    if output_files:
        manifest_path = build_links_manifest(results)
        zip_path = create_zip(
            f"batch_images_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
            output_files + ([manifest_path] if manifest_path else []),
        )
        status_text += f"\nZIP ready for download ({len(output_files)} files).\n"
    else:
        status_text += "\nNo files were successfully processed - nothing to zip.\n"

    return status_text, zip_path


# ==============================
# CLOUD LIBRARY (browse what's stored in Cloudinary)
# ==============================

def fetch_cloud_library(max_results=30):
    """
    Lists the most recently uploaded videos and images from Cloudinary.
    Returns an HTML fragment for display. Never raises - any failure
    (including "not configured") renders as a friendly message instead.
    """
    if not CLOUDINARY_CONFIGURED:
        return (
            "<p>Cloudinary is not configured (missing CLOUDINARY_CLOUD_NAME / "
            "CLOUDINARY_API_KEY / CLOUDINARY_API_SECRET environment variables). "
            "Compressed files are only kept on local disk for this session.</p>"
        )

    rows = []
    try:
        for resource_type, folder, label in [
            ("video", CLOUDINARY_VIDEO_FOLDER, "Video"),
            ("image", CLOUDINARY_IMAGE_FOLDER, "Image"),
        ]:
            result = cloudinary.api.resources(
                resource_type=resource_type,
                type="upload",
                prefix=folder,
                max_results=max_results,
                direction="desc",
            )
            for res in result.get("resources", []):
                size_kb = res.get("bytes", 0) / 1024
                size_display = f"{size_kb/1024:.2f} MB" if size_kb >= 1024 else f"{size_kb:.1f} KB"
                created = res.get("created_at", "")
                url = res.get("secure_url", "")
                filename = Path(res.get("public_id", "")).name
                rows.append((created, label, filename, size_display, url))
    except Exception as e:
        return f"<p>Could not reach Cloudinary: {str(e)}</p>"

    if not rows:
        return "<p>No files uploaded to Cloudinary yet. Compress something first!</p>"

    rows.sort(key=lambda r: r[0], reverse=True)

    html = "<table style='width:100%; border-collapse: collapse;'>"
    html += (
        "<tr>"
        "<th style='text-align:left; padding:6px; border-bottom:1px solid #ccc;'>Type</th>"
        "<th style='text-align:left; padding:6px; border-bottom:1px solid #ccc;'>File</th>"
        "<th style='text-align:left; padding:6px; border-bottom:1px solid #ccc;'>Size</th>"
        "<th style='text-align:left; padding:6px; border-bottom:1px solid #ccc;'>Uploaded</th>"
        "<th style='text-align:left; padding:6px; border-bottom:1px solid #ccc;'>Link</th>"
        "</tr>"
    )
    for created, label, filename, size_display, url in rows:
        html += (
            "<tr>"
            f"<td style='padding:6px; border-bottom:1px solid #eee;'>{label}</td>"
            f"<td style='padding:6px; border-bottom:1px solid #eee;'>{filename}</td>"
            f"<td style='padding:6px; border-bottom:1px solid #eee;'>{size_display}</td>"
            f"<td style='padding:6px; border-bottom:1px solid #eee;'>{created}</td>"
            f"<td style='padding:6px; border-bottom:1px solid #eee;'><a href='{url}' target='_blank'>Open</a></td>"
            "</tr>"
        )
    html += "</table>"
    return html


# ==============================
# UI - GRADIO INTERFACE
# ==============================

def create_ui():
    css = """
    .header {
        text-align: center;
        padding: 16px;
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        color: white;
        border-radius: 10px;
        margin-bottom: 16px;
    }
    """

    with gr.Blocks(title="Video & Image Compressor", theme=gr.themes.Soft(), css=css) as app:

        gr.HTML("""
        <div class='header'>
            <h1>🗜️ Video & Image Compressor</h1>
            <p>Shrink files to a target size while keeping the best quality that size budget allows</p>
        </div>
        """)

        with gr.Tabs():

            # ==================== TAB 1: VIDEO COMPRESSOR ====================
            with gr.Tab("🎥 Video Compressor"):

                with gr.Row():
                    with gr.Column(scale=1):
                        gr.Markdown("**Input**")
                        video_input = gr.Video(label="Upload Video", height=260)
                    with gr.Column(scale=1):
                        gr.Markdown("**Compressed Output**")
                        video_output = gr.Video(label="Compressed Video", height=260, interactive=False)

                with gr.Row():
                    with gr.Column(scale=2):
                        video_size_choice = gr.Radio(
                            choices=["Under 10 MB", "Under 5 MB", "Custom (MB)"],
                            value="Under 10 MB",
                            label="Target Size"
                        )
                        video_custom_mb = gr.Number(
                            label="Custom Target Size (MB)", value=10, minimum=0.1, visible=False
                        )
                    with gr.Column(scale=1):
                        video_compress_btn = gr.Button("🗜️ Compress Video", size="lg", variant="primary")
                        video_download = gr.File(label="Download Compressed Video")

                video_status = gr.Textbox(label="Compression Status", lines=12, interactive=False)

                video_size_choice.change(
                    fn=lambda choice: gr.update(visible=(choice == "Custom (MB)")),
                    inputs=video_size_choice,
                    outputs=video_custom_mb
                )

                video_compress_btn.click(
                    fn=process_compress_video_ui,
                    inputs=[video_input, video_size_choice, video_custom_mb],
                    outputs=[video_status, video_output, video_download]
                )

            # ==================== TAB 2: IMAGE COMPRESSOR ====================
            with gr.Tab("🖼️ Image Compressor"):

                with gr.Row():
                    with gr.Column(scale=1):
                        gr.Markdown("**Input**")
                        image_input = gr.Image(label="Upload Image", type="filepath", height=260)
                    with gr.Column(scale=1):
                        gr.Markdown("**Compressed Output**")
                        image_output = gr.Image(label="Compressed Image", height=260, interactive=False)

                with gr.Row():
                    with gr.Column(scale=2):
                        image_size_choice = gr.Radio(
                            choices=["Under 500 KB", "Under 200 KB", "Custom (KB)"],
                            value="Under 500 KB",
                            label="Target Size"
                        )
                        image_custom_kb = gr.Number(
                            label="Custom Target Size (KB)", value=200, minimum=1, visible=False
                        )
                    with gr.Column(scale=1):
                        image_compress_btn = gr.Button("🗜️ Compress Image", size="lg", variant="primary")
                        image_download = gr.File(label="Download Compressed Image")

                image_status = gr.Textbox(label="Compression Status", lines=12, interactive=False)

                image_size_choice.change(
                    fn=lambda choice: gr.update(visible=(choice == "Custom (KB)")),
                    inputs=image_size_choice,
                    outputs=image_custom_kb
                )

                image_compress_btn.click(
                    fn=process_compress_image_ui,
                    inputs=[image_input, image_size_choice, image_custom_kb],
                    outputs=[image_status, image_output, image_download]
                )

            # ==================== TAB 3: BATCH VIDEO COMPRESSOR ====================
            with gr.Tab("📦 Batch Video Compressor"):

                gr.Markdown(f"""
                Upload up to **{MAX_BATCH_FILES} videos** at once. Each one is compressed to
                your target size; videos already at or under the target are copied through
                unchanged instead of being re-encoded. Processes {BATCH_VIDEO_MAX_WORKERS} at
                a time to stay stable on this machine. Download everything as one ZIP when done.
                """)

                with gr.Row():
                    with gr.Column(scale=2):
                        batch_video_files = gr.File(
                            label="Upload Videos (multiple)",
                            file_count="multiple",
                            file_types=["video"]
                        )
                        batch_video_size_choice = gr.Radio(
                            choices=["Under 10 MB", "Under 5 MB", "Custom (MB)"],
                            value="Under 10 MB",
                            label="Target Size (per video)"
                        )
                        batch_video_custom_mb = gr.Number(
                            label="Custom Target Size (MB)", value=10, minimum=0.1, visible=False
                        )
                    with gr.Column(scale=1):
                        batch_video_btn = gr.Button("🗜️ Start Batch Compression", size="lg", variant="primary")
                        batch_video_zip = gr.File(label="Download All as ZIP")

                batch_video_status = gr.Textbox(label="Batch Status", lines=20, interactive=False)

                batch_video_size_choice.change(
                    fn=lambda choice: gr.update(visible=(choice == "Custom (MB)")),
                    inputs=batch_video_size_choice,
                    outputs=batch_video_custom_mb
                )

                batch_video_btn.click(
                    fn=process_batch_videos_ui,
                    inputs=[batch_video_files, batch_video_size_choice, batch_video_custom_mb],
                    outputs=[batch_video_status, batch_video_zip]
                )

            # ==================== TAB 4: BATCH IMAGE COMPRESSOR ====================
            with gr.Tab("📦 Batch Image Compressor"):

                gr.Markdown(f"""
                Upload up to **{MAX_BATCH_FILES} images** at once. Each one is compressed to
                your target size; images already at or under the target are copied through
                unchanged instead of being re-encoded. Processes {BATCH_IMAGE_MAX_WORKERS} at
                a time. Download everything as one ZIP when done.
                """)

                with gr.Row():
                    with gr.Column(scale=2):
                        batch_image_files = gr.File(
                            label="Upload Images (multiple)",
                            file_count="multiple",
                            file_types=["image"]
                        )
                        batch_image_size_choice = gr.Radio(
                            choices=["Under 500 KB", "Under 200 KB", "Custom (KB)"],
                            value="Under 500 KB",
                            label="Target Size (per image)"
                        )
                        batch_image_custom_kb = gr.Number(
                            label="Custom Target Size (KB)", value=200, minimum=1, visible=False
                        )
                    with gr.Column(scale=1):
                        batch_image_btn = gr.Button("🗜️ Start Batch Compression", size="lg", variant="primary")
                        batch_image_zip = gr.File(label="Download All as ZIP")

                batch_image_status = gr.Textbox(label="Batch Status", lines=20, interactive=False)

                batch_image_size_choice.change(
                    fn=lambda choice: gr.update(visible=(choice == "Custom (KB)")),
                    inputs=batch_image_size_choice,
                    outputs=batch_image_custom_kb
                )

                batch_image_btn.click(
                    fn=process_batch_images_ui,
                    inputs=[batch_image_files, batch_image_size_choice, batch_image_custom_kb],
                    outputs=[batch_image_status, batch_image_zip]
                )

            # ==================== TAB 5: CLOUD LIBRARY ====================
            with gr.Tab("☁️ Cloud Library"):

                gr.Markdown("""
                Every compressed file is uploaded to Cloudinary for **permanent storage** -
                local disk on a host like Render is wiped on every restart/redeploy, so this
                is what survives. Browse and open past results here anytime.
                """)

                cloud_library_html = gr.HTML(value=fetch_cloud_library())
                cloud_library_refresh_btn = gr.Button("🔄 Refresh", variant="secondary")

                cloud_library_refresh_btn.click(
                    fn=fetch_cloud_library,
                    outputs=cloud_library_html
                )

        return app


# ==============================
# LAUNCH APPLICATION
# ==============================

if __name__ == "__main__":
    safe_log("=== APPLICATION STARTED ===", "INFO")
    safe_log(f"Compressed videos folder: {COMPRESSED_VIDEO_FOLDER}", "INFO")
    safe_log(f"Compressed images folder: {COMPRESSED_IMAGE_FOLDER}", "INFO")

    app = create_ui()

    print("\n" + "=" * 70)
    print("VIDEO & IMAGE COMPRESSOR - STARTED")
    print("=" * 70)
    print(f"  Compressed Videos: {COMPRESSED_VIDEO_FOLDER}")
    print(f"  Compressed Images: {COMPRESSED_IMAGE_FOLDER}")
    print(f"  Logs: {LOGS_FOLDER}")
    print("=" * 70 + "\n")

    # Render (and most container hosts) assign their own PORT and expect the
    # app to bind every interface (0.0.0.0), not just localhost.
    #
    # share=True is required here even though Render already provides a
    # public URL: Gradio's own startup self-check tries to confirm
    # 127.0.0.1 is reachable, and inside a sandboxed container that check
    # can fail - when it does, Gradio hard-crashes with "When localhost is
    # not accessible, a shareable link must be created" unless share=True
    # is set (this is Gradio's own documented behavior/workaround for
    # server_name="0.0.0.0" in Docker, not something specific to this app).
    # The share tunnel it creates is simply unused - Render's own URL is
    # what people actually visit.
    running_in_container = bool(os.environ.get("PORT"))

    app.launch(
        server_name="0.0.0.0" if running_in_container else None,
        server_port=int(os.environ.get("PORT", 7860)),
        share=True,
        show_error=True,
        debug=not running_in_container,
    )
