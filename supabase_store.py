# -*- coding: utf-8 -*-
"""Supabase storage + database helpers for EduTap testimonial Streamlit app."""

from __future__ import annotations

import json
import mimetypes
import os
import time
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from functools import lru_cache

from supabase import create_client

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR
ASSETS_DIR = PROJECT_ROOT / "assets"
TEMPLATES_ROOT = ASSETS_DIR / "Templates"
OUTPUT_DIR = PROJECT_ROOT / "output"

GENERATED_BUCKET = "generated-images"
TEMPLATES_BUCKET = "templates"
TABLE_NAME = "testimonials"


def _get_secret(name: str, default: str = "") -> str:
    """Read from environment first. Streamlit app copies st.secrets into env."""
    return (os.getenv(name, default) or "").strip()


@lru_cache(maxsize=1)
def get_supabase_client():
    url = _get_secret("SUPABASE_URL").rstrip("/") + "/"
    key = _get_secret("SUPABASE_SERVICE_ROLE_KEY") or _get_secret("SUPABASE_ANON_KEY")
    if not url or not key:
        raise RuntimeError("Supabase URL/key missing. Please set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY.")
    return create_client(url, key)


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
    return t or "edutap"


def _safe_path_part(value: str) -> str:
    value = str(value or "").strip()
    value = value.replace("\\", "_").replace("/", "_")
    value = "".join(ch for ch in value if ch not in '<>:"|?*')
    value = "_".join(value.split())
    return value or "file"


def _unique_storage_name(filename: str) -> str:
    stem, ext = os.path.splitext(os.path.basename(filename))
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{_safe_path_part(stem)}_{ts}_{int(time.time() * 1000) % 100000}{ext or '.png'}"


def upload_generated_image(local_path: str, feedback_type: str) -> Dict[str, str]:
    """Upload generated image to public generated-images bucket."""
    local = Path(local_path)
    if not local.exists():
        raise FileNotFoundError(f"Generated image not found: {local_path}")

    t = normalize_type(feedback_type)
    storage_path = f"{t}/{_unique_storage_name(local.name)}"
    content_type = mimetypes.guess_type(local.name)[0] or "image/png"

    client = get_supabase_client()
    with local.open("rb") as f:
        client.storage.from_(GENERATED_BUCKET).upload(
            storage_path,
            f,
            file_options={"content-type": content_type},
        )

    public_url = client.storage.from_(GENERATED_BUCKET).get_public_url(storage_path)
    return {"image_path": storage_path, "image_url": public_url}


