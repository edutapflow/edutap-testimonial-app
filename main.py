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
    ".zmsharelink-content div[class^='zmails__']",
    ".zmsharelink-content div[class*=' zmails__']",
    "div[class^='zmails__']",
    "div[class*=' zmails__']",
    ".zmsharelink-content",
    ".zmsharelink-content-wrapper",
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

# Default if something goes wrong – EduTap overall
DEFAULT_TEMPLATE_PATH = os.path.join(
    TEMPLATES_ROOT, "EduTap Feedback", "EduTap Feedback.jpg"
)

def get_template_variants(feedback_type: str, extra: Dict[str, Any] | None) -> List[Tuple[str, str]]:
    """
    Decide which base template(s) to use based on feedback type + extra data.

    Returns a list of (template_path, variant_label).

    - Most cases → 1 item.
    - Mentor feedback with multiple mentors → 1 per mentor.
    """
    extra = extra or {}
    t = (feedback_type or "").strip().lower()

    variants: List[Tuple[str, str]] = []

    # 1) EduTap Feedback (overall)
    if t == "edutap":
        path = os.path.join(TEMPLATES_ROOT, "EduTap Feedback", "EduTap Feedback.jpg")
        variants.append((path, "edutap"))

    # 2) Event Feedback
    elif t == "event":
        mode = (extra.get("mode") or "one").strip().lower()

        if mode == "one":
            # 1 Faculty – use "<Faculty Name>.jpg" from "Event Feedback" folder
            faculty = (extra.get("faculty") or "").strip()
            if faculty:
                file_name = f"{faculty}.jpg"
                path = os.path.join(TEMPLATES_ROOT, "Event Feedback", file_name)
                variants.append((path, faculty))
            else:
                # Fallback if faculty not provided – generic template
                path = os.path.join(TEMPLATES_ROOT, "Event Feedback", "Event Feedback.jpg")
                variants.append((path, "event_one_fallback"))

        else:
            # Multiple Faculties – always generic "Event Feedback.jpg" in same folder
            path = os.path.join(TEMPLATES_ROOT, "Event Feedback", "Event Feedback.jpg")
            variants.append((path, "event_multi"))

    # 3) Mentor Feedback (1 or multiple)
    elif t == "mentor":
        mentors = extra.get("mentors") or []
        if not isinstance(mentors, list):
            mentors = [str(mentors)]

        if not mentors:
            # fallback generic mentor template if you ever add one
            path = os.path.join(TEMPLATES_ROOT, "Mentor feedback", "Mentor Feedback.jpg")
            variants.append((path, "mentor"))
        else:
            # For each selected mentor, look for "<Mentor Name>.jpg"
            for m in mentors:
                name = str(m).strip()
                if not name:
                    continue
                file_name = f"{name}.jpg"
                path = os.path.join(TEMPLATES_ROOT, "Mentor feedback", file_name)
                variants.append((path, name))

    # 4) Support Feedback
    elif t == "support":
        mode = (extra.get("mode") or "one").strip().lower()
        if mode == "one":
            member = str(extra.get("member") or "").strip()
            if member:
                # File with exact same text as dropdown, e.g. "Aditya.jpg"
                file_name = f"{member}.jpg"
                path = os.path.join(TEMPLATES_ROOT, "Support Feedback", file_name)
                variants.append((path, member))
            else:
                # Fallback to generic
                path = os.path.join(
                    TEMPLATES_ROOT, "Support Feedback", "Support Feedback.jpg"
                )
                variants.append((path, "support_one_fallback"))
        else:
            # Team template
            path = os.path.join(
                TEMPLATES_ROOT, "Support Feedback", "Support Feedback.jpg"
            )
            variants.append((path, "support_team"))

    # 5) Course Feedback
    elif t == "course":
        path = os.path.join(
            TEMPLATES_ROOT,
            "Course Feedback",
            "Course Feedback.jpg",
        )
        variants.append((path, "course"))

    # 6) Fallback (if unknown type)
    else:
        path = DEFAULT_TEMPLATE_PATH
        variants.append((path, "default"))

    # Final safety: check all files exist
    checked: List[Tuple[str, str]] = []
    for p, label in variants:
        if not os.path.exists(p):
            raise FileNotFoundError(f"Template not found: {p}")
        checked.append((p, label))

    return checked

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

