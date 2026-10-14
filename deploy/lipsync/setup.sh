#!/usr/bin/env bash
# One-time setup of the lip-sync GPU worker in your Google Cloud project (idempotent; safe to re-run).
#
#   export GOOGLE_CLOUD_PROJECT=nraiorbit-agentic-film-product
#   gcloud auth activate-service-account --key-file=<service-account.json>   # or: gcloud auth login
#   bash deploy/lipsync/setup.sh
#
# Creates: the APIs it needs, a private bucket (gs://$BUCKET), an Artifact Registry repo, the worker image (built by
# Cloud Build from deploy/lipsync/, about 20-40 min the first time), and a Cloud Run job with one NVIDIA L4 GPU
# (Cloud Run grants L4 GPUs without a quota request; billed per second, scales to zero).
set -euo pipefail

PROJECT="${GOOGLE_CLOUD_PROJECT:?set GOOGLE_CLOUD_PROJECT}"
REGION="${LIPSYNC_REGION:-us-central1}"                 # Cloud Run L4 region
BUCKET="${MEDIA_BUCKET:-${PROJECT}-media}"
BUCKET_LOCATION="${MEDIA_BUCKET_LOCATION:-asia-south1}"
REPO="${ARTIFACT_REPO:-nraiorbit}"
JOB="${LIPSYNC_JOB:-nraiorbit-lipsync}"
TAG="${LIPSYNC_TAG:-v1}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/latentsync:${TAG}"
HERE="$(cd "$(dirname "$0")" && pwd)"

gcloud config set project "$PROJECT" >/dev/null
SA="$(gcloud config get-value account 2>/dev/null)"
case "$SA" in *gserviceaccount.com) ;; *) SA="${LIPSYNC_SERVICE_ACCOUNT:?set LIPSYNC_SERVICE_ACCOUNT (the job runs as it)}";; esac

echo "== enabling APIs"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
    storage.googleapis.com logging.googleapis.com

echo "== bucket gs://${BUCKET} (private)"
gcloud storage buckets describe "gs://${BUCKET}" >/dev/null 2>&1 || \
    gcloud storage buckets create "gs://${BUCKET}" --location "$BUCKET_LOCATION" \
        --uniform-bucket-level-access --public-access-prevention
# staging files for lip-sync are deleted after 7 days; backups elsewhere in the bucket are kept
printf '{"rule":[{"action":{"type":"Delete"},"condition":{"age":7,"matchesPrefix":["lipsync/"]}}]}' > /tmp/lifecycle.json
gcloud storage buckets update "gs://${BUCKET}" --lifecycle-file=/tmp/lifecycle.json

echo "== Artifact Registry ${REPO} (${REGION})"
gcloud artifacts repositories describe "$REPO" --location "$REGION" >/dev/null 2>&1 || \
    gcloud artifacts repositories create "$REPO" --repository-format docker --location "$REGION"

echo "== building ${IMAGE} with Cloud Build"
if ! gcloud artifacts docker images describe "$IMAGE" >/dev/null 2>&1; then
    gcloud builds submit "$HERE" --tag "$IMAGE" --machine-type e2-highcpu-8 --timeout 5400s --disk-size 200
fi

echo "== Cloud Run job ${JOB} (1x NVIDIA L4)"
gcloud run jobs deploy "$JOB" --image "$IMAGE" --region "$REGION" \
    --gpu 1 --gpu-type nvidia-l4 --no-gpu-zonal-redundancy --cpu 4 --memory 16Gi \
    --task-timeout 3600s --max-retries 0 --service-account "$SA"

echo "== done. Pipeline settings (projects/<slug>/pipeline.yaml):"
cat <<EOF
providers:
  lipsync: latentsync_cloudrun
lipsync:
  job: ${JOB}
  region: ${REGION}
  bucket: ${BUCKET}
EOF
