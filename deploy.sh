#!/bin/bash
# Deploy to Hugging Face Spaces

echo "Preparing for deployment..."

# Create necessary files
echo "Creating requirements.txt..."
cat > requirements.txt << 'EOF'
gradio==4.26.0
ffmpeg-python==0.2.1
pillow==10.1.0
tqdm==4.66.1
EOF

echo "Creating README.md..."
cat > README.md << 'EOF'
# Universal Video Processor

Convert videos to Android-compatible formats with multiple output options.

Solves MediaCodecVideoRenderer ExoPlaybackException on Android devices.

## Features

- Convert to 4 different video formats
- Batch processing support
- Thumbnail generation
- Preview images
- JSON result export
- Windows/Mac/Linux support

## Profiles

1. **Optimized** (720x1280) - Android ExoPlayer
2. **Playable** (1920x1080) - High quality
3. **9:16 Portrait** (1080x1920) - Social media
4. **600x800** - Compact format

## Usage

1. Upload video
2. Enter template ID
3. Select profiles
4. Click Process

## Deployment

Deployed on Hugging Face Spaces with GPU support.
EOF

echo "Deployment files ready!"
echo "Run: gradio deploy"
