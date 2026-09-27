"""Unit tests for the Gemini Enterprise registration script."""

import base64
import io
import json
import urllib.error
from unittest.mock import MagicMock

import pytest

import register_gemini_enterprise as reg
from app.agent import agent_icon_data_uri


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://a2ui-adk-agent-abc123-uc.a.run.app", True),
        ("https://a2ui-adk-agent-123456789.us-central1.run.app/", True),
        ("http://a2ui-adk-agent-abc123-uc.a.run.app", False),
        ("https://agent.example.com", False),
        ("https://evilrun.app", False),
        ("https://svc.run.app.example.com", False),
        ("https://svc.run.app@example.com", False),
    ],
)
def test_is_cloud_run_url(url, expected):
    assert reg.is_cloud_run_url(url) is expected


def _fake_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.__enter__.return_value = resp
    return resp


def test_agent_card_fetch_sends_identity_token_to_cloud_run_only(monkeypatch):
    monkeypatch.setattr(reg, "get_identity_token", lambda: "id-token-123")
    sent = []

    def fake_urlopen(req, timeout):
        sent.append(req)
        return _fake_response({"name": "card"})

    monkeypatch.setattr(reg.urllib.request, "urlopen", fake_urlopen)

    assert reg.get_agent_card("https://svc-abc-uc.a.run.app/") == {"name": "card"}
    reg.get_agent_card("https://agent.example.com")

    assert sent[0].full_url == "https://svc-abc-uc.a.run.app/.well-known/agent-card.json"
    assert sent[0].get_header("Authorization") == "Bearer id-token-123"
    assert sent[1].get_header("Authorization") is None


def test_agent_card_fetch_explains_a_forbidden_private_service(monkeypatch, capsys):
    monkeypatch.setattr(reg, "get_identity_token", lambda: None)

    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(b""))

    monkeypatch.setattr(reg.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(SystemExit):
        reg.get_agent_card("https://svc-abc-uc.a.run.app")
    assert "roles/run.invoker" in capsys.readouterr().err


def test_build_icon_prefers_explicit_url():
    card = {"iconUrl": "data:image/png;base64,QUJD"}
    assert reg.build_icon("https://example.com/icon.png", card) == {
        "uri": "https://example.com/icon.png"
    }


def test_build_icon_uses_card_https_url():
    card = {"iconUrl": "https://example.com/icon.png"}
    assert reg.build_icon(None, card) == {"uri": "https://example.com/icon.png"}


def test_build_icon_falls_back_to_bundled_png():
    icon = reg.build_icon(None, {})
    assert icon["uri"].startswith("data:image/png;base64,")
    assert base64.b64decode(icon["uri"].split(",", 1)[1]).startswith(b"\x89PNG")


def test_agent_card_icon_is_bundled_and_registered_inline():
    """The agent's icon ships in the container; no external image hosting.

    It is registered as a data URI in icon.uri: the Gemini Enterprise UI shows
    icon.content as a letter avatar instead of the image.
    """
    icon_url = agent_icon_data_uri()
    assert icon_url.startswith("data:image/png;base64,")

    icon = reg.build_icon(None, {"iconUrl": icon_url})

    with open(reg.BUNDLED_ICON_PATH, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("ascii")
    assert icon == {"uri": f"data:image/png;base64,{encoded}"}
    assert "content" not in icon


def test_delete_requires_a_display_name(monkeypatch):
    """An empty name would match, and delete, every agent in the app."""
    monkeypatch.setattr(reg, "get_gcloud_token", MagicMock(side_effect=AssertionError))

    with pytest.raises(SystemExit):
        reg.delete_agent(project_id="p", engine_id="e", display_name="")
