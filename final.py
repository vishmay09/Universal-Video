import os
import shutil
import subprocess
from datetime import datetime
import gradio as gr
from pathlib import Path
import json
import tempfile
from PIL import Image

# ==============================
# FOLDERS - FLAT STRUCTURE
# ==============================
BASE_DIR = Path("video_processing_workspace")
OPTIMIZED_FOLDER = BASE_DIR / "01_Optimized_Android_720x1280"
PLAYABLE_FOLDER = BASE_DIR / "02_Playable_1920x1080"
PORTRAIT_9_16_FOLDER = BASE_DIR / "03_Portrait_9-16_1080x1920"
COMPACT_600_800_FOLDER = BASE_DIR / "04_Compact_600x800"
THUMBNAILS_FOLDER = BASE_DIR / "Thumbnails"
LOGS_FOLDER = BASE_DIR / "Logs"

for folder in [OPTIMIZED_FOLDER, PLAYABLE_FOLDER, PORTRAIT_9_16_FOLDER, COMPACT_600_800_FOLDER, THUMBNAILS_FOLDER, LOGS_FOLDER]:
    folder.mkdir(parents=True, exist_ok=True)

# ==============================
# PROFILE TO FOLDER MAPPING
# ==============================
PROFILE_FOLDERS = {
    "optimized": OPTIMIZED_FOLDER,
    "playable": PLAYABLE_FOLDER,
    "9_16": PORTRAIT_9_16_FOLDER,
    "600_800": COMPACT_600_800_FOLDER
}

# ==============================
# CONFIGURATION
# ==============================
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".webm", ".mpeg", ".mpg", ".3gp", ".mts", ".m2ts")

# Video profiles for different platforms
VIDEO_PROFILES = {
    "optimized": {
        "width": 720,
        "height": 1280,
        "bitrate": "1500k",
        "crf": "23",
        "preset": "medium",
        "profile": "baseline",
        "level": "3.1",
        "fps": "30",
        "description": "Android ExoPlayer Compatible",
        "folder": "01_Optimized_Android_720x1280"
    },
    "playable": {
        "width": 1920,
        "height": 1080,
        "bitrate": "3000k",
        "crf": "23",
        "preset": "medium",
        "profile": "main",
        "level": "4.0",
        "fps": "30",
        "description": "High Quality Playable",
        "folder": "02_Playable_1920x1080"
    },
    "9_16": {
        "width": 1080,
        "height": 1920,
        "bitrate": "2000k",
        "crf": "23",
        "preset": "medium",
        "profile": "main",
        "level": "4.1",
        "fps": "30",
        "description": "Portrait Social Media",
        "folder": "03_Portrait_9-16_1080x1920"
    },
    "600_800": {
        "width": 600,
        "height": 800,
        "bitrate": "1200k",
        "crf": "25",
        "preset": "medium",
        "profile": "baseline",
        "level": "3.1",
        "fps": "30",
        "description": "Compact Format",
        "folder": "04_Compact_600x800"
    }
}

# ==============================
# HELPERS
# ==============================

def safe_log(message, log_type="INFO"):
    """Safely log messages to file with UTF-8 encoding"""
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_file = LOGS_FOLDER / f"process_{datetime.now().strftime('%Y%m%d')}.log"
        with open(log_file, "a", encoding="utf-8") as f:
            clean_message = message.encode('utf-8', 'ignore').decode('utf-8')
            f.write(f"[{timestamp}] [{log_type}] {clean_message}\n")
    except Exception as e:
        print(f"Logging error: {str(e)}")

def get_size_mb(path):
    """Get file size in MB"""
    if os.path.exists(path):
        return os.path.getsize(path) / (1024 * 1024)
    return 0

def get_video_duration(video_path):
    """Get video duration"""
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1:noesc=1",
            str(video_path)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        duration = float(result.stdout.strip()) if result.stdout.strip() else 0
        return duration
    except Exception as e:
        safe_log(f"Get video duration error: {str(e)}", "ERROR")
        return 0

