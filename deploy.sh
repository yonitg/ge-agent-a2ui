#!/bin/bash
# ==============================================================================
# One-Click Cloud Run Deployment for A2UI v0.9 ADK Incident Operations Agent
# ==============================================================================
#
# Usage: ./deploy.sh [PROJECT_ID] [SERVICE_NAME] [REGION] [MODEL_NAME]
#
# Works from any directory, e.g. `bash path/to/deploy.sh my-project`.
#
# Optional environment variables:
#   GEMINI_ENTERPRISE_ENGINE_ID     Gemini Enterprise app ID. When set, the agent is
#                                   registered in that app after deployment.
#   GEMINI_ENTERPRISE_LOCATION      Location of that app: global (default), eu or us.
#   GEMINI_ENTERPRISE_PROJECT_ID    Project that hosts that app (default: PROJECT_ID).
#   GEMINI_ENTERPRISE_DISPLAY_NAME  Agent name in Gemini Enterprise (default: "A2UI 0.9 ADK Agent").
#   RUNTIME_SERVICE_ACCOUNT         Identity the service runs as. An account ID (default:
#                                   a2ui-agent-runtime) is created in PROJECT_ID if missing;
#                                   a full email address uses that existing account.
#   MAX_INSTANCES                   Cloud Run max instances (default: 1).
#   ALLOW_UNAUTHENTICATED           Set to "true" to make the service public (default: private).

set -euo pipefail

# Work from the repository root (this script's directory), wherever it is called from.
cd "$(dirname "${BASH_SOURCE[0]:-$0}")"

# Styling (plain text when the output is not a terminal, e.g. in CI logs)
if [ -t 1 ]; then
    GREEN='\033[0;32m'
    BLUE='\033[0;34m'
    YELLOW='\033[1;33m'
    RED='\033[0;31m'
    NC='\033[0m' # No Color
else
    GREEN=''
    BLUE=''
    YELLOW=''
    RED=''
    NC=''
fi

# Print an error (echo -e escapes such as \n are allowed) and stop.
fail() {
    echo -e "${RED}Error:${NC} $1" >&2
    exit 1
}

warn() {
    echo -e "${YELLOW}Warning: $1${NC}"
}

echo -e "${BLUE}======================================================${NC}"
echo -e "${BLUE}   A2UI v0.9 ADK Agent - Cloud Run Installer          ${NC}"
echo -e "${BLUE}======================================================${NC}"

# 1. Check prerequisites
if ! command -v gcloud &> /dev/null; then
    echo -e "${RED}Error: 'gcloud' CLI is not installed or not in PATH.${NC}"
    echo "Please install Google Cloud SDK: https://cloud.google.com/sdk/docs/install"
    exit 1
fi
if [ ! -f Dockerfile ] || [ ! -d app ]; then
    fail "Dockerfile or app/ not found in $(pwd). Run deploy.sh from a complete copy of the repository."
fi

# 2. Determine Project ID
PROJECT_ID="${1:-$(gcloud config get-value project 2> /dev/null || true)}"
if [ -z "$PROJECT_ID" ] || [ "$PROJECT_ID" = "(unset)" ]; then
    echo -e "${YELLOW}No default Google Cloud project found in gcloud config.${NC}"
    PROJECT_ID=""
    read -rp "Enter your Google Cloud Project ID: " PROJECT_ID || true
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
ENGINE_ID="${GEMINI_ENTERPRISE_ENGINE_ID:-}"
ENGINE_LOCATION="${GEMINI_ENTERPRISE_LOCATION:-global}"
GE_DISPLAY_NAME="${GEMINI_ENTERPRISE_DISPLAY_NAME:-A2UI 0.9 ADK Agent}"

case "$MAX_INSTANCES" in
    '' | *[!0-9]* | 0) fail "MAX_INSTANCES must be a positive whole number (got '${MAX_INSTANCES}')." ;;
esac

# The service runs as its own service account that can only call Vertex AI,
# rather than as the default compute service account, which often has Editor.
RUNTIME_SA="${RUNTIME_SERVICE_ACCOUNT:-a2ui-agent-runtime}"
case "$RUNTIME_SA" in
    *@*)
        RUNTIME_SA_EMAIL="$RUNTIME_SA"
        MANAGE_RUNTIME_SA=false
        ;;
    *)
        RUNTIME_SA_EMAIL="${RUNTIME_SA}@${PROJECT_ID}.iam.gserviceaccount.com"
        MANAGE_RUNTIME_SA=true
        ;;
