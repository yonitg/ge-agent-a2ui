"""Google IAM & OIDC ID Token Authentication Middleware for standalone/VM deployments."""

import logging
from collections.abc import Sequence

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)


class GoogleIAMAuthMiddleware(BaseHTTPMiddleware):
    """Starlette middleware to validate Google OIDC ID tokens.

    Validates that incoming requests from Gemini Enterprise Discovery Engine or
    authorized developers carry a valid Google-signed OIDC token in the
    ``Authorization: Bearer <token>`` header, minted for ``audience`` (the
    agent's own URL).
    """

    def __init__(
        self,
        app,
        allowed_service_accounts: Sequence[str] | None = None,
        public_paths: Sequence[str] | None = None,
        audience: str | Sequence[str] | None = None,
    ):
        super().__init__(app)
        self.allowed_service_accounts = set(allowed_service_accounts or [])
        self.public_paths = set(
            public_paths
            or [
                "/health",
                "/feedback",
                "/.well-known/agent-card.json",
                "/a2a/app/.well-known/agent-card.json",
                "/docs",
                "/openapi.json",
            ]
        )
        if isinstance(audience, str):
            self.audience = audience or None
        else:
            self.audience = list(audience or []) or None
        if not self.audience:
            logger.warning(
                "GoogleIAMAuthMiddleware has no audience: any Google-signed ID token will be accepted."
            )
        self._request_adapter = google_requests.Request()

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        # Always permit public discovery and health check endpoints
        if path in self.public_paths or path.rstrip("/") in self.public_paths:
            return await call_next(request)

        auth_header = request.headers.get("Authorization")
        if not auth_header:
            logger.warning("Rejected request to %s: Missing Authorization header", path)
            return JSONResponse(
                {"error": "Unauthorized: Missing Authorization header with Google OIDC Bearer token"},
                status_code=401,
            )

        parts = auth_header.split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            logger.warning("Rejected request to %s: Invalid Authorization header format", path)
            return JSONResponse(
                {"error": "Unauthorized: Authorization header must be in 'Bearer <token>' format"},
                status_code=401,
            )

        token = parts[1].strip()
        try:
            # Verify the signature and expiration with Google's public certs
            claims = id_token.verify_oauth2_token(
                token,
                self._request_adapter,
                audience=self.audience,
            )
        except Exception as e:
            logger.warning("Token verification failed for %s: %s", path, str(e))
            return JSONResponse(
                {"error": "Unauthorized: Google ID token verification failed"},
                status_code=401,
            )

        email = claims.get("email")
        if self.allowed_service_accounts and email not in self.allowed_service_accounts:
            logger.warning("Access forbidden: caller %s not in allowed list", email)
            return JSONResponse(
                {
                    "error": f"Forbidden: Service account or caller '{email}' is not permitted to access this agent."
                },
                status_code=403,
            )

        # Attach claims to request state for downstream handlers
        request.state.caller_email = email
        request.state.caller_claims = claims

        return await call_next(request)
