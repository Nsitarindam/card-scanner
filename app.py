import os
import json
import base64
import re
from datetime import datetime
from io import BytesIO

import streamlit as st
from PIL import Image
import anthropic
import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

load_dotenv()

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Business Card Scanner",
    page_icon="🪪",
    layout="centered",
)

st.title("Business Card Scanner")
st.caption("Scan a card → Claude reads it → Saved to Google Sheet automatically.")

# ── Helper: normalize any value to a list ─────────────────────────────────────
def normalize_list(val):
    if val is None:
        return []
    if isinstance(val, list):
        return [str(v) for v in val if v]
    return [str(val)]

# ── Helper: get API key ────────────────────────────────────────────────────────
def get_api_key():
    try:
        if "ANTHROPIC_API_KEY" in st.secrets:
            return str(st.secrets["ANTHROPIC_API_KEY"])
    except Exception:
        pass
    return os.getenv("ANTHROPIC_API_KEY", "")

# ── Helper: get Google Sheet credentials ──────────────────────────────────────
def get_gsheet_creds():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    # Try Streamlit Cloud secrets first
    try:
        if "GCP_CREDENTIALS_JSON" in st.secrets:
            info = json.loads(str(st.secrets["GCP_CREDENTIALS_JSON"]))
            return Credentials.from_service_account_info(info, scopes=scopes)
    except Exception as e:
        st.warning(f"Cloud credentials error: {e}")

    # Fall back to local credentials.json
    creds_path = os.path.join(os.path.dirname(__file__), "credentials.json")
    if os.path.exists(creds_path):
        try:
            return Credentials.from_service_account_file(creds_path, scopes=scopes)
        except Exception as e:
            st.warning(f"Local credentials.json error: {e}")

    return None

# ── Helper: get Sheet ID ───────────────────────────────────────────────────────
def get_sheet_id():
    try:
        if "GOOGLE_SHEET_ID" in st.secrets:
            return str(st.secrets["GOOGLE_SHEET_ID"])
    except Exception:
        pass
    return os.getenv("GOOGLE_SHEET_ID", "")

# ── Helper: encode image to base64 ────────────────────────────────────────────
def encode_image(source):
    if hasattr(source, "read"):
        raw = source.read()
    else:
        raw = bytes(source)
    img = Image.open(BytesIO(raw))
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    max_side = 1600
    if max(img.width, img.height) > max_side:
        img.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=90)
    buf.seek(0)
    return base64.standard_b64encode(buf.read()).decode("utf-8"), "image/jpeg"

# ── Helper: call Claude ────────────────────────────────────────────────────────
def scan_card(front_source, back_source=None):
    api_key = get_api_key()
    if not api_key or api_key == "your_api_key_here":
        st.error("Anthropic API key not set.")
        st.stop()

    client = anthropic.Anthropic(api_key=api_key)
    sides = "the front side" if back_source is None else "both the front and back sides"

    prompt = f"""You are a precise business card OCR system. You are given {sides} of a business card.

Rules:
- Extract and MERGE all information from all provided images into a single result
- If the same field appears on both sides, combine them — never duplicate
- If you CANNOT read a field with high confidence, set it to null — NEVER guess
- Preserve the original language/script for every field exactly as printed
- For non-Latin scripts (Chinese, Arabic, Japanese, Hindi, Korean, etc.), also add a romanized version in parentheses
- Extract ALL text verbatim, including taglines, slogans, and decorative text
- phones MUST always be a JSON array, even if there is only one number
- emails MUST always be a JSON array, even if there is only one email
- For phones: include country code if visible

Return ONLY valid JSON with no markdown or extra text:
{{
  "name": "full name as printed, or null",
  "title": "job title, or null",
  "company": "company/organization name, or null",
  "phones": ["number1", "number2"],
  "emails": ["email@example.com"],
  "address": "full address as one string, or null",
  "website": "url, or null",
  "social": {{
    "linkedin": "linkedin url or handle, or null",
    "twitter": "twitter handle, or null",
    "other": "any other social handle/url, or null"
  }},
  "tagline": "any slogan or tagline on the card, or null",
  "raw_text_front": "every word on the front of the card, verbatim",
  "raw_text_back": "every word on the back verbatim, or null if no back provided",
  "confidence": "high if everything is clear, medium if some parts are unclear, low if blurry",
  "notes": "describe anything uncertain or that you could not read, or null"
}}"""

    content = []
    try:
        front_b64, front_mime = encode_image(front_source)
        content.append({"type": "image", "source": {"type": "base64", "media_type": front_mime, "data": front_b64}})
        if back_source is not None:
            back_b64, back_mime = encode_image(back_source)
            content.append({"type": "image", "source": {"type": "base64", "media_type": back_mime, "data": back_b64}})
    except Exception as e:
        st.error(f"Image encoding error: {e}")
        st.stop()

    content.append({"type": "text", "text": prompt})

    try:
        response = client.messages.create(
            model="claude-3-haiku-20240307",
            max_tokens=2048,
            messages=[{"role": "user", "content": content}],
        )
    except Exception as e:
        st.error(f"Anthropic API error: {e}")
        st.stop()

    raw = response.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        st.error("Claude returned unexpected output. Raw response shown below.")
        st.code(raw)
        st.stop()

