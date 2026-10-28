#!/usr/bin/env bash

# Authenticate application default credentials as the demo service account
gcloud auth application-default login --impersonate-service-account=external-for-test@gdglyon-cloudrun.iam.gserviceaccount.com

# Print the exact OAuth2 Redirect URI to register in GCP Console > Credentials
# (In Cloud Shell, $WEB_HOST is persistent for your Google account)
echo "OAuth2 Redirect URI to authorize: https://8000-${WEB_HOST}/dev-ui/"

# Project, Vertex AI Agent Engine, and Cloud KMS configuration
PROJECT_ID="gdglyon-cloudrun"
PROJECT_NUMBER="751286965207"
GENAI_LOCATION="eu"
ENGINE_LOCATION="europe-west1"
REASONING_ENGINE_ID="8559187848840871936"
KMS_KEY="projects/${PROJECT_ID}/locations/${ENGINE_LOCATION}/keyRings/test_eu/cryptoKeys/test/cryptoKeyVersions/17"

# ============================================================================
# OPTION A (SCENARIOS 1 to 4 WITHOUT KMS): Start ADK Web WITHOUT KMS key
# - Client ID / Secret are fetched on the fly from Secret Manager
# - OAuth2 tokens will be stored in plaintext in the Agent Engine session
#   for Scenario 3 (bigquery_token_cache) & Scenario 4 (custom_tool_token_cache)
# - --allow_origins "*" & ADK_ALLOW_REMOTE_AGENT_BUILDER=1 are required in
#   Cloud Shell (ADK 2.12+ blocks reverse-proxy Origin / X-Forwarded-For by default)
# ============================================================================
ADK_ALLOW_REMOTE_AGENT_BUILDER=1 \
OAUTH_CLIENT_ID="$(gcloud secrets versions access latest --secret=adk-oauth-client-id --project=${PROJECT_ID})" \
OAUTH_CLIENT_SECRET="$(gcloud secrets versions access latest --secret=adk-oauth-client-secret --project=${PROJECT_ID})" \
GOOGLE_GENAI_USE_VERTEXAI=TRUE \
GOOGLE_CLOUD_LOCATION="${GENAI_LOCATION}" \
GOOGLE_CLOUD_PROJECT="${PROJECT_ID}" \
adk web --allow_origins "*" --session_service_uri "agentengine://projects/${PROJECT_NUMBER}/locations/${ENGINE_LOCATION}/reasoningEngines/${REASONING_ENGINE_ID}"


# ============================================================================
# OPTION B (SCENARIOS 3 & 4 WITH KMS): Start ADK Web WITH KMS key
# - Setting GOOGLE_CREDENTIAL_KMS_KEY enables envelope encryption (Fernet DEK +
#   Cloud KMS) in ADK 2.12+
# - OAuth2 secrets (token, refresh_token, client_secret) are stored encrypted
#   ("kms:gAAAAAB...") in the Agent Engine session state
# ============================================================================
ADK_ALLOW_REMOTE_AGENT_BUILDER=1 \
GOOGLE_CREDENTIAL_KMS_KEY="${KMS_KEY}" \
OAUTH_CLIENT_ID="$(gcloud secrets versions access latest --secret=adk-oauth-client-id --project=${PROJECT_ID})" \
OAUTH_CLIENT_SECRET="$(gcloud secrets versions access latest --secret=adk-oauth-client-secret --project=${PROJECT_ID})" \
GOOGLE_GENAI_USE_VERTEXAI=TRUE \
GOOGLE_CLOUD_LOCATION="${GENAI_LOCATION}" \
GOOGLE_CLOUD_PROJECT="${PROJECT_ID}" \
adk web --allow_origins "*" --session_service_uri "agentengine://projects/${PROJECT_NUMBER}/locations/${ENGINE_LOCATION}/reasoningEngines/${REASONING_ENGINE_ID}"


# ============================================================================
# INSPECT SESSION: View the session state stored in Agent Engine
# (Replace <SESSION_ID> with the Session ID shown in the ADK Web UI)
# ============================================================================
SESSION_ID="<SESSION_ID>"
SESSION_URL="https://${ENGINE_LOCATION}-aiplatform.googleapis.com/v1/projects/${PROJECT_ID}/locations/${ENGINE_LOCATION}/reasoningEngines/${REASONING_ENGINE_ID}/sessions/${SESSION_ID}"

curl -s -H "Authorization: Bearer $(gcloud auth print-access-token)" \
     -H "Content-Type: application/json" \
     "${SESSION_URL}" | jq .


# ============================================================================
# IMMEDIATE REVOCATION (After showing the plaintext token on screen)
# 1. Extract the access_token from the session and revoke it via Google OAuth2
#    (this immediately invalidates both the access_token and refresh_token)
# 2. Delete the plaintext session from Agent Engine
# ============================================================================
EXPOSED_TOKEN=$(curl -s -H "Authorization: Bearer $(gcloud auth print-access-token)" "${SESSION_URL}" \
  | jq -r '(.sessionState.bigquery_token_cache // .sessionState.custom_tool_token_cache) | fromjson | .token // empty')

# Revoke both access_token and refresh_token with Google OAuth2
curl -s -X POST "https://oauth2.googleapis.com/revoke?token=${EXPOSED_TOKEN}"

# (Manual alternative if you prefer pasting the displayed token directly)
# curl -s -X POST "https://oauth2.googleapis.com/revoke?token=<DISPLAYED_TOKEN>"

# Delete the session containing plaintext credentials from Agent Engine
curl -s -X DELETE -H "Authorization: Bearer $(gcloud auth print-access-token)" "${SESSION_URL}"
