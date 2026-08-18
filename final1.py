import os
import re
import json
import shutil
import zipfile
import tempfile
import subprocess
from pathlib import Path
from datetime import datetime

import gradio as gr

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import cloudinary
import cloudinary.uploader


# ==============================
# FFMPEG / FFPROBE LOCATION
# ==============================
# Prefer a bundled copy in ./ffmpeg_bin (used when ffmpeg isn't installed
# system-wide / isn't on PATH, e.g. this project's local ffmpeg_bin folder
# or an FFMPEG_BIN_DIR env var pointing at one). Falls back to plain
# "ffmpeg"/"ffprobe" so a system-wide install (or a Space's packages.txt
# install) still works unchanged.

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


# ==============================
# OUTPUT FOLDERS
# ==============================

BASE_DIR = Path("video_processing_workspace1")

OPTIMIZED_FOLDER = BASE_DIR / "01_optimized_android"
PLAYABLE_FOLDER = BASE_DIR / "02_playable_original_size"
PORTRAIT_FOLDER = BASE_DIR / "03_portrait_9x16_cropped"
COMPACT_FOLDER = BASE_DIR / "04_600x800"
CUSTOM_FOLDER = BASE_DIR / "05_custom_size"

OUTPUT_FOLDERS = {
    "optimized": OPTIMIZED_FOLDER,
    "playable": PLAYABLE_FOLDER,
    "portrait_9x16": PORTRAIT_FOLDER,
    "compact_600x800": COMPACT_FOLDER,
    "custom_size": CUSTOM_FOLDER,
}

for folder in OUTPUT_FOLDERS.values():
    folder.mkdir(parents=True, exist_ok=True)


# ==============================
# CLOUDINARY CONFIG
# ==============================
# Credentials are read from environment variables only - never hardcode
# secrets in source. Set these before running (see .env.example):
#   CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET
# On Hugging Face Spaces, set them under Settings -> Variables and secrets.

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


def upload_to_cloudinary(file_path, folder="video_processor"):
    """
    Uploads a video file to Cloudinary and returns its public URL.
    Uses upload_large (chunked) since generated videos can be sizeable.
    """
    if not CLOUDINARY_CONFIGURED:
        return {
            "success": False,
            "url": None,
            "error": "Cloudinary is not configured. Set CLOUDINARY_CLOUD_NAME, "
                     "CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET.",
        }

    if not file_path or not os.path.exists(file_path):
        return {"success": False, "url": None, "error": "File not found."}

    try:
        result = cloudinary.uploader.upload_large(
            str(file_path),
            resource_type="video",
            folder=folder,
            overwrite=True,
        )
        return {
            "success": True,
            "url": result.get("secure_url"),
            "public_id": result.get("public_id"),
            "error": "",
        }
    except Exception as e:
        return {"success": False, "url": None, "error": str(e)}


# ==============================
# VIDEO PROFILES
# ==============================

VIDEO_PROFILES = {
    "optimized": {
        "label": "Optimized Android",
        "width": 720,
        "height": 1280,
        "bitrate": "1500k",
        "maxrate": "1800k",
        "bufsize": "3000k",
        "crf": "23",
        "preset": "medium",
        "profile": "baseline",
        "level": "3.1",
        "fps": "30",
        "description": "Android ExoPlayer compatible 720x1280",
    },
    "playable": {
        "label": "Playable Original Size",
        "description": "Same resolution as input, local-storage friendly MP4",
    },
    "portrait_9x16": {
        "label": "Portrait 9:16 Cropped",
        "width": 1080,
        "height": 1920,
        "bitrate": "2500k",
        "maxrate": "3000k",
        "bufsize": "5000k",
        "crf": "23",
        "preset": "medium",
        "profile": "main",
        "level": "4.1",
        "fps": "30",
        "description": "Center-cropped 9:16 portrait video",
    },
    "compact_600x800": {
        "label": "600x800 Compact",
        "width": 600,
        "height": 800,
        "bitrate": "1200k",
        "maxrate": "1500k",
        "bufsize": "2400k",
        "crf": "25",
        "preset": "medium",
        "profile": "baseline",
        "level": "3.1",
        "fps": "30",
        "description": "Exact 600x800 compact video",
    },
}


# ==============================
# BASIC HELPERS
# ==============================

def get_file_path(file_obj):
    """
    Gradio may return:
    - string file path
    - object with .name
    - dict with 'name'
    This helper safely extracts the real path.
    """
    if file_obj is None:
        return None

    if isinstance(file_obj, str):
        return file_obj

    if isinstance(file_obj, dict):
        return file_obj.get("name") or file_obj.get("path")

    if hasattr(file_obj, "name"):
        return file_obj.name

    return str(file_obj)


def get_original_stem(file_obj):
    """
    Try to get original filename stem from Gradio upload.
    """
    if file_obj is None:
        return "video"

    if hasattr(file_obj, "orig_name") and file_obj.orig_name:
        return Path(file_obj.orig_name).stem

    if isinstance(file_obj, dict):
        if file_obj.get("orig_name"):
            return Path(file_obj["orig_name"]).stem
        if file_obj.get("name"):
            return Path(file_obj["name"]).stem

    path = get_file_path(file_obj)
    if path:
        return Path(path).stem

    return "video"