def generate_thumbnail(input_path, output_path, timestamp="00:00:01"):
    """Generate thumbnail from video"""
    try:
        subprocess.run([
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-ss", timestamp,
            "-vframes", "1",
            "-vf", "scale=320:240",
            str(output_path)
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        
        if os.path.exists(output_path):
            return True
        return False
    except Exception as e:
        safe_log(f"Thumbnail generation failed: {str(e)}", "ERROR")
        return False

def convert_video(input_path, output_path, profile_name):
    """Convert video with specified profile"""
    profile = VIDEO_PROFILES[profile_name]
    
    try:
        # Advanced filter chain for better Android compatibility
        vf_filter = (
            f"scale={profile['width']}:{profile['height']}:"
            f"force_original_aspect_ratio=decrease,"
            f"pad={profile['width']}:{profile['height']}:(ow-iw)/2:(oh-ih)/2,"
            f"format=yuv420p"
        )
        
        command = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-vf", vf_filter,
            "-c:v", "libx264",
            "-preset", profile['preset'],
            "-crf", profile['crf'],
            "-b:v", profile['bitrate'],
            "-maxrate", f"{int(int(profile['bitrate'].rstrip('k')) * 1.2)}k",
            "-bufsize", f"{int(int(profile['bitrate'].rstrip('k')) * 2)}k",
            "-r", profile['fps'],
            "-profile:v", profile['profile'],
            "-level", profile['level'],
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "128k",
            "-ar", "48000",
            "-movflags", "+faststart",
            "-fflags", "+bitexact",
            "-flags:v", "+bitexact",
            str(output_path)
        ]
        
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        
        stdout, stderr = process.communicate(timeout=3600)
        
        if process.returncode == 0:
            safe_log(f"Successfully converted to {profile_name}: {output_path}", "SUCCESS")
            return True
        else:
            safe_log(f"Conversion failed for {profile_name}: {stderr}", "ERROR")
            return False
            
    except subprocess.TimeoutExpired:
        process.kill()
        safe_log(f"Conversion timeout for {profile_name}: {output_path}", "ERROR")
        return False
    except Exception as e:
        safe_log(f"Conversion error for {profile_name}: {str(e)}", "ERROR")
        return False

def get_preview_image(video_path):
    """Get preview thumbnail for display"""
    try:
        temp_thumb = Path(tempfile.gettempdir()) / f"preview_{datetime.now().timestamp()}.jpg"
        if generate_thumbnail(video_path, temp_thumb):
            return str(temp_thumb)
    except:
        pass
    return None

def format_time(seconds):
    """Format seconds to MM:SS format"""
    mins = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{mins:02d}:{secs:02d}"

# ==============================
# MAIN PROCESS FUNCTION
# ==============================

def process_single_video(video_file, template_id, selected_profiles):
    """Process a single video with selected profiles"""
    
    try:
        if video_file is None:
            return "ERROR: Missing video file", None, {}, None, None
        
        if not template_id or not template_id.strip():
            return "ERROR: Missing Template ID", None, {}, None, None
        
        if not selected_profiles:
            return "ERROR: Select at least one profile", None, {}, None, None
        
        # Clean template ID
        template_id = template_id.strip().replace(" ", "_").replace("/", "_").replace("\\", "_")
        template_id = "".join(c for c in template_id if c.isalnum() or c in ('-', '_'))
        
        # Copy input to temp location
        temp_input = Path(tempfile.gettempdir()) / f"{template_id}_temp_{datetime.now().timestamp()}.mp4"
        shutil.copy(video_file, temp_input)
        
        # Get video info
        duration = get_video_duration(temp_input)
        original_size = get_size_mb(temp_input)
        
        status_text = f"=== VIDEO PROCESSING ===\n"
        status_text += f"Template ID: {template_id}\n"
        status_text += f"Original Size: {original_size:.2f} MB\n"
        status_text += f"Duration: {format_time(duration)}\n"
        status_text += "-" * 40 + "\n\n"
        
        # Generate thumbnail
        thumb_path = THUMBNAILS_FOLDER / f"{template_id}.jpg"
        preview_img = None
        
        if generate_thumbnail(temp_input, thumb_path):
            preview_img = str(thumb_path)
            status_text += "[OK] Thumbnail generated\n"
        
        results = {}
        status_text += "-" * 40 + "\n"
        status_text += "CONVERSION RESULTS:\n"
        status_text += "-" * 40 + "\n\n"
        
        # Process each selected profile
        for profile_name in selected_profiles:
            profile = VIDEO_PROFILES[profile_name]
            output_folder = PROFILE_FOLDERS[profile_name]
            
            # Create filename with profile info
            output_filename = f"{template_id}_{profile['width']}x{profile['height']}.mp4"
            output_path = output_folder / output_filename
            
            status_text += f"Converting to {profile_name}...\n"
            
            if convert_video(temp_input, output_path, profile_name):
                final_size = get_size_mb(output_path)
                reduction = ((original_size - final_size) / original_size * 100) if original_size > 0 else 0
                
                results[profile_name] = {
                    "filename": output_filename,
                    "path": str(output_path),
                    "folder": str(output_folder),
                    "size": f"{final_size:.2f} MB",
                    "original_size": f"{original_size:.2f} MB",
                    "reduction": f"{reduction:.2f}%",
                    "description": profile['description'],
                    "resolution": f"{profile['width']}x{profile['height']}"
                }
                
                status_text += f"  [OK] {output_filename}\n"
                status_text += f"       Size: {final_size:.2f} MB | Reduced: {reduction:.2f}%\n"
                status_text += f"       Location: {output_folder}\n\n"
            else:
                status_text += f"  [FAILED] Conversion error\n\n"
        
        # Cleanup temp file
        try:
            os.remove(temp_input)
        except:
            pass
        
        status_text += "-" * 40 + "\n"
        status_text += "[COMPLETE] Processing finished!\n"
        status_text += "-" * 40 + "\n\n"
        status_text += "VIDEO LOCATIONS:\n"
        
        for profile_name in selected_profiles:
            if profile_name in results:
                folder = PROFILE_FOLDERS[profile_name]
                status_text += f"\n{profile_name.upper()}:\n"
                status_text += f"  Folder: {folder}\n"
        
        return status_text, preview_img, results, template_id, str(datetime.now())
        
    except Exception as e:
        error_msg = f"ERROR: {str(e)}"
        safe_log(error_msg, "ERROR")
        return error_msg, None, {}, None, None

def process_batch_videos(batch_files, selected_profiles):
    """Process multiple uploaded videos"""
    
    if not batch_files:
        return "ERROR: No videos selected", None
    
    if not selected_profiles:
        return "ERROR: Select at least one profile", None
    
    status_text = f"{'=' * 60}\n"
    status_text += f"BATCH PROCESSING STARTED\n"
    status_text += f"{'=' * 60}\n"
    status_text += f"Total Videos: {len(batch_files)}\n"
    status_text += f"Selected Profiles: {', '.join(selected_profiles)}\n"
    status_text += f"{'=' * 60}\n\n"
    
    batch_results = {}
    success_count = 0
    failed_count = 0
    
    for idx, video_file in enumerate(batch_files, 1):
        try:
            # Extract filename without extension
            template_id = Path(video_file).stem
            
            status_text += f"[{idx}/{len(batch_files)}] Processing: {template_id}\n"
            status_text += "-" * 60 + "\n"
            
            result_text, thumb, results, vid_id, timestamp = process_single_video(
                video_file, template_id, selected_profiles
            )
            
            if "ERROR" not in result_text:
                batch_results[template_id] = results
                success_count += 1
                status_text += "[OK] Completed successfully\n\n"
            else:
                failed_count += 1
                status_text += f"[FAILED] {result_text}\n\n"
            
        except Exception as e:
            status_text += f"[ERROR] {str(e)}\n\n"
            safe_log(f"Batch processing error: {str(e)}", "ERROR")
            failed_count += 1
    
    status_text += f"{'=' * 60}\n"
    status_text += f"BATCH PROCESSING COMPLETE\n"
    status_text += f"{'=' * 60}\n"
    status_text += f"Success: {success_count} | Failed: {failed_count}\n\n"
    
    status_text += "OUTPUT LOCATIONS:\n"
    status_text += "-" * 60 + "\n"
    for profile_name, folder in PROFILE_FOLDERS.items():
        status_text += f"{profile_name.upper()}: {folder}\n"
    
    return status_text, json.dumps(batch_results, indent=2)

def get_folder_structure():
    """Get current folder structure with file counts"""
    structure = {}
    
    for profile_name, folder in PROFILE_FOLDERS.items():
        if folder.exists():
            files = list(folder.glob("*.mp4"))
            structure[profile_name] = {
                "path": str(folder),
                "count": len(files),
                "files": [f.name for f in files[:10]]  # Show first 10 files
            }
    
    return structure

# ==============================
# UI - GRADIO INTERFACE
# ==============================

def create_ui():
    css = """
    .header {
        text-align: center;
        padding: 20px;
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        color: white;
        border-radius: 10px;
        margin-bottom: 20px;
    }
    .info-box {
        background: #f0f4ff;
        border-left: 4px solid #667eea;
        padding: 15px;
        border-radius: 5px;
        margin: 10px 0;
    }
    .success-box {
        background: #d4edda;
        border-left: 4px solid #28a745;
        padding: 15px;
        border-radius: 5px;
    }
    .error-box {
        background: #f8d7da;
        border-left: 4px solid #dc3545;
        padding: 15px;
        border-radius: 5px;
    }
    .folder-box {
        background: #e7f3ff;
        border-left: 4px solid #0066cc;
        padding: 15px;
        border-radius: 5px;
        margin: 10px 0;
        font-family: monospace;
        font-size: 12px;
    }
    """
    
    with gr.Blocks(title="Universal Video Processor", theme=gr.themes.Soft(), css=css) as app:
        
        gr.HTML("""
        <div class='header'>
            <h1>🎬 Universal Video Processor</h1>
            <p>Convert videos to Android-compatible formats | Solve MediaCodecVideoRenderer errors</p>
        </div>
        """)
        
        with gr.Tabs():
            
            # ==================== TAB 1: SINGLE VIDEO ====================
            with gr.Tab("🎯 Single Video Processing"):
                
                with gr.Row():
                    with gr.Column(scale=2):
                        gr.Markdown("### 📤 Upload Video")
                        video_input = gr.File(
                            label="Select Video File",
                            file_count="single",
                            file_types=["video"]
                        )
                        
                        video_preview = gr.Image(
                            label="Preview Thumbnail",
                            type="filepath"
                        )
                    
                    with gr.Column(scale=1):
                        gr.Markdown("### ⚙️ Settings")
                        
                        template_id = gr.Textbox(
                            label="Video Name/ID",
                            placeholder="e.g., video_001",
                            lines=1
                        )
                        
                        gr.Markdown("### 📊 Output Profiles")
                        profiles = gr.CheckboxGroup(
                            choices=[
                                ("Optimized (720x1280) - Android", "optimized"),
                                ("Playable (1920x1080) - Desktop", "playable"),
                                ("Portrait (1080x1920) - Social", "9_16"),
                                ("Compact (600x800)", "600_800")
                            ],
                            value=["optimized"],
                            label="Select Formats"
                        )
                
                with gr.Row():
                    process_btn = gr.Button("🚀 Process Video", size="lg", variant="primary")
                    clear_btn = gr.Button("Clear All", size="lg")
                
                output_text = gr.Textbox(
                    label="Processing Status",
                    lines=15,
                    interactive=False,
                    max_lines=20
                )
                
                results_json = gr.JSON(label="Detailed Results")
                
                # Event handlers
                def process_with_preview(video_file, template_id_val, profiles_val):
                    if video_file and template_id_val and profiles_val:
                        preview = get_preview_image(video_file)
                        result = process_single_video(video_file, template_id_val, profiles_val)
                        return result + (preview,)
                    return "ERROR: Fill all fields", None, {}, None, None, None
                
                process_btn.click(
                    fn=process_with_preview,
                    inputs=[video_input, template_id, profiles],
                    outputs=[output_text, video_preview, results_json, template_id, gr.State(), video_preview]
                )
                
                clear_btn.click(
                    fn=lambda: (None, "", [], None, "", {}),
                    outputs=[video_input, template_id, profiles, output_text, gr.State(), results_json]
                )
            
            # ==================== TAB 2: BATCH PROCESSING ====================
            with gr.Tab("📦 Batch Processing"):
                
                gr.Markdown("""
                ### 📂 Process Multiple Videos
                
                Upload multiple videos and convert them all at once to selected formats.
                """)
                
                with gr.Row():
                    with gr.Column(scale=2):
                        gr.Markdown("### 📤 Upload Videos")
                        batch_files = gr.File(
                            label="Select Multiple Videos",
                            file_count="multiple",
                            file_types=["video"]
                        )
                        
                        batch_file_info = gr.Textbox(
                            label="Selected Videos Info",
                            interactive=False,
                            lines=5
                        )
                    
                    with gr.Column(scale=1):
                        gr.Markdown("### ⚙️ Batch Settings")
                        
                        batch_profiles = gr.CheckboxGroup(
                            choices=[
                                ("Optimized (720x1280)", "optimized"),
                                ("Playable (1920x1080)", "playable"),
                                ("Portrait (1080x1920)", "9_16"),
                                ("Compact (600x800)", "600_800")
                            ],
                            value=["optimized", "600_800"],
                            label="Select Formats"
                        )
                
                def update_file_info(files):
                    if files:
                        info = f"Files Selected: {len(files)}\n"
                        info += "-" * 40 + "\n"
                        for f in files:
                            size_mb = os.path.getsize(f) / (1024 * 1024)
                            info += f"{Path(f).name} - {size_mb:.2f} MB\n"
                        return info
                    return "No videos selected"
                
                batch_files.change(
                    fn=update_file_info,
                    inputs=batch_files,
                    outputs=batch_file_info
                )
                
                with gr.Row():
                    batch_btn = gr.Button("🚀 Start Batch Processing", size="lg", variant="primary")
                    batch_clear_btn = gr.Button("Clear All", size="lg")
                
                batch_status = gr.Textbox(
                    label="Batch Processing Status",
                    lines=15,
                    interactive=False,
                    max_lines=20
                )
                
                batch_results_json = gr.Textbox(
                    label="Batch Results (JSON)",
                    lines=10,
                    interactive=False
                )
                
                batch_btn.click(
                    fn=process_batch_videos,
                    inputs=[batch_files, batch_profiles],
                    outputs=[batch_status, batch_results_json]
                )
                
                batch_clear_btn.click(
                    fn=lambda: (None, "", "No videos selected", "", ""),
                    outputs=[batch_files, batch_status, batch_file_info, batch_results_json, batch_profiles]
                )
            
            # ==================== TAB 3: FOLDER MANAGEMENT ====================
            with gr.Tab("📁 Output Folders"):
                
                gr.Markdown("""
                ### 📂 Video Output Locations
                
                All converted videos are saved in these folders:
                """)
                
                folder_info = gr.Textbox(
                    label="Folder Structure",
                    interactive=False,
                    lines=15,
                    max_lines=20
                )
                
                def update_folder_info():
                    structure = get_folder_structure()
                    info = ""
                    
                    info += "=" * 70 + "\n"
                    info += "VIDEO OUTPUT FOLDERS\n"
                    info += "=" * 70 + "\n\n"
                    
                    for profile_name, profile_data in structure.items():
                        profile = VIDEO_PROFILES[profile_name]
                        info += f"[{profile_name.upper()}] {profile['description']}\n"
                        info += f"Resolution: {profile['width']}x{profile['height']}\n"
                        info += f"Path: {profile_data['path']}\n"
                        info += f"Files: {profile_data['count']}\n"
                        
                        if profile_data['files']:
                            info += "Recent Files:\n"
                            for fname in profile_data['files'][:5]:
                                info += f"  - {fname}\n"
                        info += "\n"
                    
                    return info
                
                refresh_btn = gr.Button("Refresh Folder Info", variant="secondary")
                refresh_btn.click(
                    fn=update_folder_info,
                    outputs=folder_info
                )
                
                # Initial load
                folder_info.value = update_folder_info()
            
            # ==================== TAB 4: INFORMATION ====================
            with gr.Tab("ℹ️ Help & Information"):
                
                with gr.Tabs():
                    with gr.Tab("Video Profiles"):
                        gr.Markdown("""
                        ## Video Conversion Profiles
                        
                        ### 1️⃣ Optimized (720x1280) - Android ExoPlayer
                        - **Resolution:** 720x1280 (Portrait)
                        - **Bitrate:** 1500 kbps
                        - **Profile:** Baseline
                        - **Level:** 3.1
                        - **Best For:** Android devices, solving MediaCodecVideoRenderer errors
                        - **File Size:** Small
                        - **Quality:** Good
                        
                        **Folder:** `01_Optimized_Android_720x1280`
                        
                        ---
                        
                        ### 2️⃣ Playable (1920x1080) - Desktop/Web
                        - **Resolution:** 1920x1080 (Landscape)
                        - **Bitrate:** 3000 kbps
                        - **Profile:** Main
                        - **Level:** 4.0
                        - **Best For:** Desktop viewing, web playback
                        - **File Size:** Large
                        - **Quality:** Excellent
                        
                        **Folder:** `02_Playable_1920x1080`
                        
                        ---
                        
                        ### 3️⃣ Portrait (1080x1920) - Social Media
                        - **Resolution:** 1080x1920 (Portrait)
                        - **Bitrate:** 2000 kbps
                        - **Profile:** Main
                        - **Level:** 4.1
                        - **Best For:** TikTok, Instagram Reels, YouTube Shorts
                        - **File Size:** Medium
                        - **Quality:** Excellent
                        
                        **Folder:** `03_Portrait_9-16_1080x1920`
                        
                        ---
                        
                        ### 4️⃣ Compact (600x800) - Thumbnails & Previews
                        - **Resolution:** 600x800 (Portrait)
                        - **Bitrate:** 1200 kbps
                        - **Profile:** Baseline
                        - **Level:** 3.1
                        - **Best For:** Quick streaming, storage, previews
                        - **File Size:** Very Small
                        - **Quality:** Good
                        
                        **Folder:** `04_Compact_600x800`
                        """)
                    
                    with gr.Tab("FAQ"):
                        gr.Markdown("""
                        ## Frequently Asked Questions
                        
                        ### Why MediaCodecVideoRenderer errors on Android?
                        
                        **Problem:** Some videos fail to play on Android devices with ExoPlayer
                        
                        **Cause:** 
                        - Incompatible video codec
                        - Wrong profile or level settings
                        - Unsupported pixel format
                        - High bitrate for device
                        
                        **Solution:** Use "Optimized" profile (720x1280, Baseline)
                        
                        ---
                        
                        ### Which profile should I use?
                        
                        | Use Case | Profile | Why |
                        |----------|---------|-----|
                        | Android App | Optimized | Maximum compatibility |
                        | Desktop App | Playable | Best quality |
                        | TikTok/Reels | 9:16 Portrait | Perfect aspect ratio |
                        | Storage/Web | 600x800 | Small file size |
                        | Multiple | All 4 | Future-proof |
                        
                        ---
                        
                        ### How long does processing take?
                        
                        Processing time depends on:
                        - Original video length
                        - Original resolution
                        - Selected profiles
                        - Computer performance
                        
                        **Typical Times:**
                        - 1 minute video: 1-3 minutes
                        - 10 minute video: 10-30 minutes
                        - 1 hour video: 1-3 hours
                        
                        ---
                        
                        ### Where are the output videos?
                        
                        All videos are saved in flat folders (no subfolders):
                        
                        ```
                        video_processing_workspace/
                        ├── 01_Optimized_Android_720x1280/
                        │   ├── video_001_720x1280.mp4
                        │   ├── video_002_720x1280.mp4
                        │   └── ...
                        ├── 02_Playable_1920x1080/
                        │   ├── video_001_1920x1080.mp4
                        │   └── ...
                        ├── 03_Portrait_9-16_1080x1920/
                        │   └── ...
                        ├── 04_Compact_600x800/
                        │   └── ...
                        └── Thumbnails/
                            ├── video_001.jpg
                            └── ...
                        ```
                        
                        ---
                        
                        ### Can I process batch videos?
                        
                        **Yes!** Use the "Batch Processing" tab:
                        1. Upload multiple videos
                        2. Select profiles
                        3. Click "Start Batch Processing"
                        4. All videos convert automatically
                        
                        ---
                        
                        ### What video formats are supported?
                        
                        **Input:** .mp4, .mkv, .avi, .mov, .flv, .wmv, .webm, .mpeg, .mpg, .3gp, .mts, .m2ts
                        
                        **Output:** .mp4 (H.264 codec, AAC audio)
                        
                        ---
                        
                        ### Do I lose quality?
                        
                        Quality depends on profile:
                        - **Optimized:** Good (optimized for compatibility)
                        - **Playable:** Excellent (high bitrate)
                        - **9:16 Portrait:** Excellent (high bitrate)
                        - **600x800:** Good (optimized for size)
                        
                        Original quality is generally preserved.
                        """)
                    
                    with gr.Tab("Technical Details"):
                        gr.Markdown("""
                        ## Technical Specifications
                        
                        ### Video Codec
                        - **Codec:** H.264 (libx264)
                        - **Why:** Maximum compatibility, widely supported
                        
                        ### Audio Codec
                        - **Codec:** AAC
                        - **Bitrate:** 128 kbps
                        - **Sample Rate:** 48 kHz
                        - **Why:** Widely supported, good quality
                        
                        ### Pixel Format
                        - **Format:** YUV420p
                        - **Why:** Maximum Android compatibility
                        
                        ### Frame Rate
                        - **FPS:** 30 (constant)
                        - **Why:** Optimal for mobile devices
                        
                        ### Encoding Features
                        - ✅ Fast start enabled (streaming optimization)
                        - ✅ Bitexact flag (consistency)
                        - ✅ Proper buffer sizing (smooth playback)
                        - ✅ Aspect ratio preservation
                        - ✅ Automatic scaling & padding
                        
                        ### Quality Settings
                        - **CRF:** 23-25 (high quality)
                        - **Preset:** Medium (balanced speed/quality)
                        - **Profile:** Baseline or Main (compatibility)
                        
                        ### Output Files
                        - **Extension:** .mp4
                        - **Container:** MPEG-4
                        - **Streaming:** Optimized with moov atom at start
                        
                        ---
                        
                        ## Folder Structure
                        
                        ```
                        video_processing_workspace/
                        ├── 01_Optimized_Android_720x1280/     (Android-safe videos)
                        ├── 02_Playable_1920x1080/             (High-quality videos)
                        ├── 03_Portrait_9-16_1080x1920/        (Social media videos)
                        ├── 04_Compact_600x800/                (Compact videos)
                        ├── Thumbnails/                        (Preview images)
                        └── Logs/                              (Processing logs)
                        ```
                        
                        **Note:** No subfolders! All videos in each folder have unique names.
                        """)
                    
                    with gr.Tab("Troubleshooting"):
                        gr.Markdown("""
                        ## Troubleshooting
                        
                        ### Video won't convert
                        
                        **Problem:** Conversion fails or hangs
                        
                        **Solutions:**
                        1. Check if FFmpeg is installed: `ffmpeg -version`
                        2. Try a shorter video first
                        3. Check disk space (need 2-3x original size)
                        4. Restart the application
                        5. Check logs in `Logs/` folder
                        
                        ---
                        
                        ### FFmpeg not found
                        
                        **Error:** "ffmpeg not found"
                        
                        **Installation:**
                        ```bash
                        # Ubuntu/Debian
                        sudo apt-get install ffmpeg
                        
                        # macOS (Homebrew)
                        brew install ffmpeg
                        
                        # Windows (Chocolatey)
                        choco install ffmpeg
                        ```
                        
                        ---
                        
                        ### Video plays on desktop but not on Android
                        
                        **Solution:** Use "Optimized" profile (720x1280)
                        
                        This profile is specifically designed for Android ExoPlayer compatibility.
                        
                        ---
                        
                        ### Thumbnail not generated
                        
                        **Problem:** Preview image shows black/nothing
                        
                        **Possible causes:**
                        - Video too short
                        - Video corrupted
                        - Unusual codec
                        
                        **Solution:** Processing still works; thumbnail is just preview
                        
                        ---
                        
                        ### Slow processing
                        
                        **Why it takes time:**
                        - Video encoding is CPU intensive
                        - Quality settings require more computation
                        - Large videos take longer
                        
                        **To speed up:**
                        - Close other applications
                        - Use lower bitrate (edit VIDEO_PROFILES)
                        - Process shorter videos first
                        
                        ---
                        
                        ### Check Logs
                        
                        All processing errors are logged in:
                        ```
                        video_processing_workspace/Logs/process_YYYYMMDD.log
                        ```
                        
                        Check here if something goes wrong.
                        """)
        
        return app

# ==============================
# LAUNCH APPLICATION
# ==============================

if __name__ == "__main__":
    # Create requirements file
    requirements = """gradio==4.26.0
ffmpeg-python==0.2.1
pillow==10.1.0
tqdm==4.66.1
"""
    with open("requirements.txt", "w") as f:
        f.write(requirements)
    
    safe_log("=== APPLICATION STARTED ===", "INFO")
    safe_log(f"Optimized folder: {OPTIMIZED_FOLDER}", "INFO")
    safe_log(f"Playable folder: {PLAYABLE_FOLDER}", "INFO")
    safe_log(f"Portrait folder: {PORTRAIT_9_16_FOLDER}", "INFO")
    safe_log(f"Compact folder: {COMPACT_600_800_FOLDER}", "INFO")
    
    app = create_ui()
    
    print("\n" + "=" * 70)
    print("🎬 UNIVERSAL VIDEO PROCESSOR - STARTED")
    print("=" * 70)
    print("\nOUTPUT FOLDERS:")
    print(f"  Optimized: {OPTIMIZED_FOLDER}")
    print(f"  Playable: {PLAYABLE_FOLDER}")
    print(f"  Portrait 9:16: {PORTRAIT_9_16_FOLDER}")
    print(f"  Compact 600x800: {COMPACT_600_800_FOLDER}")
    print(f"  Thumbnails: {THUMBNAILS_FOLDER}")
    print(f"  Logs: {LOGS_FOLDER}")
    print("\n" + "=" * 70 + "\n")
    
    app.launch(
        share=True,
        
        server_port=7860,
        show_error=True,
        debug=True
    )