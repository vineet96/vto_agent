#!/usr/bin/env bash
# Register (or update) the deployed Image Studio agent inside a
# Gemini Enterprise app via the Discovery Engine REST API.
#
# Required env:
#   GOOGLE_CLOUD_PROJECT          - project that owns the GE app
#   GE_APP_ID                     - the Gemini Enterprise app/engine ID
#                                   (Cloud Console -> Gemini Enterprise -> Apps)
#   GE_APP_LOCATION               - app multi-region: "global", "us", or "eu"
#                                   (default: "global")
#   AGENT_ENGINE_RESOURCE_NAME    - full reasoningEngines/<id> resource name
#                                   from deploy_to_agent_engine.py
#
# Optional env:
#   DISPLAY_NAME                  - shown to GE users (default: "Image Studio")
#   DESCRIPTION                   - one-line, shown in GE picker
#   TOOL_DESCRIPTION              - longer text the GE router uses to decide
#                                   when to delegate to this agent

set -euo pipefail

: "${GOOGLE_CLOUD_PROJECT:?GOOGLE_CLOUD_PROJECT is required}"
: "${GE_APP_ID:?GE_APP_ID is required (Cloud Console > Gemini Enterprise > Apps)}"
: "${AGENT_ENGINE_RESOURCE_NAME:?AGENT_ENGINE_RESOURCE_NAME is required}"

GE_APP_LOCATION="${GE_APP_LOCATION:-global}"
DISPLAY_NAME="${DISPLAY_NAME:-Image Studio}"
DESCRIPTION="${DESCRIPTION:-Virtual try-on and Nano Banana image editing.}"
TOOL_DESCRIPTION="${TOOL_DESCRIPTION:-Use this agent for virtual try-on (showing a person wearing a clothing product) or for generative image edits like changing colors, swapping items, or removing objects from an image. Users supply images by including gs:// URIs or public image URLs in their message.}"

# 1. Make sure the API is enabled.
gcloud services enable discoveryengine.googleapis.com \
  --project "${GOOGLE_CLOUD_PROJECT}"

# 2. Grant the Discovery Engine service agent the rights it needs to invoke
#    the deployed reasoningEngine. (Idempotent.)
PROJECT_NUMBER=$(gcloud projects describe "${GOOGLE_CLOUD_PROJECT}" --format='value(projectNumber)')
DE_SA="service-${PROJECT_NUMBER}@gcp-sa-discoveryengine.iam.gserviceaccount.com"

echo "Granting Vertex AI roles to ${DE_SA}..."
gcloud projects add-iam-policy-binding "${GOOGLE_CLOUD_PROJECT}" \
  --member="serviceAccount:${DE_SA}" \
  --role="roles/aiplatform.user" \
  --condition=None --quiet >/dev/null
gcloud projects add-iam-policy-binding "${GOOGLE_CLOUD_PROJECT}" \
  --member="serviceAccount:${DE_SA}" \
  --role="roles/aiplatform.viewer" \
  --condition=None --quiet >/dev/null

# 3. Build the endpoint host. Multi-region "global" uses the bare host;
#    "us" / "eu" use a regional prefix.
if [[ "${GE_APP_LOCATION}" == "global" ]]; then
  HOST="discoveryengine.googleapis.com"
else
  HOST="${GE_APP_LOCATION}-discoveryengine.googleapis.com"
fi

# 3a. Sanity-check: does default_user_store exist in this multi-region?
#     This catches the most common Day-1 error - the app/subscription was
#     never set up in this location, so user-facing flows (registration,
#     license assignment, querying) fail with cryptic messages.
echo "Checking that default_user_store exists in locations/${GE_APP_LOCATION}..."
USER_STORE_HTTP=$(curl -sS -o /tmp/_us_resp.json -w '%{http_code}' \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "X-Goog-User-Project: ${GOOGLE_CLOUD_PROJECT}" \
  "https://${HOST}/v1/projects/${GOOGLE_CLOUD_PROJECT}/locations/${GE_APP_LOCATION}/userStores/default_user_store" \
  || echo 000)