# ── Helper: save to Google Sheet ──────────────────────────────────────────────
def save_to_sheet(data):
    sheet_id = get_sheet_id()
    if not sheet_id or sheet_id == "your_sheet_id_here":
        st.warning("Google Sheet ID not configured in secrets.")
        return None

    creds = get_gsheet_creds()
    if creds is None:
        st.warning("Could not load Google credentials. Check that GCP_CREDENTIALS_JSON is set in Streamlit secrets.")
        return None

    try:
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(sheet_id)
        ws = sh.sheet1

        if ws.row_count == 0 or ws.cell(1, 1).value != "Timestamp":
            ws.insert_row(
                ["Timestamp", "Name", "Title", "Company", "Phones", "Emails",
                 "Address", "Website", "LinkedIn", "Twitter", "Other Social",
                 "Tagline", "Confidence", "Notes", "Raw Text (Front)", "Raw Text (Back)"],
                index=1,
            )

        social = data.get("social") or {}
        phones = normalize_list(data.get("phones"))
        emails = normalize_list(data.get("emails"))

        row = [
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            data.get("name") or "",
            data.get("title") or "",
            data.get("company") or "",
            ", ".join(phones),
            ", ".join(emails),
            data.get("address") or "",
            data.get("website") or "",
            social.get("linkedin") or "",
            social.get("twitter") or "",
            social.get("other") or "",
            data.get("tagline") or "",
            data.get("confidence") or "",
            data.get("notes") or "",
            data.get("raw_text_front") or "",
            data.get("raw_text_back") or "",
        ]
        ws.append_row(row, value_input_option="RAW")
        return f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
    except Exception as e:
        st.warning(f"Google Sheet save error: {e}")
        return None

# ── Helper: display extracted data ────────────────────────────────────────────
def display_results(data):
    confidence = data.get("confidence", "unknown")
    color = {"high": "green", "medium": "orange", "low": "red"}.get(confidence, "grey")
    st.markdown(f"**Confidence:** :{color}[{confidence.upper()}]")

    if data.get("notes"):
        st.info(f"Notes: {data['notes']}")

    phones = normalize_list(data.get("phones"))
    emails = normalize_list(data.get("emails"))

    fields = [
        ("Name", data.get("name")),
        ("Title", data.get("title")),
        ("Company", data.get("company")),
        ("Phone(s)", "\n".join(phones) if phones else None),
        ("Email(s)", "\n".join(emails) if emails else None),
        ("Address", data.get("address")),
        ("Website", data.get("website")),
        ("Tagline", data.get("tagline")),
    ]

    social = data.get("social") or {}
    for key, label in [("linkedin", "LinkedIn"), ("twitter", "Twitter"), ("other", "Other Social")]:
        if social.get(key):
            fields.append((label, social[key]))

    rows = [(label, val) for label, val in fields if val]
    if rows:
        col1, col2 = st.columns([1, 2])
        for label, val in rows:
            col1.markdown(f"**{label}**")
            col2.markdown(val)

    with st.expander("Raw text (verbatim from card)"):
        if data.get("raw_text_front"):
            st.markdown("**Front:**")
            st.text(data["raw_text_front"])
        if data.get("raw_text_back"):
            st.markdown("**Back:**")
            st.text(data["raw_text_back"])

    with st.expander("Full JSON"):
        st.json(data)

# ── Helper: get image from tab (upload or camera) ─────────────────────────────
def image_input(label, key_prefix, required=True):
    heading = f"### {label}" + ("" if required else " *(optional)*")
    st.markdown(heading)
    tab_upload, tab_camera = st.tabs(["Upload file", "Take photo"])

    with tab_upload:
        uploaded = st.file_uploader(
            f"Choose image for {label}",
            type=["jpg", "jpeg", "png", "webp", "heic"],
            key=f"{key_prefix}_upload",
            label_visibility="collapsed",
        )
        if uploaded:
            st.image(uploaded, use_container_width=True)
            return uploaded

    with tab_camera:
        captured = st.camera_input(
            f"Take photo of {label}",
            key=f"{key_prefix}_camera",
            label_visibility="collapsed",
        )
        if captured:
            st.image(captured, use_container_width=True)
            return captured

    return None

# ── Main UI ───────────────────────────────────────────────────────────────────
front_source = image_input("Front of card", "front", required=True)

st.divider()

back_source = image_input("Back of card", "back", required=False)

st.divider()

if front_source:
    if st.button("Scan Card", type="primary", use_container_width=True):
        with st.spinner("Reading card with Claude Vision..."):
            if hasattr(front_source, "seek"):
                front_source.seek(0)
            if back_source and hasattr(back_source, "seek"):
                back_source.seek(0)
            result = scan_card(front_source, back_source)

        st.success("Scan complete!")
        display_results(result)
        save_to_sheet(result)
else:
    st.info("Upload or photograph the front of a card above to get started.")