def sanitize_name(name):
    """
    Clean filename so it is safe for Windows/Linux/Mac.
    """
    name = name.strip()
    name = name.replace(" ", "_")
    name = re.sub(r"[^a-zA-Z0-9_\-]", "_", name)
    name = re.sub(r"_+", "_", name)
    return name or "video"


def get_size_mb(path):
    if path and os.path.exists(path):
        return os.path.getsize(path) / (1024 * 1024)
    return 0


def format_duration(seconds):
    if not seconds:
        return "Unknown"

    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60

    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def unique_output_path(folder, filename):
    """
    Prevent accidental overwrite.
    If file exists, creates filename_001.mp4, filename_002.mp4, etc.
    """
    folder = Path(folder)
    path = folder / filename

    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix

    counter = 1
    while True:
        new_path = folder / f"{stem}_{counter:03d}{suffix}"
        if not new_path.exists():
            return new_path
        counter += 1


def run_command(command, timeout=7200):
    """
    Run FFmpeg/FFprobe command safely.
    """
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            encoding="utf-8",
            errors="ignore",
        )

        stdout, stderr = process.communicate(timeout=timeout)

        return {
            "success": process.returncode == 0,
            "stdout": stdout,
            "stderr": stderr,
            "returncode": process.returncode,
        }

    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except Exception:
            pass

        return {
            "success": False,
            "stdout": "",
            "stderr": "FFmpeg timeout",
            "returncode": -1,
        }

    except Exception as e:
        return {
            "success": False,
            "stdout": "",
            "stderr": str(e),
            "returncode": -1,
        }


# ==============================
# FFPROBE HELPERS
# ==============================

def get_video_duration(video_path):
    command = [
        FFPROBE_BIN,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]

    result = run_command(command, timeout=30)

    if not result["success"]:
        return 0

    try:
        text = result["stdout"].strip()
        return float(text) if text else 0
    except Exception:
        return 0


def get_video_probe(video_path):
    """
    Get video/audio codec and resolution info.
    """
    command = [
        FFPROBE_BIN,
        "-v", "error",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        str(video_path),
    ]

    result = run_command(command, timeout=30)

    if not result["success"]:
        return {}

    try:
        return json.loads(result["stdout"])
    except Exception:
        return {}


def get_main_video_codec(video_path):
    probe = get_video_probe(video_path)

    for stream in probe.get("streams", []):
        if stream.get("codec_type") == "video":
            return stream.get("codec_name", "").lower()

    return ""


# ==============================
# THUMBNAIL / PREVIEW
# ==============================

def generate_thumbnail(input_path):
    """
    Creates a temporary thumbnail for Gradio preview.
    Does not create extra project folders.
    """
    if not input_path or not os.path.exists(input_path):
        return None

    temp_thumb = Path(tempfile.gettempdir()) / f"preview_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.jpg"

    command = [
        FFMPEG_BIN,
        "-y",
        "-ss", "00:00:01",
        "-i", str(input_path),
        "-vframes", "1",
        "-vf", "scale=480:-1",
        str(temp_thumb),
    ]

    result = run_command(command, timeout=60)

    if result["success"] and temp_thumb.exists():
        return str(temp_thumb)

    # fallback at 0.1 sec for very short videos
    command = [
        FFMPEG_BIN,
        "-y",
        "-ss", "00:00:00.1",
        "-i", str(input_path),
        "-vframes", "1",
        "-vf", "scale=480:-1",
        str(temp_thumb),
    ]

    result = run_command(command, timeout=60)

    if result["success"] and temp_thumb.exists():
        return str(temp_thumb)

    return None


# ==============================
# VIDEO CONVERSION FUNCTIONS
# ==============================

def convert_optimized_android(input_path, output_path):
    """
    Android-safe video:
    - 720x1280
    - H.264 baseline
    - yuv420p
    - AAC audio
    - 30fps
    """
    profile = VIDEO_PROFILES["optimized"]

    vf_filter = (
        f"scale={profile['width']}:{profile['height']}:force_original_aspect_ratio=decrease,"
        f"pad={profile['width']}:{profile['height']}:(ow-iw)/2:(oh-ih)/2,"
        "setsar=1,"
        "format=yuv420p"
    )

    command = [
        FFMPEG_BIN,
        "-y",
        "-i", str(input_path),

        "-map", "0:v:0",
        "-map", "0:a?",

        "-vf", vf_filter,

        "-c:v", "libx264",
        "-preset", profile["preset"],
        "-crf", profile["crf"],
        "-b:v", profile["bitrate"],
        "-maxrate", profile["maxrate"],
        "-bufsize", profile["bufsize"],
        "-r", profile["fps"],
        "-profile:v", profile["profile"],
        "-level", profile["level"],
        "-pix_fmt", "yuv420p",
        "-bf", "0",
        "-refs", "1",
        "-g", "60",

        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "48000",
        "-ac", "2",

        "-movflags", "+faststart",

        str(output_path),
    ]

    return run_command(command)


