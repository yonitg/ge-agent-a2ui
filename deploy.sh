#!/bin/bash
# ==============================================================================
# One-Click Cloud Run Deployment for A2UI v0.9 ADK Incident Operations Agent
# ==============================================================================
#
# Usage: ./deploy.sh [PROJECT_ID] [SERVICE_NAME] [REGION] [MODEL_NAME]
#
# Optional environment variables:
#   GEMINI_ENTERPRISE_ENGINE_ID   Gemini Enterprise app ID. When set, the agent is
#                                 registered in that app after deployment.
#   GEMINI_ENTERPRISE_LOCATION    Location of that app: global (default), eu or us.
#   GEMINI_ENTERPRISE_PROJECT_ID  Project that hosts that app (default: PROJECT_ID).
#   MAX_INSTANCES                 Cloud Run max instances (default: 1).
#   ALLOW_UNAUTHENTICATED         Set to "true" to make the service public (default: private).

set -e

# Styling
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${BLUE}======================================================${NC}"
echo -e "${BLUE}   A2UI v0.9 ADK Agent - Cloud Run Installer          ${NC}"
echo -e "${BLUE}======================================================${NC}"

# 1. Check prerequisites
if ! command -v gcloud &> /dev/null; then
    echo -e "${RED}Error: 'gcloud' CLI is not installed or not in PATH.${NC}"
    echo "Please install Google Cloud SDK: https://cloud.google.com/sdk/docs/install"
    exit 1
fi

# 2. Determine Project ID
PROJECT_ID="${1:-$(gcloud config get-value project 2>/dev/null)}"
if [ -z "$PROJECT_ID" ] || [ "$PROJECT_ID" = "(unset)" ]; then
    echo -e "${YELLOW}No default Google Cloud project found in gcloud config.${NC}"
    read -rp "Enter your Google Cloud Project ID: " PROJECT_ID
fi

if [ -z "$PROJECT_ID" ]; then
    echo -e "${RED}Project ID cannot be empty.${NC}"
    exit 1
fi

SERVICE_NAME="${2:-a2ui-adk-agent}"
REGION="${3:-us-central1}"
MODEL_NAME="${4:-gemini-3.8-flash}"
# A single instance keeps the demo's in-memory sessions and report store
# consistent, and caps Vertex AI spend. Raise it only with shared storage.
MAX_INSTANCES="${MAX_INSTANCES:-1}"
GE_PROJECT_ID="${GEMINI_ENTERPRISE_PROJECT_ID:-$PROJECT_ID}"

# Private by default: only principals with roles/run.invoker on the service
# (you, and the Gemini Enterprise service agent granted below) can call it.
if [ "${ALLOW_UNAUTHENTICATED:-false}" = "true" ]; then
    AUTH_FLAG="--allow-unauthenticated"
    ACCESS_MODE="public (ALLOW_UNAUTHENTICATED=true)"
else
    AUTH_FLAG="--no-allow-unauthenticated"
    ACCESS_MODE="private (IAM authentication required)"
fi

echo -e "\nTarget Configuration:"
echo -e "  • Project ID:    ${GREEN}$PROJECT_ID${NC}"
echo -e "  • Service Name:  ${GREEN}$SERVICE_NAME${NC}"
echo -e "  • Region:        ${GREEN}$REGION${NC}"
echo -e "  • Gemini Model:  ${GREEN}$MODEL_NAME${NC}"
echo -e "  • Access:        ${GREEN}$ACCESS_MODE${NC}"
echo -e "  • Max Instances: ${GREEN}$MAX_INSTANCES${NC}"

# 3. Enable Required Google Cloud APIs
echo -e "\n${BLUE}Step 1/4: Enabling required Google Cloud APIs...${NC}"
gcloud services enable \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    aiplatform.googleapis.com \
    artifactregistry.googleapis.com \
    --project "$PROJECT_ID"

# 4. Deploy to Cloud Run from Source
echo -e "\n${BLUE}Step 2/4: Building and deploying container to Cloud Run...${NC}"
gcloud run deploy "$SERVICE_NAME" \
    --source . \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --memory "2Gi" \
    --cpu "1" \
    --min-instances "0" \
    --max-instances "$MAX_INSTANCES" \
    "$AUTH_FLAG" \
    --set-env-vars=GOOGLE_CLOUD_PROJECT="$PROJECT_ID",GOOGLE_CLOUD_LOCATION="global",GOOGLE_GENAI_USE_VERTEXAI=TRUE,MODEL="$MODEL_NAME" \
    --quiet

