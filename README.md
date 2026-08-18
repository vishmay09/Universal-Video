---
title: Universal Video Processor
emoji: 🎬
colorFrom: indigo
colorTo: blue
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

## Deploying on Render (free, no credit card)

This repo ships a `Dockerfile` that installs `ffmpeg` via `apt-get` plus all
Python deps, so Render builds and runs it exactly as-is.

1. Push this repo to GitHub.
2. On [render.com](https://render.com), create a **New Web Service** and
   connect this GitHub repo.
3. Runtime: **Docker** (Render detects the `Dockerfile` automatically).
4. Instance type: **Free**.
5. Under **Environment**, add:
   - `CLOUDINARY_CLOUD_NAME`
   - `CLOUDINARY_API_KEY`
   - `CLOUDINARY_API_SECRET`
6. Deploy. Render builds the Dockerfile and gives you a permanent public URL
   (`https://<service-name>.onrender.com`).

Free-tier services sleep after 15 minutes of inactivity and wake up
automatically (~30-60s cold start) on the next visit - otherwise this stays
live indefinitely at no cost.

Never commit real Cloudinary credentials into this repo - always set them as
Render environment variables or in a local, git-ignored `.env` file.
