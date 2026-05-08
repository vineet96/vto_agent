#!/usr/bin/env bash
# Build with Cloud Build, deploy to Cloud Run.
#
# Required env:
#   GOOGLE_CLOUD_PROJECT          - GCP project ID
#   GOOGLE_CLOUD_LOCATION         - region for Cloud Run + Agent Engine
#   AGENT_ENGINE_RESOURCE_NAME    - full reasoningEngines/<id> resource path
#                                   (output of deploy_to_agent_engine.py)
#   UPLOAD_BUCKET                 - GCS bucket name (NO gs:// prefix) where
#                                   the web app stages user uploads. Must
#                                   be readable by the Agent Engine
#                                   service account too.
#
# Optional env:
#   SERVICE                       - Cloud Run service name (default: image-studio-web)
#   RUNTIME_SA                    - service account email used by Cloud Run
#                                   (defaults to default compute SA)
#   MAX_UPLOAD_DIM                - max dimension for downscaling (default: 1536)

set -euo pipefail

: "${GOOGLE_CLOUD_PROJECT:?GOOGLE_CLOUD_PROJECT is required}"
: "${GOOGLE_CLOUD_LOCATION:?GOOGLE_CLOUD_LOCATION is required (e.g. us-central1)}"
: "${AGENT_ENGINE_RESOURCE_NAME:?AGENT_ENGINE_RESOURCE_NAME is required}"
: "${UPLOAD_BUCKET:?UPLOAD_BUCKET is required (bucket name, no gs:// prefix)}"

SERVICE="${SERVICE:-image-studio-web}"
MAX_UPLOAD_DIM="${MAX_UPLOAD_DIM:-1536}"
IMAGE="${GOOGLE_CLOUD_LOCATION}-docker.pkg.dev/${GOOGLE_CLOUD_PROJECT}/cloud-run-source-deploy/${SERVICE}:latest"

# Ensure required APIs.
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  aiplatform.googleapis.com \
  storage.googleapis.com \
  --project "${GOOGLE_CLOUD_PROJECT}"

# Make sure the upload bucket exists. (Idempotent: ignore "already owned" error.)
if ! gsutil ls -b "gs://${UPLOAD_BUCKET}" >/dev/null 2>&1; then
  echo "Creating bucket gs://${UPLOAD_BUCKET}..."
  gsutil mb -p "${GOOGLE_CLOUD_PROJECT}" -l "${GOOGLE_CLOUD_LOCATION}" "gs://${UPLOAD_BUCKET}"
  # Auto-delete uploaded blobs after 1 day to keep costs predictable.
  cat >/tmp/lifecycle.json <<EOF
{"lifecycle":{"rule":[{"action":{"type":"Delete"},"condition":{"age":1}}]}}
EOF
  gsutil lifecycle set /tmp/lifecycle.json "gs://${UPLOAD_BUCKET}"
fi

# Resolve the Cloud Run runtime SA (default compute SA if not given).
PROJECT_NUMBER=$(gcloud projects describe "${GOOGLE_CLOUD_PROJECT}" --format='value(projectNumber)')
DEFAULT_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
RUNTIME_SA="${RUNTIME_SA:-${DEFAULT_SA}}"

echo "Cloud Run runtime SA: ${RUNTIME_SA}"

# Grant runtime SA access to the bucket (read+write) and Agent Engine.
gcloud projects add-iam-policy-binding "${GOOGLE_CLOUD_PROJECT}" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/aiplatform.user" \
  --condition=None --quiet >/dev/null

gsutil iam ch \
  "serviceAccount:${RUNTIME_SA}:objectAdmin" \
  "gs://${UPLOAD_BUCKET}"

# Vertex AI service agent (used by Agent Engine to fetch input images AND
# to publish generated try-on / edit results back) needs read+write
# access to the bucket.
VERTEX_SA="service-${PROJECT_NUMBER}@gcp-sa-aiplatform.iam.gserviceaccount.com"
gsutil iam ch \
  "serviceAccount:${VERTEX_SA}:objectAdmin" \
  "gs://${UPLOAD_BUCKET}" || true

# The agent's reasoning-engine identity also needs write access for the
# tool-publish path. By default that's the same Vertex AI service agent.
# If you used a custom SA when deploying the agent, replace VERTEX_SA above.

# Build & push the image.
echo "Building image ${IMAGE}..."
gcloud builds submit \
  --tag "${IMAGE}" \
  --project "${GOOGLE_CLOUD_PROJECT}" \
  --region "${GOOGLE_CLOUD_LOCATION}" \
  webapp
  

# Deploy.
echo "Deploying ${SERVICE}..."
gcloud run deploy "${SERVICE}" \
  --image "${IMAGE}" \
  --region "${GOOGLE_CLOUD_LOCATION}" \
  --project "${GOOGLE_CLOUD_PROJECT}" \
  --service-account "${RUNTIME_SA}" \
  --allow-unauthenticated \
  --memory 1Gi \
  --cpu 1 \
  --timeout 600 \
  --concurrency 8 \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=${GOOGLE_CLOUD_PROJECT}" \
  --set-env-vars "GOOGLE_CLOUD_LOCATION=${GOOGLE_CLOUD_LOCATION}" \
  --set-env-vars "AGENT_ENGINE_RESOURCE_NAME=${AGENT_ENGINE_RESOURCE_NAME}" \
  --set-env-vars "UPLOAD_BUCKET=${UPLOAD_BUCKET}" \
  --set-env-vars "MAX_UPLOAD_DIM=${MAX_UPLOAD_DIM}"

URL=$(gcloud run services describe "${SERVICE}" \
  --region "${GOOGLE_CLOUD_LOCATION}" \
  --project "${GOOGLE_CLOUD_PROJECT}" \
  --format='value(status.url)')

echo
echo "=================================================="
echo "Service deployed: ${URL}"
echo "Upload bucket:    gs://${UPLOAD_BUCKET}  (1-day lifecycle)"
echo "=================================================="