def convert_playable_original_size(input_path, output_path):
    """
    Playable video requirement:
    - No resize.
    - Keep original resolution.
    - Preserve video quality as much as possible.
    - Make MP4 local-storage friendly.

    First attempt:
    - Stream copy video if it is already H.264.
    - This keeps original video quality exactly.

    Fallback:
    - Re-encode to H.264 without resizing.
    - CRF 18 for very high visual quality.
    """
    video_codec = get_main_video_codec(input_path)

    # Attempt 1: If source video is H.264, preserve original video stream.
    if video_codec == "h264":
        command_copy = [
            FFMPEG_BIN,
            "-y",
            "-i", str(input_path),

            "-map", "0:v:0",
            "-map", "0:a?",

            "-c:v", "copy",

            # Audio re-encoded to AAC for local/mobile compatibility.
            "-c:a", "aac",
            "-b:a", "192k",
            "-ar", "48000",
            "-ac", "2",

            "-movflags", "+faststart",

            str(output_path),
        ]

        result = run_command(command_copy)

        if result["success"] and output_path.exists() and get_size_mb(output_path) > 0:
            return result

    # Attempt 2: Fallback re-encode without changing video size.
    command_reencode = [
        FFMPEG_BIN,
        "-y",
        "-i", str(input_path),

        "-map", "0:v:0",
        "-map", "0:a?",

        # No scale here. Original resolution is preserved.
        "-vf", "setsar=1,format=yuv420p",

        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "18",
        "-profile:v", "main",
        "-pix_fmt", "yuv420p",

        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "48000",
        "-ac", "2",

        "-movflags", "+faststart",

        str(output_path),
    ]

    return run_command(command_reencode)


def convert_portrait_9x16_cropped(input_path, output_path):
    """
    9:16 portrait requirement:
    - Crop video, do not pad.
    - Output exact 1080x1920.
    - Uses center crop.
    """
    profile = VIDEO_PROFILES["portrait_9x16"]

    vf_filter = (
        f"scale={profile['width']}:{profile['height']}:force_original_aspect_ratio=increase,"
        f"crop={profile['width']}:{profile['height']}:(iw-{profile['width']})/2:(ih-{profile['height']})/2,"
        "setsar=1,"
        "format=yuv420p"
    )

    command = [
        FFMPEG_BIN,
        "-y",
        "-i", str(input_path),

        "-map", "0:v:0",
        "-map", "0:a?",

        "-vf", vf_filter,

        "-c:v", "libx264",
        "-preset", profile["preset"],
        "-crf", profile["crf"],
        "-b:v", profile["bitrate"],
        "-maxrate", profile["maxrate"],
        "-bufsize", profile["bufsize"],
        "-r", profile["fps"],
        "-profile:v", profile["profile"],
        "-level", profile["level"],
        "-pix_fmt", "yuv420p",

        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "48000",
        "-ac", "2",

        "-movflags", "+faststart",

        str(output_path),
    ]

    return run_command(command)


def convert_600x800(input_path, output_path):
    """
    600x800 compact video.
    This keeps the full video visible using padding.
    """
    profile = VIDEO_PROFILES["compact_600x800"]

    vf_filter = (
        f"scale={profile['width']}:{profile['height']}:force_original_aspect_ratio=decrease,"
        f"pad={profile['width']}:{profile['height']}:(ow-iw)/2:(oh-ih)/2,"
        "setsar=1,"
        "format=yuv420p"
    )

    command = [
        FFMPEG_BIN,
        "-y",
        "-i", str(input_path),

        "-map", "0:v:0",
        "-map", "0:a?",

        "-vf", vf_filter,

        "-c:v", "libx264",
        "-preset", profile["preset"],
        "-crf", profile["crf"],
        "-b:v", profile["bitrate"],
        "-maxrate", profile["maxrate"],
        "-bufsize", profile["bufsize"],
        "-r", profile["fps"],
        "-profile:v", profile["profile"],
        "-level", profile["level"],
        "-pix_fmt", "yuv420p",
        "-bf", "0",
        "-refs", "1",

        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "48000",
        "-ac", "2",

        "-movflags", "+faststart",

        str(output_path),
    ]

    return run_command(command)


def convert_custom_size(input_path, output_path, width, height):
    """
    Generates a video padded to fit an exact user-chosen width x height,
    preserving the full frame (same approach as the 600x800 profile).
    """
    width = int(width)
    height = int(height)

    vf_filter = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        "setsar=1,"
        "format=yuv420p"
    )

    command = [
        FFMPEG_BIN,
        "-y",
        "-i", str(input_path),

        "-map", "0:v:0",
        "-map", "0:a?",

        "-vf", vf_filter,

        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "24",
        "-b:v", "1300k",
        "-maxrate", "1600k",
        "-bufsize", "2600k",
        "-r", "30",
        "-profile:v", "baseline",
        "-level", "3.1",
        "-pix_fmt", "yuv420p",
        "-bf", "0",
        "-refs", "1",

        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "48000",
        "-ac", "2",

        "-movflags", "+faststart",

        str(output_path),
    ]

    return run_command(command)


