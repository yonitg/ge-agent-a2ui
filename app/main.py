"""Main entry point for the A2A A2UI maintenance report agent."""

import os

import uvicorn
from a2a.server import tasks
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.agent import ReportAgent
from app.agent_executor import ReportExecutor
from app.auth_middleware import GoogleIAMAuthMiddleware
from app.config import (
    AGENT_URL,
    ALLOWED_SERVICE_ACCOUNTS,
    ENFORCE_IAM_AUTH,
)

# 1. Create the Agent, AgentCard, RequestHandler, and App.
agent = ReportAgent(base_url=AGENT_URL)
agent_card = agent.agent_card

executor = ReportExecutor(base_url=AGENT_URL, agent=agent)

request_handler = DefaultRequestHandler(
    agent_executor=executor,
    task_store=tasks.InMemoryTaskStore(),
)

# 2. The Functions Framework will automatically look for this 'app' variable.
app = A2AStarletteApplication(
    agent_card=agent_card,
    http_handler=request_handler,
).build()


async def feedback_handler(request):
    """Dummy feedback handler for tests."""
    return JSONResponse({"status": "ok"})


async def health_handler(request):
    """Health check endpoint for load balancers and container probes."""
    return JSONResponse({"status": "healthy", "service": "a2ui-adk-agent"})


app.routes.append(Route("/feedback", feedback_handler, methods=["POST"]))
app.routes.append(Route("/health", health_handler, methods=["GET"]))

# 3. Optional Google IAM / OIDC Auth Middleware (for standalone VM or tunnel).
#    On Cloud Run keep the service private instead (deploy.sh does): Cloud Run
#    IAM checks the token Gemini Enterprise sends in X-Serverless-Authorization.
if ENFORCE_IAM_AUTH or ALLOWED_SERVICE_ACCOUNTS:
    _audience = AGENT_URL.rstrip("/")
    app.add_middleware(
        GoogleIAMAuthMiddleware,
        allowed_service_accounts=ALLOWED_SERVICE_ACCOUNTS,
        # Only accept ID tokens minted for this service's URL.
        audience=[_audience, f"{_audience}/"],
    )

# 4. CORS: restrict to known local dev origins plus the deployed agent URL.
_cors_origins = [
    "http://localhost:8000",
    "http://localhost:8080",
    "http://127.0.0.1:8000",
    "http://127.0.0.1:8080",
]
if AGENT_URL and AGENT_URL not in _cors_origins:
    _cors_origins.append(AGENT_URL)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
