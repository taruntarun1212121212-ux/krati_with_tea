"""
Unified Auto-Uploader: Instagram Reels + Facebook Reels
---------------------------------------------------------
Flow for each video:
  1. Read Google Sheet to get next unprocessed video (S No, Timestamp, Drive ID)
  2. Download the file from Google Drive using the ID from sheet
  3. Upload file directly to Cloudinary (no FFmpeg conversion)
  4. Publish to Instagram Reels via Cloudinary URL
  5. Publish to Facebook Reels via Cloudinary URL
  6. Delete from Cloudinary
  7. Append video record (Drive ID, IG post ID, FB post ID, timestamp) to a
     JSON file in GitHub (creates the file if it doesn't exist yet);
     also stores last_processed_s_no so runs never repeat a video
  8. Write full session log to logs/upload_YYYY-MM-DD_HH-MM-SS.txt
  9. Upload log to Google Drive

Auth / env vars required:
  ┌─────────────────────────┬──────────────────────────────────────────────────┐
  │ Env var                 │ What it is                                       │
  ├─────────────────────────┼──────────────────────────────────────────────────┤
  │ GOOGLE_TOKEN            │ Full JSON content of token.json (Drive + Sheets) │
  │ SPREADSHEET_ID          │ Google Sheet ID (from its URL)                   │
  │ SHEET_NAME              │ Sheet tab name, e.g. "Sheet1"                    │
  │ IG_ACCESS_TOKEN         │ Instagram Graph API user access token            │
  │ IG_ID                   │ Instagram Business / Creator account ID          │
  │ FB_ACCESS_TOKEN         │ Facebook Page access token (long-lived)          │
  │ FB_PAGE_ID              │ Numeric Facebook Page ID                         │
  │ CLOUDINARY_CLOUD_NAME   │ Cloudinary cloud name                            │
  │ CLOUDINARY_API_KEY      │ Cloudinary API key                               │
  │ CLOUDINARY_API_SECRET   │ Cloudinary API secret                            │
  │ GH_PAT                  │ GitHub Personal Access Token (needs repo scope)  │
  │ GH_REPO                 │ Repo in "username/repo-name" format              │
  │ GH_JSON_PATH            │ Path inside repo, e.g. "data/processed.json"    │
  └─────────────────────────┴──────────────────────────────────────────────────┘

Google Sheet format expected:
  Column A: S No       (1, 2, 3 ...)
  Column B: Time Stamp (2026-06-24 23:05:03)
  Column C: Id         (Google Drive file ID)
"""

import os
import time
import json
import base64
import shutil
import tempfile
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

LOGS_FOLDER_ID = "1uRKlio-uOcoINHLp-pu1NDlGNZmw4l6d"


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

# ── Google Sheets ─────────────────────────────────────────
SPREADSHEET_ID = "1qMLiGlLFfXDpQ49GfpezpAC7ppFBJYBjA62wO3NNoUs"
SHEET_NAME     = os.environ.get("SHEET_NAME", "Sheet1")

# ── Google Drive / Sheets scopes ─────────────────────────
DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]

# ── Caption (shared by Instagram and Facebook) ────────────
CAPTION = """@krati_with__tea  #viral #viralpost #viralreels #trending #trendingnow #trendalert #explore #explorepage #foryou #fyp #reels #instareels #reelsviral #reelstrending #reelsoftheday #reelscreator #reels2026 #instadaily #instamood #instalife #instaviral #instagrowth #contentcreator #dailycontent #viralcontent #explore2026 #instatrending #popularposts #trendingshots #socialmediatrends"""

# ── Instagram ─────────────────────────────────────────────
IG_ACCESS_TOKEN = os.environ["IG_ACCESS_TOKEN"]
INSTAGRAM_ID    = os.environ["IG_ID"]
IG_CAPTION      = CAPTION

# ── Facebook ──────────────────────────────────────────────
FB_ACCESS_TOKEN = os.environ["FB_ACCESS_TOKEN"]
FB_PAGE_ID      = os.environ["FB_PAGE_ID"]
FB_CAPTION      = CAPTION

# ── Cloudinary ────────────────────────────────────────────
cloudinary.config(
    cloud_name = os.environ["CLOUDINARY_CLOUD_NAME"],
    api_key    = os.environ["CLOUDINARY_API_KEY"],
    api_secret = os.environ["CLOUDINARY_API_SECRET"],
)

# ── GitHub JSON tracker ───────────────────────────────────
GH_PAT           = os.environ["GH_PAT"]
GH_REPO          = os.environ["GH_REPO"]        # e.g. "john/video-tracker"
GH_JSON_PATH     = os.environ["GH_JSON_PATH"]   # e.g. "data/processed_videos.json"
GITHUB_API_BASE  = "https://api.github.com"


