"""
Unified Auto-Uploader: Instagram Reels + Facebook Reels
---------------------------------------------------------
Flow for each video:
  1. List & sort .mp4 files from Google Drive (Reel project credentials)
  2. Download the first file locally
  3. Convert to 9:16 (1080x1920) with blurred background via FFmpeg
  4. Upload converted file to Cloudinary
  5. Publish to Instagram Reels via Cloudinary URL
  6. Publish to Facebook Reels via Cloudinary URL
  7. Delete from Cloudinary
  8. Append video record (Drive ID, IG post ID, FB post ID, timestamp) to a
     JSON file in GitHub (creates the file if it doesn't exist yet)
  9. Write full session log to logs/upload_YYYY-MM-DD_HH-MM-SS.txt
    10. Upload log to Google Drive

Auth / env vars required:
  ┌─────────────────────────┬──────────────────────────────────────────────────┐
  │ Env var                 │ What it is                                       │
  ├─────────────────────────┼──────────────────────────────────────────────────┤
  │ GOOGLE_TOKEN            │ Full JSON content of token.json (Drive + IG      │
  │                         │ project — Reel project)                          │
  │ IG_ACCESS_TOKEN         │ Instagram Graph API user access token            │
  │ IG_ID                   │ Instagram Business / Creator account ID          │
  │ FB_ACCESS_TOKEN         │ Facebook Page access token (long-lived)          │
  │ FB_PAGE_ID              │ Numeric Facebook Page ID                         │
  │ CLOUDINARY_CLOUD_NAME   │ Cloudinary cloud name                            │
  │ CLOUDINARY_API_KEY      │ Cloudinary API key                               │
  │ CLOUDINARY_API_SECRET   │ Cloudinary API secret                            │
  │ GITHUB_PAT              │ GitHub Personal Access Token (needs repo scope)  │
  │ GITHUB_REPO             │ Repo in "username/repo-name" format              │
  │ GITHUB_JSON_PATH        │ Path inside repo, e.g. "data/processed.json"    │
  └─────────────────────────┴──────────────────────────────────────────────────┘
"""

import os
import re
import time
import json
import base64
import shutil
import tempfile
import subprocess
import requests
import cloudinary
import cloudinary.uploader
from datetime import datetime

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload


# ══════════════════════════════════════════════════════════
#  LOGGER — every print also writes to a session log file
# ══════════════════════════════════════════════════════════

SESSION_TIME = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
LOG_DIR      = "logs"
LOG_FILE     = os.path.join(LOG_DIR, f"upload_{SESSION_TIME}.txt")

os.makedirs(LOG_DIR, exist_ok=True)

_log_handle = open(LOG_FILE, "w", encoding="utf-8")


def log(msg=""):
    """Print to console AND write to session log file."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    _log_handle.write(line + "\n")
    _log_handle.flush()


def close_log():
    _log_handle.close()


# ══════════════════════════════════════════════════════════
#  LOGGER UPLOAD — upload log file to Google Drive
# ══════════════════════════════════════════════════════════

LOGS_FOLDER_ID = "1bniekPJ8HPIGOHAJK602KuhmQ5XmWKfB"


def upload_log_to_drive(drive):
    """Upload session log file to Google Drive logs folder."""
    log("☁️  Uploading log file to Google Drive...")

    file_metadata = {
        "name":    os.path.basename(LOG_FILE),
        "parents": [LOGS_FOLDER_ID],
    }
    media = MediaFileUpload(LOG_FILE, mimetype="text/plain")

    uploaded = drive.files().create(
        body=file_metadata,
        media_body=media,
        fields="id,name",
    ).execute()

    log(f"✅ Log uploaded to Drive: {uploaded['name']}")


# ══════════════════════════════════════════════════════════
#  CONFIGURATION
# ══════════════════════════════════════════════════════════

# ── Google Drive ──────────────────────────────────────────
DRIVE_FOLDER_ID  = "1SkQgsJRR9G3lRYQlFzyR3wXz8gyjg4l3"
DRIVE_IG_SCOPES  = ["https://www.googleapis.com/auth/drive"]

# ── Instagram ─────────────────────────────────────────────
IG_ACCESS_TOKEN = os.environ["IG_ACCESS_TOKEN"]
INSTAGRAM_ID    = os.environ["IG_ID"]

IG_CAPTION = """What starts as justice slowly turns into obsession… and that's what makes Death Note one of the greatest psychological thriller anime of all time.

