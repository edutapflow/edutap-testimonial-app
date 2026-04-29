# -*- coding: utf-8 -*-
"""EduTap Testimonial Graphic Maker - Streamlit + Supabase version."""

from __future__ import annotations

import asyncio
import html
import base64
import json
import os
import zipfile
from io import BytesIO
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

if sys.platform.startswith("win"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

import pandas as pd
import streamlit as st
from PIL import Image
try:
    from st_aggrid import AgGrid, GridUpdateMode, DataReturnMode, JsCode
    HAS_AGGRID = True
except Exception:
    HAS_AGGRID = False


# Copy Streamlit secrets into environment BEFORE importing project modules.
for key in [
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "SUPABASE_URL",
    "SUPABASE_ANON_KEY",
    "SUPABASE_SERVICE_ROLE_KEY",
    "APP_PASSWORD",
]:
    try:
        if key in st.secrets and not os.getenv(key):
            os.environ[key] = str(st.secrets[key])
    except Exception:
        pass

import supabase_store as store
from main import (
    TEMPLATES_ROOT,
    _save_uploaded_template_as_jpg,
    friendly_error_message,
    get_template_variants,
    load_people_lists,
    run_pipeline,
    save_people_lists,
)

st.set_page_config(page_title="EduTap Testimonial Graphic Maker", layout="wide")

FEEDBACK_LABELS = {
    "edutap": "EduTap Feedback",
    "event": "Event Feedback",
    "mentor": "Mentor Feedback",
    "support": "Support Feedback",
    "course": "Course Feedback",
}

TYPE_FROM_LABEL = {v: k for k, v in FEEDBACK_LABELS.items()}


# ------------------------- State / CSS -------------------------

def init_state() -> None:
    st.session_state.setdefault("results", [])
    st.session_state.setdefault("operator_name", "")
    st.session_state.setdefault("job_queue", [])
    st.session_state.setdefault("page", "Create Graphic")
    st.session_state.setdefault("status_update_seen", set())
    st.session_state.setdefault("operator_input", st.session_state.get("operator_name", ""))
    st.session_state.setdefault("link_input", "")
    st.session_state.setdefault("feedback_label_input", "EduTap Feedback")
    st.session_state.setdefault("course_name_input", "")
    st.session_state.setdefault("event_mode_input", "1 Faculty")
    st.session_state.setdefault("event_faculty_input", "")
    st.session_state.setdefault("mentor_select_input", [])
    st.session_state.setdefault("support_mode_input", "1 Member")
    st.session_state.setdefault("support_member_input", "")


def inject_css() -> None:
    st.markdown(
        """
        <style>
        :root { color-scheme: dark; }
        html, body, [data-testid="stAppViewContainer"], [data-testid="stHeader"] { background: #0b1120 !important; color: #e5e7eb !important; }
        [data-testid="stAppViewContainer"] > .main { background: #0b1120 !important; }
        .block-container { padding-top: 2.2rem; max-width: 1280px; }
        h1, h2, h3, h4, h5, h6, p, label, span, div { color: #e5e7eb; }
        [data-testid="stCaptionContainer"], .stCaption, small { color: #94a3b8 !important; }
        hr { border-color: #334155 !important; }
        div[data-baseweb="input"] > div, textarea { background: #111827 !important; border-color: #334155 !important; color: #e5e7eb !important; }
        /* Make dropdowns visually different from text input boxes */
        div[data-baseweb="select"] > div {
            background: linear-gradient(135deg, #172554 0%, #0f766e 100%) !important;
            border: 1px solid #22d3ee !important;
            color: #ffffff !important;
            box-shadow: 0 0 0 1px rgba(34,211,238,0.14), 0 10px 22px rgba(8,145,178,0.16) !important;
            border-radius: 12px !important;
        }
        div[data-baseweb="select"] span, div[data-baseweb="select"] svg { color: #ffffff !important; fill: #ffffff !important; }
        div[data-baseweb="popover"], div[data-baseweb="menu"], ul[role="listbox"], div[role="listbox"] {
            background: linear-gradient(135deg, #172554 0%, #0f766e 100%) !important;
            border: 1px solid #22d3ee !important;
            border-radius: 12px !important;
            box-shadow: 0 18px 38px rgba(8,145,178,0.24) !important;
        }
        ul[role="listbox"] li, div[role="option"], div[data-baseweb="menu"] li {
            background: transparent !important;
            color: #e5e7eb !important;
        }
        ul[role="listbox"] li:hover, div[role="option"]:hover, div[data-baseweb="menu"] li:hover {
            background: rgba(34,211,238,0.18) !important;
            color: #ffffff !important;
        }
        div[role="option"][aria-selected="true"] {
            background: rgba(15,23,42,0.35) !important;
            color: #ffffff !important;
        }
        input, textarea { color: #e5e7eb !important; }
        button, .stButton > button, .stDownloadButton > button { background: #0f172a !important; color: #e5e7eb !important; border: 1px solid #334155 !important; border-radius: 10px !important; }
        .stButton > button[kind="primary"], .stDownloadButton > button[kind="primary"] { background: #0891b2 !important; border-color: #06b6d4 !important; color: white !important; }
        .stAlert { background: #111827 !important; border-color: #334155 !important; color: #e5e7eb !important; }
        .edutap-preview-card { background: #111827; border: 1px solid #334155; border-radius: 16px; padding: 12px; box-shadow: 0 14px 30px rgba(0,0,0,0.25); width: 100%; max-width: 320px; }
        .edutap-preview-image-wrap { position: relative; }
        .edutap-preview-card img { display: block; width: 100%; height: auto; border-radius: 10px; background: #0b1120; }
        .edutap-preview-name { color: #cbd5e1; text-align: center; font-size: 12px; margin-top: 8px; overflow-wrap: anywhere; padding: 0 30px 0 2px; }
        .edutap-preview-download { position: absolute; right: 10px; bottom: 10px; width: 34px; height: 34px; border-radius: 999px; background: rgba(15, 23, 42, 0.88); border: 1px solid #334155; color: #e5e7eb !important; display: inline-flex; align-items: center; justify-content: center; text-decoration: none !important; font-size: 17px; box-shadow: 0 8px 20px rgba(0,0,0,0.28); }
        .edutap-preview-download:hover { background: #0891b2; border-color: #06b6d4; color: white !important; }
        .edutap-queue-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(245px, 1fr)); gap: 12px; margin-top: 12px; }
        .edutap-queue-card { border: 1px solid #334155; border-radius: 12px; background: #111827; padding: 12px 14px; box-shadow: 0 3px 12px rgba(0, 0, 0, 0.22); min-height: 120px; }
        .edutap-queue-card-title { font-weight: 700; font-size: 14px; margin-bottom: 5px; color:#e5e7eb; }
        .edutap-queue-meta { color: #94a3b8; font-size: 12px; line-height: 1.5; word-break: break-word; }
        .edutap-small-note { color: #94a3b8; font-size: 12px; }
        .edutap-overlay { position: fixed; inset: 0; z-index: 999999; background: rgba(2, 6, 23, 0.78); backdrop-filter: blur(2px); display: flex; align-items: center; justify-content: center; }
        .edutap-overlay-card { background: #111827; color: #e5e7eb; border: 1px solid #334155; border-radius: 18px; padding: 28px 30px; width: 420px; max-width: 92vw; text-align: center; box-shadow: 0 30px 70px rgba(0, 0, 0, 0.55); }
        .edutap-spinner { width: 42px; height: 42px; border-radius: 999px; border: 4px solid #334155; border-top-color: #06b6d4; animation: edutap-spin 0.8s linear infinite; margin: 0 auto 14px; }
        @keyframes edutap-spin { to { transform: rotate(360deg); } }
        .edutap-cancel { display: inline-block; margin-top: 14px; border: 1px solid #475569; border-radius: 999px; padding: 8px 16px; color: #e5e7eb; text-decoration: none; background: #0f172a; font-size: 13px; }
        .ag-theme-streamlit, .ag-theme-balham-dark { --ag-background-color: #111827; --ag-foreground-color: #e5e7eb; --ag-header-background-color: #0f172a; --ag-border-color: #334155; --ag-row-hover-color: #1e293b; --ag-selected-row-background-color: #164e63; }
        </style>
        """,
        unsafe_allow_html=True,
    )


# ------------------------- Helpers -------------------------

@st.cache_resource(show_spinner=False)
def ensure_playwright_browser() -> bool:
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True,
            timeout=300,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return True
    except Exception as exc:
        print(f"WARNING: Playwright install step failed or was skipped: {exc}")
        return False


def run_async(coro):
    """Run async Playwright work safely under Streamlit on Windows."""
    import threading

    result_box = {}
    error_box = {}

    def _runner():
        if sys.platform.startswith("win"):
            try:
                asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
            except Exception:
                pass

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            result_box["value"] = loop.run_until_complete(coro)
        except Exception as exc:
            error_box["error"] = exc
        finally:
            try:
                loop.close()
            finally:
                asyncio.set_event_loop(None)

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()

    if "error" in error_box:
        raise error_box["error"]
    return result_box.get("value")


def safe_template_name(name: str) -> str:
    name = " ".join(str(name or "").strip().split())
    bad = '<>:"/\\|?*'
    for ch in bad:
        name = name.replace(ch, "")
    if not name:
        raise ValueError("Please enter the person name for this template.")
    return name


def _template_image_stems_from_local(folder: str) -> List[str]:
    """Read template image names from the deployed GitHub assets folder."""
    folder_path = Path(TEMPLATES_ROOT) / folder
    if not folder_path.exists():
        return []

    blocked = {
        "edutap feedback",
        "course feedback",
        "event feedback",
        "mentor feedback",
        "support feedback",
        ".gitkeep",
    }
    names: List[str] = []
    for file in folder_path.iterdir():
        if not file.is_file():
            continue
        if file.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            continue
        stem = file.stem.strip()
        if not stem or stem.lower() in blocked:
            continue
        names.append(stem)
    return sorted(set(names), key=lambda x: x.lower())


@st.cache_data(ttl=120, show_spinner=False)
def list_template_names_cached(folder: str) -> List[str]:
    return store.list_template_names(folder)


@st.cache_data(ttl=120, show_spinner=False)
def get_blank_template_reference_cached(feedback_type: str) -> Dict[str, Any] | None:
    return store.get_blank_template_reference(feedback_type)


@st.cache_data(ttl=120, show_spinner=False)
def get_people_lists_cached() -> Dict[str, List[str]]:
    """
    Build dropdown values live from template image filenames.

    Priority:
    1. local GitHub assets folder after deployment,
    2. Supabase templates bucket, if used from Upload Template tab,
    3. people_lists.json fallback.

    This is intentionally not cached, so new template names appear after a
    Streamlit rerun/redeploy without waiting for cache expiry.
    """
    lists = load_people_lists()
    faculty = set(lists.get("faculty") or [])
    support = set(lists.get("support") or [])

    # Local repo templates uploaded through GitHub website.
    for folder in ["Mentor feedback", "Event Feedback"]:
        faculty.update(_template_image_stems_from_local(folder))

    support.update(_template_image_stems_from_local("Support Feedback"))

    # Supabase templates uploaded through the app.
    for folder in ["Mentor feedback", "Event Feedback"]:
        for name in list_template_names_cached(folder):
            if name.lower() not in {"event feedback", "mentor feedback"}:
                faculty.add(name)

    for name in list_template_names_cached("Support Feedback"):
        if name.lower() != "support feedback":
            support.add(name)

    return {
        "faculty": sorted(faculty, key=lambda x: x.lower()),
        "support": sorted(support, key=lambda x: x.lower()),
    }


def show_processing_overlay(done: int, total: int) -> None:
    st.markdown(
        f"""
        <div class="edutap-overlay">
          <div class="edutap-overlay-card">
            <div class="edutap-spinner"></div>
            <h3>Making your graphic{'' if total == 1 else 's'}...</h3>
            <p>{done} of {total} completed. Please wait.</p>
            <p class="edutap-small-note">Do not close this tab while processing.</p>
            <a class="edutap-cancel" href="javascript:window.location.reload()">Cancel / Reload</a>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def show_upload_overlay() -> None:
    """Blocking upload overlay. No cancel link, to avoid opening about:blank."""
    st.markdown(
        """
        <div class="edutap-overlay">
          <div class="edutap-overlay-card">
            <div class="edutap-spinner"></div>
            <h3>Uploading template...</h3>
            <p>Please wait while we check and save the image online.</p>
            <p class="edutap-small-note">Do not close this tab while processing.</p>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def make_job_summary(job: Dict[str, Any]) -> str:
    t = FEEDBACK_LABELS.get(job.get("type"), job.get("type", "Feedback"))
    data = job.get("data") or {}
    details = []
    if job.get("type") == "mentor":
        details.append("Mentors: " + ", ".join(data.get("mentors") or []))
    elif job.get("type") == "event":
        details.append("Mode: " + ("Single" if data.get("mode") == "one" else "Multi"))
        if data.get("faculty"):
            details.append("Faculty: " + data.get("faculty"))
    elif job.get("type") == "support":
        details.append("Mode: " + ("Single" if data.get("mode") == "one" else "Team"))
        if data.get("member"):
            details.append("Member: " + data.get("member"))
    elif job.get("type") == "course":
        details.append("Course: " + (data.get("course_name") or ""))
    return f"{t}" + (f" | {' | '.join(details)}" if details else "")



def canonical_job_key(job: Dict[str, Any]) -> str:
    """Stable identity for duplicate queue detection."""
    data = dict(job.get("data") or {})
    # Sort list fields so the same mentors selected in a different order still match.
    for key, value in list(data.items()):
        if isinstance(value, list):
            data[key] = sorted(str(v).strip().lower() for v in value)
        elif isinstance(value, str):
            data[key] = value.strip().lower()
    payload = {
        "link": str(job.get("link") or "").strip().lower(),
        "type": str(job.get("type") or "").strip().lower(),
        "data": data,
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def queue_has_duplicate(job: Dict[str, Any]) -> bool:
    new_key = canonical_job_key(job)
    return any(canonical_job_key(existing) == new_key for existing in st.session_state.job_queue)


def load_job_into_form(job: Dict[str, Any]) -> None:
    """Move a queued job back into the form for editing."""
    st.session_state.operator_input = job.get("operator") or ""
    st.session_state.operator_name = job.get("operator") or ""
    st.session_state.link_input = job.get("link") or ""
    st.session_state.feedback_label_input = FEEDBACK_LABELS.get(job.get("type"), "EduTap Feedback")

    data = job.get("data") or {}
    job_type = job.get("type")
    if job_type == "course":
        st.session_state.course_name_input = data.get("course_name") or ""
    elif job_type == "event":
        st.session_state.event_mode_input = "1 Faculty" if data.get("mode") == "one" else "Multiple Faculties"
        st.session_state.event_faculty_input = data.get("faculty") or ""
    elif job_type == "mentor":
        st.session_state.mentor_select_input = list(data.get("mentors") or [])
    elif job_type == "support":
        st.session_state.support_mode_input = "1 Member" if data.get("mode") == "one" else "Team"
        st.session_state.support_member_input = data.get("member") or ""

def render_queue_grid() -> None:
    queue = st.session_state.job_queue
    st.markdown(f"### Queued Entries ({len(queue)})")
    if not queue:
        st.info("No entries added yet.")
        return

    # Render as real Streamlit cards, not one large raw HTML block. This prevents
    # Streamlit/Markdown from showing HTML code when many entries are queued.
    per_row = 3
    for row_start in range(0, len(queue), per_row):
        cols = st.columns(per_row)
        for offset, job in enumerate(queue[row_start:row_start + per_row]):
            idx = row_start + offset + 1
            with cols[offset]:
                with st.container(border=True):
                    st.markdown(f"**#{idx} {FEEDBACK_LABELS.get(job.get('type'), job.get('type', 'Feedback'))}**")
                    st.caption(make_job_summary(job))
                    link = str(job.get("link") or "")
                    if len(link) > 72:
                        link = link[:69] + "..."
                    st.caption(f"Link: {link}")
                    edit_col, remove_col = st.columns(2)
                    with edit_col:
                        if st.button("Edit", key=f"edit_queue_{idx}_{job.get('link','')}"):
                            load_job_into_form(job)
                            del st.session_state.job_queue[idx - 1]
                            st.rerun()
                    with remove_col:
                        if st.button("Remove", key=f"remove_queue_{idx}_{job.get('link','')}"):
                            del st.session_state.job_queue[idx - 1]
                            st.rerun()

    c1, c2 = st.columns([1, 5])
    with c1:
        if st.button("Clear Queue"):
            st.session_state.job_queue = []
            st.rerun()




def _image_to_data_uri(path: Path) -> str:
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _download_data_uri(path: Path) -> str:
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def build_local_zip_for_results(results: List[Dict[str, Any]]) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for result in results:
            path = Path(result.get("image_path") or "")
            if path.exists():
                zf.writestr(path.name, path.read_bytes())
    return buf.getvalue()


def normalize_aggrid_selected_rows(value: Any) -> List[Dict[str, Any]]:
    """AgGrid may return selected_rows as a list OR a DataFrame depending on version."""
    if value is None:
        return []
    if isinstance(value, pd.DataFrame):
        if value.empty:
            return []
        return value.to_dict("records")
    if isinstance(value, list):
        return value
    try:
        return list(value)
    except Exception:
        return []

def password_gate() -> None:
    """Simple password screen for public Streamlit deployment."""
    expected_password = (os.getenv("APP_PASSWORD") or "test@123").strip() or "test@123"

    if st.session_state.get("authenticated", False):
        return

    st.markdown("<br><br>", unsafe_allow_html=True)
    c1, c2, c3 = st.columns([1, 1.2, 1])
    with c2:
        st.title("EduTap Access")
        st.caption("Enter password to open the testimonial app.")
        with st.form("edutap_login_form", clear_on_submit=False):
            password = st.text_input("Password", type="password", placeholder="Enter password")
            submitted = st.form_submit_button("Open App", type="primary")
            if submitted:
                if password == expected_password:
                    st.session_state["authenticated"] = True
                    st.rerun()
                else:
                    st.error("Wrong password. Please try again.")
    st.stop()


# ------------------------- Template Upload Tab -------------------------

UPLOAD_TEMPLATE_LABELS = ["Mentor Feedback", "Support Feedback"]


def validate_uploaded_template_image(upload) -> Image.Image:
    """Validate uploaded template and show exact reasons.

    Do not route these errors through friendly_error_message(), because
    operators need exact validation reasons for template uploads.
    """
    if upload is None:
        raise ValueError("Please select a template image.")

    errors: list[str] = []
    suffix = Path(upload.name or "").suffix.lower()

    try:
        img = Image.open(BytesIO(upload.getvalue()))
        img.load()
    except Exception:
        raise ValueError("Template image could not be opened. Please upload a valid image file.")

    if suffix != ".jpg":
        errors.append(f"Template image must be a .jpg file. Current file type is {suffix or 'unknown'}.")

    if (img.width, img.height) != (1080, 1080):
        errors.append(f"Template image must be exactly 1080 px x 1080 px. Current size: {img.width} x {img.height} px.")

    if errors:
        raise ValueError(" ".join(errors))

    return img.convert("RGB")

def show_sample_reference(feedback_type: str) -> None:
    """Offer a blank template reference download for the selected feedback type."""
    try:
        sample = get_blank_template_reference_cached(feedback_type)
    except Exception:
        sample = None

    st.markdown("#### Blank template reference")
    st.caption("Download the blank template reference and share it with the designer for the required 1080 x 1080 JPG template style.")

    if not sample:
        st.info(
            "No blank template reference was found in template storage for this feedback type. "
            "Please keep at least one blank/person template in Supabase Storage → templates for this feedback type."
        )
        return

    sample_bytes = sample.get("bytes") or b""
    filename = sample.get("name") or f"{feedback_type}_blank_template_reference.jpg"

    if sample_bytes:
        st.download_button(
            "Download Blank Template Reference",
            data=sample_bytes,
            file_name=filename,
            mime="image/jpeg",
            type="secondary",
        )
        st.caption(f"Reference file: {filename}")


def upload_template_ui() -> None:
    st.subheader("Upload Person Template")

    feedback_label = st.selectbox(
        "Feedback Type",
        UPLOAD_TEMPLATE_LABELS,
        key="tpl_type_label",
    )
    feedback_type = TYPE_FROM_LABEL[feedback_label]

    show_sample_reference(feedback_type)
    st.divider()

    if feedback_type == "mentor":
        folder = "Mentor feedback"
        list_key = "faculty"
        name_label = "Mentor Name"
        name_placeholder = "Example: Rohit Sharma"
    elif feedback_type == "support":
        folder = "Support Feedback"
        list_key = "support"
        name_label = "Support Member Name"
        name_placeholder = "Example: Rohit Sharma"
    else:
        st.error("Only Mentor and Support templates can be uploaded here.")
        return

    with st.form("upload_person_template_form", clear_on_submit=False):
        person_name = st.text_input(name_label, placeholder=name_placeholder)
        st.caption("The dropdown name will be created from this person name. The uploaded image must be exactly 1080 x 1080 px and .jpg.")
        upload = st.file_uploader("Template Image (.jpg only, 1080 x 1080 px)", type=["jpg", "jpeg", "png", "webp"])
        submitted = st.form_submit_button("Upload Template", type="primary")

    if submitted:
        # Validate first. If the file is not JPG or not 1080 x 1080, show the error
        # immediately and do not show the blocking upload overlay.
        try:
            safe = safe_template_name(person_name)
            img = validate_uploaded_template_image(upload)
        except Exception as exc:
            st.error(str(exc))
            return

        overlay = st.empty()
        with overlay:
            show_upload_overlay()

        try:
            rel = f"{folder}/{safe}.jpg"
            local_path = Path(TEMPLATES_ROOT) / rel
            local_path.parent.mkdir(parents=True, exist_ok=True)

            # Save a clean RGB JPG locally, then upload the same file to Supabase.
            img.save(local_path, format="JPEG", quality=95, optimize=True)

            lists = load_people_lists()
            existing = set(lists.get(list_key) or [])
            if safe not in existing:
                lists.setdefault(list_key, []).append(safe)
                lists[list_key] = sorted(set(lists[list_key]), key=lambda x: x.lower())
                save_people_lists(lists)

            store.upload_template_file(str(local_path), rel)
            st.cache_data.clear()
            overlay.empty()
            st.success(f"Template uploaded successfully for {safe}. It is stored online in Supabase Storage → templates/{rel}")
        except Exception as exc:
            overlay.empty()
            st.error(friendly_error_message(exc))


# ------------------------- Create Graphic Tab -------------------------

def current_extra_data_ui(feedback_type: str, people: Dict[str, List[str]]) -> Dict[str, Any] | None:
    if feedback_type == "edutap":
        return {}

    if feedback_type == "course":
        course_name = st.text_input("Course Name", placeholder="Example: RBI Grade B 2026", key="course_name_input")
        if not course_name.strip():
            st.info("Enter course name.")
            return None
        return {"course_name": course_name.strip()}

    if feedback_type == "event":
        mode_label = st.radio("Event type", ["1 Faculty", "Multiple Faculties"], horizontal=True, key="event_mode_input")
        if mode_label == "1 Faculty":
            faculty_options = [""] + people.get("faculty", [])
            if st.session_state.get("event_faculty_input") not in faculty_options:
                st.session_state.event_faculty_input = ""
            faculty = st.selectbox("Faculty", faculty_options, key="event_faculty_input")
            if not faculty:
                st.info("Select faculty.")
                return None
            return {"mode": "one", "faculty": faculty, "person": faculty}
        return {"mode": "multi"}

    if feedback_type == "mentor":
        available_mentors = people.get("faculty", [])
        st.session_state.mentor_select_input = [m for m in st.session_state.get("mentor_select_input", []) if m in available_mentors]
        mentors = st.multiselect("Select mentor(s)", available_mentors, key="mentor_select_input")
        if not mentors:
            st.info("Select at least one mentor.")
            return None
        return {"mentors": mentors}

    if feedback_type == "support":
        mode_label = st.radio("Support type", ["1 Member", "Team"], horizontal=True, key="support_mode_input")
        if mode_label == "1 Member":
            support_options = [""] + people.get("support", [])
            if st.session_state.get("support_member_input") not in support_options:
                st.session_state.support_member_input = ""
            member = st.selectbox("Support member", support_options, key="support_member_input")
            if not member:
                st.info("Select support member.")
                return None
            return {"mode": "one", "member": member, "person": member}
        return {"mode": "team"}

    return {}


def validate_job(operator: str, link: str, feedback_type: str, data: Dict[str, Any] | None) -> Dict[str, Any]:
    if not operator.strip():
        raise ValueError("Please enter operator name.")
    if not link.strip().lower().startswith(("http://", "https://")):
        raise ValueError("Please enter a valid Zoho email link.")
    if data is None:
        raise ValueError("Please complete the required fields.")

    return {
        "operator": operator.strip(),
        "link": link.strip(),
        "type": feedback_type,
        "data": dict(data or {}),
    }


def process_jobs(jobs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not jobs:
        return []

    # Show the blocking overlay immediately, before Playwright install/check,
    # template checks, browser launch, GPT call, or image generation.
    overlay = st.empty()
    with overlay:
        show_processing_overlay(0, max(len(jobs), 1))
    time.sleep(0.05)

    ensure_playwright_browser()
    all_results: List[Dict[str, Any]] = []
    total_units = 0

    for job in jobs:
        store.ensure_templates_available(job["type"], job.get("data") or {})
        total_units += len(get_template_variants(job["type"], job.get("data") or {}))

    done_units = 0
    overlay.empty()
    with overlay:
        show_processing_overlay(done_units, total_units)

    try:
        for job in jobs:
            variants = get_template_variants(job["type"], job.get("data") or {})
            for template_path, label in variants:
                result = run_async(run_pipeline(job["link"], template_path, filename_suffix=label))

                per_data = dict(job.get("data") or {})
                per_data["student_name"] = result.get("student_name") or ""
                if job["type"] == "mentor" and label:
                    per_data["person"] = label

                record = store.save_generated_record(
                    feedback_type=job["type"],
                    entered_by=job["operator"],
                    email_link=job["link"],
                    local_image_path=result["image_path"],
                    student_name=result.get("student_name") or "",
                    data=per_data,
                )
                result["record"] = record
                all_results.append(result)

                done_units += 1
                overlay.empty()
                with overlay:
                    show_processing_overlay(done_units, total_units)
    finally:
        overlay.empty()

    return all_results


def generate_ui() -> None:
    st.subheader("Create Testimonial Graphic")
    people = get_people_lists_cached()

    operator = st.text_input("Operator Name (Entered by)", key="operator_input")
    st.session_state.operator_name = operator.strip()

    link = st.text_input("Zoho Email Link", placeholder="https://zopen.to/...", key="link_input")
    feedback_options = list(FEEDBACK_LABELS.values())
    if st.session_state.get("feedback_label_input") not in feedback_options:
        st.session_state.feedback_label_input = "EduTap Feedback"
    feedback_label = st.selectbox("Type of Feedback", feedback_options, key="feedback_label_input")
    feedback_type = TYPE_FROM_LABEL[feedback_label]

    data = current_extra_data_ui(feedback_type, people)

    b1, b2, b3 = st.columns([1, 1.3, 5])
    with b1:
        add_clicked = st.button("Add to Batch", type="secondary")
    with b2:
        submit_clicked = st.button("Submit Current Only", type="secondary")
    with b3:
        submit_all_clicked = st.button(
            f"Submit All Queued ({len(st.session_state.job_queue)})",
            type="primary",
            disabled=(len(st.session_state.job_queue) == 0),
        )

    if st.session_state.job_queue:
        st.caption("Use **Submit All Queued** to generate every queued entry. **Submit Current Only** generates only the form currently visible above.")

    if add_clicked:
        try:
            job = validate_job(operator, link, feedback_type, data)
            if queue_has_duplicate(job):
                st.warning("This same entry is already in the queue. Duplicate not added.")
            else:
                st.session_state.job_queue.append(job)
                st.success("Entry added to batch.")
                st.rerun()
        except Exception as exc:
            st.error(friendly_error_message(exc))

    if submit_clicked or submit_all_clicked:
        try:
            if submit_all_clicked:
                jobs = list(st.session_state.job_queue)
                if not jobs:
                    raise ValueError("No queued entries found.")
            else:
                jobs = [validate_job(operator, link, feedback_type, data)]

            results = process_jobs(jobs)
            st.session_state.results = results
            if submit_all_clicked:
                st.session_state.job_queue = []
            st.success(f"{len(results)} graphic(s) generated and saved online.")
            st.cache_data.clear()
        except Exception as exc:
            st.error(friendly_error_message(exc))

    render_queue_grid()

    if st.session_state.results:
        st.subheader("Live Preview")
        valid_results = [r for r in list(st.session_state.results) if r.get("image_path") and Path(r.get("image_path")).exists()]

        if len(valid_results) > 1:
            st.download_button(
                "Download All Live Images as ZIP",
                data=build_local_zip_for_results(valid_results),
                file_name="generated_testimonials.zip",
                mime="application/zip",
                type="primary",
            )

        cols_per_row = 3
        for row_start in range(0, len(valid_results), cols_per_row):
            cols = st.columns(cols_per_row)
            for offset, result in enumerate(valid_results[row_start:row_start + cols_per_row]):
                idx = row_start + offset
                path = Path(result.get("image_path"))
                with cols[offset]:
                    st.markdown(
                        f"""
                        <div class="edutap-preview-card">
                            <div class="edutap-preview-image-wrap">
                                <img src="{_image_to_data_uri(path)}" alt="{html.escape(path.name)}" />
                                <a class="edutap-preview-download" href="{_download_data_uri(path)}" download="{html.escape(path.name)}" title="Download image" aria-label="Download image">&#8681;</a>
                            </div>
                            <div class="edutap-preview-name">{html.escape(path.name)}</div>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )


# ------------------------- Saved Records Tab -------------------------

def get_records_cached(type_filter: str, status_filter: str, search: str) -> List[Dict[str, Any]]:
    # Do not cache records: operators expect newly generated graphics and scheduling changes to appear immediately.
    return store.list_records(type_filter, status_filter, search)


def rows_to_editor_df(rows: List[Dict[str, Any]]) -> pd.DataFrame:
    data = []
    for row in rows:
        data.append(
            {
                "Select": False,
                "ID": int(row.get("id") or 0),
                "Date & Time": str(row.get("created_at") or "")[:16].replace("T", " "),
                "Type": (row.get("feedback_type") or "").title(),
                "Entered By": row.get("entered_by") or "",
                "Student": row.get("student_name") or "",
                "Person / Course": row.get("person_name") or row.get("course_name") or "",
                "Email Link": row.get("email_link") or "",
                "Image Link": row.get("image_url") or "",
                "Scheduling": row.get("scheduled_status") or "Pending",
            }
        )
    return pd.DataFrame(data)


def records_ui() -> None:
    st.subheader("Saved Records")
    c1, c2, c3, c4 = st.columns([1, 1, 2, 1])
    with c1:
        type_filter_label = st.selectbox("Feedback Type", ["All Types"] + list(FEEDBACK_LABELS.values()))
        type_filter = "all" if type_filter_label == "All Types" else TYPE_FROM_LABEL[type_filter_label]
    with c2:
        status_filter = st.selectbox("Scheduling", ["all", "Pending", "Done"], format_func=lambda x: "All" if x == "all" else x)
    with c3:
        search = st.text_input("Search", placeholder="Student, person, course, link...")
    with c4:
        st.write("")
        if st.button("Refresh Table"):
            st.cache_data.clear()
            st.rerun()

    try:
        rows = get_records_cached(type_filter, status_filter, search)
    except Exception as exc:
        st.error(friendly_error_message(exc))
        with st.expander("Technical details"):
            st.code(str(exc))
        rows = []

    st.caption(f"{len(rows)} records")

    if not rows:
        st.info("No records found.")
        return

    df = rows_to_editor_df(rows)
    original_status = {int(r.get("id")): (r.get("scheduled_status") or "Pending") for r in rows}

    # Preferred table: AgGrid. It supports row checkboxes, dropdown editing,
    # clickable links, and green Done rows in one main table.
    if HAS_AGGRID:
        email_link_renderer = JsCode('''
        class UrlCellRenderer {
          init(params) {
            this.eGui = document.createElement('a');
            this.eGui.innerText = params.value ? 'Open' : '';
            this.eGui.href = params.value || '#';
            this.eGui.target = '_blank';
            this.eGui.style.color = '#0369a1';
            this.eGui.style.textDecoration = 'underline';
          }
          getGui() { return this.eGui; }
        }
        ''')
        image_link_renderer = JsCode('''
        class ImageUrlCellRenderer {
          init(params) {
            this.eGui = document.createElement('a');
            this.eGui.innerText = params.value ? 'Image' : '';
            this.eGui.href = params.value || '#';
            this.eGui.target = '_blank';
            this.eGui.style.color = '#0369a1';
            this.eGui.style.textDecoration = 'underline';
          }
          getGui() { return this.eGui; }
        }
        ''')
        row_style = JsCode('''
        function(params) {
          if (params.data && params.data.Scheduling === 'Done') {
            return {'backgroundColor': '#d9ead3'};
          }
          return {};
        }
        ''')

        grid_options = {
            "rowSelection": "multiple",
            "suppressRowClickSelection": True,
            "getRowStyle": row_style,
            "defaultColDef": {"resizable": True, "sortable": True, "filter": True},
            "columnDefs": [
                {"field": "ID", "headerName": "ID", "width": 80, "checkboxSelection": True, "headerCheckboxSelection": True},
                {"field": "Date & Time", "width": 150, "editable": False},
                {"field": "Type", "width": 105, "editable": False},
                {"field": "Entered By", "width": 170, "editable": False},
                {"field": "Student", "width": 145, "editable": False},
                {"field": "Person / Course", "width": 180, "editable": False},
                {"field": "Email Link", "width": 115, "editable": False, "cellRenderer": email_link_renderer},
                {"field": "Image Link", "width": 115, "editable": False, "cellRenderer": image_link_renderer},
                {"field": "Scheduling", "width": 140, "editable": True, "cellEditor": "agSelectCellEditor", "cellEditorParams": {"values": ["Pending", "Done"]}},
            ],
        }

        grid_response = AgGrid(
            df,
            gridOptions=grid_options,
            update_mode=GridUpdateMode.MODEL_CHANGED | GridUpdateMode.SELECTION_CHANGED,
            data_return_mode=DataReturnMode.FILTERED_AND_SORTED,
            allow_unsafe_jscode=True,
            fit_columns_on_grid_load=True,
            height=min(650, 125 + 42 * max(len(df), 1)),
            theme="balham-dark",
            key="records_aggrid",
        )

        grid_data = pd.DataFrame(grid_response.get("data", []))
        changed = []
        if not grid_data.empty and "ID" in grid_data.columns and "Scheduling" in grid_data.columns:
            for _, row in grid_data.iterrows():
                rid = int(row["ID"])
                status = str(row.get("Scheduling") or "Pending")
                if original_status.get(rid) != status:
                    changed.append((rid, status))

        if changed:
            try:
                actually_updated = []
                for rid, status in changed:
                    marker = (rid, status)
                    # AgGrid can resend the same edited value on a later rerun. Avoid duplicate updates/toasts.
                    if marker in st.session_state.status_update_seen:
                        continue
                    store.update_scheduling(rid, status)
                    st.session_state.status_update_seen.add(marker)
                    actually_updated.append(marker)

                if actually_updated:
                    st.toast("Scheduling updated.", icon="✅")
                    # No st.rerun here. The table itself already shows the changed dropdown value and green Done row.
            except Exception as exc:
                st.error(friendly_error_message(exc))

        selected_rows = normalize_aggrid_selected_rows(grid_response.get("selected_rows"))
        selected_ids = set()
        for row in selected_rows:
            try:
                selected_ids.add(int(row.get("ID")))
            except Exception:
                pass
        selected_source_rows = [r for r in rows if int(r.get("id") or 0) in selected_ids]

        if selected_source_rows:
            zip_bytes = store.build_zip_for_records(selected_source_rows)
            st.download_button(
                f"Download Selected ZIP ({len(selected_source_rows)})",
                data=zip_bytes,
                file_name="selected_testimonials.zip",
                mime="application/zip",
                type="primary",
            )
        else:
            st.button("Download Selected ZIP", disabled=True)

        return

    # Fallback if streamlit-aggrid is not installed: editable table without row coloring.
    st.warning("For best table controls, install streamlit-aggrid: pip install streamlit-aggrid")
    edited = st.data_editor(
        df,
        hide_index=True,
        width="stretch",
        height=min(620, 92 + 38 * max(len(df), 1)),
        column_config={
            "Select": st.column_config.CheckboxColumn("Select", help="Select for ZIP download"),
            "ID": st.column_config.NumberColumn("ID", disabled=True),
            "Email Link": st.column_config.LinkColumn("Email Link", display_text="Open"),
            "Image Link": st.column_config.LinkColumn("Image Link", display_text="Image"),
            "Scheduling": st.column_config.SelectboxColumn("Scheduling", options=["Pending", "Done"], required=True),
        },
        disabled=["ID", "Date & Time", "Type", "Entered By", "Student", "Person / Course", "Email Link", "Image Link"],
        key="records_editor_fallback",
    )
    selected_ids = set(int(x) for x in edited.loc[edited["Select"] == True, "ID"].tolist()) if not edited.empty else set()
    edited_status = {int(row["ID"]): row["Scheduling"] for _, row in edited.iterrows()}
    changed = [(rid, status) for rid, status in edited_status.items() if original_status.get(rid) != status]
    if changed:
        for rid, status in changed:
            marker = (rid, status)
            if marker not in st.session_state.status_update_seen:
                store.update_scheduling(rid, status)
                st.session_state.status_update_seen.add(marker)
        st.toast("Scheduling updated.", icon="✅")
    selected_rows = [r for r in rows if int(r.get("id") or 0) in selected_ids]
    if selected_rows:
        st.download_button("Download Selected ZIP", data=store.build_zip_for_records(selected_rows), file_name="selected_testimonials.zip", mime="application/zip")
    else:
        st.button("Download Selected ZIP", disabled=True)


# ------------------------- Main -------------------------

def main() -> None:
    init_state()
    inject_css()
    password_gate()

    st.title("Testimonial Graphic Maker")
    st.caption("EduTap online version: Streamlit + Supabase")

    missing = [k for k in ["OPENAI_API_KEY", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"] if not os.getenv(k)]
    if missing:
        st.warning("Missing secrets: " + ", ".join(missing))

    page = st.radio(
        "Page",
        ["Create Graphic", "Saved Records", "Upload Template"],
        horizontal=True,
        label_visibility="collapsed",
        key="page",
    )
    st.divider()

    # IMPORTANT: use a radio-based page switch instead of st.tabs. Streamlit renders
    # all tabs at once, which caused the records table to reload while the operator
    # was only filling the create form. This renders only the active page.
    if page == "Create Graphic":
        generate_ui()
    elif page == "Saved Records":
        records_ui()
    else:
        upload_template_ui()


if __name__ == "__main__":
    main()
