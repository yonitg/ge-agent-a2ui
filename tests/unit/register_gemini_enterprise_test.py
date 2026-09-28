"""Unit tests for the Gemini Enterprise registration script."""

import base64
import io
import json
import sys
import urllib.error
import urllib.request
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

    def fake_open(req, timeout):
        sent.append(req)
        return _fake_response({"name": "card"})

    monkeypatch.setattr(reg._HTTPS_OPENER, "open", fake_open)

    assert reg.get_agent_card("https://svc-abc-uc.a.run.app/") == {"name": "card"}
    reg.get_agent_card("https://agent.example.com")

    assert sent[0].full_url == "https://svc-abc-uc.a.run.app/.well-known/agent-card.json"
    assert sent[0].get_header("Authorization") == "Bearer id-token-123"
    assert sent[1].get_header("Authorization") is None


def test_agent_card_fetch_explains_a_forbidden_private_service(monkeypatch, capsys):
    monkeypatch.setattr(reg, "get_identity_token", lambda: None)

    def fake_open(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(b""))

    monkeypatch.setattr(reg._HTTPS_OPENER, "open", fake_open)

    with pytest.raises(SystemExit):
        reg.get_agent_card("https://svc-abc-uc.a.run.app")
    assert "roles/run.invoker" in capsys.readouterr().err


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://svc-abc-uc.a.run.app/"])
def test_requests_only_go_over_https(url):
    """Unlike urlopen, the opener has no file:// or plain-http handler."""
    with pytest.raises(urllib.error.URLError, match="unknown url type"):
        reg._HTTPS_OPENER.open(url, timeout=5)


def test_requests_do_not_follow_redirects():
    """A followed redirect would carry the Authorization header to another host."""
    handler_types = {type(handler) for handler in reg._HTTPS_OPENER.handlers}
    assert urllib.request.HTTPRedirectHandler not in handler_types


@pytest.mark.parametrize(
    ("location", "endpoint"),
    [
        ("global", "https://discoveryengine.googleapis.com"),
        ("eu", "https://eu-discoveryengine.googleapis.com"),
        ("us", "https://us-discoveryengine.googleapis.com"),
    ],
)
def test_api_endpoint(location, endpoint):
    assert reg.get_api_endpoint(location) == endpoint


@pytest.mark.parametrize(
    "location", ["attacker.example/x?", "attacker.example#", "attacker.example:443/"]
)
def test_api_endpoint_rejects_a_location_that_changes_the_host(location):
    """The gcloud access token must only ever go to the Discovery Engine API."""
    with pytest.raises(ValueError, match="invalid Gemini Enterprise location"):
        reg.get_api_endpoint(location)


@pytest.mark.parametrize(
    "args",
    [
        ["--service-url", "file:///etc/passwd"],
        ["--service-url", "http://svc-abc-uc.a.run.app"],
        ["--service-url", "https://svc-abc-uc.a.run.app", "--location", "attacker.example/x?"],
    ],
)
def test_cli_rejects_unsafe_urls_before_fetching_a_token(monkeypatch, capsys, args):
    monkeypatch.setattr(reg, "get_gcloud_token", MagicMock(side_effect=AssertionError))
    argv = ["register_gemini_enterprise.py", "--project", "p", "--engine", "e", *args]
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(SystemExit) as exc:
        reg.main()

    assert exc.value.code == 2
    assert "invalid" in capsys.readouterr().err


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
