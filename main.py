# -*- coding: utf-8 -*-
# EduTap – LLM Blur + Final Graphic Composer (merged with image2.py settings)
# - Node-spanning blur via Playwright + GPT JSON
# - Final composition uses bold-only highlights, hex colors, full-width email backdrop
# - Footer transparency trimmed; optional scaling; email pasted "as-is" (no upscaling)
# - Safe for embedded double quotes in testimonial/highlight strings

from io import BytesIO
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
import os
import re
import json
import sys
import asyncio
import base64
from typing import List, Optional, Tuple, Dict, Any, Literal
from pathlib import Path
from datetime import date
import threading

from difflib import SequenceMatcher
import requests
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageChops
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from google_clients import (
    save_testimonial_and_image,
    LOCAL_OUTPUT_DIR,
    get_app_operator_name,
    set_app_operator_name,
)
# expose current Playwright context to the composer (for emoji rendering)
pw_context = None

# Simple in-memory cache so we don't call GPT multiple times
# for the same email link during a session.
gpt_cache: dict[str, dict[str, Any]] = {}

# ===================== CONFIG: PAGE / BLUR / OUTPUT =====================
TARGET_URL = "https://zopen.to/VxzORohuVyYs08INOA61"     # <-- Put your URL
ELEMENT_SELECTOR_CANDIDATES = [
    # Exact Zoho mail card - keeps screenshot tight.
    "div[tabindex='0'][class*='zmail__']",
    ".zmsharelink-content div[tabindex='0'][class*='zmail__']",
    "div[class*='zmail--expanded__']",
    "div[class^='zmail__']",
    "div[class*=' zmail__']",
    # Fallback wrappers.
    ".zmsharelink-content div[class^='zmails__']",
    ".zmsharelink-content div[class*=' zmails__']",
    "div[class^='zmails__']",
    "div[class*=' zmails__']",
]
OUTPUT_IMAGE = "element_screenshot_blurred.png"

# ===================== CONFIG: TESTIMONIAL GRAPHIC (image2.py settings) ======================

# Streamlit version: main.py lives in the project root.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = BASE_DIR

# JSON file with faculty/support lists (stays inside project root)
PEOPLE_LISTS_FILE = os.path.join(BASE_DIR, "people_lists.json")

# assets folder inside project root
ASSETS_DIR = os.path.join(PROJECT_ROOT, "assets")

# templates folder inside assets
TEMPLATES_ROOT = os.path.join(ASSETS_DIR, "Templates")

# New fixed PNG design templates.
NEW_DESIGN_TEMPLATES_ROOT = os.path.join(TEMPLATES_ROOT, "NewDesign")
NEW_DESIGN_TEMPLATE_FILES = {
    "edutap": "EduTap.png",
    "event": "Event.png",
    "mentor": "Mentor.png",
    "support": "Support.png",
    "course": "Course.png",
}

# Default if something goes wrong - EduTap overall.
DEFAULT_TEMPLATE_PATH = os.path.join(
    NEW_DESIGN_TEMPLATES_ROOT,
    NEW_DESIGN_TEMPLATE_FILES["edutap"],
)


def _normalize_feedback_type(feedback_type: str) -> str:
    t = (feedback_type or "").strip().lower()
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
    return "edutap"


def _new_design_template_path(feedback_type: str) -> str:
    t = _normalize_feedback_type(feedback_type)
    return os.path.join(NEW_DESIGN_TEMPLATES_ROOT, NEW_DESIGN_TEMPLATE_FILES.get(t, "EduTap.png"))


def get_template_variants(feedback_type: str, extra: Dict[str, Any] | None) -> List[Tuple[str, str]]:
    """
    New design system:
    - one fixed 1080 x 1080 PNG template per feedback type,
    - no testimonial text or student name is printed on the graphic,
    - person/course fields are still kept only for metadata/database behavior.

    Existing UI/batch behavior is preserved:
    - Mentor with multiple mentors still creates one image per selected mentor,
      but each uses the same Mentor.png design.
    - Event/Support person selections still affect labels/metadata only.
    """
    extra = extra or {}
    t = _normalize_feedback_type(feedback_type)
    path = _new_design_template_path(t)

    labels: List[str] = []
    if t == "mentor":
        mentors = extra.get("mentors") or []
        if isinstance(mentors, str):
            mentors = [mentors]
        labels = [str(m).strip() for m in mentors if str(m).strip()]
        if not labels:
            labels = ["mentor"]
    elif t == "event":
        mode = str(extra.get("mode") or "one").strip().lower()
        if mode == "one" and str(extra.get("faculty") or "").strip():
            labels = [str(extra.get("faculty")).strip()]
        else:
            labels = ["event_multi"]
    elif t == "support":
        mode = str(extra.get("mode") or "one").strip().lower()
        if mode == "one" and str(extra.get("member") or "").strip():
            labels = [str(extra.get("member")).strip()]
        else:
            labels = ["support_team"]
    elif t == "course":
        labels = ["course"]
    else:
        labels = ["edutap"]

    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Template not found: {path}. Put the file here: assets/Templates/NewDesign/{NEW_DESIGN_TEMPLATE_FILES.get(t, 'EduTap.png')}"
        )

    return [(path, label) for label in labels]


def _default_people_lists() -> Dict[str, List[str]]:
    """Default lists if JSON file not present or corrupt."""
    return {
        "faculty": [
            "Himanshu Arora",
            "Kuldeep Singh",
            "Neha Sharma",
            "Prateek Jain",
        ],
        "support": [
            "Aditya (Support)",
            "Shruti (Support)",
            "Rohan (Support)",
        ],
    }

