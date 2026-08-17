---
title: Universal Video Processor
emoji: 🎬
colorFrom: indigo
colorTo: blue
sdk: gradio
sdk_version: 4.26.0
app_file: final1.py
pinned: false
---

# Universal Video Processor

Generates Optimized Android, Playable Original Size, Cropped 9:16, 600x800,
and a user-chosen custom size (including a 360x800 preset) from an uploaded
video, with optional automatic upload of every output to Cloudinary.

## Running locally

```bash
pip install -r requirements.txt
# ffmpeg + ffprobe must also be installed and on PATH
```

Create a `.env` file (see `.env.example`) with your Cloudinary credentials,
then run:

```bash
python final1.py
```

## Deploying on Hugging Face Spaces

1. Create a new Space with SDK = **Gradio**.
2. Push this repo's contents (`final1.py`, `requirements.txt`, `packages.txt`,
   this `README.md`) to the Space's git remote.
3. In the Space's **Settings -> Variables and secrets**, add:
   - `CLOUDINARY_CLOUD_NAME`
   - `CLOUDINARY_API_KEY`
   - `CLOUDINARY_API_SECRET`
4. The Space builds automatically and gives you a permanent public URL
   (`https://huggingface.co/spaces/<you>/<space-name>`) that stays up
   without your machine needing to run anything.

`packages.txt` installs `ffmpeg` on the Space's Linux build image, which this
app requires for all video conversions.

Never commit real Cloudinary credentials into this repo - always set them as
Space secrets or in a local, git-ignored `.env` file.