esac

# Private by default: only principals with roles/run.invoker on the service
# (you, and the Gemini Enterprise service agent granted below) can call it.
if [ "${ALLOW_UNAUTHENTICATED:-false}" = "true" ]; then
    AUTH_FLAG="--allow-unauthenticated"
    ACCESS_MODE="public (ALLOW_UNAUTHENTICATED=true)"
else
    AUTH_FLAG="--no-allow-unauthenticated"
    ACCESS_MODE="private (IAM authentication required)"
fi

# 3. Preflight: check the login, project access and local tools before changing anything
echo -e "\nRunning preflight checks..."
ACTIVE_ACCOUNT="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2> /dev/null || true)"
if [ -z "$ACTIVE_ACCOUNT" ]; then
    fail "No active gcloud account. Run 'gcloud auth login' (in CI: 'gcloud auth activate-service-account') and try again."
fi

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)' 2> /dev/null || true)"
if [ -z "$PROJECT_NUMBER" ]; then
    fail "Cannot access project '${PROJECT_ID}' as ${ACTIVE_ACCOUNT}.\n  Check the project ID and your permissions, or run 'gcloud auth login' if your login expired."
fi

# The Gemini Enterprise service agent is named after the number of the project that hosts the app.
if [ "$GE_PROJECT_ID" = "$PROJECT_ID" ]; then
    GE_PROJECT_NUMBER="$PROJECT_NUMBER"
else
    GE_PROJECT_NUMBER="$(gcloud projects describe "$GE_PROJECT_ID" --format='value(projectNumber)' 2> /dev/null || true)"
    if [ -z "$GE_PROJECT_NUMBER" ]; then
        fail "Cannot access the Gemini Enterprise project '${GE_PROJECT_ID}' (GEMINI_ENTERPRISE_PROJECT_ID) as ${ACTIVE_ACCOUNT}."
    fi
fi

PYTHON_BIN=""
if [ -n "$ENGINE_ID" ]; then
    case "$ENGINE_LOCATION" in
        global | eu | us) ;;
        *) fail "GEMINI_ENTERPRISE_LOCATION must be global, eu or us (got '${ENGINE_LOCATION}')." ;;
    esac
    if [ -x ".venv/bin/python3" ]; then
        PYTHON_BIN=".venv/bin/python3"
    elif command -v python3 &> /dev/null; then
        PYTHON_BIN="python3"
    else
        fail "python3 is needed to register the agent in Gemini Enterprise (GEMINI_ENTERPRISE_ENGINE_ID is set).\n  Install Python 3, or unset GEMINI_ENTERPRISE_ENGINE_ID to deploy without registering."
    fi
fi

echo -e "\nTarget Configuration:"
echo -e "  • Project ID:      ${GREEN}$PROJECT_ID${NC} ($PROJECT_NUMBER)"
echo -e "  • Deploying as:    ${GREEN}$ACTIVE_ACCOUNT${NC}"
echo -e "  • Service Name:    ${GREEN}$SERVICE_NAME${NC}"
echo -e "  • Region:          ${GREEN}$REGION${NC}"
echo -e "  • Gemini Model:    ${GREEN}$MODEL_NAME${NC}"
echo -e "  • Runtime Account: ${GREEN}$RUNTIME_SA_EMAIL${NC}"
echo -e "  • Access:          ${GREEN}$ACCESS_MODE${NC}"
echo -e "  • Max Instances:   ${GREEN}$MAX_INSTANCES${NC}"
if [ -n "$ENGINE_ID" ]; then
    echo -e "  • Register in:     ${GREEN}$ENGINE_ID${NC} ($ENGINE_LOCATION, project $GE_PROJECT_ID)"
fi

# 4. Enable Required Google Cloud APIs
echo -e "\n${BLUE}Step 1/6: Enabling required Google Cloud APIs...${NC}"
if ! gcloud services enable \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    artifactregistry.googleapis.com \
    aiplatform.googleapis.com \
    iam.googleapis.com \
    --project "$PROJECT_ID"; then
    warn "could not enable the APIs above. Continuing in case they are already enabled;\n  otherwise ask a project admin to enable them."
