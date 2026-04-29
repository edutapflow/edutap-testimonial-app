import os
from datetime import datetime
from typing import Dict, List, Tuple

from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from google.oauth2.credentials import Credentials as UserCredentials
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.http import MediaFileUpload

# ===================== CONFIG =====================

# backend/ folder (where google_clients.py is)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# project root = one level above backend/  (EduTap-Testimonial-App folder)
PROJECT_ROOT = os.path.dirname(BASE_DIR)

# credentials folder at: EduTap-Testimonial-App/credentials
CREDENTIALS_DIR = os.path.join(PROJECT_ROOT, "credentials")

# Where your local PNGs are written by run_pipeline
# (EduTap-Testimonial-App/output)
LOCAL_OUTPUT_DIR = os.path.join(PROJECT_ROOT, "output")

# --- Service account for SHEETS ---
SERVICE_ACCOUNT_FILE = os.path.join(CREDENTIALS_DIR, "service_account.json")
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# --- OAuth client for DRIVE ---
OAUTH_CLIENT_FILE = os.path.join(CREDENTIALS_DIR, "credentials.json")
TOKEN_FILE = os.path.join(CREDENTIALS_DIR, "token.json")
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]

# --- Spreadsheet + tabs ---
SPREADSHEET_ID = "1qWLiMVLsUKSO9AMrwTgGse4gJQnRNNacvfoNxscFCLI"

SHEET_TABS: Dict[str, str] = {
    "edutap": "EduTap Feedback",
    "event": "Event Feedback",
    "mentor": "Mentor feedback",
    "course": "Course Feedback",
    "support": "Support Feedback",
}

# >>>>>>> NEW: hard-coded operator name (will go into "Entered by" column) <<<<<<
# Change this value on each machine where the app is installed.
# ===================== OPERATOR (Entered by) =====================
# UI will set this via FastAPI endpoint (/operator).
# Stored locally in backend folder so every machine can have its own name.

OPERATOR_CONFIG_FILE = os.path.join(BASE_DIR, "operator_config.json")
DEFAULT_OPERATOR_NAME = "Rohit Sharma"

def get_app_operator_name() -> str:
    """Read operator name from operator_config.json; fallback to DEFAULT_OPERATOR_NAME."""
    try:
        import json
        if os.path.exists(OPERATOR_CONFIG_FILE):
            with open(OPERATOR_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            name = str(data.get("name") or "").strip()
            return name if name else DEFAULT_OPERATOR_NAME
    except Exception:
        pass
    return DEFAULT_OPERATOR_NAME

def set_app_operator_name(name: str) -> str:
    """Persist operator name to operator_config.json. Returns saved name."""
    import json
    safe = str(name or "").strip()
    if not safe:
        safe = DEFAULT_OPERATOR_NAME
    with open(OPERATOR_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"name": safe}, f, ensure_ascii=False, indent=2)
    return safe

# --- Drive folders (user’s Drive, NOT service account) ---
FOLDER_IDS: Dict[str, str] = {
    "master":  "1-Xv3-mrP74LR6He0eUzn5ICCBHYHV_yZ",
    "edutap":  "1nz2sV_lXYVRTDa_bigN2PuIvBh-26qSn",
    "event":   "1t6JqjtNmu5U_usH67ocuiTxQnGUZjpJM",
    "mentor":  "1GKoRJ0je-1Cxo8hUro1hvsF4LBCOM83h",
    "course": "1Bd0uqYSA7chjgvRlyfWaZjQ2Ow2OOlIc",
    "support": "17rMLa3xErF3REvk5sPyo_d9Q-Atiupn-",
}

# ===================== BASIC CLIENTS =====================


def get_sheets_service():
    """Sheets via service account."""
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        raise RuntimeError(f"Missing service account file: {SERVICE_ACCOUNT_FILE}")

    creds = ServiceAccountCredentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=SHEETS_SCOPES,
    )
    return build("sheets", "v4", credentials=creds)