CUSTOM_SIZE_PRESETS = {
    "600 x 800": (600, 800),
    "360 x 800": (360, 800),
}


def resolve_custom_size(size_choice, custom_width, custom_height):
    """
    Turns the UI's size selection into a (width, height) tuple, or None
    if the user asked for no extra custom-size output.
    """
    if not size_choice or size_choice == "None":
        return None

    if size_choice in CUSTOM_SIZE_PRESETS:
        return CUSTOM_SIZE_PRESETS[size_choice]

    if size_choice == "Custom":
        try:
            width = int(custom_width)
            height = int(custom_height)
        except (TypeError, ValueError):
            return None

        if width <= 0 or height <= 0:
            return None

        return (width, height)

    return None


# ==============================
# PROCESSING CORE
# ==============================

def process_video_core(input_video_path, video_name, progress=None,
                        custom_size=None, upload_to_cloud=False):
    """
    Creates all 4 fixed outputs from one input video, plus an optional
    5th output at a user-chosen (width, height). No per-video subfolders
    are created. If upload_to_cloud is True, each successful output is
    also pushed to Cloudinary and its URL is attached to the result.
    """
    input_video_path = Path(input_video_path)

    if not input_video_path.exists():
        raise FileNotFoundError("Input video not found")

    clean_name = sanitize_name(video_name)

    original_size = get_size_mb(input_video_path)
    duration = get_video_duration(input_video_path)
    thumbnail = generate_thumbnail(input_video_path)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    output_paths = {
        "optimized": unique_output_path(
            OPTIMIZED_FOLDER,
            f"{clean_name}_optimized_720x1280.mp4"
        ),
        "playable": unique_output_path(
            PLAYABLE_FOLDER,
            f"{clean_name}_playable_original_size.mp4"
        ),
        "portrait_9x16": unique_output_path(
            PORTRAIT_FOLDER,
            f"{clean_name}_portrait_9x16_cropped_1080x1920.mp4"
        ),
        "compact_600x800": unique_output_path(
            COMPACT_FOLDER,
            f"{clean_name}_600x800.mp4"
        ),
    }

    converters = {
        "optimized": convert_optimized_android,
        "playable": convert_playable_original_size,
        "portrait_9x16": convert_portrait_9x16_cropped,
        "compact_600x800": convert_600x800,
    }

    labels = {key: VIDEO_PROFILES[key]["label"] for key in converters}
    descriptions = {key: VIDEO_PROFILES[key]["description"] for key in converters}

    order = ["optimized", "playable", "portrait_9x16", "compact_600x800"]

    if custom_size:
        width, height = custom_size
        output_paths["custom_size"] = unique_output_path(
            CUSTOM_FOLDER,
            f"{clean_name}_{width}x{height}.mp4"
        )
        converters["custom_size"] = lambda inp, out, w=width, h=height: convert_custom_size(inp, out, w, h)
        labels["custom_size"] = f"Custom {width}x{height}"
        descriptions["custom_size"] = f"User-selected exact {width}x{height} video"
        order.append("custom_size")

    results = {}

    for index, key in enumerate(order):
        if progress:
            progress(
                (index + 1) / len(order),
                desc=f"Generating {labels[key]}"
            )

        converter = converters[key]
        output_path = output_paths[key]

        result = converter(input_video_path, output_path)

        success = result["success"] and output_path.exists()
        final_size = get_size_mb(output_path) if output_path.exists() else 0

        cloud_url = None
        cloud_error = ""

        if success and upload_to_cloud:
            upload_result = upload_to_cloudinary(output_path)
            cloud_url = upload_result.get("url")
            if not upload_result["success"]:
                cloud_error = upload_result.get("error", "")

        results[key] = {
            "success": success,
            "label": labels[key],
            "description": descriptions[key],
            "path": str(output_path) if output_path.exists() else None,
            "filename": output_path.name if output_path.exists() else None,
            "folder": str(output_path.parent),
            "size_mb": round(final_size, 2),
            "error": "" if result["success"] else result["stderr"][-1000:],
            "cloudinary_url": cloud_url,
            "cloudinary_error": cloud_error,
        }

    return {
        "name": clean_name,
        "original_path": str(input_video_path),
        "original_size_mb": round(original_size, 2),
        "duration": format_duration(duration),
        "thumbnail": thumbnail,
        "outputs": results,
        "created_at": timestamp,
    }


def create_zip(zip_name, file_paths):
    """
    Creates ZIP for download.
    ZIP is stored in system temp folder, not project folder.
    """
    zip_path = Path(tempfile.gettempdir()) / zip_name

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for file_path in file_paths:
            if file_path and os.path.exists(file_path):
                zipf.write(file_path, arcname=Path(file_path).name)

    return str(zip_path)


# ==============================
# GRADIO SINGLE VIDEO FUNCTIONS
# ==============================

def preview_single_upload(video_file):
    if video_file is None:
        return None, ""

    path = get_file_path(video_file)
    if not path:
        return None, ""

    thumb = generate_thumbnail(path)
    name = sanitize_name(get_original_stem(video_file))

    return thumb, name