The battle between Light Yagami and L isn't just about intelligence — it's a war of ideology, ego, manipulation, and power. Every episode keeps raising the tension, every move feels like a chess match, and every scene reminds us why Death Note became a legendary anime worldwide.

This scene perfectly captures the dark atmosphere, genius writing, intense mind games, and iconic character development that made Death Note a masterpiece for anime fans.

🔥 Follow for more anime edits, viral anime moments, and legendary scenes.

#DeathNote #DeathNoteEdit #LightYagami #LLawliet #Kira #Ryuk #Anime #AnimeEdit #AnimeReels #AnimeScene #PsychologicalAnime #ThrillerAnime #AnimeFans #Otaku #Weeb #Manga #AnimeLover #AnimeCommunity #AnimeClips #AnimeMoments #JapaneseAnime #DarkAnime #MindGames #AnimeTrending #ViralAnime #AnimeAesthetic #AnimeShorts #AnimeVideo #AnimeContent #AnimeWorld"""

# ── Facebook ──────────────────────────────────────────────
FB_ACCESS_TOKEN = os.environ["FB_ACCESS_TOKEN"]
FB_PAGE_ID      = os.environ["FB_PAGE_ID"]

FB_CAPTION = IG_CAPTION   # reuse the same caption; change if you want a different one

# ── Cloudinary ────────────────────────────────────────────
cloudinary.config(
    cloud_name = os.environ["CLOUDINARY_CLOUD_NAME"],
    api_key    = os.environ["CLOUDINARY_API_KEY"],
    api_secret = os.environ["CLOUDINARY_API_SECRET"],
)

# ── GitHub JSON tracker ───────────────────────────────────
GITHUB_PAT       = os.environ["GITHUB_PAT"]
GITHUB_REPO      = os.environ["GITHUB_REPO"]        # e.g. "john/video-tracker"
GITHUB_JSON_PATH = os.environ["GITHUB_JSON_PATH"]   # e.g. "data/processed_videos.json"
GITHUB_API_BASE  = "https://api.github.com"


# ══════════════════════════════════════════════════════════
#  FFMPEG CHECK
# ══════════════════════════════════════════════════════════

def check_ffmpeg():
    if shutil.which("ffmpeg") is None:
        raise EnvironmentError(
            "ffmpeg not found!\n"
            "  Windows : https://ffmpeg.org/download.html\n"
            "  Mac     : brew install ffmpeg\n"
            "  Linux   : sudo apt install ffmpeg"
        )
    log("✅ ffmpeg found.")


# ══════════════════════════════════════════════════════════
#  AUTH
# ══════════════════════════════════════════════════════════

def get_drive_ig_service():
    """
    Drive + Instagram auth.
    Uses GOOGLE_TOKEN env var (Reel project token.json content).
    """
    creds = Credentials.from_authorized_user_info(
        json.loads(os.environ["GOOGLE_TOKEN"]),
        DRIVE_IG_SCOPES,
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())

    drive = build("drive", "v3", credentials=creds)
    return drive


# ══════════════════════════════════════════════════════════
#  STEP 1 — LIST FILES FROM DRIVE
# ══════════════════════════════════════════════════════════