if [[ "${USER_STORE_HTTP}" != "200" ]]; then
  cat <<EOF >&2

ERROR: default_user_store is not provisioned for locations/${GE_APP_LOCATION}.
HTTP ${USER_STORE_HTTP}: $(cat /tmp/_us_resp.json)

Gemini Enterprise creates this resource when you finish the app-setup
flow (subscription + at least one assigned license) in that multi-region.

To fix:
  1. Cloud Console -> Gemini Enterprise
  2. Manage subscriptions -> Create subscription, pick multi-region
     "${GE_APP_LOCATION}".
  3. Licenses -> Add users, assign at least one license.
  4. Re-run this script.

If your app actually lives in a different multi-region, set GE_APP_LOCATION
to "global", "us", or "eu" to match it. To list your apps:

  curl -sS -H "Authorization: Bearer \$(gcloud auth print-access-token)" \\
    -H "X-Goog-User-Project: ${GOOGLE_CLOUD_PROJECT}" \\
    "https://discoveryengine.googleapis.com/v1/projects/${GOOGLE_CLOUD_PROJECT}/locations/global/collections/default_collection/engines"

EOF
  exit 1
fi
echo "  default_user_store OK."

BASE_URL="https://${HOST}/v1alpha/projects/${GOOGLE_CLOUD_PROJECT}/locations/${GE_APP_LOCATION}/collections/default_collection/engines/${GE_APP_ID}/assistants/default_assistant/agents"

# 4. Idempotency: list existing agents and look for one with our DISPLAY_NAME.
echo "Checking for existing registration..."
EXISTING=$(curl -sS \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "X-Goog-User-Project: ${GOOGLE_CLOUD_PROJECT}" \
  "${BASE_URL}" \
  | python3 -c "
import json, sys, os
data = json.load(sys.stdin)
target = os.environ['DISPLAY_NAME']
for a in data.get('agents', []):
    if a.get('displayName') == target:
        print(a['name'])
        break
" || true)

# 5. Build the request body. heredoc + envsubst would be clean, but
#    keeping it as a python here-script avoids a dep on envsubst.
BODY=$(python3 -c "
import json, os
print(json.dumps({
    'displayName': os.environ['DISPLAY_NAME'],
    'description': os.environ['DESCRIPTION'],
    'adkAgentDefinition': {
        'toolSettings': {
            'toolDescription': os.environ['TOOL_DESCRIPTION'],
        },
        'provisionedReasoningEngine': {
            'reasoningEngine': os.environ['AGENT_ENGINE_RESOURCE_NAME'],
        },
    },
}))
")

if [[ -n "${EXISTING}" ]]; then
  echo "Updating existing agent: ${EXISTING}"
  RESP=$(curl -sS -X PATCH \
    -H "Authorization: Bearer $(gcloud auth print-access-token)" \
    -H "Content-Type: application/json" \
    -H "X-Goog-User-Project: ${GOOGLE_CLOUD_PROJECT}" \
    "https://${HOST}/v1alpha/${EXISTING}" \
    -d "${BODY}")
else
  echo "Registering new agent in app ${GE_APP_ID}..."
  RESP=$(curl -sS -X POST \
    -H "Authorization: Bearer $(gcloud auth print-access-token)" \
    -H "Content-Type: application/json" \
    -H "X-Goog-User-Project: ${GOOGLE_CLOUD_PROJECT}" \
    "${BASE_URL}" \
    -d "${BODY}")
fi

echo
echo "=================================================="
echo "Response:"
echo "${RESP}" | python3 -m json.tool
echo "=================================================="
echo
echo "Open the Gemini Enterprise app in the Cloud Console to see it:"
echo "  Cloud Console -> Gemini Enterprise -> Apps -> ${GE_APP_ID} -> Agents"
echo