def process_single_video_ui(
    video_file,
    template_id,
    size_choice,
    custom_width,
    custom_height,
    upload_choice,
    progress=gr.Progress(track_tqdm=False),
):
    empty_return = (
        None, None, None, None, None, None, None, None, {},
    )

    if video_file is None:
        return ("ERROR: Please upload a video.",) + empty_return[1:]

    input_path = get_file_path(video_file)

    if not input_path or not os.path.exists(input_path):
        return ("ERROR: Uploaded video path not found.",) + empty_return[1:]

    if not template_id or not template_id.strip():
        template_id = get_original_stem(video_file)

    template_id = sanitize_name(template_id)

    custom_size = resolve_custom_size(size_choice, custom_width, custom_height)
    upload_to_cloud = bool(upload_choice)

    if upload_to_cloud and not CLOUDINARY_CONFIGURED:
        upload_to_cloud = False
        cloudinary_warning = (
            "\nNOTE: Cloudinary upload was requested but is not configured "
            "(missing CLOUDINARY_CLOUD_NAME / CLOUDINARY_API_KEY / "
            "CLOUDINARY_API_SECRET environment variables). Skipped upload.\n"
        )
    else:
        cloudinary_warning = ""

    try:
        result = process_video_core(
            input_path,
            template_id,
            progress=progress,
            custom_size=custom_size,
            upload_to_cloud=upload_to_cloud,
        )

        outputs = result["outputs"]

        optimized_path = outputs["optimized"]["path"]
        playable_path = outputs["playable"]["path"]
        portrait_path = outputs["portrait_9x16"]["path"]
        compact_path = outputs["compact_600x800"]["path"]
        custom_path = outputs.get("custom_size", {}).get("path")

        downloadable_files = [
            optimized_path,
            playable_path,
            portrait_path,
            compact_path,
            custom_path,
        ]

        downloadable_files = [f for f in downloadable_files if f and os.path.exists(f)]

        zip_path = create_zip(
            f"{template_id}_all_outputs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
            downloadable_files,
        )

        status = ""
        status += "PROCESSING COMPLETE\n"
        status += "=" * 60 + "\n"
        status += f"Video Name: {result['name']}\n"
        status += f"Original Size: {result['original_size_mb']} MB\n"
        status += f"Duration: {result['duration']}\n"
        status += "=" * 60 + "\n"
        status += cloudinary_warning
        status += "\n"

        cloudinary_links = {}

        for key, data in outputs.items():
            status += f"{data['label']}\n"
            status += "-" * 40 + "\n"

            if data["success"]:
                status += f"Status: OK\n"
                status += f"File: {data['filename']}\n"
                status += f"Size: {data['size_mb']} MB\n"
                status += f"Folder: {data['folder']}\n"

                if data.get("cloudinary_url"):
                    status += f"Cloudinary URL: {data['cloudinary_url']}\n"
                    cloudinary_links[key] = data["cloudinary_url"]
                elif upload_to_cloud and data.get("cloudinary_error"):
                    status += f"Cloudinary upload failed: {data['cloudinary_error']}\n"
            else:
                status += "Status: FAILED\n"
                status += f"Error: {data['error']}\n"

            status += "\n"

        status += "OUTPUT FOLDERS\n"
        status += "=" * 60 + "\n"
        status += f"Optimized Android: {OPTIMIZED_FOLDER}\n"
        status += f"Playable Original Size: {PLAYABLE_FOLDER}\n"
        status += f"Portrait 9:16 Cropped: {PORTRAIT_FOLDER}\n"
        status += f"600x800: {COMPACT_FOLDER}\n"
        if custom_path:
            status += f"Custom Size: {CUSTOM_FOLDER}\n"

        return (
            status,
            result["thumbnail"],
            optimized_path,
            playable_path,
            portrait_path,
            compact_path,
            custom_path,
            downloadable_files + [zip_path],
            {"result": result, "cloudinary_links": cloudinary_links},
        )

    except Exception as e:
        return (f"ERROR: {str(e)}",) + empty_return[1:]


# ==============================
# GRADIO BATCH FUNCTIONS
# ==============================

def batch_file_info(batch_files):
    if not batch_files:
        return "No videos selected."

    info = ""
    info += f"Selected Videos: {len(batch_files)}\n"
    info += "=" * 60 + "\n"

    for file_obj in batch_files:
        path = get_file_path(file_obj)
        name = get_original_stem(file_obj)

        size = get_size_mb(path)
        info += f"{sanitize_name(name)} - {size:.2f} MB\n"

    return info