def sort_key(filename):
    """Sort by numeric pattern: '1 (2)_clip3' → (1, 2, 3). Others go last."""
    match = re.match(r"(\d+)\s*\((\d+)\)_clip(\d+)", filename)
    if match:
        return (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    return (999, 999, 999)


def fetch_drive_videos(drive):
    """
    Fetch all .mp4 files from the Drive folder (with pagination).
    Returns the first file sorted by filename pattern, or None.
    """
    log("📂 Fetching video list from Google Drive...")

    query      = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
    all_files  = []
    page_token = None

    while True:
        params = {
            "q":        query,
            "fields":   "nextPageToken, files(id, name, mimeType)",
            "pageSize": 1000,
        }
        if page_token:
            params["pageToken"] = page_token

        results    = drive.files().list(**params).execute()
        all_files += results.get("files", [])
        page_token = results.get("nextPageToken")
        if not page_token:
            break

    mp4_files = [f for f in all_files if f["name"].lower().endswith(".mp4")]

    if not mp4_files:
        log("⚠️  No .mp4 files found in Drive folder.")
        return None

    sorted_files = sorted(mp4_files, key=lambda f: sort_key(f["name"]))
    target       = sorted_files[0]

    log(f"   Found {len(mp4_files)} file(s). Processing first: {target['name']}")
    return target


# ══════════════════════════════════════════════════════════
#  STEP 2 — DOWNLOAD FROM DRIVE
# ══════════════════════════════════════════════════════════

def download_from_drive(drive, file_id, file_name):
    """Download Drive file to a local temp .mp4. Returns temp file path."""
    log(f"⬇️  Downloading: {file_name}")
    request = drive.files().get_media(fileId=file_id)

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
    downloader = MediaIoBaseDownload(tmp, request)

    done = False
    while not done:
        status, done = downloader.next_chunk()
        if status:
            log(f"   Download: {int(status.progress() * 100)}%")

    tmp.close()
    log(f"✅ Downloaded → {tmp.name}")
    return tmp.name


# ══════════════════════════════════════════════════════════
#  STEP 3 — FFMPEG: CONVERT TO 9:16 BLURRED BACKGROUND
# ══════════════════════════════════════════════════════════

def convert_to_vertical(input_path):
    """
    Convert video to 1080x1920 (9:16) with blurred background.

    Layout:
    ┌──────────────────┐
    │  blurred enlarged│  ← bg: original scaled to cover + gblur
    │  ┌────────────┐  │
    │  │  original  │  │  ← fg: original scaled to fit, centered
    │  │   video    │  │
    │  └────────────┘  │
    │  blurred enlarged│
    └──────────────────┘
    """
    log("🎨 Converting to 9:16 with blurred background...")

    out_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
    out_tmp.close()
    output_path = out_tmp.name

    W, H = 1080, 1920

    filtergraph = (
        f"[0:v]split=2[bg][fg];"
        f"[bg]scale={W}:{H}:force_original_aspect_ratio=increase,"
        f"crop={W}:{H},"
        f"gblur=sigma=30[blurred];"
        f"[fg]scale={W}:{H}:force_original_aspect_ratio=decrease[scaled];"
        f"[blurred][scaled]overlay=(W-w)/2:(H-h)/2,"
        f"format=yuv420p[out]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-filter_complex", filtergraph,
        "-map", "[out]",
        "-map", "0:a?",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        output_path,
    ]

    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    if result.returncode != 0:
        log(f"❌ FFmpeg failed:\n{result.stderr[-1500:]}")
        raise RuntimeError("FFmpeg conversion failed.")

    log(f"✅ Conversion done → {output_path}")
    return output_path


# ══════════════════════════════════════════════════════════
#  STEP 4 — UPLOAD TO CLOUDINARY
# ══════════════════════════════════════════════════════════

def upload_to_cloudinary(file_path):
    """Upload video to Cloudinary. Returns (secure_url, public_id)."""
    log("☁️  Uploading to Cloudinary...")

    result    = cloudinary.uploader.upload_large(file_path, resource_type="video")
    video_url = result["secure_url"]
    public_id = result["public_id"]

    log(f"✅ Cloudinary URL: {video_url}")
    return video_url, public_id


# ══════════════════════════════════════════════════════════
#  STEP 5 — PUBLISH TO INSTAGRAM REELS
# ══════════════════════════════════════════════════════════

def publish_instagram_reel(video_url):
    """
    Create an Instagram Reel container from Cloudinary URL,
    wait for processing, then publish. Returns post ID or None.
    """
    log("📸 Creating Instagram Reel container...")

    response = requests.post(
        f"https://graph.facebook.com/v20.0/{INSTAGRAM_ID}/media",
        data={
            "media_type":   "REELS",
            "video_url":    video_url,
            "caption":      IG_CAPTION,
            "access_token": IG_ACCESS_TOKEN,
        },
    )
    result      = response.json()
    creation_id = result.get("id")

    if not creation_id:
        log(f"❌ Instagram container creation failed: {result}")
        return None

    log(f"   Container ID: {creation_id}. Waiting for Instagram to process...")

    for attempt in range(30):
        status_resp = requests.get(
            f"https://graph.facebook.com/v20.0/{creation_id}",
            params={"fields": "status_code", "access_token": IG_ACCESS_TOKEN},
        )
        status = status_resp.json().get("status_code")
        log(f"   Attempt {attempt + 1}/30: status = {status}")

        if status == "FINISHED":
            break
        elif status == "ERROR":
            log("❌ Instagram processing error.")
            return None

        time.sleep(10)
    else:
        log("❌ Instagram processing timed out (5 minutes).")
        return None

    log("📤 Publishing Instagram Reel...")
    pub_resp   = requests.post(
        f"https://graph.facebook.com/v20.0/{INSTAGRAM_ID}/media_publish",
        data={"creation_id": creation_id, "access_token": IG_ACCESS_TOKEN},
    )
    pub_result = pub_resp.json()
    post_id    = pub_result.get("id")

    if post_id:
        log(f"✅ Instagram Reel published! Post ID: {post_id}")
    else:
        log(f"❌ Instagram publish failed: {pub_result}")

    return post_id


# ══════════════════════════════════════════════════════════
#  STEP 6 — PUBLISH TO FACEBOOK REELS
# ══════════════════════════════════════════════════════════

def publish_facebook_reel(video_url):
    """
    Publish a Facebook Reel to a Page using the video_reels endpoint.

    Flow:
      A. Initialize upload session → get video_id + upload_url
      B. Upload the video bytes via PUT to upload_url
      C. Publish using /{page_id}/video_reels with PUBLISHED status

    Returns the Facebook video/post ID or None on failure.
    """

    # ── A. Initialize upload session ─────────────────────
    log("📘 Initializing Facebook Reels upload session...")

    init_resp = requests.post(
        f"https://graph.facebook.com/v20.0/{FB_PAGE_ID}/video_reels",
        data={
            "upload_phase":  "start",
            "access_token":  FB_ACCESS_TOKEN,
        },
    )
    init_data = init_resp.json()

    video_id   = init_data.get("video_id")
    upload_url = init_data.get("upload_url")

    if not video_id or not upload_url:
        log(f"❌ Facebook Reels init failed: {init_data}")
        return None

    log(f"   FB video_id: {video_id}")

    # ── B. Download Cloudinary video and PUT to Facebook ─
    log("⬆️  Uploading video bytes to Facebook...")

    # Stream the Cloudinary URL directly into the Facebook upload PUT
    with requests.get(video_url, stream=True) as dl:
        dl.raise_for_status()
        upload_resp = requests.put(
            upload_url,
            headers={
                "Authorization":  f"OAuth {FB_ACCESS_TOKEN}",
                "Content-Type":   "application/octet-stream",
                # Content-Length is required by Facebook's resumable uploader
                "Content-Length": dl.headers.get("Content-Length", ""),
            },
            data=dl.iter_content(chunk_size=1024 * 1024),  # 1 MB chunks
            stream=True,
        )

    if upload_resp.status_code not in (200, 204):
        log(f"❌ Facebook video byte upload failed ({upload_resp.status_code}): {upload_resp.text}")
        return None

    log("   Video bytes uploaded to Facebook.")

    # ── C. Publish the Reel ───────────────────────────────
    log("📤 Publishing Facebook Reel...")

    pub_resp = requests.post(
        f"https://graph.facebook.com/v20.0/{FB_PAGE_ID}/video_reels",
        data={
            "video_id":      video_id,
            "upload_phase":  "finish",
            "video_state":   "PUBLISHED",
            "description":   FB_CAPTION,
            "access_token":  FB_ACCESS_TOKEN,
        },
    )
    pub_data = pub_resp.json()

    # Facebook returns {"success": true} on publish; the video_id is the post reference
    if pub_data.get("success"):
        log(f"✅ Facebook Reel published! Video ID: {video_id}")
        return video_id
    else:
        log(f"❌ Facebook Reel publish failed: {pub_data}")
        return None


# ══════════════════════════════════════════════════════════
#  STEP 7 — DELETE FROM CLOUDINARY
# ══════════════════════════════════════════════════════════

def delete_from_cloudinary(public_id):
    """Remove video from Cloudinary after both platforms are done."""
    log(f"🗑️  Deleting from Cloudinary: {public_id}")
    cloudinary.uploader.destroy(public_id, resource_type="video")
    log("✅ Cloudinary file deleted.")


# ══════════════════════════════════════════════════════════
#  STEP 8 — APPEND RECORD TO GITHUB JSON
# ══════════════════════════════════════════════════════════

def append_to_github_json(drive_file_id, drive_file_name, ig_post_id, fb_video_id):
    """
    Read the JSON tracking file from GitHub (creates it if absent),
    append a new record for this session, and push the updated file back.

    Record schema:
    {
        "drive_file_id":   "...",
        "drive_file_name": "...",
        "ig_post_id":      "..." | null,
        "fb_video_id":     "..." | null,
        "timestamp":       "YYYY-MM-DD HH:MM:SS"
    }
    """
    log("📝 Updating GitHub JSON tracker...")

    headers = {
        "Authorization": f"Bearer {GITHUB_PAT}",
        "Accept":        "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url = f"{GITHUB_API_BASE}/repos/{GITHUB_REPO}/contents/{GITHUB_JSON_PATH}"

    # ── Try to fetch existing file ────────────────────────
    get_resp = requests.get(url, headers=headers)
    sha      = None
    records  = []

    if get_resp.status_code == 200:
        file_data = get_resp.json()
        sha       = file_data["sha"]
        content   = base64.b64decode(file_data["content"]).decode("utf-8")
        try:
            records = json.loads(content)
            if not isinstance(records, list):
                log("⚠️  GitHub JSON is not a list — resetting to empty list.")
                records = []
        except json.JSONDecodeError:
            log("⚠️  GitHub JSON is malformed — resetting to empty list.")
            records = []
    elif get_resp.status_code == 404:
        log("   JSON file not found in repo — will create it.")
    else:
        log(f"⚠️  Could not fetch GitHub JSON ({get_resp.status_code}): {get_resp.text}")
        log("   Skipping GitHub update — record NOT saved.")
        return

    # ── Append new record ─────────────────────────────────
    new_record = {
        "drive_file_id":   drive_file_id,
        "drive_file_name": drive_file_name,
        "ig_post_id":      ig_post_id,
        "fb_video_id":     fb_video_id,
        "timestamp":       datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    records.append(new_record)
    log(f"   Appending record: {new_record}")

    # ── Push updated file back to GitHub ──────────────────
    updated_content = json.dumps(records, indent=2, ensure_ascii=False)
    encoded_content = base64.b64encode(updated_content.encode("utf-8")).decode("utf-8")

    push_body = {
        "message": f"chore: log processed video {drive_file_name} [{SESSION_TIME}]",
        "content": encoded_content,
    }
    if sha:
        push_body["sha"] = sha   # required for updates; omit only for new file

    put_resp = requests.put(url, headers=headers, json=push_body)

    if put_resp.status_code in (200, 201):
        action = "updated" if sha else "created"
        log(f"✅ GitHub JSON {action}: {GITHUB_REPO}/{GITHUB_JSON_PATH}")
    else:
        log(f"❌ GitHub push failed ({put_resp.status_code}): {put_resp.text}")


# ══════════════════════════════════════════════════════════
#  CLEANUP TEMP FILES
# ══════════════════════════════════════════════════════════

def cleanup(*paths):
    for path in paths:
        if path and os.path.exists(path):
            try:
                os.remove(path)
                log(f"🧹 Deleted temp file: {path}")
            except Exception as e:
                log(f"⚠️  Could not delete {path}: {e}")


# ══════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════

def main():
    log("=" * 55)
    log("  🎬 Unified Uploader: Instagram Reels + Facebook Reels")
    log(f"  Session: {SESSION_TIME}")
    log("=" * 55)

    # ── Pre-flight checks ────────────────────────────────
    check_ffmpeg()

    # ── Authenticate Drive ───────────────────────────────
    log("\n🔐 Authenticating Drive / Instagram (Reel project)...")
    drive = get_drive_ig_service()

    # ── Fetch first video from Drive ─────────────────────
    log("\n" + "─" * 55)
    target = fetch_drive_videos(drive)
    if not target:
        log("Nothing to do. Exiting.")
        _log_handle.flush()
        upload_log_to_drive(drive)
        close_log()
        return

    file_id   = target["id"]
    file_name = target["name"]

    # Tracking vars
    raw_path       = None
    converted_path = None
    cloudinary_id  = None
    ig_post_id     = None
    fb_video_id    = None

    try:
        # ── Step 2: Download ──────────────────────────────
        log("\n" + "─" * 55)
        raw_path = download_from_drive(drive, file_id, file_name)

        # ── Step 3: FFmpeg convert ────────────────────────
        log("\n" + "─" * 55)
        converted_path = convert_to_vertical(raw_path)

        cleanup(raw_path)
        raw_path = None

        # ── Step 4: Upload to Cloudinary ──────────────────
        log("\n" + "─" * 55)
        video_url     = None
        cloudinary_id = None
        try:
            video_url, cloudinary_id = upload_to_cloudinary(converted_path)
        except Exception as e:
            log(f"⚠️  Cloudinary upload failed — Instagram & Facebook will be skipped. Reason: {e}")

        # ── Step 5: Publish Instagram Reel ────────────────
        log("\n" + "─" * 55)
        if video_url:
            try:
                ig_post_id = publish_instagram_reel(video_url)
            except Exception as e:
                log(f"⚠️  Instagram upload failed — continuing. Reason: {e}")
                ig_post_id = None
        else:
            log("⏭️  Skipping Instagram — no Cloudinary URL available.")

        # ── Step 6: Publish Facebook Reel ─────────────────
        log("\n" + "─" * 55)
        if video_url:
            try:
                fb_video_id = publish_facebook_reel(video_url)
            except Exception as e:
                log(f"⚠️  Facebook upload failed — continuing. Reason: {e}")
                fb_video_id = None
        else:
            log("⏭️  Skipping Facebook — no Cloudinary URL available.")

        # ── Step 7: Delete from Cloudinary ────────────────
        log("\n" + "─" * 55)
        if cloudinary_id:
            try:
                delete_from_cloudinary(cloudinary_id)
                cloudinary_id = None
            except Exception as e:
                log(f"⚠️  Cloudinary delete failed — manual cleanup may be needed. Reason: {e}")

        # ── Step 8: Append record to GitHub JSON ──────────
        # Runs regardless of whether uploads succeeded, so every attempt is logged
        log("\n" + "─" * 55)
        try:
            append_to_github_json(file_id, file_name, ig_post_id, fb_video_id)
        except Exception as e:
            log(f"⚠️  GitHub JSON update failed — continuing. Reason: {e}")

    except Exception as e:
        log(f"\n❌ FATAL ERROR: {e}")
        import traceback
        log(traceback.format_exc())

    finally:
        cleanup(raw_path, converted_path)
        if cloudinary_id:
            try:
                delete_from_cloudinary(cloudinary_id)
            except Exception:
                log("⚠️  Could not clean up Cloudinary after error.")

    # ── Summary ───────────────────────────────────────────
    log("\n" + "=" * 55)
    log("  📊 SESSION SUMMARY")
    log("=" * 55)
    log(f"  File processed : {file_name}")
    log(f"  Instagram Reel : {'✅ ' + str(ig_post_id) if ig_post_id else '❌ Failed'}")
    log(f"  Facebook Reel  : {'✅ Video ID ' + str(fb_video_id) if fb_video_id else '❌ Failed'}")
    log(f"  GitHub JSON    : {GITHUB_REPO}/{GITHUB_JSON_PATH}")
    log(f"  Log saved to   : {LOG_FILE}")
    log("=" * 55)
    _log_handle.flush()
    upload_log_to_drive(drive)
    close_log()


if __name__ == "__main__":
    main()