fi

# 5. Runtime service account with only Vertex AI User
echo -e "\n${BLUE}Step 2/6: Preparing the runtime service account...${NC}"
SA_CREATED=false
if gcloud iam service-accounts describe "$RUNTIME_SA_EMAIL" \
    --project "$PROJECT_ID" \
    --format='value(email)' &> /dev/null; then
    echo "Using existing service account ${RUNTIME_SA_EMAIL}."
elif [ "$MANAGE_RUNTIME_SA" = "true" ]; then
    echo "Creating service account ${RUNTIME_SA_EMAIL}..."
    if ! gcloud iam service-accounts create "$RUNTIME_SA" \
        --project "$PROJECT_ID" \
        --display-name "A2UI agent runtime" \
        --description "Runtime identity of the ${SERVICE_NAME} Cloud Run service (Vertex AI User only)" \
        --quiet; then
        fail "Could not create service account ${RUNTIME_SA_EMAIL}.\n  Creating it needs roles/iam.serviceAccountAdmin, and the org policy iam.disableServiceAccountCreation must allow it.\n  Alternatively set RUNTIME_SERVICE_ACCOUNT to the email of an existing service account that has roles/aiplatform.user."
    fi
    SA_CREATED=true
else
    fail "Service account ${RUNTIME_SA_EMAIL} (RUNTIME_SERVICE_ACCOUNT) does not exist, or ${ACTIVE_ACCOUNT} cannot access it."
fi

# True if the account holds roles/aiplatform.user directly on the project.
has_vertex_ai_user() {
    local members
    members="$(gcloud projects get-iam-policy "$PROJECT_ID" \
        --flatten='bindings[].members' \
        --filter='bindings.role=roles/aiplatform.user' \
        --format='value(bindings.members)' 2> /dev/null)" || return 1
    grep -Fxqi "serviceAccount:${RUNTIME_SA_EMAIL}" <<< "$members"
}

if has_vertex_ai_user; then
    echo "Vertex AI User (roles/aiplatform.user) is already granted."
else
    echo "Granting Vertex AI User (roles/aiplatform.user) on project ${PROJECT_ID}..."
    GRANTED=false
    GRANT_ERROR=""
    for attempt in 1 2 3 4 5 6; do
        if GRANT_ERROR="$(gcloud projects add-iam-policy-binding "$PROJECT_ID" \
            --member="serviceAccount:${RUNTIME_SA_EMAIL}" \
            --role="roles/aiplatform.user" \
            --condition=None \
            --quiet 2>&1 > /dev/null)"; then
            GRANTED=true
            break
        fi
        # A new service account can take up to a minute to become visible to IAM.
        if [ "$SA_CREATED" != "true" ] || [ "$attempt" -eq 6 ]; then
            break
        fi
        echo "Waiting for the new service account to propagate (attempt ${attempt}/6)..."
        sleep 10
    done
    if [ "$GRANTED" != "true" ]; then
        if [ -n "$GRANT_ERROR" ]; then
            echo "$GRANT_ERROR" >&2
        fi
        if [ "$MANAGE_RUNTIME_SA" = "true" ]; then
            fail "Could not grant roles/aiplatform.user to ${RUNTIME_SA_EMAIL}; the agent cannot call Gemini without it.\n  Ask a project admin to run:\n    gcloud projects add-iam-policy-binding ${PROJECT_ID} --member=serviceAccount:${RUNTIME_SA_EMAIL} --role=roles/aiplatform.user --condition=None\n  and then run this script again."
        fi
        warn "could not grant roles/aiplatform.user to ${RUNTIME_SA_EMAIL}.\n  Continuing: make sure it has Vertex AI User some other way (for example through a group)."
    fi
fi

# 6. Deploy to Cloud Run from Source
echo -e "\n${BLUE}Step 3/6: Building and deploying container to Cloud Run...${NC}"
# --set-env-vars replaces every variable, so pass the current AGENT_URL along on
# redeploys. On the first deploy it is set in step 4, once the URL is known.
EXISTING_URL="$(gcloud run services describe "$SERVICE_NAME" \
    --platform managed \
    --region "$REGION" \
    --project "$PROJECT_ID" \
    --format 'value(status.url)' 2> /dev/null || true)"