def load_people_lists() -> Dict[str, List[str]]:
    """Load faculty/support lists from JSON file, with safe fallback."""
    if not os.path.exists(PEOPLE_LISTS_FILE):
        return _default_people_lists()
    try:
        with open(PEOPLE_LISTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Ensure keys exist
        if "faculty" not in data or not isinstance(data["faculty"], list):
            data["faculty"] = []
        if "support" not in data or not isinstance(data["support"], list):
            data["support"] = []
        return data
    except Exception:
        # If file corrupted, fall back to defaults
        return _default_people_lists()

def save_people_lists(data: Dict[str, List[str]]) -> None:
    """Persist updated lists into JSON file."""
    os.makedirs(os.path.dirname(PEOPLE_LISTS_FILE), exist_ok=True)
    with open(PEOPLE_LISTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def _safe_template_name(name: str) -> str:
    """
    Keeps names compatible with existing template lookup:
    <Name>.jpg
    """
    name = str(name or "").strip()
    name = re.sub(r'[<>:"/\\\\|?*]+', "", name)
    name = re.sub(r"\s+", " ", name)
    if not name:
        raise ValueError("Template name cannot be empty.")
    return name


def _save_uploaded_template_as_jpg(upload: UploadFile, destination_path: str) -> None:
    """
    Converts uploaded template to JPG because current template resolver expects .jpg.
    """
    os.makedirs(os.path.dirname(destination_path), exist_ok=True)

    try:
        img = Image.open(upload.file).convert("RGBA")

        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        bg.alpha_composite(img)

        rgb = bg.convert("RGB")
        rgb.save(destination_path, "JPEG", quality=95)
    except Exception as e:
        raise RuntimeError(f"Could not save template image: {e}")

FOOTER_IMAGE_PATH  = os.path.join(ASSETS_DIR, "footer", "Footer.png")
EMAIL_SCREENSHOT_PATH = OUTPUT_IMAGE
FINAL_GRAPHIC_PATH = os.path.join(PROJECT_ROOT, "output", "testimonial_final.png")

# ---- Typography & palette (hex supported) ----
FONT_REGULAR_PATH = os.path.join(ASSETS_DIR, "fonts", "Poppins-Regular.ttf")
FONT_BOLD_PATH    = os.path.join(ASSETS_DIR, "fonts", "Poppins-BoldItalic.ttf")
NAME_FONT_PATH    = os.path.join(ASSETS_DIR, "fonts", "Poppins-ExtraBold.ttf")
EMOJI_FONT_PATH   = os.path.join(ASSETS_DIR, "fonts", "NotoColorEmoji-Regular.ttf")

# --- Real color emoji rendering via Chromium (Playwright) ---
RENDER_QUOTE_WITH_BROWSER = True
BROWSER_FONT_STACK = "Segoe UI, Arial, 'Noto Color Emoji', 'Apple Color Emoji', 'Twemoji Mozilla', sans-serif"

QUOTE_FONT_SIZE    = 28
LINE_HEIGHT_MULT   = 1.35
ADD_QUOTES         = True
OPEN_Q, CLOSE_Q    = "“", "”"

QUOTE_TEXT_COLOR   = "#161716"
BOLD_TEXT_COLOR    = "#008094"

NAME_FONT_SIZE     = 35
NAME_IS_BOLD       = True
NAME_COLOR         = "#151716"

# ---- OLD LAYOUT (Name ABOVE the email block)
QUOTE_TOP_Y         = 400
QUOTE_SIDE_PADDING  = 100
NAME_TOP_MARGIN     = 13          # gap between quote & name
EMAIL_TOP_MARGIN    = 28         # gap between name & teal panel

# ---- Star rating row (5-star png under name) ----
STAR_IMAGE_PATH        = os.path.join(ASSETS_DIR, "icons", "star.png")
STAR_SIZE_PX           = 150   # SINGLE size control: increase/decrease to scale stars
STAR_MARGIN_BELOW_NAME = -40   # vertical gap between name and stars
STAR_MARGIN_ABOVE_EMAIL= -40   # vertical gap between stars and teal panel

# ---- Email panel (classic version)
EMAIL_PANEL_H        = 280
EMAIL_SIDE_PADDING   = 60
EMAIL_BOTTOM_MARGIN  = 18

# ---- Email scaling
EMAIL_ALLOW_UPSCALE  = False      # ← MISSING EARLIER (now added)

# ---- Hashtag / tagline inside teal email block ----
TAGLINE_TEXT              = "#WeGotYourBack"
TAGLINE_FONT_SIZE         = 23
TAGLINE_COLOR             = "#FFFFFF"
TAGLINE_MARGIN_ABOVE_BOTTOM = 24   # gap from bottom of teal block
TAGLINE_MIN_GAP_FROM_EMAIL  = 15   # minimum gap below email screenshot

# ---- Email backdrop settings
EMAIL_BACKDROP_ENABLE      = True
EMAIL_BACKDROP_COLOR       = "#0FA4A5"
EMAIL_BACKDROP_TOP_PAD     = 20
EMAIL_BACKDROP_BOTTOM_PAD  = 26
EMAIL_BACKDROP_SIDE_PAD    = 0

# ---- Email quality
EMAIL_SHARPEN_AMOUNT = 0

# ---- Email proof capture + placement settings for new templates ----
# Capture a compact Zoho layout like the standalone command:
# python capture_zoho_email.py --output email.png
EMAIL_CAPTURE_VIEWPORT_WIDTH = 980
EMAIL_CAPTURE_VIEWPORT_HEIGHT = 760
EMAIL_SCREENSHOT_DEVICE_SCALE = 2

EMAIL_SCREENSHOT_CROP_ENABLE = True
EMAIL_SCREENSHOT_CROP_PADDING = 8
EMAIL_SCREENSHOT_CROP_THRESHOLD = 12
EMAIL_SCREENSHOT_LIGHT_SHARPEN = True
EMAIL_SCREENSHOT_SHARPEN_RADIUS = 1.0
EMAIL_SCREENSHOT_SHARPEN_PERCENT = 90
EMAIL_SCREENSHOT_SHARPEN_THRESHOLD = 2

FINAL_GRAPHIC_WIDTH = 1080
FINAL_GRAPHIC_HEIGHT = 1080
EMAIL_PASTE_CENTER_X = 540.0
EMAIL_PASTE_CENTER_Y = 592.5

# Maximum design area where the email proof is allowed to fit.
# IMPORTANT: EduTap wants width to be consumed properly.
# So final composition is WIDTH-PRIORITY:
# - screenshot is scaled to EMAIL_PASTE_TARGET_WIDTH first,
# - aspect ratio is preserved,
# - no stretching is done,
# - wider browser capture is used so long emails wrap less and remain usable.
EMAIL_PASTE_MAX_WIDTH = 916
EMAIL_PASTE_MAX_HEIGHT = 560
EMAIL_PASTE_TARGET_WIDTH = 916
EMAIL_PASTE_ALLOW_UPSCALE = True
EMAIL_PASTE_WIDTH_PRIORITY = True

# Email card styling to match the Photoshop sample.
EMAIL_PROOF_CORNER_RADIUS = 14
EMAIL_PROOF_STROKE_ENABLE = True
EMAIL_PROOF_STROKE_WIDTH = 1
EMAIL_PROOF_STROKE_RGBA = (185, 236, 243, 255)  # light cyan 1px inside stroke

EMAIL_PROOF_SHADOW_ENABLE = True
EMAIL_PROOF_SHADOW_COLOR_RGBA = (18, 163, 189, 23)  # teal shadow, ~9% opacity
EMAIL_PROOF_SHADOW_ANGLE_DEG = 108
EMAIL_PROOF_SHADOW_DISTANCE = 19
EMAIL_PROOF_SHADOW_BLUR = 23   # Photoshop size ~46 approximated as Gaussian radius ~23
EMAIL_PROOF_SHADOW_SPREAD = 0
EMAIL_PROOF_SHADOW_ALPHA = 23

# ---- Footer
FOOTER_BOTTOM_MARGIN = 7
FOOTER_WHITE_PADDING = 0
FOOTER_WIDTH_RATIO   = 0.80
FOOTER_MAX_HEIGHT    = None
FOOTER_TRIM_ALPHA    = True

# ===================== CONFIG: LLM INTEGRATION ==========================
USE_GPT = True

def _load_local_env_file() -> None:
    """
    Simple .env loader so non-technical users can put OPENAI_API_KEY in:
    - project root/.env, or
    - backend/.env
    without installing python-dotenv.
    """
    for env_path in (os.path.join(PROJECT_ROOT, ".env"), os.path.join(BASE_DIR, ".env")):
        if not os.path.exists(env_path):
            continue
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = value
        except Exception as e:
            print(f"WARNING: could not load .env file {env_path}: {e}")

_load_local_env_file()

API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
MODEL_NAME = os.getenv("OPENAI_MODEL", "gpt-5").strip() or "gpt-5"
OPENAI_CHAT_COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"
GPT_TIMEOUT_SEC = 300

# ======= PROMPT: blur-only for new design =======
GPT_BASE_PROMPT = r"""
You are an expert privacy and testimonial-cleanup assistant for EduTap.
You will receive the visible raw text from a Zoho email testimonial.
Your jobs are:
1) identify exact text fragments that should be blurred in the email screenshot, and
2) extract the most likely student/customer name from the visible email text for metadata and filename use.

Return JSON only.
Do not write testimonial copy.
Do not create highlights.
Do not rewrite the feedback.
Do not summarize the feedback.

Blur these items when present:
- personal email addresses except feedback@edutap.co.in,
- phone numbers or numeric identifiers of 6 or more digits,
- roll numbers, transaction/order IDs, coupon codes, account/bank/payment identifiers,
- greetings/salutations/sign-offs that are not testimonial content,
- personal exam identity declarations such as aspirant/candidate/attempt/year/grade,
- enrollment/subscription/payment/course-purchase details,
- operational requests, scheduling requests, recording/link/login issues, follow-up requests,
- internal material/resource references such as PDFs, notes, Telegram material, booklets, sheets, compilations,
- non-testimonial personal/logistical explanations.

Do not blur:
- dates and timestamps,
- the word feedback,
- ZM initials/avatar text,
- feedback@edutap.co.in,
- genuine feedback praise that should remain visible.

For student_name:
- extract only the most likely student/customer name visible in the email,
- prefer the real person who gave the feedback, not the EduTap mailbox,
- if a forwarded/shared mail shows a forwarder name at top and the actual student is visible in the body or quoted line, choose the actual student,
- return plain name text only, without labels, punctuation decoration, or email address,
- if you are not reasonably confident, return an empty string.

Every phrase must be copied exactly from the input text when possible.
If a phrase is embedded in a longer sentence and exact matching may be difficult, use mode "loose".
"""

GPT_OUTPUT_CONTRACT = r"""
OUTPUT FORMAT - JSON ONLY
Return only this schema. No prose, no Markdown, no code fences.

{
  "version": "blurlist-1.1",
  "student_name": "Most likely student name, or empty string if unknown",
  "phrases": [
    {
      "text": "literal string to blur",
      "mode": "normal",
      "case": "insensitive"
    }
  ]
}

Rules:
- version must be exactly "blurlist-1.1".
- student_name must always be present. Use empty string if unknown.
- phrases must always be present.
- phrases[].text must be a literal substring to blur, not regex.
- phrases[].mode can be "normal", "loose", or "strict".
- phrases[].case can be "insensitive" or "sensitive".
- Deduplicate identical text values.
- If nothing should be blurred, return {"version":"blurlist-1.1","student_name":"","phrases":[]}.
"""


# ----- Prompt editing config -----
EDIT_PROMPT_PASSWORD = "EditPrompt@123"

PROMPT_FILE_PATH = os.path.join(BASE_DIR, "prompt.txt")

# This is the prompt actually used. It starts from GPT_BASE_PROMPT,
# but can be overridden from prompt.txt via /prompt API.
CURRENT_GPT_PROMPT = GPT_BASE_PROMPT


def load_prompt_from_file() -> None:
    """On startup, if prompt.txt exists, load it instead of the hard-coded prompt."""
    global CURRENT_GPT_PROMPT
    try:
        if os.path.exists(PROMPT_FILE_PATH):
            with open(PROMPT_FILE_PATH, "r", encoding="utf-8") as f:
                text = f.read()
            if text.strip():
                CURRENT_GPT_PROMPT = text
    except Exception as e:
        print(f"WARNING: could not load prompt file: {e}")


# Load once when server starts
load_prompt_from_file()

# ======= FALLBACK: Manual phrases (kept as-is) =======
RAW_TEXTS_BLOCK = r"""
"""

# ===================== Helpers (shared) =====================

def normalize_artifacts(s: str) -> str:
    if not isinstance(s, str): return ""
    cleaned = s.replace("\u0000", "").replace("\ufeff", "")
    cleaned = cleaned.replace("\u00A0", " ").replace("\u202F", " ").replace("\u2007", " ")
    cleaned = re.sub(r'(?i)\\u00a0|\\xa0|a0', ' ', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned

def _split_quoted_and_angle(line: str) -> List[str]:
    parts: List[str] = []
    i = 0; n = len(line)
    while i < n:
        if line[i] == '"':
            j = i + 1
            while j < n and line[j] != '"': j += 1
            q = line[i+1:j]
            if q.strip(): parts.append(q.strip())
            i = j + 1 if j < n else n
        elif line[i] == '<':
            j = i + 1
            while j < n and line[j] != '>': j += 1
            ang = line[i:j+1] if j < n else line[i:]
            if ang.strip(): parts.append(ang.strip())
            i = j + 1 if j < n else n
        else:
            j = i
            while j < n and line[j] not in '"<': j += 1
            tok = line[i:j].strip()
            if tok: parts.append(tok)
            i = j
    return parts

def _expand_long_phrase_into_clauses(s: str) -> List[str]:
    """
    For very long phrases, split into comma/semicolon/colon/dash/period-separated
    clauses, merge short pieces back to preserve meaning, and keep only blur-worthy chunks.
    """
    if not isinstance(s, str):
        return []
    s = normalize_artifacts(s)

    # Use as-is for short text
    if len(s) <= 160:
        return [s]

    # Split on major punctuation boundaries
    parts = re.split(r'(?<=[.;,–—\-:])\s+', s)
    cleaned_parts = [normalize_artifacts(p) for p in parts if len(p.strip()) > 0]

    merged: List[str] = []
    buffer = ""
    for p in cleaned_parts:
        buffer = (buffer + " " + p).strip()
        # commit if reasonably sized
        if len(buffer) > 150 or p.endswith("."):
            merged.append(buffer.strip())
            buffer = ""
    if buffer:
        merged.append(buffer.strip())

    # keep only meaningful parts
    filtered = [m for m in merged if 25 <= len(m) <= 400]
    return filtered or [s]

def build_texts_to_blur(raw_block: str) -> List[str]:
    seen=set(); out=[]
    for raw_line in (raw_block or "").splitlines():
        line = raw_line.strip()
        if not line: continue
        parts = _split_quoted_and_angle(line) if ('"' in line or '<' in line) else [line]
        for p in parts:
            if p and p not in seen:
                out.append(p); seen.add(p)
    return out

def _norm_map_build(raw: str):
    if not raw: return "", []
    def _clean_char(ch: str) -> str:
        ch = ch.replace("\u0000", "").replace("\ufeff", "")
        ch = ch.replace("\u00A0"," ").replace("\u202F"," ").replace("\u2007"," ")
        return ch
    out_chars=[]; spans=[]; i=0; n=len(raw)
    while i<n:
        ch=_clean_char(raw[i])
        if not ch: i+=1; continue
        if ch.isspace():
            j=i+1
            while j<n:
                ch2=_clean_char(raw[j])
                if not ch2 or not ch2.isspace(): break
                j+=1
            out_chars.append(' '); spans.append((i,j)); i=j
        else:
            out_chars.append(ch.lower()); spans.append((i,i+1)); i+=1
    return ''.join(out_chars).strip(), spans

def _normalize_phrase(s: str) -> str:
    if not isinstance(s, str): return ""
    s=s.replace("\u0000","").replace("\ufeff","")
    s=s.replace("\u00A0"," ").replace("\u202F"," ").replace("\u2007"," ")
    s=re.sub(r'(?i)\\u00a0|\\xa0|a0',' ',s)
    s=re.sub(r'\s+',' ',s).strip().lower()
    return s

def snap_to_dom_substring(dom_text: str, phrase: str, min_ratio: float = 0.92) -> Optional[str]:
    if not dom_text or not phrase: return None
    norm_dom, dom_spans = _norm_map_build(dom_text)
    norm_phrase = _normalize_phrase(phrase)
    if not norm_dom or not norm_phrase: return None

    pos = norm_dom.find(norm_phrase)
    if pos != -1:
        start_raw = dom_spans[pos][0]; end_raw = dom_spans[pos+len(norm_phrase)-1][1]
        return dom_text[start_raw:end_raw]

    if len(norm_phrase) < 10: return None

    L=len(norm_phrase); best_ratio=0.0; best_start=-1; best_end=-1
    for w in range(max(6,L-3), L+4):
        for start in range(0, max(0, len(norm_dom)-w+1)):
            window = norm_dom[start:start+w]
            r = SequenceMatcher(None, window, norm_phrase).ratio()
            if r>best_ratio: best_ratio, best_start, best_end = r, start, start+w
    if best_ratio < min_ratio or best_start < 0: return None
    start_raw = dom_spans[best_start][0]; end_raw = dom_spans[best_end-1][1]
    return dom_text[start_raw:end_raw]

# ===================== Graphic helpers from image2.py (bold-only, backdrop, trim) =====================

def _hex_to_rgba(color, alpha_default: int = 255):
    if isinstance(color, (tuple, list)):
        if len(color)==3: return (int(color[0]), int(color[1]), int(color[2]), alpha_default)
        if len(color)==4: return tuple(map(int, color))
    if isinstance(color, str) and color.startswith("#"):
        s=color[1:]
        if len(s)==3:
            r,g,b = [int(c*2,16) for c in s]; return (r,g,b,alpha_default)
        if len(s)==6:
            r=int(s[0:2],16); g=int(s[2:4],16); b=int(s[4:6],16); return (r,g,b,alpha_default)
        if len(s)==8:
            r=int(s[0:2],16); g=int(s[2:4],16); b=int(s[4:6],16); a=int(s[6:8],16); return (r,g,b,a)
    return (0,0,0,alpha_default)

def _load_font(path: Optional[str], size: int) -> ImageFont.FreeTypeFont:
    try:
        if path and os.path.exists(path): return ImageFont.truetype(path, size=size)
    except Exception:
        pass
    return ImageFont.load_default()

def _measure(draw: ImageDraw.ImageDraw, font: ImageFont.FreeTypeFont, text: str) -> Tuple[int,int]:
    bbox = draw.textbbox((0,0), text, font=font)
    return bbox[2]-bbox[0], bbox[3]-bbox[1]

# ---------- Emoji-aware text helpers ----------
_EMOJI_RX = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF\u2764\uFE0F]", flags=re.UNICODE)

def strip_emojis_for_graphic_text(value: str) -> str:
    """Remove emojis / unsupported symbols from testimonial text before final graphic render."""
    if not isinstance(value, str):
        return ""

    # Remove emoji characters covered by the existing emoji regex.
    value = _EMOJI_RX.sub("", value)

    # Remove common invisible emoji variation/joiner chars.
    value = value.replace("\uFE0F", "").replace("\u200D", "")

    # Remove object replacement / missing-glyph style remnants if present.
    value = value.replace("□", "")

    # Clean extra spacing before punctuation.
    value = re.sub(r"\s+([,.!?;:])", r"\1", value)
    value = re.sub(r"\s+", " ", value).strip()

    return value

def _split_by_emoji(s: str):
    out = []
    i = 0
    while i < len(s):
        ch = s[i]
        if _EMOJI_RX.match(ch):
            j = i + 1
            # include optional VS16 (U+FE0F) that colors the preceding glyph
            if j < len(s) and s[j] == "\uFE0F":
                out.append((s[i:j+1], True))
                i = j + 1
            else:
                out.append((ch, True))
                i += 1
        else:
            j = i + 1
            while j < len(s) and not _EMOJI_RX.match(s[j]):
                j += 1
            out.append((s[i:j], False))
            i = j
    return out

def _draw_chunked_text(draw, x, y, text, font_text, font_emoji, color_rgb):
    """Draw text at (x,y), using font_emoji only for emoji chunks. Returns width drawn."""
    if not text:
        return 0
    width = 0
    for chunk, is_emoji in _split_by_emoji(text):
        if not chunk:
            continue
        f = font_emoji if (is_emoji and font_emoji is not None) else font_text
        w, _ = _measure(draw, f, chunk)
        draw.text((x + width, y), chunk, font=f, fill=color_rgb)
        width += w
    return width
# ----------------------------------------------

def _tokenize_with_spans(text: str) -> List[Tuple[str,int,int]]:
    out=[]; 
    for m in re.finditer(r"\S+\s*", text): out.append((m.group(0), m.start(), m.end()))
    return out

def _wrap_with_original_spans(draw, text: str, max_w: int, font: ImageFont.FreeTypeFont):
    tokens = _tokenize_with_spans(text)
    lines=[]; spans=[]
    cur_text=""; cur_start=None; cur_end=None
    for tok, s, e in tokens:
        cand = cur_text + tok
        w_px,_ = _measure(draw, font, cand)
        if cur_text and w_px>max_w:
            lines.append(cur_text); spans.append((cur_start, cur_end))
            cur_text = tok; cur_start = s; cur_end = e
        else:
            if not cur_text: cur_start = s
            cur_text = cand; cur_end = e
    if cur_text: lines.append(cur_text); spans.append((cur_start, cur_end))
    return lines, spans

def _find_highlight_spans_in_original(text: str, phrases: List[str]) -> List[Tuple[int,int]]:
    spans=[]
    for p in phrases or []:
        if not p: continue
        start=0
        while True:
            idx=text.find(p, start)
            if idx==-1: break
            spans.append((idx, idx+len(p)))
            start=idx+1
    return spans

def _merge_intervals(intervals: List[Tuple[int,int]]) -> List[Tuple[int,int]]:
    if not intervals: return []
    intervals=sorted(intervals); merged=[intervals[0]]
    for s,e in intervals[1:]:
        ps,pe=merged[-1]
        if s<=pe: merged[-1]=(ps, max(pe,e))
        else: merged.append((s,e))
    return merged

def _draw_quote_centered_with_highlights(
    base: Image.Image,
    draw: ImageDraw.ImageDraw,
    text: str,
    highlights: List[Dict[str,str]],
    top_y: int,
    side_pad: int,
    font_regular: ImageFont.FreeTypeFont,
    font_bold: ImageFont.FreeTypeFont,
    font_emoji: ImageFont.FreeTypeFont | None = None,  # <— NEW PARAM
) -> Tuple[int,int]:
    W,_H = base.size
    max_w = max(10, W - 2*side_pad)
    content = text or ""
    trimmed = content.strip()
    if ADD_QUOTES and trimmed and not (trimmed.startswith(OPEN_Q) and trimmed.endswith(CLOSE_Q)):
        content = f"{OPEN_Q}{content}{CLOSE_Q}"

    lines, spans = _wrap_with_original_spans(draw, content, max_w, font_regular)
    _, line_h = _measure(draw, font_regular, "Ay")
    line_step = int(line_h * LINE_HEIGHT_MULT)

    hl_texts = [(h or {}).get("text","") for h in (highlights or []) if isinstance(h, dict)]
    hl_texts = [s for s in hl_texts if s]
    hl_spans = _find_highlight_spans_in_original(content, hl_texts)

    y = top_y
    normal_color = _hex_to_rgba(QUOTE_TEXT_COLOR)[:3]
    bold_color   = _hex_to_rgba(BOLD_TEXT_COLOR)[:3]

    for (ln, (ls, le)) in zip(lines, spans):
        lw,_ = _measure(draw, font_regular, ln)
        start_x = (W - lw)//2

        overlaps=[]
        for hs,he in hl_spans:
            a=max(ls,hs); b=min(le,he)
            if a<b: overlaps.append((a-ls, b-ls))
        overlaps=_merge_intervals(overlaps)

        runs=[]; cursor=0
        for a,b in overlaps:
            if cursor<a: runs.append((cursor,a,False))
            runs.append((a,b,True)); cursor=b
        if cursor < len(ln): runs.append((cursor, len(ln), False))

        dx=0
        for a,b,is_bold in runs:
            seg=ln[a:b]
            f_text = font_bold if is_bold else font_regular
            color  = bold_color if is_bold else normal_color
            dx += _draw_chunked_text(draw, start_x+dx, y, seg, f_text, font_emoji, color)

        y += line_step

    last_line_bottom = y - (line_step - line_h)
    return last_line_bottom, last_line_bottom - top_y

def _trim_alpha(img: Image.Image) -> Image.Image:
    if img.mode != "RGBA": img = img.convert("RGBA")
    bbox = img.split()[-1].getbbox()
    return img.crop(bbox) if bbox else img

def _paste_footer_scaled(base: Image.Image, footer_path: str, bottom_margin: int,
                         width_ratio: float, max_height: Optional[int], white_pad: int,
                         trim_alpha: bool) -> int:
    if not os.path.exists(footer_path): return 0
    W,H = base.size
    foot = Image.open(footer_path).convert("RGBA")
    if trim_alpha: foot = _trim_alpha(foot)
    fw,fh = foot.size

    if 0 < width_ratio < 1.0:
        new_w = int(W*width_ratio); scale = new_w / fw; new_h = int(fh*scale)
        if max_height is not None and new_h > max_height:
            scale = max_height / fh; new_w = int(fw*scale); new_h = int(fh*scale)
        foot = foot.resize((new_w,new_h), Image.LANCZOS); fw,fh = foot.size; x=(W-fw)//2
    else:
        scale = min(1.0, W/fw); new_w=int(fw*scale); new_h=int(fh*scale)
        if (new_w,new_h)!=(fw,fh):
            foot=foot.resize((new_w,new_h), Image.LANCZOS); fw,fh = foot.size
        x=(W-fw)//2

    if white_pad>0:
        y_top = H - fh - bottom_margin - white_pad
        white = Image.new("RGBA", (W, fh+white_pad), (255,255,255,255))
        base.alpha_composite(white, (0, y_top))

    base.alpha_composite(foot, (x, H - fh - bottom_margin))
    return fh + white_pad


def _edge_background_rgb(img: Image.Image) -> tuple[int, int, int]:
    """
    Estimate the screenshot background color from its edges/corners.
    Zoho shared email screenshots are usually white/light grey, but using
    the actual edge color makes cropping safer if the theme changes.
    """
    rgba = img.convert("RGBA")
    w, h = rgba.size
    px = rgba.load()
    coords = [
        (0, 0),
        (max(0, w - 1), 0),
        (0, max(0, h - 1)),
        (max(0, w - 1), max(0, h - 1)),
        (w // 2, 0),
        (w // 2, max(0, h - 1)),
        (0, h // 2),
        (max(0, w - 1), h // 2),
    ]
    samples = []
    for x, y in coords:
        r, g, b, a = px[x, y]
        if a >= 10:
            samples.append((r, g, b))
    if not samples:
        return (255, 255, 255)
    return tuple(int(sum(c[i] for c in samples) / len(samples)) for i in range(3))


def _crop_email_whitespace(img: Image.Image) -> Image.Image:
    """
    Remove unnecessary blank outer space from the Zoho email screenshot.
    This does not change the final proof-panel size; it only makes the
    useful email content occupy more of that fixed area.
    """
    rgba = img.convert("RGBA")
    if not EMAIL_SCREENSHOT_CROP_ENABLE:
        return rgba

    w, h = rgba.size
    if w < 20 or h < 20:
        return rgba

    bg = _edge_background_rgb(rgba)
    bg_img = Image.new("RGBA", rgba.size, (*bg, 255))
    diff = ImageChops.difference(rgba, bg_img).convert("L")
    mask = diff.point(lambda p: 255 if p > EMAIL_SCREENSHOT_CROP_THRESHOLD else 0)
    bbox = mask.getbbox()
    if not bbox:
        return rgba

    left, top, right, bottom = bbox
    crop_w = right - left
    crop_h = bottom - top

    # Safety: avoid over-cropping to a tiny accidental pixel area.
    if crop_w < max(80, int(w * 0.12)) or crop_h < max(60, int(h * 0.12)):
        return rgba

    pad = max(0, int(EMAIL_SCREENSHOT_CROP_PADDING))
    left = max(0, left - pad)
    top = max(0, top - pad)
    right = min(w, right + pad)
    bottom = min(h, bottom + pad)
    return rgba.crop((left, top, right, bottom))


def optimize_email_screenshot_bytes(screenshot_bytes: bytes) -> bytes:
    """
    Improves proof screenshot clarity before placement in the fixed panel:
    1. crop whitespace,
    2. apply a light sharpen,
    3. keep authentic screenshot pixels, no AI rewriting/enhancement.
    """
    try:
        img = Image.open(BytesIO(screenshot_bytes)).convert("RGBA")
        img = _crop_email_whitespace(img)
        if EMAIL_SCREENSHOT_LIGHT_SHARPEN:
            img = img.filter(
                ImageFilter.UnsharpMask(
                    radius=EMAIL_SCREENSHOT_SHARPEN_RADIUS,
                    percent=EMAIL_SCREENSHOT_SHARPEN_PERCENT,
                    threshold=EMAIL_SCREENSHOT_SHARPEN_THRESHOLD,
                )
            )
        out = BytesIO()
        img.save(out, format="PNG", optimize=False)
        return out.getvalue()
    except Exception as e:
        print(f"WARNING: email screenshot optimization skipped: {e}")
        return screenshot_bytes


def _paste_email_to_fit_with_backdrop(
    base: Image.Image,
    email_image,  # PIL.Image.Image, bytes/bytearray, or path string
    y_top: int,
    y_bottom: int,
    side_pad_inside: int,
    backdrop_enable: bool,
    backdrop_color: str,
    backdrop_side_pad: int,
    backdrop_top_pad: int,
    backdrop_bottom_pad: int,
    allow_upscale: bool,
    sharpen_amount: int,
) -> Tuple[int, int]:
    """
    Draws a fixed-height teal panel between y_top and y_bottom.
    Inside that panel, it creates an inner box and scales the email screenshot
    UP or DOWN (depending on allow_upscale) so the whole image fits without cropping.

    Returns (email_img_top, email_img_bottom) in canvas coordinates.
    """

    if y_bottom <= y_top:
        return y_top, y_top

    W, H = base.size

    # Outer teal backdrop (usually full width)
    back_left  = 0 + max(0, int(backdrop_side_pad))
    back_right = W - max(0, int(backdrop_side_pad))
    back_top   = y_top
    back_bottom= y_bottom

    # Inner content box (padding inside the teal)
    inner_left   = back_left  + max(0, int(side_pad_inside))
    inner_right  = back_right - max(0, int(side_pad_inside))
    inner_top    = back_top   + max(0, int(backdrop_top_pad))
    inner_bottom = back_bottom- max(0, int(backdrop_bottom_pad))

    box_w = max(10, inner_right - inner_left)
    box_h = max(10, inner_bottom - inner_top)

    # Fill teal panel
    if backdrop_enable:
        rgba = _hex_to_rgba(backdrop_color)
        ImageDraw.Draw(base).rectangle([back_left, back_top, back_right, back_bottom], fill=rgba)

    # Load email image
    if isinstance(email_image, Image.Image):
        img = email_image.convert("RGBA")
    elif isinstance(email_image, (bytes, bytearray)):
        img = Image.open(BytesIO(email_image)).convert("RGBA")
    else:
        img = Image.open(str(email_image)).convert("RGBA")

    w0, h0 = img.size

    # SCALE: "contain" – no crop, maintain aspect ratio.
    scale = min(box_w / float(w0), box_h / float(h0))
    if not allow_upscale:
        # If upscaling is disabled, clamp to 1.0
        scale = min(1.0, scale)

    new_w = max(1, int(w0 * scale))
    new_h = max(1, int(h0 * scale))
    if (new_w, new_h) != (w0, h0):
        img = img.resize((new_w, new_h), Image.LANCZOS)

    if sharpen_amount and sharpen_amount > 0:
        img = img.filter(ImageFilter.UnsharpMask(radius=1.2, percent=sharpen_amount, threshold=3))

    # Center inside the inner box
    paste_x = inner_left + (box_w - new_w) // 2
    paste_y = inner_top  + (box_h - new_h) // 2
    base.paste(img, (paste_x, paste_y), img)

    email_img_top = paste_y
    email_img_bottom = paste_y + new_h
    return email_img_top, email_img_bottom

async def _render_quote_html_png(
    context,                          # Playwright BrowserContext
    width_px: int,
    text: str,
    highlights: list,
    font_stack: str,
    font_size_px: int,
    line_height: float,
    normal_hex: str,
    bold_hex: str,
    add_quotes: bool,
    open_q: str,
    close_q: str,
) -> Image.Image:
    """
    Build a minimal HTML snippet that uses a fixed width and lets Chromium do line-wrapping,
    font shaping, and TRUE-COLOR EMOJIS. Returns a PIL Image with transparent background.
    """
    # prepare text with optional quotes
    t = (text or "").strip()
    if add_quotes and t and not (t.startswith(open_q) and t.endswith(close_q)):
        t = f"{open_q}{t}{close_q}"

    # inject <span class="b">...</span> for highlights (verbatim substrings)
    # We do a simple, safe replace by splitting on each match in order.
    def html_escape(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;")
                 .replace(">", "&gt;").replace('"', "&quot;"))

    safe = html_escape(t)
    # apply highlights (short, verbatim). We wrap first occurrence of each.
    for h in (highlights or []):
        s = (h or {}).get("text", "")
        if not s:
            continue
        s_html = html_escape(s)
        # only replace first occurrence to avoid over-highlighting
        safe = safe.replace(s_html, f"<span class='b'>{s_html}</span>", 1)

    html = f"""
    <!doctype html>
    <meta charset="utf-8">
    <style>
      html,body{{margin:0;padding:0;background:transparent}}
      .wrap{{
        width:{width_px}px;
        font:{font_size_px}px {font_stack};
        line-height:{line_height};
        color:{normal_hex};
        white-space:pre-wrap;
        word-break:break-word;
        text-align:center;  /* ✅ Centers text */
      }}
      .b{{font-weight:700;color:{bold_hex};}}
    </style>
    <div id="box" class="wrap">{safe}</div>
    """.strip()

    page = await context.new_page()
    try:
        await page.set_viewport_size({"width": width_px, "height": 10})
        await page.set_content(html, wait_until="load")
        box = page.locator("#box")
        await box.wait_for(state="visible")
        # Let it auto-size, then read its box
        bb = await box.bounding_box()
        h = int(bb["height"]) if bb and bb.get("height") else 10
        # Set a taller viewport to capture full content
        await page.set_viewport_size({"width": width_px, "height": h})
        png_bytes = await box.screenshot(omit_background=True)  # transparent PNG
    finally:
        await page.close()

    from io import BytesIO
    return Image.open(BytesIO(png_bytes)).convert("RGBA")

def _make_rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    w, h = size
    mask = Image.new("L", (max(1, w), max(1, h)), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([(0, 0), (w - 1, h - 1)], radius=max(0, radius), fill=255)
    return mask


def _build_email_card_layer(email: Image.Image) -> Image.Image:
    """Apply rounded clipping and a 1px inside stroke to the email proof."""
    email = email.convert("RGBA")
    w, h = email.size
    radius = max(0, min(EMAIL_PROOF_CORNER_RADIUS, min(w, h) // 2))
    rounded_mask = _make_rounded_mask((w, h), radius)

    clipped = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    clipped.paste(email, (0, 0), rounded_mask)

    if EMAIL_PROOF_STROKE_ENABLE and EMAIL_PROOF_STROKE_WIDTH > 0:
        overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        inset = max(0, EMAIL_PROOF_STROKE_WIDTH // 2)
        od.rounded_rectangle(
            [(inset, inset), (w - 1 - inset, h - 1 - inset)],
            radius=max(0, radius - inset),
            outline=EMAIL_PROOF_STROKE_RGBA,
            width=EMAIL_PROOF_STROKE_WIDTH,
        )
        clipped = Image.alpha_composite(clipped, overlay)

    return clipped


def _build_email_shadow(size: tuple[int, int], radius: int) -> Image.Image:
    """Build a Photoshop-like teal drop shadow from the rounded card shape."""
    w, h = size
    shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    shadow_fill = EMAIL_PROOF_SHADOW_COLOR_RGBA
    sd.rounded_rectangle([(0, 0), (w - 1, h - 1)], radius=max(0, radius), fill=shadow_fill)
    if EMAIL_PROOF_SHADOW_BLUR > 0:
        shadow = shadow.filter(ImageFilter.GaussianBlur(EMAIL_PROOF_SHADOW_BLUR))
    return shadow


def _shadow_offset_from_angle(distance: float, angle_deg: float) -> tuple[int, int]:
    import math
    rad = math.radians(angle_deg)
    dx = int(round(math.cos(rad) * distance))
    dy = int(round(math.sin(rad) * distance))
    return dx, dy


# --- replace your current def compose_testimonial_graphic(...) with this async version ---
# --- replace your current def compose_testimonial_graphic(...) with this async version ---
async def compose_testimonial_graphic(
    raw_template_path: str,
    footer_path: str,
    email_screenshot_image,
    out_path: str,
    testimonial_text: str,
    highlight_items: List[Dict[str, str]],
    student_name: str,
) -> str:
    """
    New template composer.

    The template already contains the complete design, logo, title, hashtag,
    footer, background, etc. We only paste the blurred/tight Zoho email proof
    screenshot at the fixed center point requested by EduTap:

        x = 540.00 px
        y = 592.50 px

    No testimonial text, highlights, student name, stars, footer, or teal panel
    are drawn by code anymore.
    """
    if not os.path.exists(raw_template_path):
        raise FileNotFoundError(raw_template_path)

    base = Image.open(raw_template_path).convert("RGBA")

    # Final export must be exactly 1080 x 1080.
    if base.size != (FINAL_GRAPHIC_WIDTH, FINAL_GRAPHIC_HEIGHT):
        base = base.resize((FINAL_GRAPHIC_WIDTH, FINAL_GRAPHIC_HEIGHT), Image.LANCZOS)

    if isinstance(email_screenshot_image, Image.Image):
        email = email_screenshot_image.convert("RGBA")
    elif isinstance(email_screenshot_image, (bytes, bytearray)):
        email = Image.open(BytesIO(email_screenshot_image)).convert("RGBA")
    else:
        email = Image.open(str(email_screenshot_image)).convert("RGBA")

    ew, eh = email.size
    if ew <= 0 or eh <= 0:
        raise RuntimeError("Email screenshot is empty.")

    # WIDTH-PRIORITY scaling:
    # - consumes the full target width whenever possible,
    # - preserves aspect ratio completely,
    # - never stretches the email screenshot,
    # - uses height only as a safety fallback if width-priority is disabled.
    if EMAIL_PASTE_WIDTH_PRIORITY:
        scale = EMAIL_PASTE_TARGET_WIDTH / float(ew)
        if not EMAIL_PASTE_ALLOW_UPSCALE:
            scale = min(scale, 1.0)
    else:
        scale = min(
            EMAIL_PASTE_MAX_WIDTH / float(ew),
            EMAIL_PASTE_MAX_HEIGHT / float(eh),
        )
        if not EMAIL_PASTE_ALLOW_UPSCALE:
            scale = min(scale, 1.0)

    new_w = max(1, int(round(ew * scale)))
    new_h = max(1, int(round(eh * scale)))

    # Absolute safety: never exceed canvas width. This does not stretch;
    # it only scales down if an unusual screenshot becomes wider than allowed.
    if new_w > EMAIL_PASTE_MAX_WIDTH:
        safe_scale = EMAIL_PASTE_MAX_WIDTH / float(new_w)
        new_w = max(1, int(round(new_w * safe_scale)))
        new_h = max(1, int(round(new_h * safe_scale)))

    if (new_w, new_h) != (ew, eh):
        email = email.resize((new_w, new_h), Image.LANCZOS)

    # Apply rounded card clipping + thin cyan inside stroke.
    email = _build_email_card_layer(email)
    radius = max(0, min(EMAIL_PROOF_CORNER_RADIUS, min(email.width, email.height) // 2))

    paste_x = int(round(EMAIL_PASTE_CENTER_X - email.width / 2.0))
    paste_y = int(round(EMAIL_PASTE_CENTER_Y - email.height / 2.0))

    # Photoshop-like teal drop shadow.
    if EMAIL_PROOF_SHADOW_ENABLE:
        dx, dy = _shadow_offset_from_angle(EMAIL_PROOF_SHADOW_DISTANCE, EMAIL_PROOF_SHADOW_ANGLE_DEG)
        shadow = _build_email_shadow(email.size, radius)
        base.alpha_composite(shadow, (paste_x + dx, paste_y + dy))

    base.alpha_composite(email, (paste_x, paste_y))

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    base.save(out_path, format="PNG")
    return out_path

# ===================== JS blur helper (unchanged from your main) =====================
JS_BLUR_HELPER = r"""
({ selector, phrases, blurPx }) => {
  const NBSP_CLASS = "[\\u00A0\\u202F\\u2007]";
  const SPACE0_RX  = `(?:\\s|${NBSP_CLASS})*`;
  const APOS_RX    = `(?:['\\u2019\\u2018\\u02BC])`;
  const MAX_MATCHES_PER_PHRASE = 50000;

  function escRegex(s){ return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }

  function normalizeCandidate(p){
    if (p == null) return "";
    let s = String(p).normalize("NFKC");
    s = s.replace(/\u0000/gu,"");
    s = s.replace(/\u00A0|\u202F|\u2007/gu," ");
    s = s.replace(/\u00a0|\xa0/giu," ");
    s = s.replace(/\s+/g," ").trim();
    return s;
  }

  function isSkippableCandidate(p){
    if (!p) return {skip:true, reason:"empty"};
    const c = p.replace(/\s+/g,"");
    if (!c.length) return {skip:true, reason:"whitespace-only"};
    if (c.length < 2) return {skip:true, reason:"too-short"};
    return {skip:false, reason:null};
  }

  function buildTolerantRegex(p){
    let s = normalizeCandidate(p);
    s = escRegex(s);
    // tolerate smart quotes & optional spaces around '
    s = s.replace(/'/g, `${SPACE0_RX}${APOS_RX}${SPACE0_RX}`);
    // collapse any literal whitespace to optional whitespace
    s = s.replace(/\\s+/g, SPACE0_RX);
    // tolerate hyphen variants
    s = s.replace(/-/g, "[\\-–—]");
    try {
      const rx = new RegExp(s, "gi");
      // guard against zero-length matchers
      const probe = "".match(rx);
      if (probe && probe[0] !== undefined && probe[0].length === 0) return null;
      return rx;
    } catch (e) {
      return null;
    }
  }

  const root = document.querySelector(selector);
  if (!root) return {summary:`No element found for ${selector}`,hits:[],mergedRanges:[],skipped:[]};

  // collect text nodes
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null, false);
  const nodes = [];
  while (walker.nextNode()){
    const n = walker.currentNode;
    if (n.nodeType === Node.TEXT_NODE && n.textContent && n.textContent.length) nodes.push(n);
  }
  if (!nodes.length) return {summary:"Blurred 0 text fragment(s)",hits:[],mergedRanges:[],skipped:[]};

  const texts = nodes.map(n=>n.textContent);
  const cumLen=[0];
  for (let i=0;i<texts.length;i++) cumLen.push(cumLen[cumLen.length-1]+texts[i].length);
  const allText = texts.join("");

  function findAllRanges(phrases){
    const ranges=[]; const hits=[]; const skipped=[];
    (phrases || []).forEach((raw, idx) => {
      const cand = normalizeCandidate(raw);
      const sk = isSkippableCandidate(cand);
      if (sk.skip){ skipped.push({phrase:raw,reason:sk.reason}); return; }

      const rx = buildTolerantRegex(cand);
      if (!rx){ skipped.push({phrase:raw,reason:"invalid-regex"}); return; }

      let m, guard=0;
      while ((m = rx.exec(allText)) !== null){
        const seg = (m[0] ?? "");
        if (seg.length === 0){
          rx.lastIndex++;
          if (rx.lastIndex > allText.length) break;
          continue;
        }
        const start = m.index, end = m.index + seg.length;
        ranges.push({start,end,phraseIndex:idx});
        hits.push({phraseIndex:idx,phrase:raw,start,end,text:allText.slice(start,end)});

        guard++;
        if (guard >= MAX_MATCHES_PER_PHRASE){
          skipped.push({phrase:raw,reason:"match-cap-reached"});
          break;
        }
        if (rx.lastIndex === m.index) rx.lastIndex = m.index + seg.length;
      }
    });

    ranges.sort((a,b)=> a.start - b.start || a.end - b.end);
    const merged=[];
    for (const r of ranges){
      if (!merged.length || r.start > merged[merged.length-1].end){
        merged.push({start:r.start,end:r.end});
      } else {
        merged[merged.length-1].end = Math.max(merged[merged.length-1].end, r.end);
      }
    }
    return {merged,hits,skipped};
  }

  function locate(g){
    let lo=0, hi=cumLen.length-1;
    while (lo<=hi){
      const mid=(lo+hi)>>1;
      if (cumLen[mid] <= g){
        if (mid===cumLen.length-1 || cumLen[mid+1] > g) return {nodeIndex:mid, offset:g-cumLen[mid]};
        lo = mid+1;
      } else {
        hi = mid-1;
      }
    }
    return {nodeIndex:nodes.length-1, offset:nodes[nodes.length-1].textContent.length};
  }

  function blurRange(start,end){
    let {nodeIndex:i, offset:off} = locate(start);
    let remaining = end - start;
    while (remaining>0 && i<nodes.length){
      const node = nodes[i];
      const text = node.textContent;
      const take = Math.min(remaining, text.length - off);

      const before = text.slice(0,off);
      const target = text.slice(off, off+take);
      const after  = text.slice(off+take);

      const frag = document.createDocumentFragment();
      if (before) frag.appendChild(document.createTextNode(before));
      const span = document.createElement("span");
      span.style.filter = `blur(${blurPx}px)`;
      span.textContent = target;
      frag.appendChild(span);
      if (after) frag.appendChild(document.createTextNode(after));

      const parent = node.parentNode;
      parent.replaceChild(frag, node);

      // rebuild structures
      const walker2 = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null, false);
      nodes.length = 0;
      while (walker2.nextNode()){
        const n2 = walker2.currentNode;
        if (n2.nodeType===Node.TEXT_NODE && n2.textContent && n2.textContent.length) nodes.push(n2);
      }
      const texts2 = nodes.map(n=>n.textContent);
      cumLen.length = 0; cumLen.push(0);
      for (let k=0;k<texts2.length;k++) cumLen.push(cumLen[cumLen.length-1]+texts2[k].length);

      const newGlobal = start + take;
      ({nodeIndex:i, offset:off} = locate(newGlobal));
      remaining = end - newGlobal;
      start = newGlobal;
    }
  }

  // ---------- 1) phrase-based ranges ----------
  const {merged, hits:allHits, skipped:allSkipped} = findAllRanges(phrases||[]);

  // ---------- 2) auto-detect phone & email in plain text ----------
  function findPhoneEmailRanges(txt){
    const ranges=[];
    // emails
    const emailRx = /[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi;
    let m;
    while ((m=emailRx.exec(txt)) !== null){
      const seg = m[0] ?? "";
      if (seg.length) ranges.push({start:m.index, end:m.index+seg.length});
      if (emailRx.lastIndex === m.index) emailRx.lastIndex = m.index + seg.length;
    }
    // phones: >=6 digits allowing separators
    const phoneRx = /\+?\d[\d\s().-]{5,}/g;
    while ((m=phoneRx.exec(txt)) !== null){
      const seg = m[0] ?? "";
      const digits = (seg.match(/\d/g) || []).length;
      if (digits >= 6) ranges.push({start:m.index, end:m.index+seg.length});
      if (phoneRx.lastIndex === m.index) phoneRx.lastIndex = m.index + seg.length;
    }
    ranges.sort((a,b)=> a.start - b.start || a.end - b.end);
    const mergedPE=[];
    for (const r of ranges){
      if (!mergedPE.length || r.start > mergedPE[mergedPE.length-1].end){
        mergedPE.push({start:r.start, end:r.end});
      } else {
        mergedPE[mergedPE.length-1].end = Math.max(mergedPE[mergedPE.length-1].end, r.end);
      }
    }
    return mergedPE;
  }
  const autoPE = findPhoneEmailRanges(allText);

  // merge phrase-based + auto phone/email
  const allMerged = merged.concat(autoPE).sort((a,b)=> a.start - b.start || a.end - b.end);
  const finalMerged=[];
  for (const r of allMerged){
    if (!finalMerged.length || r.start > finalMerged[finalMerged.length-1].end){
      finalMerged.push({start:r.start, end:r.end});
    } else {
      finalMerged[finalMerged.length-1].end = Math.max(finalMerged[finalMerged.length-1].end, r.end);
    }
  }

  // blur from end
  for (let r=finalMerged.length-1; r>=0; r--) blurRange(finalMerged[r].start, finalMerged[r].end);

  // ---------- 3) blur explicit mailto:/tel: anchors ----------
  const anchors = root.querySelectorAll('a[href^="mailto:"], a[href^="tel:"]');
  anchors.forEach(a => {
    a.style.filter = `blur(${blurPx}px)`;
    if (a.firstChild && a.firstChild.nodeType === Node.TEXT_NODE) {
      a.firstChild.textContent = a.firstChild.textContent; // force split
    }
  });

  // ---------- 4) fallback: blur blocks that contain phone-like digits or emails (covers signature images) ----------
  (function blurDigitAndEmailBlocks(){
    const DIGIT_BLOCK_RX = /\d[\d\s().-]{5,}/;  // >=6 digits with separators
    const EMAIL_RX = /[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/i;

    function isInline(el){
      const cs = getComputedStyle(el);
      return cs.display === "inline" || cs.display === "inline-block";
    }
    function blurNearestBlock(el){
      let n = el;
      while (n && n !== root){
        if (!isInline(n)){ n.style.filter = `blur(${blurPx}px)`; break; }
        n = n.parentElement;
      }
    }

    // scan text nodes; if digits/email present, blur nearest block
    const walker3 = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null, false);
    while (walker3.nextNode()){
      const tn = walker3.currentNode;
      const t  = (tn && tn.textContent) ? tn.textContent : "";
      if (DIGIT_BLOCK_RX.test(t) || EMAIL_RX.test(t)) {
        blurNearestBlock(tn.parentElement || root);
      }
    }

    // blur any images inside such blocks as well (handles rasterized signatures)
    const allBlocks = root.querySelectorAll("*");
    allBlocks.forEach(el => {
      const txt = (el.textContent || "").replace(/\s+/g," ");
      if (DIGIT_BLOCK_RX.test(txt) || EMAIL_RX.test(txt)) {
        el.querySelectorAll("img").forEach(img => { img.style.filter = `blur(${blurPx}px)`; });
      }
    });
  })();

  return {
    summary: `Blurred ${finalMerged.length} merged range(s) (incl. auto phone/email), from ${allHits.length} raw hit(s).`,
    mergedRanges: finalMerged.map(m => ({start:m.start, end:m.end, text: allText.slice(m.start,m.end)})),
    hits: allHits,
    skipped: allSkipped,
    phrasesUsed: phrases || [],
    blurPx
  };
}
"""



# ===================== USER-FRIENDLY ERROR HANDLING =====================

def friendly_error_message(err: Exception | str) -> str:
    """
    Convert technical errors into simple English messages for operators.
    Keep technical details in logs/API only for developer debugging.
    """
    text = str(err or "").strip()
    low = text.lower()

    if "daily limit" in low:
        return "Daily limit reached. Please try again tomorrow or contact admin."

    if "insufficient_quota" in low or "quota" in low or "billing" in low or "credits" in low:
        return "OpenAI credits are finished. Please recharge the OpenAI account and try again."

    if "invalid_api_key" in low or "incorrect api key" in low or "401" in low or "openai_api_key" in low or "api key" in low:
        return "OpenAI API key is missing or incorrect. Please check the .env file and try again."

    if "openai" in low or "gpt" in low or "chat/completions" in low:
        return "OpenAI could not process this feedback right now. Please try again after some time."

    if "template not found" in low or "no such file" in low and "templates" in low:
        return "The required template is missing. Please upload the correct template first."

    if "could not find the email body" in low or "timed out" in low or "timeout" in low or "net::" in low or "navigation" in low:
        return "The Zoho email page could not be opened or detected. Please check the link and try again."

    if "missing service account" in low or "credentials" in low or "token" in low or "oauth" in low:
        return "Google Drive or Sheet login is not configured correctly. Please check the credentials folder."

    if "drive" in low or "spreadsheet" in low or "sheets" in low or "google" in low:
        return "Google Drive or Sheet saving failed. Please check internet connection and Google access."

    if "local file not found" in low or "image file not found" in low:
        return "The generated image file was not found. Please generate the graphic again."

    if "unsupported testimonial type" in low or "invalid feedback type" in low:
        return "Invalid feedback type selected. Please select the correct feedback type and try again."

    if "template must be" in low:
        return "Template must be an image file: JPG, JPEG, PNG, or WEBP."

    if "template name cannot be empty" in low:
        return "Please enter the person name for this template."

    return "Something went wrong. Please try again. If it happens again, contact the technical team."


def error_payload(err: Exception | str, code: str = "APP_ERROR") -> dict:
    technical = str(err or "")
    return {
        "ok": False,
        "code": code,
        "message": friendly_error_message(technical),
        "technical_message": technical,
    }


def raise_friendly_http(err: Exception | str, status_code: int = 500, code: str = "APP_ERROR"):
    raise HTTPException(status_code=status_code, detail=error_payload(err, code=code))

# ===================== GPT call (unchanged behavior) =====================

def call_gpt(div_text: str):
    if not API_KEY or not API_KEY.startswith("sk-"):
        raise RuntimeError("OpenAI API key is missing or incorrect. Please set OPENAI_API_KEY in .env.")

    user_content = (
        CURRENT_GPT_PROMPT.strip()
        + "\n\n---\n\nCONTEXT (DIV TEXT):\n"
        + (div_text or "")
        + "\n\n---\n\n"
        + GPT_OUTPUT_CONTRACT
    )

    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": "You are a precise JSON-only generator. Always return valid JSON."},
            {"role": "user", "content": user_content},
        ],
        "response_format": {"type": "json_object"},
    }

    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

    try:
        resp = requests.post(
            OPENAI_CHAT_COMPLETIONS_URL,
            headers=headers,
            data=json.dumps(payload, ensure_ascii=False),
            timeout=GPT_TIMEOUT_SEC,
        )
    except Exception as e:
        raise RuntimeError(f"OpenAI request failed: {e}")

    if resp.status_code != 200:
        raise RuntimeError(f"OpenAI API error {resp.status_code}: {resp.text[:800]}")

    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        payload_obj = json.loads(content)
    except Exception as e:
        raise RuntimeError(f"OpenAI returned an unreadable response: {e}")

    print("\n=== GPT RAW JSON ===")
    print(json.dumps(payload_obj, ensure_ascii=False, indent=2))
    print("====================\n")

    phrases_in = payload_obj.get("phrases", []) or []
    raw_name = payload_obj.get("student_name")

    # Clean phrases
    phrases: List[str] = []
    seen = set()
    for item in phrases_in:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            cleaned = normalize_artifacts(item["text"])
            if cleaned and cleaned not in seen:
                phrases.append(cleaned)
                seen.add(cleaned)

    # New design no longer uses testimonial text/highlights on the image,
    # but keep the return shape stable for the rest of the pipeline.
    t_text = ""
    t_high_clean: List[Dict[str, str]] = []

    student_name = normalize_artifacts(raw_name.strip()) if isinstance(raw_name, str) and raw_name.strip() else "Student Name"
    return phrases, {"text": t_text, "highlights": t_high_clean}, student_name

# ===================== MAIN =====================

async def find_email_element(page):
    """
    Finds the Zoho email body without depending on the changing class suffix.

    Priority:
    1. Stable Zoho shared mail containers.
    2. Classes starting with zmails__.
    3. Largest visible div containing actual email text.
    """

    async def is_good_locator(locator):
        try:
            await locator.wait_for(state="visible", timeout=3000)
            box = await locator.bounding_box()
            text = await locator.inner_text(timeout=3000)

            if not box or not text:
                return False

            text_clean = " ".join(text.split())

            if box["width"] < 300:
                return False

            if box["height"] < 100:
                return False

            if len(text_clean) < 40:
                return False

            return True
        except Exception:
            return False

    # 1. Try known stable/prefix selectors
    for selector in ELEMENT_SELECTOR_CANDIDATES:
        try:
            locators = page.locator(selector)
            count = await locators.count()

            for i in range(min(count, 10)):
                loc = locators.nth(i)
                if await is_good_locator(loc):
                    return loc
        except Exception:
            continue

    # 2. Fallback: choose largest meaningful div
    best = None
    best_score = 0

    divs = page.locator("div")
    count = await divs.count()

    for i in range(min(count, 500)):
        try:
            loc = divs.nth(i)
            box = await loc.bounding_box()
            text = await loc.inner_text(timeout=1000)

            if not box or not text:
                continue

            text_clean = " ".join(text.split())

            if box["width"] < 300 or box["height"] < 100:
                continue

            if len(text_clean) < 40:
                continue

            score = len(text_clean) + int(box["height"])

            # Prefer actual mail body text
            if "feedback" in text_clean.lower():
                score += 500

            if "wrote:" in text_clean.lower():
                score += 200

            if score > best_score:
                best = loc
                best_score = score

        except Exception:
            continue

    if best:
        return best

    raise RuntimeError(
        "Could not find the email body on this Zoho page. Please check whether the link is accessible."
    )


def _norm_person_for_compare(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _looks_like_person_header_line(line: str) -> bool:
    line = (line or "").strip()
    if not line or len(line) > 45:
        return False
    low = line.lower()
    blocked = [
        "feedback", "subject", "forwarded", "message", "from:", "to:", "cc:", "bcc:",
        "reply", "date", "sent", "mail", "zoho", "edutap", "hello", "dear", "sir", "mam",
        "http", "www", "@", "+0530", "am", "pm"
    ]
    if any(b in low for b in blocked):
        return False
    if re.search(r"\d", line):
        return False
    # Name-like: 1-4 alphabetic words, allows apostrophes/dots/spaces.
    words = re.findall(r"[A-Za-z][A-Za-z.'-]*", line)
    if not (1 <= len(words) <= 4):
        return False
    return sum(len(w) for w in words) >= 3


def get_header_names_to_blur(div_text: str, student_name: str) -> List[str]:
    """Blur visible email/forwarder names in Zoho header/proof area.

    GPT is asked not to blur the actual student name. In forwarded emails, Zoho often
    shows the person who forwarded the message at the top of the email card. That name
    is proof metadata, not the testimonial student name, so blur it deterministically.
    """
    student_norm = _norm_person_for_compare(student_name)
    out: List[str] = []
    seen = set()

    lines = [ln.strip() for ln in (div_text or "").splitlines() if ln.strip()]
    # Look mainly at the first visible header block. Stop after feedback/subject-ish markers.
    header_lines = []
    for ln in lines[:18]:
        header_lines.append(ln)
        if ln.lower() in {"feedback", "subject"}:
            break

    for ln in header_lines:
        if not _looks_like_person_header_line(ln):
            continue
        if student_norm and _norm_person_for_compare(ln) == student_norm:
            continue
        key = ln.lower()
        if key not in seen:
            seen.add(key)
            out.append(ln)

    return out


async def run_pipeline(url: str, raw_template_path: str, filename_suffix: str = "") -> dict:
    """
    Core pipeline:
    - open the given URL
    - blur PII using GPT rules
    - take screenshot of the email div
    - compose final testimonial graphic
    - return paths + metadata as a dict (no prints for UI use)
    """
    print("Launching Chromium browser...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": EMAIL_CAPTURE_VIEWPORT_WIDTH, "height": EMAIL_CAPTURE_VIEWPORT_HEIGHT},
            device_scale_factor=EMAIL_SCREENSHOT_DEVICE_SCALE,
        )

        # Make this context available to the composer
        global pw_context
        pw_context = context

        page = await context.new_page()

        try:
            print(f"Navigating to URL: {url}")
            await page.goto(url, wait_until="networkidle", timeout=45_000)

            # Make headless Chromium render emojis correctly in the email proof screenshot.
            # Without a color emoji font on Linux/Streamlit Cloud, emojis may appear as square boxes.
            try:
                await page.add_style_tag(content="""
                    * {
                        font-family:
                            Arial,
                            "Segoe UI Emoji",
                            "Noto Color Emoji",
                            "Apple Color Emoji",
                            "Twemoji Mozilla",
                            sans-serif !important;
                    }
                """)
            except Exception as e:
                print(f"WARNING: could not inject emoji font fallback CSS: {e}")

            elem = await find_email_element(page)
            div_text = await elem.inner_text(timeout=5000)

            if not div_text.strip():
                print("WARNING: target element has no innerText.")

            # 2) Fallback blur phrases
            fallback_phrases = build_texts_to_blur(RAW_TEXTS_BLOCK)

            # 3) GPT (with simple cache per link)
            final_phrases = fallback_phrases
            testimonial = {"text": "", "highlights": []}
            student_name = "Student Name"

            cache_key = url.strip()

            if USE_GPT:
                global gpt_cache
                if cache_key in gpt_cache:
                    print("\n=== Reusing cached GPT result for this link ===")
                    cached = gpt_cache[cache_key]
                    final_phrases = cached.get("phrases") or fallback_phrases
                    testimonial = cached.get("testimonial") or testimonial
                    student_name = cached.get("student_name") or student_name
                else:
                    print("\n=== Calling GPT for blur-only JSON ===")
                    gpt_phrases, gpt_testimonial, gpt_student = call_gpt(div_text)
                    final_phrases = gpt_phrases
                    testimonial = gpt_testimonial or testimonial
                    student_name = gpt_student or student_name

                    gpt_cache[cache_key] = {
                        "phrases": final_phrases,
                        "testimonial": testimonial,
                        "student_name": student_name,
                    }
            else:
                print("\n=== USE_GPT is False – using fallback blur phrases only ===")

            # 3.4) Deterministic blur for visible Zoho header / forwarder names.
            # This handles forwarded email cases where the top email-card name belongs
            # to the person who forwarded the mail, while the student name is extracted
            # correctly from the actual feedback body.
            header_names_to_blur = get_header_names_to_blur(div_text, student_name)
            if header_names_to_blur:
                print("\n=== HEADER / FORWARDER NAMES TO BLUR ===")
                for nm in header_names_to_blur:
                    print(f"- {nm}")
                final_phrases = list(final_phrases or []) + header_names_to_blur

            # 3.5) Expand long phrases into clauses, then snap each to DOM substrings
            expanded: list[str] = []
            for ptxt in final_phrases:
                expanded.extend(_expand_long_phrase_into_clauses(ptxt))
            final_phrases = expanded

            snapped: list[str] = []
            for ptxt in final_phrases:
                dom_exact = snap_to_dom_substring(div_text, ptxt, min_ratio=0.75)
                snapped.append(dom_exact if dom_exact else ptxt)
            final_phrases = snapped

            print("\n=== FINAL PHRASES TO BLUR ===")
            for ptxt in final_phrases:
                preview = (ptxt[:140] + "…") if len(ptxt) > 140 else ptxt
                print(f"- {preview}")

            # 4) Inject blur
            print("\n=== Injecting blur ===")
            element_handle = await elem.element_handle()

            result = await page.evaluate(
                """
                async ({ element, phrases, blurPx, helperCode }) => {
                    window.__edutapTargetEmailElement = element;

                    const wrappedHelper = eval(helperCode);
                    return wrappedHelper({
                        selector: "__DIRECT_ELEMENT__",
                        phrases,
                        blurPx
                    });
                }
                """,
                {
                    "element": element_handle,
                    "phrases": final_phrases,
                    "blurPx": 5,
                    "helperCode": JS_BLUR_HELPER.replace(
                        "const root = document.querySelector(selector);",
                        "const root = selector === '__DIRECT_ELEMENT__' ? window.__edutapTargetEmailElement : document.querySelector(selector);"
                    )
                }
            )
            print(result.get("summary", "No summary"))
            skipped = result.get("skipped", [])
            if skipped:
                print("Skipped (no match / capped):")
                for s in skipped[:20]:
                    reason = s.get("reason")
                    phrase = s.get("phrase") or ""
                    preview = (phrase[:120] + "…") if len(phrase) > 120 else phrase
                    print(f"  - {reason}: {preview}")

            # 5) Screenshot the blurred element IN MEMORY (no file on disk)
            raw_email_bytes = await elem.screenshot()  # returns bytes
            email_bytes = optimize_email_screenshot_bytes(raw_email_bytes)

            # 6) Build final filename with student name + optional variant (mentor name, etc.)
            def _slugify(n: str) -> str:
                s = re.sub(r"[^A-Za-z0-9]+", "_", (n or "").strip())
                s = s.strip("_")
                return s or "Student_Name"

            student_slug = _slugify(student_name)
            suffix_slug = _slugify(filename_suffix) if filename_suffix else ""

            if suffix_slug:
                name_part = f"{student_slug}_{suffix_slug}"
            else:
                name_part = student_slug

            # Use the same folder as google_clients.py
            SAVE_DIR = LOCAL_OUTPUT_DIR
            os.makedirs(SAVE_DIR, exist_ok=True)
            final_filename = os.path.join(SAVE_DIR, f"{name_part}.png")

            # 7) Compose final graphic using in-memory screenshot
            print("Composing testimonial graphic...")
            final_path = await compose_testimonial_graphic(
                raw_template_path,
                FOOTER_IMAGE_PATH,
                email_bytes,
                final_filename,
                testimonial.get("text", ""),
                testimonial.get("highlights", []) or [],
                student_name,
            )

            print(f"Saved testimonial image: {final_path}")

            # return everything needed by the UI
            return {
                "image_path": final_path,
                "student_name": student_name,
                "testimonial_text": testimonial.get("text", ""),
                "highlights": testimonial.get("highlights", []),
            }

        except PlaywrightTimeoutError:
            msg = "Timed out while loading or locating the element."
            print("ERROR:", msg)
            raise RuntimeError(msg)
        except Exception as e:
            print(f"ERROR: {e}")
            raise
        finally:
            await browser.close()

class PromptUpdate(BaseModel):
    password: str
    prompt: str

class GenerateRequest(BaseModel):
    link: str
    type: str
    data: Dict[str, Any] | None = None

class SaveRequest(BaseModel):
    type: str
    link: str
    # support both old "filename" and new "filenames"
    filenames: Optional[List[str]] = None
    filename: Optional[str] = None
    data: Dict[str, Any] | None = None

class OperatorUpdate(BaseModel):
    name: str

# ===================== DAILY LIMIT CONFIG =====================
DAILY_LIMIT = 500  # max submissions per day
_daily_state_lock = threading.Lock()
_daily_state = {
    "date": None,
    "count": 0,
}


def _reset_if_new_day():
    """Reset counter when date changes."""
    today = date.today().isoformat()
    if _daily_state["date"] != today:
        _daily_state["date"] = today
        _daily_state["count"] = 0


def check_and_increment_daily(n: int = 1):
    """
    Check if we can consume `n` submissions today.
    Raises HTTPException(429) if limit is exceeded.
    """
    from fastapi import HTTPException  # local import to avoid cycles

    with _daily_state_lock:
        _reset_if_new_day()
        if _daily_state["count"] + n > DAILY_LIMIT:
            remaining = max(0, DAILY_LIMIT - _daily_state["count"])
            raise HTTPException(
                status_code=429,
                detail=f"Daily limit reached. Remaining today: {remaining}"
            )
        _daily_state["count"] += n


def get_daily_status():
    """Return current usage status for the API."""
    with _daily_state_lock:
        _reset_if_new_day()
        return {
            "date": _daily_state["date"],
            "used": _daily_state["count"],
            "limit": DAILY_LIMIT,
            "remaining": max(0, DAILY_LIMIT - _daily_state["count"]),
        }
# ==============================================================

app = FastAPI(title="EduTap Testimonial API")

# Allow UI (running from file or another port) to call this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)



# ================== PEOPLE LISTS API (faculty/mentor + support) ==================

DELETE_PASSWORD = "Udado@123"  # same password your UI prompts for delete


class UpdateListRequest(BaseModel):
    kind: Literal["faculty", "support"]   # which list
    action: Literal["add", "delete"]      # add or delete
    name: str                             # full display name
    password: Optional[str] = None        # required only for delete


@app.get("/lists")
def get_people_lists():
    """
    Returns the current Faculty (mentor) and Support lists from JSON.
    Response shape:
    {
      "faculty": [...],
      "support": [...]
    }
    """
    return load_people_lists()

@app.get("/operator")
def get_operator():
    """
    Returns current operator name used for Google Sheet 'Entered by'.
    """
    return {"name": get_app_operator_name()}

@app.post("/operator")
def set_operator(payload: OperatorUpdate):
    """
    Updates operator name (Entered by). Stored locally for this machine.
    """
    saved = set_app_operator_name(payload.name)
    return {"ok": True, "name": saved}


@app.post("/lists")
def update_people_list(payload: UpdateListRequest):
    """
    Add or delete an entry from Faculty/Support list.
    - action: "add" or "delete"
    - kind: "faculty" or "support"
    - name: text to store (must match template filenames)
    - password: required only for delete; must match DELETE_PASSWORD
    """
    lists = load_people_lists()
    kind = payload.kind
    action = payload.action
    name = (payload.name or "").strip()

    if not name:
        raise HTTPException(status_code=400, detail="Name cannot be empty.")

    if kind not in lists:
        raise HTTPException(status_code=400, detail="Invalid list type.")

    current = lists[kind]

    if action == "delete":
        if payload.password != DELETE_PASSWORD:
            raise HTTPException(status_code=403, detail="Invalid password for delete.")
        if name in current:
            current.remove(name)
        else:
            raise HTTPException(status_code=404, detail="Name not found in list.")
    elif action == "add":
        if name not in current:
            current.append(name)
    else:
        raise HTTPException(status_code=400, detail="Invalid action.")

    save_people_lists(lists)
    return lists

@app.get("/prompt")
def get_prompt():
    """
    Return the current GPT prompt text (the one used in call_gpt).
    No password required just to view; UI will do its own password gate.
    """
    return {"prompt": CURRENT_GPT_PROMPT}


@app.post("/prompt")
def update_prompt(payload: PromptUpdate):
    """
    Update the GPT prompt text.
    Requires password = EDIT_PROMPT_PASSWORD.
    Writes to prompt.txt and updates CURRENT_GPT_PROMPT.
    """
    if payload.password != EDIT_PROMPT_PASSWORD:
        raise HTTPException(status_code=403, detail="Invalid password for prompt edit.")

    new_text = (payload.prompt or "").rstrip()
    if not new_text:
        raise HTTPException(status_code=400, detail="Prompt cannot be empty.")

    global CURRENT_GPT_PROMPT
    CURRENT_GPT_PROMPT = new_text

    try:
        with open(PROMPT_FILE_PATH, "w", encoding="utf-8") as f:
            f.write(CURRENT_GPT_PROMPT)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save prompt file: {e}")

    return {"ok": True}

@app.get("/daily_limit")
def daily_limit_status():
    """
    Return today's usage: used, remaining, limit, date.
    """
    return get_daily_status()

@app.post("/generate")
async def generate(payload: GenerateRequest):
    """
    API endpoint used by the HTML UI.

    Expects JSON:
      {
        "link": "https://...",
        "type": "edutap" | "event" | "mentor" | "support" | "course",
        "data": {...}  # optional extra fields (faculty, mentors, support mode, etc.)
      }
    """
    # 1) Daily limit – 1 call = 1 submission
    try:
        check_and_increment_daily(1)
    except HTTPException as e:
        raise_friendly_http(e.detail, status_code=e.status_code, code="DAILY_LIMIT")

    # 2) Basic validation
    link = (payload.link or "").strip()
    if not link:
        raise_friendly_http("link is required", status_code=400, code="VALIDATION_ERROR")

    # 3) Decide which base template(s) to use for this request
    try:
        template_variants = get_template_variants(payload.type, payload.data or {})
    except FileNotFoundError as e:
        raise_friendly_http(e, status_code=500, code="TEMPLATE_MISSING")
    except Exception as e:
        raise_friendly_http(e, status_code=500, code="TEMPLATE_ERROR")

    all_results: list[dict[str, Any]] = []

    # 4) Run the full pipeline once per template
    for template_path, label in template_variants:
        try:
            pipeline_result = await run_pipeline(link, template_path, filename_suffix=label)
        except Exception as e:
            raise_friendly_http(e, status_code=500, code="GENERATION_FAILED")

        image_path = pipeline_result.get("image_path")
        if not image_path or not os.path.exists(image_path):
            raise_friendly_http(
                f"image was not created for template: {template_path}",
                status_code=500,
                code="IMAGE_NOT_CREATED",
            )

        with open(image_path, "rb") as f:
            img_bytes = f.read()
        img_b64 = base64.b64encode(img_bytes).decode("ascii")

        filename = os.path.basename(image_path)

        # ---- Build per-image data so Google Sheet gets correct fields ----
        per_image_data = dict(payload.data or {})
        type_lower = (payload.type or "").strip().lower()

        # Student name is extracted by GPT in run_pipeline().
        # Store it in original_request.data so /save can send it to Google Sheet.
        per_image_data["student_name"] = pipeline_result.get("student_name") or ""

        # For mentor feedback with multiple mentors, "label" is the mentor name.
        # Store it explicitly as "person" so append_testimonial_rows uses it.
        if type_lower == "mentor" and label:
            per_image_data["person"] = label

        all_results.append(
            {
                "filename": filename,
                "image_base64": img_b64,
                "original_request": {
                    "link": link,
                    "type": payload.type,
                    "data": per_image_data,
                },
            }
        )

    # 5) Return list of all generated images
    return all_results

@app.post("/templates/upload")
async def upload_template(
    feedback_type: str = Form(...),
    template_scope: str = Form(...),  # generic | person
    name: Optional[str] = Form(None),
    file: UploadFile = File(...),
):
    """
    Uploads template into the existing folder structure:

    EduTap:
      assets/Templates/EduTap Feedback/EduTap Feedback.jpg

    Course:
      assets/Templates/Course Feedback/Course Feedback.jpg

    Event generic:
      assets/Templates/Event Feedback/Event Feedback.jpg

    Event person:
      assets/Templates/Event Feedback/<Faculty>.jpg

    Mentor person:
      assets/Templates/Mentor feedback/<Mentor>.jpg

    Support generic:
      assets/Templates/Support Feedback/Support Feedback.jpg

    Support person:
      assets/Templates/Support Feedback/<Member>.jpg
    """

    t = (feedback_type or "").strip().lower()
    scope = (template_scope or "").strip().lower()

    if not file:
        raise_friendly_http("Please upload a template image.", status_code=400, code="VALIDATION_ERROR")

    if not file.filename.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
        raise_friendly_http(
            "Template must be JPG, PNG, JPEG, or WEBP.",
            status_code=400,
            code="VALIDATION_ERROR"
        )

    try:
        if t == "edutap":
            dest = os.path.join(TEMPLATES_ROOT, "EduTap Feedback", "EduTap Feedback.jpg")

        elif t == "course":
            dest = os.path.join(TEMPLATES_ROOT, "Course Feedback", "Course Feedback.jpg")

        elif t == "event":
            if scope == "generic":
                dest = os.path.join(TEMPLATES_ROOT, "Event Feedback", "Event Feedback.jpg")
            else:
                safe_name = _safe_template_name(name)
                dest = os.path.join(TEMPLATES_ROOT, "Event Feedback", f"{safe_name}.jpg")

                lists = load_people_lists()
                if safe_name not in lists.get("faculty", []):
                    lists.setdefault("faculty", []).append(safe_name)
                    save_people_lists(lists)

        elif t == "mentor":
            safe_name = _safe_template_name(name)
            dest = os.path.join(TEMPLATES_ROOT, "Mentor feedback", f"{safe_name}.jpg")

            lists = load_people_lists()
            if safe_name not in lists.get("faculty", []):
                lists.setdefault("faculty", []).append(safe_name)
                save_people_lists(lists)

        elif t == "support":
            if scope == "generic":
                dest = os.path.join(TEMPLATES_ROOT, "Support Feedback", "Support Feedback.jpg")
            else:
                safe_name = _safe_template_name(name)
                dest = os.path.join(TEMPLATES_ROOT, "Support Feedback", f"{safe_name}.jpg")

                lists = load_people_lists()
                if safe_name not in lists.get("support", []):
                    lists.setdefault("support", []).append(safe_name)
                    save_people_lists(lists)

        else:
            raise_friendly_http("Invalid feedback type.", status_code=400, code="VALIDATION_ERROR")

        _save_uploaded_template_as_jpg(file, dest)

        return {
            "ok": True,
            "message": "Template uploaded successfully.",
            "path": dest,
            "people_lists": load_people_lists(),
        }

    except HTTPException:
        raise
    except Exception as e:
        raise_friendly_http(e, status_code=500, code="TEMPLATE_UPLOAD_FAILED")

@app.post("/save")
async def save_to_google(payload: SaveRequest):
    """
    Called ONLY when user clicks Download / Download All in the UI.

    Accepts either:
      { "type": "...", "link": "...", "filename": "Abhay_Kumar.png", "data": {...} }
    or:
      { "type": "...", "link": "...", "filenames": ["Abhay_Kumar.png", ...], "data": {...} }

    For each filename:
      - upload to Google Drive + append to Google Sheet
      - IF that succeeds, delete the local file from LOCAL_OUTPUT_DIR
    """
    t = (payload.type or "").strip().lower()

    # Normalise to a list of filenames
    if payload.filenames and len(payload.filenames) > 0:
        filenames = payload.filenames
    elif payload.filename:
        filenames = [payload.filename]
    else:
        raise_friendly_http("filename(s) is required", status_code=400, code="VALIDATION_ERROR")

    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for fname in filenames:
        local_path = os.path.join(LOCAL_OUTPUT_DIR, fname)

        if not os.path.exists(local_path):
            technical_error = f"Local file not found: {local_path}"
            errors.append(
                {"filename": fname, "message": friendly_error_message(technical_error), "error": technical_error}
            )
            continue

        # Build data dict that google_clients expects
        data = dict(payload.data or {})
        if "email_link" not in data:
            data["email_link"] = payload.link

        try:
            print(f"[SAVE] Uploading {local_path} for type={t} ...")
            info = await asyncio.to_thread(
                save_testimonial_and_image,
                t,
                data,
                local_path,
            )
            print(f"[SAVE] Uploaded OK, Drive link: {info.get('drive_link')}")

            # Delete local file only after successful upload
            try:
                os.remove(local_path)
                print(f"[SAVE] Deleted local file {local_path}")
            except FileNotFoundError:
                print(f"[SAVE] Local file already gone: {local_path}")
            except Exception as del_err:
                print(f"[SAVE] WARNING: could not delete {local_path}: {del_err}")

            results.append({"filename": fname, "info": info})

        except Exception as e:
            print(f"[SAVE] ERROR for {fname}: {e}")
            errors.append({"filename": fname, "message": friendly_error_message(e), "error": str(e)})

    return {
        "saved": results,
        "errors": errors,
    }

# ---------- Optional: keep CLI usage for debugging ----------

if __name__ == "__main__":
    # Hard-coded test link – not used when running via uvicorn
    test_link = "https://zopen.to/UF3bZkOoIxCxJ3xLvasz"
    # Simple manual test using default EduTap template
    asyncio.run(run_pipeline(test_link, DEFAULT_TEMPLATE_PATH, filename_suffix="manual"))