# 5. Retrieve Service URL & Update AGENT_URL
echo -e "\n${BLUE}Step 3/4: Finalizing agent configuration...${NC}"
SERVICE_URL=$(gcloud run services describe "$SERVICE_NAME" \
    --platform managed \
    --region "$REGION" \
    --project "$PROJECT_ID" \
    --format 'value(status.url)')

gcloud run services update "$SERVICE_NAME" \
    --platform managed \
    --region "$REGION" \
    --project "$PROJECT_ID" \
    --update-env-vars AGENT_URL="$SERVICE_URL" \
    --quiet

# Let Gemini Enterprise call the private service: grant Cloud Run Invoker to the
# Discovery Engine service agent of the project that hosts the Gemini Enterprise app.
GE_PROJECT_NUMBER=$(gcloud projects describe "$GE_PROJECT_ID" --format='value(projectNumber)')
GE_SERVICE_AGENT="service-${GE_PROJECT_NUMBER}@gcp-sa-discoveryengine.iam.gserviceaccount.com"
echo -e "Granting Cloud Run Invoker to the Gemini Enterprise service agent (${GE_SERVICE_AGENT})..."
if ! gcloud run services add-iam-policy-binding "$SERVICE_NAME" \
    --platform managed \
    --region "$REGION" \
    --project "$PROJECT_ID" \
    --member="serviceAccount:${GE_SERVICE_AGENT}" \
    --role="roles/run.invoker" \
    --quiet > /dev/null; then
    echo -e "${YELLOW}Warning: could not grant roles/run.invoker to ${GE_SERVICE_AGENT}.${NC}"
    echo -e "${YELLOW}Gemini Enterprise gets HTTP 403 from the agent until this binding exists (see README).${NC}"
fi

# 6. (Optional) Register with Gemini Enterprise via Discovery Engine API
#    Set GEMINI_ENTERPRISE_ENGINE_ID (and GEMINI_ENTERPRISE_LOCATION if your app
#    is not in "global", e.g. "eu" or "us") to register automatically.
echo -e "\n${BLUE}Step 4/4: Registering agent into Google Gemini Enterprise...${NC}"
ENGINE_ID="${GEMINI_ENTERPRISE_ENGINE_ID:-}"
ENGINE_LOCATION="${GEMINI_ENTERPRISE_LOCATION:-global}"
if [ -z "$ENGINE_ID" ]; then
    echo -e "${YELLOW}GEMINI_ENTERPRISE_ENGINE_ID is not set - skipping automatic registration.${NC}"
    echo -e "To register later, run:"
    echo -e "  python3 register_gemini_enterprise.py --project \"$GE_PROJECT_ID\" \\\\"
    echo -e "      --service-url \"$SERVICE_URL\" --engine <YOUR_ENGINE_ID> --location <global|eu|us>"
else
    if [ -f ".venv/bin/python3" ]; then
        PYTHON_BIN=".venv/bin/python3"
    else
        PYTHON_BIN="python3"
    fi
    "$PYTHON_BIN" register_gemini_enterprise.py \
        --project "$GE_PROJECT_ID" \
        --service-url "$SERVICE_URL" \
        --engine "$ENGINE_ID" \
        --location "$ENGINE_LOCATION" \
        --display-name "A2UI 0.9 ADK Agent"
fi

echo -e "\n${GREEN}======================================================${NC}"
echo -e "${GREEN}   Deployment Complete!                               ${NC}"
echo -e "${GREEN}======================================================${NC}"
echo -e "Service URL:       ${BLUE}${SERVICE_URL}${NC}"
echo -e "Access:            ${BLUE}${ACCESS_MODE}${NC}"
echo -e "Health Check:      ${BLUE}${SERVICE_URL}/health${NC}"
echo -e "Agent Card:        ${BLUE}${SERVICE_URL}/.well-known/agent-card.json${NC}"
echo -e "A2A App Endpoint:  ${BLUE}${SERVICE_URL}/${NC}"

echo -e "\nVerification Checks:"
if [ "$AUTH_FLAG" = "--allow-unauthenticated" ]; then
    echo -e "  curl -s ${SERVICE_URL}/health"
    echo -e "  curl -s ${SERVICE_URL}/.well-known/agent-card.json | head -c 400; echo\n"
else
    echo -e "  curl -s -H \"Authorization: Bearer \$(gcloud auth print-identity-token)\" ${SERVICE_URL}/health"
    echo -e "  curl -s -H \"Authorization: Bearer \$(gcloud auth print-identity-token)\" ${SERVICE_URL}/.well-known/agent-card.json | head -c 400; echo\n"
fi