def create_record(
    *,
    feedback_type: str,
    entered_by: str,
    email_link: str,
    student_name: str,
    image_filename: str,
    image_path: str,
    image_url: str,
    data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Insert one row in Supabase testimonials table."""
    data = data or {}
    t = normalize_type(feedback_type)

    person_name = ""
    course_name = ""
    mode = str(data.get("mode") or "").strip()

    if t == "course":
        course_name = str(data.get("course_name") or "").strip()
    elif t == "mentor":
        person_name = str(data.get("person") or data.get("name") or "").strip()
        mentors = data.get("mentors")
        if not person_name and isinstance(mentors, list) and mentors:
            person_name = str(mentors[0]).strip()
        elif not person_name and isinstance(mentors, str):
            person_name = mentors.strip()
    elif t == "event":
        person_name = str(data.get("person") or data.get("name") or data.get("faculty") or "").strip()
    elif t == "support":
        person_name = str(data.get("person") or data.get("member") or "").strip()

    row = {
        "feedback_type": t,
        "entered_by": entered_by or "",
        "email_link": email_link or "",
        "student_name": student_name or "",
        "person_name": person_name,
        "course_name": course_name,
        "mode": mode,
        "image_filename": image_filename,
        "image_path": image_path,
        "image_url": image_url,
        "scheduled_status": "Pending",
        "extra_json": data,
    }

    res = get_supabase_client().table(TABLE_NAME).insert(row).execute()
    return (res.data or [row])[0]


def save_generated_record(
    *,
    feedback_type: str,
    entered_by: str,
    email_link: str,
    local_image_path: str,
    student_name: str,
    data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    uploaded = upload_generated_image(local_image_path, feedback_type)
    return create_record(
        feedback_type=feedback_type,
        entered_by=entered_by,
        email_link=email_link,
        student_name=student_name,
        image_filename=os.path.basename(local_image_path),
        image_path=uploaded["image_path"],
        image_url=uploaded["image_url"],
        data=data or {},
    )


def list_records(
    feedback_type: str = "all",
    scheduled_status: str = "all",
    search: str = "",
    limit: int = 500,
) -> List[Dict[str, Any]]:
    query = get_supabase_client().table(TABLE_NAME).select("*").order("created_at", desc=True).limit(limit)

    t = normalize_type(feedback_type) if feedback_type and feedback_type != "all" else "all"
    if t != "all":
        query = query.eq("feedback_type", t)

    if scheduled_status and scheduled_status != "all":
        query = query.eq("scheduled_status", scheduled_status)

    res = query.execute()
    rows = res.data or []

    q = (search or "").strip().lower()
    if q:
        def match(row: Dict[str, Any]) -> bool:
            hay = " ".join(str(row.get(k) or "") for k in [
                "entered_by", "email_link", "student_name", "person_name", "course_name", "image_filename"
            ]).lower()
            return q in hay
        rows = [r for r in rows if match(r)]

    return rows


def update_scheduling(record_id: int, status: str) -> Dict[str, Any]:
    status = "Done" if status == "Done" else "Pending"
    payload: Dict[str, Any] = {"scheduled_status": status}
    payload["scheduled_done_at"] = datetime.now(timezone.utc).isoformat() if status == "Done" else None
    res = get_supabase_client().table(TABLE_NAME).update(payload).eq("id", record_id).execute()
    return (res.data or [payload])[0]


def download_generated_bytes(image_path: str) -> bytes:
    data = get_supabase_client().storage.from_(GENERATED_BUCKET).download(image_path)
    if isinstance(data, bytes):
        return data
    return bytes(data)


def build_zip_for_records(records: Iterable[Dict[str, Any]]) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for row in records:
            image_path = row.get("image_path") or ""
            if not image_path:
                continue
            filename = row.get("image_filename") or os.path.basename(image_path) or f"testimonial_{row.get('id')}.png"
            try:
                data = download_generated_bytes(image_path)
                zf.writestr(filename, data)
            except Exception:
                # Skip failed files so one missing image doesn't break the whole ZIP.
                continue
    return buf.getvalue()


def upload_template_file(local_template_path: str, relative_template_path: str) -> None:
    local = Path(local_template_path)
    if not local.exists():
        raise FileNotFoundError(f"Template file not found: {local_template_path}")
    rel = relative_template_path.replace("\\", "/").lstrip("/")
    content_type = mimetypes.guess_type(local.name)[0] or "image/jpeg"
    client = get_supabase_client()
    # If same template already exists, remove first, then upload fresh.
    try:
        client.storage.from_(TEMPLATES_BUCKET).remove([rel])
    except Exception:
        pass
    with local.open("rb") as f:
        client.storage.from_(TEMPLATES_BUCKET).upload(
            rel,
            f,
            file_options={"content-type": content_type},
        )


def download_template_if_missing(relative_template_path: str, local_template_path: str) -> bool:
    local = Path(local_template_path)
    if local.exists():
        return True
    rel = relative_template_path.replace("\\", "/").lstrip("/")
    try:
        data = get_supabase_client().storage.from_(TEMPLATES_BUCKET).download(rel)
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(data if isinstance(data, bytes) else bytes(data))
        return True
    except Exception:
        return False


def list_template_names(folder: str) -> List[str]:
    """Return filename stems from templates/<folder> in Supabase bucket."""
    names: List[str] = []
    try:
        items = get_supabase_client().storage.from_(TEMPLATES_BUCKET).list(folder)
        for item in items or []:
            name = item.get("name") if isinstance(item, dict) else getattr(item, "name", "")
            if not name:
                continue
            stem, ext = os.path.splitext(name)
            if ext.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                names.append(stem)
    except Exception:
        pass
    return sorted(set(names))

def download_template_bytes(relative_template_path: str) -> bytes:
    """Download one blank/person template image from the Supabase templates bucket."""
    rel = (relative_template_path or "").replace("\\", "/").lstrip("/")
    if not rel:
        raise FileNotFoundError("Template path is empty.")
    data = get_supabase_client().storage.from_(TEMPLATES_BUCKET).download(rel)
    if isinstance(data, bytes):
        return data
    return bytes(data)


def _template_item_names(folder: str) -> List[str]:
    """Return image file names inside a Supabase template folder."""
    names: List[str] = []
    try:
        items = get_supabase_client().storage.from_(TEMPLATES_BUCKET).list(folder)
        for item in items or []:
            name = item.get("name") if isinstance(item, dict) else getattr(item, "name", "")
            if not name:
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext in {".jpg", ".jpeg", ".png", ".webp"}:
                names.append(name)
    except Exception:
        pass
    return sorted(set(names), key=lambda x: x.lower())


def get_blank_template_reference(feedback_type: str) -> Optional[Dict[str, Any]]:
    """Return a template reference from the templates bucket/local assets for upload guidance.

    This intentionally uses blank/person template images only, not generated testimonial images.
    For Upload Template, the app only uses Mentor and Support. Support must use a
    person-specific template as reference, not the generic Support Feedback template.
    """
    t = normalize_type(feedback_type)
    candidates: List[str] = []
    exclude_stems = set()

    if t == "edutap":
        folder = "EduTap Feedback"
        preferred = ["EduTap Feedback.jpg", "EduTap Feedback.jpeg", "EduTap Feedback.png"]
    elif t == "course":
        folder = "Course Feedback"
        preferred = ["Course Feedback.jpg", "Course Feedback.jpeg", "Course Feedback.png"]
    elif t == "event":
        folder = "Event Feedback"
        preferred = ["Event Feedback.jpg", "Event Feedback.jpeg", "Event Feedback.png"]
    elif t == "mentor":
        folder = "Mentor feedback"
        # Fixed person-specific reference requested by EduTap.
        preferred = ["Anchit.jpg", "Anchit.jpeg", "Anchit.png"]
        # Do not use temporary/test templates as fallback references.
        exclude_stems = {"rohit sharma", "rohitsharma", "rohit_sharma"}
    elif t == "support":
        folder = "Support Feedback"
        # Fixed person-specific reference requested by EduTap.
        preferred = ["Anshul.jpg", "Anshul.jpeg", "Anshul.png"]
        # Do not use generic support reference as fallback.
        exclude_stems = {"support feedback", "support_feedback", "supportfeedback"}
    else:
        return None

    def allowed_file(name: str) -> bool:
        stem = os.path.splitext(os.path.basename(name or ""))[0].strip().lower()
        stem_key = stem.replace(" ", "_")
        stem_compact = stem.replace(" ", "")
        return stem not in exclude_stems and stem_key not in exclude_stems and stem_compact not in exclude_stems

    candidates.extend([f"{folder}/{name}" for name in preferred if allowed_file(name)])

    # If there is no selected generic/blank file, use the first allowed person template in that folder.
    for name in _template_item_names(folder):
        if allowed_file(name):
            candidates.append(f"{folder}/{name}")

    # Remove duplicates while keeping order.
    deduped: List[str] = []
    seen = set()
    for rel in candidates:
        key = rel.lower()
        if key not in seen:
            deduped.append(rel)
            seen.add(key)

    # Supabase first.
    for rel in deduped:
        try:
            data = download_template_bytes(rel)
            if data:
                return {
                    "name": os.path.basename(rel),
                    "path": rel,
                    "bytes": data,
                    "source": "supabase",
                }
        except Exception:
            continue

    # Local GitHub asset fallback.
    for rel in deduped:
        local = TEMPLATES_ROOT / rel
        try:
            if local.exists() and local.is_file():
                return {
                    "name": local.name,
                    "path": rel,
                    "bytes": local.read_bytes(),
                    "source": "local",
                }
        except Exception:
            continue

    return None




def get_latest_sample_record(feedback_type: str) -> Optional[Dict[str, Any]]:
    """Return latest generated image record for the selected feedback type, for upload-template reference."""
    t = normalize_type(feedback_type)
    try:
        res = (
            get_supabase_client()
            .table(TABLE_NAME)
            .select("*")
            .eq("feedback_type", t)
            .order("created_at", desc=True)
            .limit(20)
            .execute()
        )
        for row in res.data or []:
            if row.get("image_path") or row.get("image_url"):
                return row
    except Exception:
        return None
    return None


def expected_template_relpaths(feedback_type: str, data: Optional[Dict[str, Any]] = None) -> List[str]:
    data = data or {}
    t = normalize_type(feedback_type)
    rels: List[str] = []

    if t == "edutap":
        rels.append("EduTap Feedback/EduTap Feedback.jpg")
    elif t == "course":
        rels.append("Course Feedback/Course Feedback.jpg")
    elif t == "event":
        mode = (data.get("mode") or "one").strip().lower()
        if mode == "one" and data.get("faculty"):
            rels.append(f"Event Feedback/{data.get('faculty')}.jpg")
        else:
            rels.append("Event Feedback/Event Feedback.jpg")
    elif t == "mentor":
        mentors = data.get("mentors") or []
        if isinstance(mentors, str):
            mentors = [mentors]
        for m in mentors:
            if str(m).strip():
                rels.append(f"Mentor feedback/{str(m).strip()}.jpg")
    elif t == "support":
        mode = (data.get("mode") or "one").strip().lower()
        if mode == "one" and data.get("member"):
            rels.append(f"Support Feedback/{data.get('member')}.jpg")
        else:
            rels.append("Support Feedback/Support Feedback.jpg")
    return rels


def ensure_templates_available(feedback_type: str, data: Optional[Dict[str, Any]] = None) -> None:
    """Download missing templates from Supabase bucket before main.get_template_variants checks local files."""
    missing = []
    for rel in expected_template_relpaths(feedback_type, data):
        local = TEMPLATES_ROOT / rel
        ok = download_template_if_missing(rel, str(local))
        if not ok and not local.exists():
            missing.append(rel)
    if missing:
        raise FileNotFoundError("Template not found: " + ", ".join(missing))
