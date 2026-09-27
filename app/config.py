"""Centralized configuration for the A2UI v0.9 ADK Maintenance & Operations Agent.

All environment-driven settings live here. Values are loaded from environment
variables or an optional `.env` file via dotenv.
"""

import logging
import os

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Google Cloud & Vertex AI
# ---------------------------------------------------------------------------
GOOGLE_CLOUD_PROJECT: str = os.getenv("GOOGLE_CLOUD_PROJECT", "")
GOOGLE_CLOUD_LOCATION: str = os.getenv("GOOGLE_CLOUD_LOCATION", "global")

if "GOOGLE_GENAI_USE_VERTEXAI" not in os.environ:
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true"

A2UI_EXTENSION_URI: str = "https://a2ui.org/a2a-extension/a2ui/v0.9"
A2UI_EXTENSION_URI_V0_9: str = "https://a2ui.org/a2a-extension/a2ui/v0.9"
COMPOSITE_CATALOG_ID: str = (
    "https://www.gstatic.com/vertexaisearch/a2ui/v0_9/gemini_enterprise_composite_catalog.json"
)

# ---------------------------------------------------------------------------
# Models (Gemini 3.8 Flash by default)
# ---------------------------------------------------------------------------
DEFAULT_MODEL: str = os.getenv("MODEL", "gemini-3.8-flash")

# Image model for generating incident sketches
IMAGE_GEN_MODEL: str = os.getenv("IMAGE_MODEL", "gemini-3.1-flash-image")
IMAGE_GEN_LOCATION: str = os.getenv("IMAGE_LOCATION", "global")

# ---------------------------------------------------------------------------
# Service & Agent URL
# ---------------------------------------------------------------------------
AGENT_URL: str = os.getenv("AGENT_URL", "http://127.0.0.1:8080")
# Optional in-app ID token check for hosting outside Cloud Run (VM, tunnel).
# On Cloud Run, deploy.sh keeps the service private and Cloud Run IAM does this.
ENFORCE_IAM_AUTH: bool = os.getenv("ENFORCE_IAM_AUTH", "false").lower() in ("true", "1", "yes")
ALLOWED_SERVICE_ACCOUNTS: list[str] = [
    s.strip() for s in os.getenv("ALLOWED_SERVICE_ACCOUNTS", "").split(",") if s.strip()
]


def build_vertex_model_name(model: str | None = None) -> str:
    """Return a fully-qualified Vertex AI model resource name or bare model ID."""
    model = model or DEFAULT_MODEL
    if GOOGLE_CLOUD_PROJECT:
        return (
            f"projects/{GOOGLE_CLOUD_PROJECT}"
            f"/locations/{GOOGLE_CLOUD_LOCATION}"
            f"/publishers/google/models/{model}"
        )
    return model


def extract_json_from_llm_response(text: str) -> str:
    """Strip optional markdown code fences from an LLM response."""
    text = text.strip()
    if "```json" in text:
        start = text.find("```json") + 7
        end = text.find("```", start)
        return text[start:end].strip()
    if "```" in text:
        start = text.find("```") + 3
        end = text.find("```", start)
        return text[start:end].strip()
    return text