ENV_VARS="GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=global,GOOGLE_GENAI_USE_VERTEXAI=TRUE,MODEL=${MODEL_NAME}"
if [ -n "$EXISTING_URL" ]; then
    ENV_VARS="${ENV_VARS},AGENT_URL=${EXISTING_URL}"
fi

if ! gcloud run deploy "$SERVICE_NAME" \
    --source . \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --service-account "$RUNTIME_SA_EMAIL" \
    --memory "2Gi" \
    --cpu "1" \
    --min-instances "0" \
    --max-instances "$MAX_INSTANCES" \
    "$AUTH_FLAG" \
    --set-env-vars="$ENV_VARS" \
    --quiet; then
    fail "Deployment failed; see the error above. Common causes:\n  • Build permissions. In newer projects the build runs as the default compute service account,\n    which needs the Cloud Run Builder role:\n      gcloud projects add-iam-policy-binding ${PROJECT_ID} --member=serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com --role=roles/run.builder --condition=None\n  • Deploying as ${RUNTIME_SA_EMAIL} needs iam.serviceAccounts.actAs on it (roles/iam.serviceAccountUser).\n  • The APIs from step 1 are not enabled."
fi

# 7. Retrieve Service URL & Update AGENT_URL
echo -e "\n${BLUE}Step 4/6: Finalizing agent configuration...${NC}"
SERVICE_URL="$(gcloud run services describe "$SERVICE_NAME" \
    --platform managed \
    --region "$REGION" \
    --project "$PROJECT_ID" \
    --format 'value(status.url)')"
if [ -z "$SERVICE_URL" ]; then
    fail "Could not read the URL of Cloud Run service ${SERVICE_NAME}."
fi

if [ "$SERVICE_URL" != "$EXISTING_URL" ]; then
    echo "Setting AGENT_URL=${SERVICE_URL}..."
    gcloud run services update "$SERVICE_NAME" \
        --platform managed \
        --region "$REGION" \
        --project "$PROJECT_ID" \
        --update-env-vars AGENT_URL="$SERVICE_URL" \
        --quiet
fi

# Let Gemini Enterprise call the private service: grant Cloud Run Invoker to the
# Discovery Engine service agent of the project that hosts the Gemini Enterprise app.
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

# 8. Smoke test: call /health and the agent card with your gcloud identity
echo -e "\n${BLUE}Step 5/6: Smoke-testing the deployed service...${NC}"
SMOKE_RESULT="skipped (curl not found)"

# Prints the HTTP status of GET ${SERVICE_URL}$1 (000 if unreachable); the body goes to $SMOKE_DIR/body.
http_status() {
    curl -s -o "$SMOKE_DIR/body" -w '%{http_code}' --max-time 30 \
        -H @"$SMOKE_DIR/auth_header" "${SERVICE_URL}$1" 2> /dev/null || true
}

if command -v curl &> /dev/null; then
    SMOKE_DIR="$(mktemp -d 2> /dev/null || mktemp -d -t a2ui-deploy)"
    trap 'rm -rf "$SMOKE_DIR"' EXIT
    # The ID token goes in a private header file, not on the curl command line.
    # Service accounts must name the service as audience; user accounts cannot.
    case "$ACTIVE_ACCOUNT" in
        *.gserviceaccount.com) ID_TOKEN="$(gcloud auth print-identity-token --audiences="$SERVICE_URL" 2> /dev/null || true)" ;;
        *) ID_TOKEN="$(gcloud auth print-identity-token 2> /dev/null || true)" ;;
    esac
    if [ -n "$ID_TOKEN" ]; then
        printf 'Authorization: Bearer %s\n' "$ID_TOKEN" > "$SMOKE_DIR/auth_header"
    else
        : > "$SMOKE_DIR/auth_header"
    fi
    ID_TOKEN=""

    # Retry while a new revision cold-starts.
    HEALTH_CODE="000"
    for attempt in 1 2 3 4 5 6; do
        HEALTH_CODE="$(http_status /health)"
        if [ "$HEALTH_CODE" = "200" ] || [ "$attempt" -eq 6 ]; then
            break
        fi
        sleep 5
    done

    case "$HEALTH_CODE" in
        200)
            echo -e "  /health:     ${GREEN}OK${NC}"
            CARD_CODE="$(http_status /.well-known/agent-card.json)"
            URL_PATTERN="${SERVICE_URL//./\\.}"
            if [ "$CARD_CODE" = "200" ] && grep -Eq "\"url\" *: *\"${URL_PATTERN}/?\"" "$SMOKE_DIR/body"; then
                echo -e "  agent card:  ${GREEN}OK${NC} (url = ${SERVICE_URL})"
                SMOKE_RESULT="passed"
            else
                echo -e "  agent card:  ${RED}FAILED${NC} (HTTP ${CARD_CODE}, or its url is not ${SERVICE_URL})"
                SMOKE_RESULT="failed"
            fi
            ;;
        401 | 403)
            warn "could not verify the service: HTTP ${HEALTH_CODE}. ${ACTIVE_ACCOUNT} may not invoke it\n  (needs run.routes.invoke, e.g. roles/run.invoker). Gemini Enterprise uses its own grant from step 4."
            SMOKE_RESULT="not verified (HTTP ${HEALTH_CODE})"
            ;;
        *)
            echo -e "  /health:     ${RED}FAILED${NC} (HTTP ${HEALTH_CODE})"
            SMOKE_RESULT="failed"
            ;;
    esac