def get_drive_service():
    """Drive via OAuth user (uses credentials.json + token.json)."""
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = UserCredentials.from_authorized_user_file(TOKEN_FILE, DRIVE_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            # auto-refresh
            from google.auth.transport.requests import Request
            creds.refresh(Request())
        else:
            # first-time auth
            flow = InstalledAppFlow.from_client_secrets_file(
                OAUTH_CLIENT_FILE,
                DRIVE_SCOPES,
            )
            creds = flow.run_local_server(port=0)

        os.makedirs(CREDENTIALS_DIR, exist_ok=True)
        with open(TOKEN_FILE, "w", encoding="utf-8") as token:
            token.write(creds.to_json())

    return build("drive", "v3", credentials=creds)


# ===================== HELPERS: TYPE NORMALIZATION =====================

def normalize_type(t: str) -> str:
    t = (t or "").strip().lower()
    if t in ("edutap", "edutap feedback"):
        return "edutap"
    if t in ("event", "event feedback"):
        return "event"
    if t in ("mentor", "mentor feedback"):
        return "mentor"
    if t in ("course", "course feedback"):
        return "course"
    if t in ("support", "support feedback"):
        return "support"
    # default fallback
    return "edutap"


# ===================== DRIVE: UPLOAD WITH 2,3,4… SUFFIX =====================

def _find_available_name(drive, folder_id: str, base_name: str) -> str:
    """
    If <base_name> exists in folder, return '<name>_2.ext', '_3', ... etc.
    """
    name, ext = os.path.splitext(base_name)
    candidate = base_name
    counter = 2

    while True:
        q = (
            f"'{folder_id}' in parents and "
            f"name = '{candidate.replace('\"', '\\\"')}' and "
            "trashed = false"
        )
        res = drive.files().list(
            q=q,
            fields="files(id, name)",
            pageSize=1,
        ).execute()
        if not res.get("files"):
            return candidate

        candidate = f"{name}_{counter}{ext}"
        counter += 1


def upload_image_to_drive(
    testimonial_type: str,
    local_filename: str,
) -> Tuple[str, str]:
    """
    Uploads local PNG into correct folder.
    Returns (final_filename_on_drive, webViewLink).
    """
    t = normalize_type(testimonial_type)
    folder_id = FOLDER_IDS.get(t)
    if not folder_id:
        raise RuntimeError(f"No Drive folder mapped for type '{testimonial_type}'")

    local_path = os.path.join(LOCAL_OUTPUT_DIR, local_filename)
    if not os.path.exists(local_path):
        raise FileNotFoundError(local_path)

    drive = get_drive_service()
    base_name = os.path.basename(local_path)
    final_name = _find_available_name(drive, folder_id, base_name)

    media = MediaFileUpload(local_path, mimetype="image/png")
    metadata = {
        "name": final_name,
        "parents": [folder_id],
    }

    file = drive.files().create(
        body=metadata,
        media_body=media,
        fields="id, webViewLink",
    ).execute()

    return final_name, file.get("webViewLink", "")


# ===================== SHEETS: APPEND ROWS PER TYPE =====================

def _now_str() -> str:
    """
    Return timestamp in this format for Google Sheet:
    28-11-2025 10:51  ->  DD-MM-YYYY HH:MM (24-hour)
    Leading single quote keeps the formatting in Sheets.
    """
    return datetime.now().strftime("'%d-%m-%Y %H:%M")


def append_testimonial_rows(
    testimonial_type: str,
    email_link: str,
    image_links: List[str],
    data: Dict = None,
):
    """
    Appends one row per image_link into the appropriate sheet tab.

    UPDATED STRUCTURE WITH STUDENT NAME:
    - EduTap:
      Entered by | Date & Time | Email Link | Student Name | Image Link
    - Course:
      Entered by | Date & Time | Email Link | Student Name | Course Name | Image Link
    - Mentor:
      Entered by | Date & Time | Email Link | Student Name | Name of Person | Image Link
    - Event / Support:
      Entered by | Date & Time | Email Link | Student Name | Single/Multi | Name | Image Link
    """
    t = normalize_type(testimonial_type)
    tab = SHEET_TABS.get(t)
    if not tab:
        raise RuntimeError(f"No sheet tab mapped for type '{testimonial_type}'")

    data = data or {}
    ts = _now_str()  # format: DD-MM-YYYY HH:MM
    entered_by = get_app_operator_name() or ""
    student_name = str(
        data.get("student_name")
        or data.get("student")
        or data.get("name_of_student")
        or ""
    ).strip()

    rows: List[List[str]] = []

    for img_link in image_links:
        # EduTap:
        # Entered by | Date & Time | Email Link | Student Name | Image Link
        if t == "edutap":
            rows.append([entered_by, ts, email_link, student_name, img_link])

        # Course Feedback:
        # Entered by | Date & Time | Email Link | Student Name | Course Name | Image Link
        elif t == "course":
            c_name = str(data.get("course_name") or "").strip()
            rows.append([entered_by, ts, email_link, student_name, c_name, img_link])

        # Mentor Feedback:
        # Entered by | Date & Time | Email Link | Student Name | Name of Person | Image Link
        elif t == "mentor":
            person = str(data.get("person") or data.get("name") or "").strip()
            mentors_value = data.get("mentors") or data.get("mentor")
            if not person and isinstance(mentors_value, list) and mentors_value:
                person = str(mentors_value[0]).strip()
            elif not person and isinstance(mentors_value, str):
                person = mentors_value.strip()

            rows.append([entered_by, ts, email_link, student_name, person, img_link])

        # Event Feedback:
        # Entered by | Date & Time | Email Link | Student Name | Single/Multi | Name | Image Link
        elif t == "event":
            mode = str(data.get("mode") or "one").strip().lower()
            single_multi = "Single" if mode == "one" else "Multi"
            person = str(
                data.get("person")
                or data.get("name")
                or data.get("faculty")
                or ""
            ).strip()
            rows.append([entered_by, ts, email_link, student_name, single_multi, person, img_link])

        # Support Feedback:
        # Entered by | Date & Time | Email Link | Student Name | Single/Multi | Name | Image Link
        elif t == "support":
            mode = str(data.get("mode") or "one").strip().lower()
            single_multi = "Single" if mode == "one" else "Multi"
            person = str(data.get("person") or data.get("member") or "").strip()
            rows.append([entered_by, ts, email_link, student_name, single_multi, person, img_link])

        # Fallback
        else:
            rows.append([entered_by, ts, email_link, student_name, img_link])

    if not rows:
        return

    sheets = get_sheets_service()
    body = {"values": rows}

    sheets.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{tab}'!A1",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body=body,
    ).execute()


