"""Report-domain tools: Self-contained JSON-backed CRUD with Base64 sketch delivery.

This standalone implementation stores reports in local JSON / memory (zero GCS
dependencies) and delivers sketches as optimized Base64 Data URIs (<8KB) to
ensure instant rendering in Gemini Enterprise and A2A webviews without CORS/CSP friction.
"""

import base64
import glob
import io
import json
import logging
import os
import shutil
from datetime import datetime
from typing import Any

from google import genai
from google.genai import types
from PIL import Image
from pydantic import BaseModel

from app.config import (
    GOOGLE_CLOUD_PROJECT,
    IMAGE_GEN_LOCATION,
    IMAGE_GEN_MODEL,
)

logger = logging.getLogger(__name__)

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_ASSETS_DIR = os.path.join(_APP_DIR, "assets")
_SEED_DATA_PATH = os.path.join(_APP_DIR, "data", "reports.json")
_RUNTIME_DATA_PATH = os.environ.get("REPORTS_STORAGE_PATH", "/tmp/reports.json")

_genai_client: genai.Client | None = None
_IMAGE_CACHE: dict[str, str] = {}
_DEFAULT_SKETCH_DATA_URI: str | None = None


class Report(BaseModel):
    id: str | None = None
    room: str
    item: str
    issue: str
    timestamp: str
    image_blob: str | None = None
    reporter_email: str | None = None
    reporter_name: str | None = None
    priority: str | None = None
    category: str | None = None


def _init_storage() -> str:
    """Ensure runtime reports JSON file is initialized from packaged seed data."""
    if not os.path.exists(_RUNTIME_DATA_PATH):
        try:
            if os.path.exists(_SEED_DATA_PATH):
                os.makedirs(os.path.dirname(_RUNTIME_DATA_PATH), exist_ok=True)
                shutil.copyfile(_SEED_DATA_PATH, _RUNTIME_DATA_PATH)
                logger.info(f"Initialized storage at {_RUNTIME_DATA_PATH} from {_SEED_DATA_PATH}")
            else:
                with open(_RUNTIME_DATA_PATH, "w") as f:
                    json.dump([], f)
        except Exception as e:
            logger.warning(f"Failed to copy seed data to {_RUNTIME_DATA_PATH}: {e}")
            return _SEED_DATA_PATH
    return _RUNTIME_DATA_PATH


def _load_reports() -> list[dict]:
    """Load reports from the runtime file or packaged seed file."""
    path = _init_storage()
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
    except Exception as e:
        logger.error(f"Error loading reports from {path}: {e}")
    return []


def _save_reports(reports: list[dict]) -> bool:
    """Save reports to runtime file."""
    path = _init_storage()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(reports, f, indent=2)
        return True
    except Exception as e:
        logger.error(f"Error saving reports to {path}: {e}")
        return False