else
    warn "curl not found - skipping the smoke test."
fi

if [ "$SMOKE_RESULT" = "failed" ]; then
    SKIPPED_NOTE=""
    if [ -n "$ENGINE_ID" ]; then
        SKIPPED_NOTE="\n  Gemini Enterprise registration was skipped; run this script again once the service is healthy."
    fi
    fail "The service was deployed but failed the smoke test. Check its logs:\n  https://console.cloud.google.com/run/detail/${REGION}/${SERVICE_NAME}/logs?project=${PROJECT_ID}${SKIPPED_NOTE}"
fi

# 9. (Optional) Register with Gemini Enterprise via Discovery Engine API
#    Set GEMINI_ENTERPRISE_ENGINE_ID (and GEMINI_ENTERPRISE_LOCATION if your app
#    is not in "global", e.g. "eu" or "us") to register automatically.
echo -e "\n${BLUE}Step 6/6: Registering agent into Google Gemini Enterprise...${NC}"
REGISTER_FAILED=false
if [ -z "$ENGINE_ID" ]; then
    echo -e "${YELLOW}GEMINI_ENTERPRISE_ENGINE_ID is not set - skipping automatic registration.${NC}"
    echo -e "To register later, run:"
    echo -e "  python3 register_gemini_enterprise.py --project \"$GE_PROJECT_ID\" \\\\"
    echo -e "      --service-url \"$SERVICE_URL\" --engine <YOUR_ENGINE_ID> --location <global|eu|us>"
elif ! "$PYTHON_BIN" register_gemini_enterprise.py \
    --project "$GE_PROJECT_ID" \
    --service-url "$SERVICE_URL" \
    --engine "$ENGINE_ID" \
    --location "$ENGINE_LOCATION" \
    --display-name "$GE_DISPLAY_NAME"; then
    REGISTER_FAILED=true
    warn "registering the agent in Gemini Enterprise failed (see above). The Cloud Run service is deployed;\n  fix the cause and run this script again."
fi

if [ "$REGISTER_FAILED" = "true" ]; then
    DONE_COLOR="$YELLOW"
    DONE_TITLE="   Deployed, but Gemini Enterprise registration failed"
else
    DONE_COLOR="$GREEN"
    DONE_TITLE="   Deployment Complete!                               "
fi
echo -e "\n${DONE_COLOR}======================================================${NC}"
echo -e "${DONE_COLOR}${DONE_TITLE}${NC}"
echo -e "${DONE_COLOR}======================================================${NC}"
echo -e "Service URL:       ${BLUE}${SERVICE_URL}${NC}"
echo -e "Access:            ${BLUE}${ACCESS_MODE}${NC}"
echo -e "Runtime Account:   ${BLUE}${RUNTIME_SA_EMAIL}${NC}"
echo -e "Smoke Test:        ${BLUE}${SMOKE_RESULT}${NC}"
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

if [ "$REGISTER_FAILED" = "true" ]; then
    exit 1
fi