# ══════════════════════════════════════════════════════════
#  AUTH
# ══════════════════════════════════════════════════════════

def get_google_services():
    """
    Authenticate with Google and return (drive_service, sheets_service).
    Uses GOOGLE_TOKEN env var (full token.json content).
    """
    creds = Credentials.from_authorized_user_info(
        json.loads(os.environ["GOOGLE_TOKEN"]),
        DRIVE_SCOPES,
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())

    drive  = build("drive",  "v3", credentials=creds)
    sheets = build("sheets", "v4", credentials=creds)
    return drive, sheets


# ══════════════════════════════════════════════════════════
#  HELPER — LOAD TRACKER STATE FROM GITHUB JSON
#  Returns: (records_list, last_processed_s_no, file_sha)
# ══════════════════════════════════════════════════════════

def load_tracker_from_github():
    """
    Fetch the GitHub JSON tracker.
    Returns (records, last_processed_s_no, sha).
      - records              : list of past run dicts
      - last_processed_s_no  : int — highest S No successfully processed (0 if none)
      - sha                  : str or None — needed for PUT updates
    """
    headers = {
        "Authorization": f"Bearer {GH_PAT}",
        "Accept":        "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url      = f"{GITHUB_API_BASE}/repos/{GH_REPO}/contents/{GH_JSON_PATH}"
    response = requests.get(url, headers=headers)

    if response.status_code == 404:
        log("   GitHub JSON tracker not found — starting fresh.")
        return [], 0, None

    if response.status_code != 200:
        log(f"⚠️  Could not fetch GitHub JSON ({response.status_code}) — assuming no prior runs.")
        return [], 0, None

    file_data = response.json()
    sha       = file_data["sha"]
    content   = base64.b64decode(file_data["content"]).decode("utf-8")

    try:
        data = json.loads(content)
        if not isinstance(data, dict):
            # Legacy format: plain list — migrate gracefully
            log("   Legacy list format detected in GitHub JSON — migrating.")
            records = data if isinstance(data, list) else []
            last_s_no = 0
        else:
            records   = data.get("records", [])
            last_s_no = int(data.get("last_processed_s_no", 0))

        log(f"   Last processed S No: {last_s_no}  |  Total records: {len(records)}")
        return records, last_s_no, sha

    except (json.JSONDecodeError, ValueError):
        log("⚠️  GitHub JSON is malformed — resetting.")
        return [], 0, sha


# ══════════════════════════════════════════════════════════
#  STEP 1 — READ VIDEO ID FROM GOOGLE SHEET
# ══════════════════════════════════════════════════════════

def fetch_next_video_from_sheet(sheets, last_processed_s_no: int):
    """
    Read all rows from the Google Sheet and return the first row
    whose S No (int) is greater than last_processed_s_no.

    Sheet columns (row 1 = header, data starts row 2):
      A: S No | B: Time Stamp | C: Id

    Returns dict: { "s_no": 5, "timestamp": "...", "drive_id": "..." }
    or None if nothing is left to process.
    """
    log(f"📋 Reading Google Sheet: {SPREADSHEET_ID} / {SHEET_NAME}")
    log(f"   Skipping S No ≤ {last_processed_s_no} (already processed)")

    range_name = f"{SHEET_NAME}!A:C"
    result = (
        sheets.spreadsheets()
        .values()
        .get(spreadsheetId=SPREADSHEET_ID, range=range_name)
        .execute()
    )
    rows = result.get("values", [])

    if not rows or len(rows) < 2:
        log("⚠️  Sheet is empty or has only a header row.")
        return None

    header = rows[0]
    log(f"   Sheet header: {header}")
    log(f"   Total data rows: {len(rows) - 1}")

    for row in rows[1:]:
        if len(row) < 3:
            continue

        raw_s_no  = row[0].strip()
        timestamp = row[1].strip()
        drive_id  = row[2].strip()

        if not drive_id or not raw_s_no:
            continue

        try:
            s_no_int = int(raw_s_no)
        except ValueError:
            log(f"   ⚠️  Non-integer S No '{raw_s_no}' — skipping row.")
            continue

        if s_no_int <= last_processed_s_no:
            log(f"   ⏭️  S No {s_no_int} already processed — skipping.")
            continue

        log(f"   ✅ Next video → S No {s_no_int} | {timestamp} | {drive_id}")
        return {"s_no": s_no_int, "timestamp": timestamp, "drive_id": drive_id}

    log("⚠️  All rows in sheet have already been processed.")
    return None


# ══════════════════════════════════════════════════════════
#  STEP 2 — DOWNLOAD FROM DRIVE
# ══════════════════════════════════════════════════════════

def get_file_name_from_drive(drive, file_id):
    """Fetch the file name from Drive metadata."""
    meta = drive.files().get(fileId=file_id, fields="name").execute()
    return meta.get("name", f"{file_id}.mp4")


def download_from_drive(drive, file_id, file_name):
    """Download Drive file to a local temp file. Returns temp file path."""
    log(f"⬇️  Downloading: {file_name} ({file_id})")
    request = drive.files().get_media(fileId=file_id)

    # Preserve original file extension
    ext = os.path.splitext(file_name)[1] or ".mp4"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
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
#  STEP 3 — UPLOAD DIRECTLY TO CLOUDINARY (no conversion)
# ══════════════════════════════════════════════════════════

def upload_to_cloudinary(file_path):
    """Upload video to Cloudinary as-is. Returns (secure_url, public_id)."""
    log("☁️  Uploading to Cloudinary...")

    result    = cloudinary.uploader.upload_large(file_path, resource_type="video")
    video_url = result["secure_url"]
    public_id = result["public_id"]

    log(f"✅ Cloudinary URL: {video_url}")
    return video_url, public_id


# ══════════════════════════════════════════════════════════
#  STEP 4 — PUBLISH TO INSTAGRAM REELS
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
#  STEP 5 — PUBLISH TO FACEBOOK REELS
# ══════════════════════════════════════════════════════════

def publish_facebook_reel(video_url):
    """
    Publish a Facebook Reel to a Page using the video_reels endpoint.
    Returns the Facebook video ID or None on failure.
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

    with requests.get(video_url, stream=True) as dl:
        dl.raise_for_status()
        upload_resp = requests.put(
            upload_url,
            headers={
                "Authorization":  f"OAuth {FB_ACCESS_TOKEN}",
                "Content-Type":   "application/octet-stream",
                "Content-Length": dl.headers.get("Content-Length", ""),
            },
            data=dl.iter_content(chunk_size=1024 * 1024),
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

    if pub_data.get("success"):
        log(f"✅ Facebook Reel published! Video ID: {video_id}")
        return video_id
    else:
        log(f"❌ Facebook Reel publish failed: {pub_data}")
        return None


# ══════════════════════════════════════════════════════════
#  STEP 6 — DELETE FROM CLOUDINARY
# ══════════════════════════════════════════════════════════

def delete_from_cloudinary(public_id):
    """Remove video from Cloudinary after both platforms are done."""
    log(f"🗑️  Deleting from Cloudinary: {public_id}")
    cloudinary.uploader.destroy(public_id, resource_type="video")
    log("✅ Cloudinary file deleted.")


# ══════════════════════════════════════════════════════════
#  STEP 7 — UPDATE GITHUB JSON TRACKER
#
#  JSON structure stored in GitHub:
#  {
#    "last_processed_s_no": 5,
#    "records": [
#      {
#        "s_no": 5,
#        "drive_file_id": "...",
#        "drive_file_name": "...",
#        "ig_post_id": "...",
#        "fb_video_id": "...",
#        "timestamp": "2026-06-25 10:00:00"
#      },
#      ...
#    ]
#  }
# ══════════════════════════════════════════════════════════

def update_github_tracker(s_no, drive_file_id, drive_file_name,
                          ig_post_id, fb_video_id,
                          existing_records, existing_sha):
    """
    Append a new record and update last_processed_s_no, then push to GitHub.
    """
    log("📝 Updating GitHub JSON tracker...")

    new_record = {
        "s_no":            s_no,
        "drive_file_id":   drive_file_id,
        "drive_file_name": drive_file_name,
        "ig_post_id":      ig_post_id,
        "fb_video_id":     fb_video_id,
        "timestamp":       datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    existing_records.append(new_record)
    log(f"   Appending record: {new_record}")

    payload = {
        "last_processed_s_no": s_no,
        "records":             existing_records,
    }

    updated_content = json.dumps(payload, indent=2, ensure_ascii=False)
    encoded_content = base64.b64encode(updated_content.encode("utf-8")).decode("utf-8")

    headers = {
        "Authorization": f"Bearer {GH_PAT}",
        "Accept":        "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url = f"{GITHUB_API_BASE}/repos/{GH_REPO}/contents/{GH_JSON_PATH}"

    push_body = {
        "message": f"chore: processed S No {s_no} — {drive_file_name} [{SESSION_TIME}]",
        "content": encoded_content,
    }
    if existing_sha:
        push_body["sha"] = existing_sha

    put_resp = requests.put(url, headers=headers, json=push_body)

    if put_resp.status_code in (200, 201):
        action = "updated" if existing_sha else "created"
        log(f"✅ GitHub JSON {action}: {GH_REPO}/{GH_JSON_PATH}  (last_processed_s_no={s_no})")
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

    # ── Authenticate Google (Drive + Sheets) ─────────────
    log("\n🔐 Authenticating Google services...")
    drive, sheets = get_google_services()

    # ── Load tracker state from GitHub ───────────────────
    log("\n📦 Loading tracker state from GitHub...")
    existing_records, last_processed_s_no, existing_sha = load_tracker_from_github()

    # ── Step 1: Read next video from Google Sheet ─────────
    log("\n" + "─" * 55)
    row = fetch_next_video_from_sheet(sheets, last_processed_s_no)

    if not row:
        log("Nothing to do. Exiting.")
        _log_handle.flush()
        upload_log_to_drive(drive)
        close_log()
        return

    file_id  = row["drive_id"]
    s_no     = row["s_no"]       # int
    sheet_ts = row["timestamp"]

    # Get actual filename from Drive metadata
    file_name = get_file_name_from_drive(drive, file_id)
    log(f"   Drive file name: {file_name}")

    # Tracking vars
    raw_path      = None
    cloudinary_id = None
    ig_post_id    = None
    fb_video_id   = None

    try:
        # ── Step 2: Download from Drive ───────────────────
        log("\n" + "─" * 55)
        raw_path = download_from_drive(drive, file_id, file_name)

        # ── Step 3: Upload directly to Cloudinary ─────────
        log("\n" + "─" * 55)
        video_url     = None
        cloudinary_id = None
        try:
            video_url, cloudinary_id = upload_to_cloudinary(raw_path)
        except Exception as e:
            log(f"⚠️  Cloudinary upload failed — Instagram & Facebook will be skipped. Reason: {e}")

        cleanup(raw_path)
        raw_path = None

        # ── Step 4: Publish Instagram Reel ────────────────
        log("\n" + "─" * 55)
        if video_url:
            try:
                ig_post_id = publish_instagram_reel(video_url)
            except Exception as e:
                log(f"⚠️  Instagram upload failed — continuing. Reason: {e}")
                ig_post_id = None
        else:
            log("⏭️  Skipping Instagram — no Cloudinary URL available.")

        # ── Step 5: Publish Facebook Reel ─────────────────
        log("\n" + "─" * 55)
        if video_url:
            try:
                fb_video_id = publish_facebook_reel(video_url)
            except Exception as e:
                log(f"⚠️  Facebook upload failed — continuing. Reason: {e}")
                fb_video_id = None
        else:
            log("⏭️  Skipping Facebook — no Cloudinary URL available.")

        # ── Step 6: Delete from Cloudinary ────────────────
        log("\n" + "─" * 55)
        if cloudinary_id:
            try:
                delete_from_cloudinary(cloudinary_id)
                cloudinary_id = None
            except Exception as e:
                log(f"⚠️  Cloudinary delete failed — manual cleanup may be needed. Reason: {e}")

        # ── Step 7: Update GitHub JSON tracker ────────────
        log("\n" + "─" * 55)
        try:
            update_github_tracker(
                s_no, file_id, file_name,
                ig_post_id, fb_video_id,
                existing_records, existing_sha,
            )
        except Exception as e:
            log(f"⚠️  GitHub JSON update failed — continuing. Reason: {e}")

    except Exception as e:
        log(f"\n❌ FATAL ERROR: {e}")
        import traceback
        log(traceback.format_exc())

    finally:
        cleanup(raw_path)
        if cloudinary_id:
            try:
                delete_from_cloudinary(cloudinary_id)
            except Exception:
                log("⚠️  Could not clean up Cloudinary after error.")

    # ── Summary ───────────────────────────────────────────
    log("\n" + "=" * 55)
    log("  📊 SESSION SUMMARY")
    log("=" * 55)
    log(f"  Sheet Row      : S No {s_no} | {sheet_ts}")
    log(f"  Drive File ID  : {file_id}")
    log(f"  File Name      : {file_name}")
    log(f"  Instagram Reel : {'✅ ' + str(ig_post_id) if ig_post_id else '❌ Failed'}")
    log(f"  Facebook Reel  : {'✅ Video ID ' + str(fb_video_id) if fb_video_id else '❌ Failed'}")
    log(f"  GitHub JSON    : {GH_REPO}/{GH_JSON_PATH}")
    log(f"  Log saved to   : {LOG_FILE}")
    log("=" * 55)
    _log_handle.flush()
    upload_log_to_drive(drive)
    close_log()


if __name__ == "__main__":
    main()