def _to_thumbnail_data_uri(raw_bytes: bytes, max_width: int = 320) -> str:
    """Compress image bytes to a lightweight JPEG thumbnail data URI (~5-8KB)."""
    try:
        img = Image.open(io.BytesIO(raw_bytes))
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail((max_width, int(max_width * 9 / 16)), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=75, optimize=True)
        b64 = base64.b64encode(out.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{b64}"
    except Exception as e:
        logger.debug(f"Thumbnail compression failed: {e}")
        mime = "image/jpeg" if raw_bytes.startswith(b"\xff\xd8") else "image/png"
        b64 = base64.b64encode(raw_bytes).decode("ascii")
        return f"data:{mime};base64,{b64}"


def _get_default_sketch_data_uri() -> str:
    """Return fallback thumbnail data URI for banners/unresolved images."""
    global _DEFAULT_SKETCH_DATA_URI
    if _DEFAULT_SKETCH_DATA_URI is None:
        jpg_header = os.path.join(_ASSETS_DIR, "welcome_header.jpg")
        png_header = os.path.join(_ASSETS_DIR, "welcome_header.png")
        target = jpg_header if os.path.exists(jpg_header) else png_header
        if os.path.exists(target):
            with open(target, "rb") as f:
                _DEFAULT_SKETCH_DATA_URI = _to_thumbnail_data_uri(f.read(), max_width=320)
        else:
            _DEFAULT_SKETCH_DATA_URI = ""
    return _DEFAULT_SKETCH_DATA_URI


def _get_image_data_uri(blob_name: str) -> str:
    """Retrieve image as a Base64 data URI with in-memory caching."""
    if not blob_name:
        return _get_default_sketch_data_uri()
    if blob_name in _IMAGE_CACHE:
        return _IMAGE_CACHE[blob_name]

    candidates = [
        os.path.join(_ASSETS_DIR, "issue_images", os.path.basename(blob_name)),
        os.path.join(_ASSETS_DIR, blob_name),
    ]
    for local_path in candidates:
        if os.path.exists(local_path):
            try:
                with open(local_path, "rb") as f:
                    data_uri = _to_thumbnail_data_uri(f.read())
                    _IMAGE_CACHE[blob_name] = data_uri
                    return data_uri
            except Exception as e:
                logger.debug(f"Could not load local asset {local_path}: {e}")

    default_uri = _get_default_sketch_data_uri()
    if default_uri:
        _IMAGE_CACHE[blob_name] = default_uri
        return default_uri
    return ""


def _get_genai_client() -> genai.Client | None:
    """Return a cached google.genai client bound to Vertex AI if configured."""
    global _genai_client
    if _genai_client is None and GOOGLE_CLOUD_PROJECT:
        try:
            _genai_client = genai.Client(
                vertexai=True,
                project=GOOGLE_CLOUD_PROJECT,
                location=IMAGE_GEN_LOCATION,
            )
        except Exception as e:
            logger.warning(f"Could not initialize google.genai Vertex client: {e}")
    return _genai_client


def _get_bundled_fallback_image(room: str, item: str, issue: str) -> tuple[str, bytes]:
    """Select an appropriate bundled image from packaged assets as fallback."""
    issue_dir = os.path.join(_ASSETS_DIR, "issue_images")
    files = sorted(glob.glob(os.path.join(issue_dir, "*.png")))
    if not files:
        files = sorted(glob.glob(os.path.join(_ASSETS_DIR, "*.png")))

    if files:
        # Deterministically select based on hash of room+item to look consistent
        idx = (abs(hash(f"{room}-{item}-{issue}")) % len(files))
        selected_file = files[idx]
        with open(selected_file, "rb") as f:
            return os.path.basename(selected_file), f.read()

    # Ultimate fallback 1x1 png
    blank_png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
        b"\x00\x00\x00\x1f\x15c4\x00\x00\x00\rIDATx\x9cc`\x00\x00\x00\x02\x00\x01"
        b"H\xaf\xa4q\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    return "default_blank.png", blank_png


def generate_visual_sketch(room: str, item: str, issue: str) -> dict:
    """Generates or selects a photo-realistic 16:9 sketch of the defect.

    Attempts generation via Vertex AI Image model. If unauthenticated, quota exceeded,
    or project unset, seamlessly selects from the bundled asset library.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    image_blob_name = f"{timestamp}_issue.png"

    prompt = (
        f"A clear, realistic, documentary photograph of maintenance damage in an office or commercial building: "
        f"{item} in {room}. The problem is: {issue}. "
        f"Professional architectural inspection photo, clean studio lighting, 16:9 wide aspect ratio."
    )

    image_bytes: bytes | None = None
    client = _get_genai_client()

    if client:
        try:
            config = types.GenerateContentConfig(
                response_modalities=["TEXT", "IMAGE"],
                image_config=types.ImageConfig(aspect_ratio="16:9"),
            )
            response = client.models.generate_content(
                model=IMAGE_GEN_MODEL, contents=prompt, config=config
            )
            for part in response.candidates[0].content.parts:
                inline = getattr(part, "inline_data", None)
                if inline is not None and inline.data:
                    image_bytes = inline.data
                    break
        except Exception as e:
            logger.warning(f"Vertex image generation failed ({e}); using bundled library fallback.")

    if not image_bytes:
        image_blob_name, image_bytes = _get_bundled_fallback_image(room, item, issue)

    data_uri = _to_thumbnail_data_uri(image_bytes)
    _IMAGE_CACHE[image_blob_name] = data_uri

    return {
        "status": "success",
        "message": "Sketch successfully prepared. Use `image_url` to display the sketch.",
        "image_blob": image_blob_name,
        "image_url": data_uri,
    }


def get_welcome_header() -> dict:
    """Return the Base64 data URI for the welcome header banner."""
    header_uri = _get_default_sketch_data_uri()
    if header_uri:
        return {
            "status": "success",
            "url": header_uri,
            "message": "Use `url` verbatim as the welcome header Image.url.",
        }
    return {
        "status": "error",
        "url": None,
        "message": "Header image unavailable.",
    }


# --- Backend CRUD Tools ---


def submit_maintenance_report(
    tool_context: Any,
    issue: str,
    reporter_email: str = "",
    reporter_name: str = "",
    room: str = "Unknown",
    item: str = "Unknown",
    description: str = "",
    image_blob: str = "",
    priority: str = "",
    category: str = "",
) -> dict:
    """Appends a new maintenance report to the central incident database."""
    reporter_email = reporter_email or "anonymous@example.com"
    reporter_name = reporter_name or "Anonymous"

    if description:
        full_issue_text = f"{issue} - {description}"
    else:
        full_issue_text = issue

    try:
        reports = _load_reports()
        next_id = str(len(reports) + 1)

        # If image_blob is missing, auto-assign a sketch
        if not image_blob:
            sketch_res = generate_visual_sketch(room, item, full_issue_text)
            image_blob = sketch_res.get("image_blob", "")

        new_report = {
            "id": next_id,
            "timestamp": datetime.now().isoformat(),
            "reporter_email": reporter_email,
            "reporter_name": reporter_name,
            "room": room,
            "item": item,
            "issue": full_issue_text,
            "image_blob": image_blob or None,
        }
        # Optional form selections; when absent they are derived from the text.
        if priority:
            new_report["priority"] = priority
        if category:
            new_report["category"] = category

        reports.append(new_report)
        _save_reports(reports)

        return {
            "status": "success",
            "message": f"Report successfully added. Total reports: {len(reports)}.",
            "total_reports": len(reports),
            "report_id": next_id,
            "image_url": _get_image_data_uri(image_blob) if image_blob else None,
        }
    except Exception as e:
        logger.error(f"Error submitting report: {e}")
        return {"status": "error", "message": f"Error saving report: {e}"}


def list_reports(tool_context: Any = None) -> list[dict]:
    """Retrieves all building incidents, each with an optimized Base64 sketch URL."""
    try:
        reports = _load_reports()
        for report in reports:
            image_blob = report.get("image_blob")
            report["image_url"] = _get_image_data_uri(image_blob) if image_blob else None
        return reports
    except Exception as e:
        logger.error(f"Error in list_reports: {e}")
        return []
