"""Unit tests for GoogleIAMAuthMiddleware."""

import logging
from unittest.mock import patch

from google.auth.transport import requests as google_requests
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.auth_middleware import GoogleIAMAuthMiddleware


async def mock_endpoint(request):
    return JSONResponse({"status": "ok", "caller": getattr(request.state, "caller_email", None)})


def create_test_app(allowed_service_accounts=None, audience=None):
    routes = [
        Route("/health", mock_endpoint, methods=["GET"]),
        Route("/.well-known/agent-card.json", mock_endpoint, methods=["GET"]),
        Route("/run_sse", mock_endpoint, methods=["POST"]),
    ]
    middleware = [
        Middleware(
            GoogleIAMAuthMiddleware,
            allowed_service_accounts=allowed_service_accounts,
            audience=audience,
        )
    ]
    return Starlette(routes=routes, middleware=middleware)


def test_public_endpoints_allowed_without_auth():
    app = create_test_app(allowed_service_accounts=["test-sa@example.com"])
    client = TestClient(app)

    res_health = client.get("/health")
    assert res_health.status_code == 200
    assert res_health.json() == {"status": "ok", "caller": None}

    res_card = client.get("/.well-known/agent-card.json")
    assert res_card.status_code == 200


def test_protected_endpoint_missing_auth_header():
    app = create_test_app(allowed_service_accounts=["test-sa@example.com"])
    client = TestClient(app)

    res = client.post("/run_sse")
    assert res.status_code == 401
    assert "Missing Authorization header" in res.json()["error"]


def test_protected_endpoint_invalid_bearer_format():
    app = create_test_app(allowed_service_accounts=["test-sa@example.com"])
    client = TestClient(app)

    res = client.post("/run_sse", headers={"Authorization": "Basic 12345"})
    assert res.status_code == 401
    assert "Bearer <token>" in res.json()["error"]


@patch("google.oauth2.id_token.verify_oauth2_token")
def test_protected_endpoint_valid_token_allowed_account(mock_verify):
    mock_verify.return_value = {
        "email": "service-12345@gcp-sa-discoveryengine.iam.gserviceaccount.com",
        "iss": "https://accounts.google.com",
    }
    app = create_test_app(
        allowed_service_accounts=["service-12345@gcp-sa-discoveryengine.iam.gserviceaccount.com"]
    )
    client = TestClient(app)

    res = client.post("/run_sse", headers={"Authorization": "Bearer valid-mock-token"})
    assert res.status_code == 200
    assert res.json()["caller"] == "service-12345@gcp-sa-discoveryengine.iam.gserviceaccount.com"


@patch("google.oauth2.id_token.verify_oauth2_token")
def test_protected_endpoint_disallowed_account(mock_verify):
    mock_verify.return_value = {
        "email": "unauthorized-sa@example.com",
        "iss": "https://accounts.google.com",
    }
    app = create_test_app(
        allowed_service_accounts=["service-12345@gcp-sa-discoveryengine.iam.gserviceaccount.com"]
    )
    client = TestClient(app)

    res = client.post("/run_sse", headers={"Authorization": "Bearer valid-mock-token"})
    assert res.status_code == 403
    assert "Forbidden" in res.json()["error"]


@patch("google.oauth2.id_token.verify_oauth2_token")
def test_protected_endpoint_invalid_token_signature(mock_verify):
    mock_verify.side_effect = ValueError("Token has expired")
    app = create_test_app(allowed_service_accounts=["test-sa@example.com"])
    client = TestClient(app)

    res = client.post("/run_sse", headers={"Authorization": "Bearer expired-token"})
    assert res.status_code == 401
    assert "verification failed" in res.json()["error"]


@patch("google.oauth2.id_token.verify_oauth2_token")
def test_token_audience_is_the_agent_url(mock_verify):
    """Tokens minted for another service must not be accepted."""
    mock_verify.return_value = {"email": "test-sa@example.com"}
    audience = ["https://agent.example.com", "https://agent.example.com/"]
    app = create_test_app(allowed_service_accounts=["test-sa@example.com"], audience=audience)
    client = TestClient(app)

    res = client.post("/run_sse", headers={"Authorization": "Bearer valid-mock-token"})

    assert res.status_code == 200
    assert mock_verify.call_args.kwargs["audience"] == audience


@patch("google.oauth2.id_token.verify_oauth2_token")
def test_verification_error_details_are_not_returned(mock_verify):
    mock_verify.side_effect = ValueError(
        "Token has wrong audience https://internal.example, expected one of [...]"
    )
    app = create_test_app(audience="https://agent.example.com")
    client = TestClient(app)

    res = client.post("/run_sse", headers={"Authorization": "Bearer other-service-token"})

    assert res.status_code == 401
    assert res.json() == {"error": "Unauthorized: Google ID token verification failed"}


class _EmptyCertsResponse:
    status = 200
    data = b"{}"


def test_rejected_token_is_not_logged(monkeypatch, caplog):
    """google-auth copies a malformed token into its error text; the log line must not.

    The realistic case is an OAuth access token sent instead of an ID token.
    """
    monkeypatch.setattr(
        google_requests, "Request", lambda: lambda *args, **kwargs: _EmptyCertsResponse()
    )
    # Stand-in for an OAuth access token: one dot, so it is not a JWT.
    access_token = "example-access-token.not-an-id-token"
    client = TestClient(create_test_app(audience="https://agent.example.com"))

    with caplog.at_level(logging.WARNING, logger="app.auth_middleware"):
        res = client.post("/run_sse", headers={"Authorization": f"Bearer {access_token}"})

    assert res.status_code == 401
    assert "MalformedError" in caplog.text
    assert "[redacted]" in caplog.text
    assert access_token not in caplog.text


@patch("google.oauth2.id_token.verify_oauth2_token")
def test_rejection_reason_is_logged(mock_verify, caplog):
    """The reason (wrong audience, expired, ...) stays in the log for debugging."""
    mock_verify.side_effect = ValueError("Token has wrong audience https://other.example")
    client = TestClient(create_test_app(audience="https://agent.example.com"))

    with caplog.at_level(logging.WARNING, logger="app.auth_middleware"):
        client.post("/run_sse", headers={"Authorization": "Bearer other-service-token"})

    assert "ValueError: Token has wrong audience https://other.example" in caplog.text