# ---- Email proof clarity settings ----
# Keep the final proof panel size fixed. These settings improve the source
# screenshot before it is placed inside the existing panel.
EMAIL_SCREENSHOT_DEVICE_SCALE = 3
EMAIL_SCREENSHOT_CROP_ENABLE = True
EMAIL_SCREENSHOT_CROP_PADDING = 28
EMAIL_SCREENSHOT_CROP_THRESHOLD = 18
EMAIL_SCREENSHOT_LIGHT_SHARPEN = True
EMAIL_SCREENSHOT_SHARPEN_RADIUS = 1.0
EMAIL_SCREENSHOT_SHARPEN_PERCENT = 90
EMAIL_SCREENSHOT_SHARPEN_THRESHOLD = 2

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

# ======= PROMPT (kept from your mainwithllm.py) =======
GPT_BASE_PROMPT = r"""You are an expert feedback analyst for EduTap, an EdTech organization providing online courses for government examinations in India. You will receive the complete raw text of a student’s feedback - which may include greetings, personal details, and operational content. Your job is to analyze it with absolute precision, following EduTap’s internal editorial logic, and output results strictly in JSON format as specified below.

🔒 RULES FOR "texts to blur"

These are non-testimonial, personal, or operational parts of the message that must be removed or hidden. Include in "texts to blur" only those text fragments that exactly match the original text (case, punctuation, and spacing).

Blur the following:

1️⃣ Greetings, Signatures, or Sign-offs
Blur any opening or closing salutation that does not carry testimonial value.
Examples: "Hello Team,", "Hi,", "Dear EduTap Team,", "Dear Sir,", "Dear Ma’am,", "Respected Sir,", "Hi Sir,", "Hello Ma’am,", "Warm regards,", "Thanks & Regards,", "Best,", "Regards,".

2️⃣ Specific things to blur:

Email addresses (any string containing “@”), even if they appear inside angle brackets < > or quotation marks " ".
✅ Examples to blur:
"Sreyanti Kabasi"rohitsharma556@gmail.com
,
prashantprabhav9693@gmail.com
,
user.name@yahoo.com

Phone numbers - any numeric sequence of 6 or more digits.
Roll numbers, bank details, account credentials, or any similar identifier.
Mentions in headers: If the PII appears in lines beginning with From:, To:, Cc:, Bcc:, or Reply-To:, blur the entire quoted portion containing it (for example "John Doe"johndoe@gmail.com
).
Detection is case-insensitive, but record each blurred fragment exactly as it appears (matching case, punctuation, and spacing).

Enrollment identifiers & commercial details: Blur order/transaction IDs, batch IDs/names, subscription plan names, validity dates, coupon codes, and amounts paid when they relate to enrollment/subscription.

Exam identity declarations (must blur):
Blur any self-identification that states or implies the student’s exam, level/grade, year/attempt, or candidacy. Treat these as personal identifiers. If part of a longer sentence, blur the entire clause (use "mode": "loose").
Examples (blur):

“I’m Pooja Jamkhedkar, NABARD Grade A 2023 aspirant.”

“RBI Grade B 2024 candidate.”

“SEBI Grade A 2025 (first attempt).”

“Targeting/appearing for UPSC 2024.”

“NABARD Mains 2023 Batch B.”

“Class of 2024 / Attempt 2025.”
Notes: Apply regardless of capitalization or order (e.g., “Aspirant for NABARD Grade A 2023”). Prefer "mode": "loose" to capture variations and smart quotes/spaces.

Do not blur mentions of exams when they are generic subject references inside testimonial praise (e.g., “Economy section for NABARD is clearer now”) and do not identify the student’s status.

❌ Do not blur: feedback@edutap.co.in
 and student name from anywhere because we have to show that and not blur "ZM" and dates like "Mon, 01 Sep 2025 2:48:27 AM +0530" and also do not blur word "feedback".

3️⃣ Operational or Procedural Content

Blur any content that refers to internal operations, instructions, or logistics such as:
"Please share class recording.", "I will update later.", "Facing login issue.", "Please share Telegram ID.", "Share contact handle.", "Mentorship schedule will be shared.", "Class link not received."

Also blur any narrative or paragraph that mainly explains personal circumstances or technical/logistical issues rather than giving feedback on learning quality.

This includes:

Mentions of network issues, concentration problems, environment, connectivity, or missing classes.

Also blur any meta or status-update statements such as:

“I can’t write more right now / due to paucity of time.”

“My feedback will count only after the results.”

“I’ll share more later.”
These are operational or time-based disclaimers, not feedback, and must be hidden.

Explanations of why the student could not attend, understand, or complete lessons.

Apologies, justifications, or self-analysis about learning gaps (e.g., “I was distracted due to network problem,” “Sometimes I miss class due to timing,” etc.).

References to teacher’s personal life or family (e.g., “You talk about your parents,” “You share personal experiences”) when not relevant to course content.

✅ If several sentences describe such personal or logistical context, combine them into one "text" entry with "mode": "loose" in the JSON output.

f a sentence mixes self-status or scheduling justification with mild appreciation (e.g., “I can’t write more due to time but I feel better now”), blur the whole line as non-testimonial operational content.

Also blur any sentences describing missed classes, time management issues, or plans to watch recordings later.
Enrollment / Subscription Mentions (must blur):
Blur any sentence or clause that states or implies the student’s enrollment/registration/subscription/admission in an EduTap course or batch. Treat this as personal/operational information.
Examples (blur):

“I am enrolled in your GA crash course.”

“I joined/purchased/subscribed to the Master Course last week.”

“My batch is RBI Mains 2024 (Batch B).”

“I registered yesterday for the Economy module.”

“My order/transaction ID is … / I paid ₹… for the course.”
Scope: If the enrollment status is part of a longer sentence, blur only the enrollment clause; if the whole sentence is primarily about enrollment or payment, blur the entire sentence using "mode": "loose".

And if student says anything like  i am enrolled in this course or i have taken this course or any class must be blurred also.

Internal study material and resource mentions (must blur):
Blur any line or phrase referring to EduTap’s internal study material, documents, PDFs, booklets, notes, or content repositories — whether mentioned by name or indirectly (e.g., “RBI Newstap”, “CA PDF”, “lecture notes”, “practice sheet”).
These references describe internal resources or access patterns and are not testimonial in nature.
Examples (blur):

“I had gone through the PDF provided in RBI Newstap before the session.”

“I revised from your CA Booklet #3.”

“The notes shared on Telegram were very detailed.”

“Please share the monthly compilation.”
Use "mode": "loose" if the phrase is embedded in a longer testimonial.

4️⃣ Redundant Courtesy Lines
Blur repetitive or filler phrases that do not add testimonial value.
Examples: "Thankyou!", "Thank you once again!", "Thanks a lot!"

5️⃣ Personalized Greetings and Faculty Salutations
Blur any greeting or salutation line that directly addresses a faculty or staff member at the start of the message or serves purely as an address.
Do not blur factual or contextual mentions of a faculty member within a testimonial sentence.
Examples to blur: "Hi Sir,", "Dear Kuldeep Sir,", "Respected Ma’am,", "Hello Mam,"
(Reason: Personal salutation - not part of testimonial.)
Also blur cultural or informal greetings such as “Namaste Sir,” “Pranam Ma’am,” “Good evening Sir,” or similar expressions of respect or salutation directed personally to faculty. These count as greetings, not feedback.

6️⃣ Requests, Follow-ups, and Procedural Lines
Blur all sentences involving requests, scheduling, permissions, or follow-ups.
Examples: "Therefore, I request a 1-on-1 session for 10 mins.", "Kindly revert as soon as possible.", "Please let me know if this can be arranged.", "I would be highly obliged if you revert to this as soon as possible.", "Hence, I request for the same."
(Reason: operational or process-related, not feedback.)
Include feedback disclaimers or deferments, e.g., “I’ll share detailed feedback later,” “I’ll update after exam results,” or “Feedback will count later.” These are process-related, not testimonial.

7️⃣ Course or Deliverable Mentions
Blur any mention of EduTap’s internal course structure, deliverables, or commitments.
Examples: "as per the deliverables of the master course", "as communicated during orientation", "as per the mentorship plan"
(Reason: administrative content, not testimonial.)

Do NOT blur the following:
Dates and timestamps (e.g., "Thu, 28 Aug 2025 11:43:26 AM +0530")
The word "feedback"
Student names in testimonial sentences (only blur when used as signature)

Case Sensitivity Rule:
Maintain exact case, punctuation, and spacing in every blurred snippet. When identifying identifiers (emails, phone numbers), case-insensitivity is allowed for detection, but record them exactly as they appear in text.

Interpretive note:
When praise/testimonial text is combined with an enrollment/subscription disclosure, keep the praise visible and blur only the enrollment/subscription disclosure (use "mode": "loose" if separation is not clean).

For Testimonial text : This is the text which is left after blurring means this is the text that is only real testimonial text that will be displayes as visual text on graphic, so do no write any html or any irrelvant in this.

🌟 RULES FOR "Texts to be highlight"
Include only short, high-impact testimonial phrases that reflect genuine learning outcomes, appreciation, or emotional transformation.
These highlights are meant to showcase the essence of positive feedback, not every positive statement.

1️⃣ Selection Criteria
Do not select 90% of testimonial text to be highlighted , highlight text means those text from testimonial text that are very very very good , not all remember this strictly that Do not select 90% of testimonial text to be highlighted

4️⃣ Format and Case Sensitivity
Each highlighted text must:

Match the exact case, punctuation, and spacing from the original testimonial.

Be quoted exactly - no paraphrasing, summarizing, or rewording.

Be short and impactful (ideally one line).

5️⃣ Fallback Rule
If there are no clear praise or learning-oriented lines, return an empty list for "Texts to be highlight".

How to give all things:
1. blur text is defined
2. testimonial text will be give that is left from testimonial text only , means that we can write in graphics, do no include any html tag or any other text from your input that is not a part of testimonial text , means do not write other email things in it like ZM\nHimanshu Arora\nSun, 31 Aug 2025 2:18:27 PM -0700\nfeedback\n\n\n because this is real visual text that has to be written on visual graphic.
3. Highlighted text: Same logic applies to this as well as of testimonial text , but this text must from testimonial text only.

🧩 OUTPUT FORMAT

Return only a single JSON object (no prose, no Markdown, no code fences).
UTF-8 JSON, no comments, no trailing commas.
All items in phrases[].text are literal substrings to blur (not regex).
If nothing should be blurred, return an empty list.

Exact schema
{
  "version": "blurlist-1.0",
  "phrases": [
    {
      "text": "literal string to blur",
      "mode": "normal",
      "case": "insensitive"
    }
  ],
  "testimonial": {
    "text": "Single consolidated testimonial text suitable for display on a graphic.",
    "highlights": [
      { "text": "literal substring from testimonial.text to visually emphasize" }
    ]
  },
  "student_name": "Student Name or extracted literal name from the feedback but First Character of name must be capital"
}


Field rules

version (required): exactly "blurlist-1.0".

phrases (required): array of objects; deduplicate identical text values.

phrases[].text (required): literal substring to blur. May contain any characters (quotes, <, >, backslashes, braces, brackets, slashes, punctuation, Unicode, emojis, and newlines). GPT must escape characters per JSON.

phrases[].mode (optional): "loose" | "normal" | "strict" (guides tolerance; default "normal").

phrases[].case (optional): "insensitive" (default) or "sensitive" (use "sensitive" for tokens like IDs/codes where case matters).

testimonial (required): object describing what to print on a graphic.

testimonial.text (required): one clean paragraph (max ~400 chars) that captures the praise/impact.
Do not include PII, greetings/closings (“Dear…”, “Thank you”), operational lines (“please arrange classes”), links, emails, or phone numbers.
Important: output literal characters only - no HTML entities (e.g., &amp;, &lt;) and no Unicode escapes (e.g., \u2019). Use the actual characters.

testimonial.highlights (required): 0–4 items. Each item:

text (required): a verbatim substring of testimonial.text to bold/highlight.
Length ≤ 80 chars. Deduplicate identical highlights.
Important: literal characters only (no HTML entities, no \uXXXX escapes).

No other properties are allowed at the top level or inside phrases[] / testimonial.

Do not return regex patterns. Do not wrap the JSON in code fences.

Empty case

Blur only empty:

{"version":"blurlist-1.0","phrases":[]}


Full empty (no blur and no testimonial):

{"version":"blurlist-1.0","phrases":[],"testimonial":{"text":"","highlights":[]}}

USE-CASE EXAMPLES (illustrative; the model must output only one JSON object)

The following are examples of valid outputs for common edge cases. They show how to represent difficult strings safely in JSON. The model should return only one JSON object tailored to the actual input; these are just references inside your prompt.

Text contains double quotes (")

{
  "version": "blurlist-1.0",
  "phrases": [
    { "text": "The chapter is called \"Company Law Basics\"", "mode": "normal", "case": "insensitive" }
  ],
  "testimonial": { "text": "", "highlights": [] }
}


Text contains angle brackets (< and >)

{
  "version": "blurlist-1.0",
  "phrases": [
    { "text": "Contact <support@edutap.in> for details", "mode": "strict", "case": "insensitive" }
  ],
  "testimonial": { "text": "", "highlights": [] }
}


Text contains backslashes and newlines

{
  "version": "blurlist-1.0",
  "phrases": [
    { "text": "Path C:\\\\Users\\\\EduTap\\\\Docs\\nPlease review on Monday.", "mode": "normal", "case": "insensitive" }
  ],
  "testimonial": { "text": "", "highlights": [] }
}


Very long paragraph (multi-sentence)

{
  "version": "blurlist-1.0",
  "phrases": [
    {
      "text": "My only concern for now being company law, I would like to request you you to have some additional classes for company law since it's really comprehensive and we, as a students have not been able to grasp it well. Arsh Ma'am has been excellent as well but it would be great to have some lectures of company law for all of us to pursue our preparation and our goals better. Since the exam has been postponed, I, on behalf of almost all of us would request you to arrange some lectures as far as company law is concerned. I understand there must be some logistics involved so I can try to convince my fellow batch mates to pay some additional fees as well. I hope you understand that this exam means a lot to all of us. Please consider my request.",
      "mode": "loose",
      "case": "insensitive"
    }
  ],
  "testimonial": { "text": "", "highlights": [] }
}


Smart quotes, em/en dashes, ellipsis

{
  "version": "blurlist-1.0",
  "phrases": [
    { "text": "“Company Law” - advanced…", "mode": "normal", "case": "insensitive" }
  ],
  "testimonial": { "text": "", "highlights": [] }
}


*Regex metacharacters that must be treated literally: .+?^$()[]{}|*

{
  "version": "blurlist-1.0",
  "phrases": [
    { "text": "Match these literally: .+?^$()[]{}|\\", "mode": "strict", "case": "insensitive" }
  ],
  "testimonial": { "text": "", "highlights": [] }
}


URLs with query strings and &

{
  "version": "blurlist-1.0",
  "phrases": [
    { "text": "https://portal.edutap.in/course?name=company-law&unit=3", "mode": "strict", "case": "insensitive" }
  ],
  "testimonial": { "text": "", "highlights": [] }
}


Emails with plus-addressing

{
  "version": "blurlist-1.0",
  "phrases": [
    { "text": "arsh.maam+batchA@edutap.in", "mode": "strict", "case": "insensitive" }
  ],
  "testimonial": { "text": "", "highlights": [] }
}


Code-like strings with braces/brackets/quotes

{
  "version": "blurlist-1.0",
  "phrases": [
    { "text": "const note = { title: \"Company Law\", tags: [\"exam\",\"revision\"] };", "mode": "normal", "case": "insensitive" }
  ],
  "testimonial": { "text": "", "highlights": [] }
}


Devanagari / Hindi (non-Latin)

{
  "version": "blurlist-1.0",
  "phrases": [
    { "text": "कंपनी क़ानून की अतिरिक्त कक्षा चाहिए", "mode": "normal", "case": "insensitive" }
  ],
  "testimonial": { "text": "", "highlights": [] }
}


Right-to-left (Arabic/Hebrew)

{
  "version": "blurlist-1.0",
  "phrases": [
    { "text": "القانون التجاري للشركات", "mode": "normal", "case": "insensitive" }
  ],
  "testimonial": { "text": "", "highlights": [] }
}


Emojis and mixed Unicode

{
  "version": "blurlist-1.0",
  "phrases": [
    { "text": "Need extra classes 🙏📚", "mode": "normal", "case": "insensitive" }
  ],
  "testimonial": { "text": "", "highlights": [] }
}


Parentheses, quotes, and punctuation soup

{
  "version": "blurlist-1.0",
  "phrases": [
    { "text": "Arsh Ma'am (Company Law) – \"urgent\"", "mode": "normal", "case": "insensitive" }
  ],
  "testimonial": { "text": "", "highlights": [] }
}


Duplicate candidates → must be deduplicated

{
  "version": "blurlist-1.0",
  "phrases": [
    { "text": "Company Law", "mode": "normal", "case": "insensitive" },
    { "text": "Company Law", "mode": "normal", "case": "insensitive" }
  ],
  "testimonial": { "text": "", "highlights": [] }
}


Final output must include only one "Company Law" entry.

Multi-line literal (explicit newline inside text)

{
  "version": "blurlist-1.0",
  "phrases": [
    { "text": "First line\\nSecond line about Company Law", "mode": "normal", "case": "insensitive" }
  ],
  "testimonial": { "text": "", "highlights": [] }
}


Case sensitivity override for IDs/codes

{
  "version": "blurlist-1.0",
  "phrases": [
    { "text": "FORM-INC-22A", "mode": "strict", "case": "sensitive" }
  ],
  "testimonial": { "text": "", "highlights": [] }
}


Nothing to blur

{"version":"blurlist-1.0","phrases":[],"testimonial":{"text":"","highlights":[]}}
"""

