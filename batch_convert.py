import os
import subprocess
import uuid
from pathlib import Path

def log(msg):
    print(f"[INFO] {msg}")

def err(msg):
    print(f"[ERROR] {msg}")

def optimize_video(input_path, output_path):
    command = [
        "ffmpeg",
        "-y",
        "-i", str(input_path),

        # ✅ FIXED 9:16
        "-vf", "scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280",

        "-c:v", "libx264",
        "-profile:v", "baseline",
        "-level", "3.1",
        "-pix_fmt", "yuv420p",

        "-r", "30",
        "-b:v", "1800k",
        "-maxrate", "1800k",
        "-bufsize", "3600k",

        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "44100",

        "-movflags", "+faststart",

        str(output_path),
    ]

    try:
        subprocess.run(command, check=True)
        return True
    except subprocess.CalledProcessError as e:
        err(f"FFmpeg failed: {input_path}")
        return False


def batch_convert(input_folder, output_folder):
    input_folder = Path(input_folder)
    output_folder = Path(output_folder)

    output_folder.mkdir(parents=True, exist_ok=True)

    video_ext = (".mp4", ".mov", ".mkv", ".avi", ".webm")
    files = [f for f in input_folder.iterdir() if f.suffix.lower() in video_ext]

    log(f"Found {len(files)} videos")

    success = 0
    failed = 0

    for i, file in enumerate(files, 1):
        output_file = output_folder / f"opt_{i}_{uuid.uuid4().hex[:6]}.mp4"

        log(f"Processing: {file.name}")

        ok = optimize_video(file, output_file)

        if ok:
            log(f"✅ Done: {file.name}")
            success += 1
        else:
            log(f"❌ Failed: {file.name}")
            failed += 1

    log("=================================")
    log(f"Finished")
    log(f"Success: {success}")
    log(f"Failed: {failed}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3:
        print("Usage: python batch_convert.py <input_folder> <output_folder>")
        exit(1)

    batch_convert(sys.argv[1], sys.argv[2])