def process_batch_video_ui(
    batch_files,
    size_choice,
    custom_width,
    custom_height,
    upload_choice,
    progress=gr.Progress(track_tqdm=False),
):
    if not batch_files:
        return "ERROR: Please upload videos.", None, {}

    custom_size = resolve_custom_size(size_choice, custom_width, custom_height)
    upload_to_cloud = bool(upload_choice)

    batch_results = {}
    all_output_files = []

    status = ""
    status += "BATCH PROCESSING STARTED\n"
    status += "=" * 70 + "\n"
    status += f"Total Videos: {len(batch_files)}\n"
    status += "=" * 70 + "\n\n"

    if upload_to_cloud and not CLOUDINARY_CONFIGURED:
        upload_to_cloud = False
        status += (
            "NOTE: Cloudinary upload was requested but is not configured "
            "(missing CLOUDINARY_CLOUD_NAME / CLOUDINARY_API_KEY / "
            "CLOUDINARY_API_SECRET environment variables). Skipped upload.\n\n"
        )

    total = len(batch_files)

    for index, file_obj in enumerate(batch_files):
        input_path = get_file_path(file_obj)

        if not input_path or not os.path.exists(input_path):
            status += f"[{index + 1}/{total}] FAILED: file path missing\n\n"
            continue

        video_name = sanitize_name(get_original_stem(file_obj))

        progress(
            index / total,
            desc=f"Processing {video_name}"
        )

        status += f"[{index + 1}/{total}] {video_name}\n"
        status += "-" * 60 + "\n"

        try:
            result = process_video_core(
                input_path,
                video_name,
                progress=None,
                custom_size=custom_size,
                upload_to_cloud=upload_to_cloud,
            )
            batch_results[video_name] = result

            for key, data in result["outputs"].items():
                if data["success"]:
                    all_output_files.append(data["path"])
                    status += f"OK - {data['label']} - {data['filename']} - {data['size_mb']} MB\n"
                    if data.get("cloudinary_url"):
                        status += f"    Cloudinary URL: {data['cloudinary_url']}\n"
                    elif upload_to_cloud and data.get("cloudinary_error"):
                        status += f"    Cloudinary upload failed: {data['cloudinary_error']}\n"
                else:
                    status += f"FAILED - {data['label']} - {data['error']}\n"

            status += "\n"

        except Exception as e:
            status += f"ERROR: {str(e)}\n\n"

    progress(1.0, desc="Batch complete")

    zip_path = create_zip(
        f"batch_outputs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
        all_output_files,
    )

    status += "=" * 70 + "\n"
    status += "BATCH COMPLETE\n"
    status += "=" * 70 + "\n\n"

    status += "OUTPUT FOLDERS\n"
    status += "-" * 60 + "\n"
    status += f"Optimized Android: {OPTIMIZED_FOLDER}\n"
    status += f"Playable Original Size: {PLAYABLE_FOLDER}\n"
    status += f"Portrait 9:16 Cropped: {PORTRAIT_FOLDER}\n"
    status += f"600x800: {COMPACT_FOLDER}\n"

    return status, zip_path, batch_results


# ==============================
# UI
# ==============================