def save_testimonial_and_image(
    t: str,
    data: dict,
    local_path: str,
) -> dict:
    """
    High-level helper used by the main app.

    - t: testimonial type (keys: 'edutap', 'event', 'mentor', 'product', 'support')
    - data: dict of captured fields from the UI, already in our internal format
            MUST contain the original email link in either data["email_link"] or data["link"]
    - local_path: FULL path to the final testimonial PNG on disk

    Returns a small dict with info about the uploaded file.
    """
    t_norm = normalize_type(t)

    if t_norm not in {"edutap", "event", "mentor", "course", "support"}:
        raise ValueError(f"Unsupported testimonial type: {t}")

    if not local_path or not os.path.exists(local_path):
        raise FileNotFoundError(f"Image file not found: {local_path}")

    # 1) Derive the filename that upload_image_to_drive expects
    local_filename = os.path.basename(local_path)

    # 2) Upload PNG to the right Drive folder
    final_name, drive_link = upload_image_to_drive(t_norm, local_filename)

    # 3) Extract the original email link from data
    email_link = str(
        data.get("email_link") or data.get("link") or ""
    ).strip()

    # 4) Append one row into the corresponding Sheet tab
    append_testimonial_rows(
        testimonial_type=t_norm,
        email_link=email_link,
        image_links=[drive_link],
        data=data,
    )

    # 5) Return something useful to the caller
    return {
        "type": t_norm,
        "drive_file_name": final_name,
        "drive_link": drive_link,
        "local_path": local_path,
        "email_link": email_link,
    }