# We keep the full OUTPUT CONTRACT exactly from your current script
GPT_OUTPUT_CONTRACT = r"""
OUTPUT FORMAT (MANDATORY) - JSON ONLY

Return only a single JSON object (no prose, no Markdown, no code fences).
UTF-8 JSON, no comments, no trailing commas.
All items in phrases[].text are literal substrings to blur (not regex).
If nothing should be blurred, return an empty list.

Exact schema
{
  "version": "blurlist-1.0",
  "phrases": [
    {
      "text": "literal string to blur",
      "mode": "normal",
      "case": "insensitive"
    }
  ],
  "testimonial": {
    "text": "Single consolidated testimonial text suitable for display on a graphic.",
    "highlights": [
      { "text": "literal substring from testimonial.text to visually emphasize" }
    ]
  },
  "student_name": "Student Name"
}

Field rules
- version (required): exactly "blurlist-1.0".
- phrases (required): array of objects; deduplicate identical text values.
- phrases[].text (required): literal substring to blur. May contain any characters (quotes, <, >, backslashes,
  braces, brackets, slashes, punctuation, Unicode, emojis, and newlines). Escape per JSON.
- phrases[].mode (optional): "loose" | "normal" | "strict" (guides tolerance; default "normal").
- phrases[].case (optional): "insensitive" (default) or "sensitive" (use "sensitive" for tokens like IDs/codes).
- testimonial (required): object describing what to print on a graphic.
- testimonial.text (required): one clean paragraph (max ~400 chars) that captures the praise/impact. Do not include PII,
  greetings/closings ("Dear...", "Thank you"), operational lines ("please arrange classes"), links, emails, or phone numbers.
  IMPORTANT: output literal characters only - no HTML entities (e.g., &amp;, &lt;) and no Unicode escapes (e.g., \\u2019). Use the actual characters.
- testimonial.highlights (required): 0-4 items. Each item "text" is a verbatim substring of testimonial.text to bold/highlight.
  Each <= 80 chars. Deduplicate identical highlights. IMPORTANT: literal characters only (no HTML entities, no \\uXXXX escapes).
- student_name (required): if a name is present in the feedback, return the exact literal name; otherwise return "Student Name".
  Literal characters only (no HTML entities, no \\uXXXX escapes).

No other properties are allowed at the top level or inside phrases[] / testimonial.
Do not return regex patterns. Do not wrap the JSON in code fences.

Empty case
- Blur only empty:
  {"version":"blurlist-1.0","phrases":[]}
- Full empty (no blur and no testimonial):
  {"version":"blurlist-1.0","phrases":[],"testimonial":{"text":"","highlights":[]},"student_name":"Student Name"}
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

# --- replace your current def compose_testimonial_graphic(...) with this async version ---
# --- replace your current def compose_testimonial_graphic(...) with this async version ---
async def compose_testimonial_graphic(
    raw_template_path: str,
    footer_path: str,
    email_screenshot_image,   # bytes / bytearray / PIL.Image.Image / path
    out_path: str,
    testimonial_text: str,
    highlight_items: List[Dict[str, str]],
    student_name: str,
) -> str:
    if not os.path.exists(raw_template_path):
        raise FileNotFoundError(raw_template_path)

    # --- Load canvas ---
    base = Image.open(raw_template_path).convert("RGBA")
    W, H = base.size
    draw = ImageDraw.Draw(base)

    # --- Fonts ---
    f_reg   = _load_font(FONT_REGULAR_PATH, QUOTE_FONT_SIZE)
    f_bold  = _load_font(FONT_BOLD_PATH,    QUOTE_FONT_SIZE)
    f_name  = _load_font(FONT_BOLD_PATH if NAME_IS_BOLD else FONT_REGULAR_PATH, NAME_FONT_SIZE)
    f_emoji = _load_font(EMOJI_FONT_PATH, QUOTE_FONT_SIZE) if EMOJI_FONT_PATH else None

    # =========================================================
    # 1) FOOTER FIRST (fixed at canvas bottom)
    # =========================================================
    footer_occupied = 0
    if os.path.exists(footer_path):
        footer_occupied = _paste_footer_scaled(
            base, footer_path,
            bottom_margin=FOOTER_BOTTOM_MARGIN,
            width_ratio=FOOTER_WIDTH_RATIO,
            max_height=FOOTER_MAX_HEIGHT,
            white_pad=FOOTER_WHITE_PADDING,
            trim_alpha=FOOTER_TRIM_ALPHA,
        )

    # =========================================================
    # 2) EMAIL PANEL (fixed height) DIRECTLY ABOVE FOOTER
    # =========================================================
    # Bottom edge of teal block (just above footer + bottom margin)
    email_bottom = H - max(footer_occupied, 0) - EMAIL_BOTTOM_MARGIN
    # Top edge determined by fixed EMAIL_PANEL_H
    email_top    = email_bottom - int(EMAIL_PANEL_H)

    # --- Reserve vertical space for hashtag inside teal block ---
    if TAGLINE_TEXT:
        # we only use this to know how tall the hashtag line is
        f_tag_reserve = _load_font(FONT_BOLD_PATH, TAGLINE_FONT_SIZE)
        _, tag_h_reserve = _measure(draw, f_tag_reserve, TAGLINE_TEXT)

        # email must stay at least this far above the bottom of teal:
        #   (gap from email to hashtag) + (hashtag height) + (gap from hashtag to bottom)
        reserved_for_tag = (
            TAGLINE_MIN_GAP_FROM_EMAIL
            + TAGLINE_MARGIN_ABOVE_BOTTOM
            + tag_h_reserve
        )
    else:
        reserved_for_tag = 0

    # increase teal "bottom padding" so email never goes into hashtag area
    bottom_pad_for_email = max(EMAIL_BACKDROP_BOTTOM_PAD, reserved_for_tag)

    # Paste email screenshot and get where it actually sits
    email_img_top, email_img_bottom = _paste_email_to_fit_with_backdrop(
        base,
        email_screenshot_image,
        y_top=email_top,
        y_bottom=email_bottom,                 # <<< added, fixes missing arg
        side_pad_inside=EMAIL_SIDE_PADDING,
        backdrop_enable=EMAIL_BACKDROP_ENABLE,
        backdrop_color=EMAIL_BACKDROP_COLOR,
        backdrop_side_pad=EMAIL_BACKDROP_SIDE_PAD,
        backdrop_top_pad=EMAIL_BACKDROP_TOP_PAD,
        backdrop_bottom_pad=bottom_pad_for_email,   # 👈 CHANGED
        allow_upscale=EMAIL_ALLOW_UPSCALE,          # up OR down
        sharpen_amount=EMAIL_SHARPEN_AMOUNT,
    )

    # ---------- Hashtag "#WeGotYourBack" inside teal block ----------
    if TAGLINE_TEXT:
        f_tag = _load_font(FONT_BOLD_PATH, TAGLINE_FONT_SIZE)
        tag_w, tag_h = _measure(draw, f_tag, TAGLINE_TEXT)

        # First choice: just below the email screenshot
        tag_y_candidate = email_img_bottom + TAGLINE_MIN_GAP_FROM_EMAIL

        # Do not go too close to bottom of teal block
        tag_y_max = email_bottom - TAGLINE_MARGIN_ABOVE_BOTTOM - tag_h

        tag_y = min(tag_y_candidate, tag_y_max)
        # Also ensure we stay inside the teal area at the top
        tag_y = max(email_top + EMAIL_BACKDROP_TOP_PAD, tag_y)

        tag_x = (W - tag_w) // 2
        draw.text(
            (tag_x, tag_y),
            TAGLINE_TEXT,
            font=f_tag,
            fill=_hex_to_rgba(TAGLINE_COLOR)[:3],
        )

    # =========================================================
    # 3) STAR RATING ROW (5-star png under name)
    # =========================================================
    star_w = star_h = 0
    star_img = None

    if os.path.exists(STAR_IMAGE_PATH):
        star_img = Image.open(STAR_IMAGE_PATH).convert("RGBA")
        w0, h0 = star_img.size

        # --- single size control: STAR_SIZE_PX is target height in pixels ---
        target_h = float(STAR_SIZE_PX)
        if target_h <= 0:
            target_h = float(h0)  # safety: fall back to original height

        scale = target_h / float(h0)     # same scale used for width and height
        new_w = int(w0 * scale)
        new_h = int(h0 * scale)

        star_img = star_img.resize((new_w, new_h), Image.LANCZOS)
        star_w, star_h = new_w, new_h

    # <<< ADD THIS LINE >>>
    star_y = email_top - STAR_MARGIN_ABOVE_EMAIL - (star_h or 0)

    # =========================================================
    # 4) STUDENT NAME – FIXED FONT SIZE, ABOVE STARS
    # =========================================================
    name_text = (student_name or "").strip()

    # Ensure we have a height even if name is empty
    dummy_text = name_text if name_text else "Ay"
    name_w, name_h = _measure(draw, f_name, dummy_text)

    # Place name above star row with vertical gap
    name_y = star_y - STAR_MARGIN_BELOW_NAME - name_h
    # Guard so it never overlaps header/top
    name_y = max(QUOTE_TOP_Y + 10, name_y)

    if name_text and name_text != "Student Name":
        name_x = (W - name_w) // 2
        draw.text((name_x, name_y), name_text, font=f_name,
                  fill=_hex_to_rgba(NAME_COLOR)[:3])

    # Now that we know final name_y, if we have a star image, paste it
    if star_img is not None and star_w > 0 and star_h > 0:
        star_x = (W - star_w) // 2
        base.alpha_composite(star_img, (star_x, star_y))

    # This is the bottom limit for testimonial text
    quote_bottom_limit = name_y - NAME_TOP_MARGIN

    # =========================================================
    # 4) TESTIMONIAL TEXT – AUTO FONT SIZE TO FILL GAP
    # =========================================================
    available_quote_h = max(30, int(quote_bottom_limit - QUOTE_TOP_Y))
    content_w = max(10, W - 2 * QUOTE_SIDE_PADDING)

    async def render_quote(font_size_px: int) -> Image.Image:
        return await _render_quote_html_png(
            pw_context,
            width_px=content_w,
            text=testimonial_text or "",
            highlights=highlight_items or [],
            font_stack=BROWSER_FONT_STACK,
            font_size_px=font_size_px,
            line_height=LINE_HEIGHT_MULT,
            normal_hex=QUOTE_TEXT_COLOR,
            bold_hex=BOLD_TEXT_COLOR,
            add_quotes=ADD_QUOTES,
            open_q=OPEN_Q,
            close_q=CLOSE_Q,
        )

    if RENDER_QUOTE_WITH_BROWSER and "pw_context" in globals() and pw_context is not None:
        # Binary search for largest font size that fits vertically
        fs_min = max(10, QUOTE_FONT_SIZE - 4)
        fs_max = QUOTE_FONT_SIZE + 20

        best_img = None
        best_h = 0
        lo, hi = fs_min, fs_max

        while lo <= hi:
            mid = (lo + hi) // 2
            img = await render_quote(mid)

            qw_tmp, qh_tmp = img.size
            # Normalize width to requested content_w
            if qw_tmp != content_w:
                scale = content_w / float(qw_tmp)
                img = img.resize((content_w, int(qh_tmp * scale)), Image.LANCZOS)
                qh_tmp = img.size[1]

            if qh_tmp <= available_quote_h:
                # Fits – try larger
                best_img, best_h = img, qh_tmp
                lo = mid + 1
            else:
                # Too tall – try smaller
                hi = mid - 1

        # Fallback to base font size if nothing else worked
        quote_img = best_img or await render_quote(QUOTE_FONT_SIZE)
        qw, qh = quote_img.size
        quote_x = (W - qw) // 2
        base.alpha_composite(quote_img, (quote_x, QUOTE_TOP_Y))
    else:
        # Pillow fallback (no browser, no emoji colour)
        _draw_quote_centered_with_highlights(
            base, draw,
            testimonial_text or "",
            highlight_items or [],
            QUOTE_TOP_Y,
            QUOTE_SIDE_PADDING,
            f_reg, f_bold, f_emoji
        )

    # =========================================================
    # 5) SAVE
    # =========================================================
    base.save(out_path)
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
    testimonial_in = payload_obj.get("testimonial") or {}
    raw_name = payload_obj.get("student_name")

    # Clean phrases
    phrases: List[str] = []
    seen=set()
    for item in phrases_in:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            cleaned = normalize_artifacts(item["text"])
            if cleaned and cleaned not in seen:
                phrases.append(cleaned); seen.add(cleaned)

    # Testimonial + highlights
    t_text = ""
    t_high_clean: List[Dict[str,str]] = []
    if isinstance(testimonial_in, dict):
        t_text = normalize_artifacts(testimonial_in.get("text","") or "")
        highs = testimonial_in.get("highlights", []) or []
        if isinstance(highs, list):
            seen_h=set()
            for h in highs:
                if isinstance(h, dict) and isinstance(h.get("text"), str):
                    ht = normalize_artifacts(h["text"])
                    if ht and ht not in seen_h:
                        t_high_clean.append({"text": ht}); seen_h.add(ht)

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
        context = await browser.new_context(device_scale_factor=EMAIL_SCREENSHOT_DEVICE_SCALE)

        # Make this context available to the composer
        global pw_context
        pw_context = context

        page = await context.new_page()

        try:
            print(f"Navigating to URL: {url}")
            await page.goto(url, wait_until="networkidle", timeout=45_000)

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
                    print("\n=== Calling GPT for blur + testimonial JSON ===")
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