def create_ui():
    css = """
    .main-title {
        text-align: center;
        padding: 18px;
        border-radius: 12px;
        background: linear-gradient(135deg, #1f2937, #4f46e5);
        color: white;
        margin-bottom: 18px;
    }

    .note-box {
        padding: 12px;
        border-left: 4px solid #4f46e5;
        background: #eef2ff;
        border-radius: 8px;
        margin: 10px 0;
    }

    .folder-box {
        font-family: monospace;
        font-size: 13px;
        background: #f8fafc;
        border: 1px solid #e5e7eb;
        padding: 12px;
        border-radius: 8px;
    }
    """

    with gr.Blocks(
        title="Universal Video Processor",
        theme=gr.themes.Soft(),
        css=css,
    ) as app:

        gr.HTML("""
        <div class="main-title">
            <h1>🎬 Universal Video Processor</h1>
            <p>Generate Optimized Android, Playable Original Size, Cropped 9:16, and 600x800 videos</p>
        </div>
        """)

        with gr.Tabs():

            # ==============================
            # SINGLE VIDEO TAB
            # ==============================
            with gr.Tab("Single Video"):

                with gr.Row():
                    with gr.Column(scale=1):
                        gr.Markdown("### Upload Video")

                        single_file = gr.File(
                            label="Upload one video",
                            file_count="single",
                            file_types=["video"],
                        )

                        single_name = gr.Textbox(
                            label="Video Name / Template ID",
                            placeholder="Example: video_001",
                            lines=1,
                        )

                        single_thumb = gr.Image(
                            label="Input Preview Thumbnail",
                            type="filepath",
                            height=300,
                        )

                        gr.Markdown("### Desired Output Size (5th video, optional)")

                        single_size_choice = gr.Dropdown(
                            label="Choose an extra output size",
                            choices=["None", "600 x 800", "360 x 800", "Custom"],
                            value="600 x 800",
                        )

                        with gr.Row(visible=False) as single_custom_size_row:
                            single_custom_width = gr.Number(
                                label="Custom Width (px)",
                                value=600,
                                precision=0,
                            )
                            single_custom_height = gr.Number(
                                label="Custom Height (px)",
                                value=800,
                                precision=0,
                            )

                        single_upload_cloud = gr.Checkbox(
                            label="Upload generated videos to Cloudinary",
                            value=CLOUDINARY_CONFIGURED,
                            info=(
                                "Cloudinary is configured and ready."
                                if CLOUDINARY_CONFIGURED
                                else "Set CLOUDINARY_CLOUD_NAME / CLOUDINARY_API_KEY / "
                                     "CLOUDINARY_API_SECRET env vars to enable this."
                            ),
                        )

                        single_process_btn = gr.Button(
                            "Generate Videos",
                            variant="primary",
                            size="lg",
                        )

                    with gr.Column(scale=1):
                        gr.Markdown("### Important Behavior")

                        gr.HTML(f"""
                        <div class="note-box">
                            <b>Playable:</b> Keeps original resolution. No resize. Tries to preserve original video quality.<br><br>
                            <b>9:16:</b> Center-crops to 1080x1920. No padding.<br><br>
                            <b>Optimized:</b> Android ExoPlayer-safe 720x1280.<br><br>
                            <b>600x800:</b> Exact 600x800 with padding.<br><br>
                            <b>Custom size:</b> Pick 600x800, 360x800, or enter any width/height you need &mdash; padded to fit exactly, nothing cropped out.
                        </div>
                        """)

                        gr.HTML(f"""
                        <div class="folder-box">
                        Output folders:<br>
                        1. {OPTIMIZED_FOLDER}<br>
                        2. {PLAYABLE_FOLDER}<br>
                        3. {PORTRAIT_FOLDER}<br>
                        4. {COMPACT_FOLDER}<br>
                        5. {CUSTOM_FOLDER}
                        </div>
                        """)

                single_status = gr.Textbox(
                    label="Processing Status",
                    lines=20,
                    interactive=False,
                )

                gr.Markdown("### Output Preview")

                with gr.Row():
                    optimized_video = gr.Video(
                        label="Optimized Android 720x1280",
                        height=360,
                    )

                    playable_video = gr.Video(
                        label="Playable Original Size",
                        height=360,
                    )

                with gr.Row():
                    portrait_video = gr.Video(
                        label="Cropped Portrait 9:16 1080x1920",
                        height=360,
                    )

                    compact_video = gr.Video(
                        label="600x800",
                        height=360,
                    )

                with gr.Row():
                    custom_video = gr.Video(
                        label="Custom Size (your selection)",
                        height=360,
                    )

                single_downloads = gr.File(
                    label="Download Generated Files",
                    file_count="multiple",
                )

                single_json = gr.JSON(
                    label="Detailed Result JSON (includes Cloudinary links)",
                )

                single_file.change(
                    fn=preview_single_upload,
                    inputs=single_file,
                    outputs=[single_thumb, single_name],
                )

                single_size_choice.change(
                    fn=lambda choice: gr.update(visible=(choice == "Custom")),
                    inputs=single_size_choice,
                    outputs=single_custom_size_row,
                )

                single_process_btn.click(
                    fn=process_single_video_ui,
                    inputs=[
                        single_file,
                        single_name,
                        single_size_choice,
                        single_custom_width,
                        single_custom_height,
                        single_upload_cloud,
                    ],
                    outputs=[
                        single_status,
                        single_thumb,
                        optimized_video,
                        playable_video,
                        portrait_video,
                        compact_video,
                        custom_video,
                        single_downloads,
                        single_json,
                    ],
                )

            # ==============================
            # BATCH TAB
            # ==============================
            with gr.Tab("Batch Processing"):

                gr.Markdown("""
                ### Batch Upload

                Upload multiple videos.  
                Each video will generate all 4 outputs:

                1. Optimized Android  
                2. Playable original size  
                3. Cropped 9:16 portrait  
                4. 600x800  
                """)

                with gr.Row():
                    with gr.Column(scale=1):
                        batch_files = gr.File(
                            label="Upload multiple videos",
                            file_count="multiple",
                            file_types=["video"],
                        )

                        batch_info = gr.Textbox(
                            label="Selected Video Info",
                            lines=10,
                            interactive=False,
                        )

                        batch_size_choice = gr.Dropdown(
                            label="Choose an extra output size for every video",
                            choices=["None", "600 x 800", "360 x 800", "Custom"],
                            value="600 x 800",
                        )

                        with gr.Row(visible=False) as batch_custom_size_row:
                            batch_custom_width = gr.Number(
                                label="Custom Width (px)",
                                value=600,
                                precision=0,
                            )
                            batch_custom_height = gr.Number(
                                label="Custom Height (px)",
                                value=800,
                                precision=0,
                            )

                        batch_upload_cloud = gr.Checkbox(
                            label="Upload generated videos to Cloudinary",
                            value=CLOUDINARY_CONFIGURED,
                            info=(
                                "Cloudinary is configured and ready."
                                if CLOUDINARY_CONFIGURED
                                else "Set CLOUDINARY_CLOUD_NAME / CLOUDINARY_API_KEY / "
                                     "CLOUDINARY_API_SECRET env vars to enable this."
                            ),
                        )

                        batch_process_btn = gr.Button(
                            "Start Batch Generation",
                            variant="primary",
                            size="lg",
                        )

                    with gr.Column(scale=1):
                        gr.HTML(f"""
                        <div class="folder-box">
                        Batch output folders:<br><br>
                        Optimized Android:<br>
                        {OPTIMIZED_FOLDER}<br><br>

                        Playable Original Size:<br>
                        {PLAYABLE_FOLDER}<br><br>

                        Cropped 9:16:<br>
                        {PORTRAIT_FOLDER}<br><br>

                        600x800:<br>
                        {COMPACT_FOLDER}<br><br>

                        Custom Size:<br>
                        {CUSTOM_FOLDER}
                        </div>
                        """)

                batch_status = gr.Textbox(
                    label="Batch Status",
                    lines=22,
                    interactive=False,
                )

                batch_zip = gr.File(
                    label="Download Batch ZIP",
                )

                batch_json = gr.JSON(
                    label="Batch Result JSON",
                )

                batch_files.change(
                    fn=batch_file_info,
                    inputs=batch_files,
                    outputs=batch_info,
                )

                batch_size_choice.change(
                    fn=lambda choice: gr.update(visible=(choice == "Custom")),
                    inputs=batch_size_choice,
                    outputs=batch_custom_size_row,
                )

                batch_process_btn.click(
                    fn=process_batch_video_ui,
                    inputs=[
                        batch_files,
                        batch_size_choice,
                        batch_custom_width,
                        batch_custom_height,
                        batch_upload_cloud,
                    ],
                    outputs=[batch_status, batch_zip, batch_json],
                )

            # ==============================
            # HELP TAB
            # ==============================
            with gr.Tab("Help"):

                gr.Markdown(f"""
                ## Output Folder Structure

                This app creates only these 4 output folders inside:

                `{BASE_DIR}`

                ```text
                video_processing_workspace/
                ├── 01_optimized_android/
                ├── 02_playable_original_size/
                ├── 03_portrait_9x16_cropped/
                └── 04_600x800/
                ```

                No per-video subfolders are created.

                ---

                ## Playable Original Size

                The playable output does **not resize** your video.

                It works like this:

                1. If input video is already H.264:
                   - It copies the video stream.
                   - This preserves original video quality.
                   - It converts audio to AAC.
                   - It adds `+faststart`.

                2. If input video is not H.264:
                   - It re-encodes to H.264.
                   - It keeps the original resolution.
                   - It uses high quality `CRF 18`.

                ---

                ## Cropped 9:16 Portrait

                The 9:16 output uses this idea:

                ```text
                scale to cover 1080x1920
                then center crop
                ```

                This means the video fills the full 9:16 frame without black borders.

                ---

                ## Android Error Fix

                The optimized output helps fix errors like:

                ```text
                MediaCodecVideoRenderer error
                ExoPlaybackException
                avc1.640028
                ```

                It uses:

                - H.264
                - Baseline profile
                - Level 3.1
                - yuv420p
                - AAC audio
                - 30 FPS
                - MP4 faststart

                ---

                ## Custom Output Size

                Alongside the 4 fixed outputs, pick a 5th size from the dropdown:

                - **600 x 800** and **360 x 800** are ready-made presets.
                - **Custom** lets you type any exact width/height.

                The video is padded (not cropped) to fit your chosen size exactly,
                so nothing gets cut off.

                ---

                ## Cloudinary Upload

                Enable "Upload generated videos to Cloudinary" to push every
                successfully generated video to your Cloudinary account and get
                back a shareable `https://res.cloudinary.com/...` URL.

                This requires three environment variables to be set **before
                launching the app** (never hardcode them in the source file):

                ```text
                CLOUDINARY_CLOUD_NAME=your_cloud_name
                CLOUDINARY_API_KEY=your_api_key
                CLOUDINARY_API_SECRET=your_api_secret
                ```

                Locally, put these in a `.env` file next to this script
                (loaded automatically). On Hugging Face Spaces, set them under
                **Settings -> Variables and secrets** so they never appear in
                your repo.
                """)

        return app


# ==============================
# LAUNCH
# ==============================

if __name__ == "__main__":

    print("=" * 70)
    print("Universal Video Processor Started")
    print("=" * 70)
    print("Output folders:")
    print(f"Optimized Android:        {OPTIMIZED_FOLDER}")
    print(f"Playable Original Size:   {PLAYABLE_FOLDER}")
    print(f"Cropped 9:16 Portrait:    {PORTRAIT_FOLDER}")
    print(f"600x800:                  {COMPACT_FOLDER}")
    print(f"Custom Size:              {CUSTOM_FOLDER}")
    print("=" * 70)
    print(f"Cloudinary configured:    {CLOUDINARY_CONFIGURED}")
    print("=" * 70)

    app = create_ui()

    # Hugging Face Spaces sets SPACE_ID automatically and already exposes a
    # public URL for the app, so we don't need Gradio's own share tunnel
    # there. Running locally still gets a temporary public share link.
    running_on_spaces = bool(os.environ.get("SPACE_ID"))

    app.launch(
    server_name="0.0.0.0",
    server_port=int(os.environ.get("PORT", 7860)),
    share=False,
    show_error=True,
    debug=True,
)