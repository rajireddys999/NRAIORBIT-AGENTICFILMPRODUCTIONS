#!/usr/bin/env bash
# One-time setup of the MuseTalk lip-sync GPU worker in your Google Cloud project (idempotent; safe to re-run).
# The same shape as deploy/lipsync/setup.sh (LatentSync); the two workers live side by side.
#
#   export GOOGLE_CLOUD_PROJECT=nraiorbit-agentic-film-product
#   gcloud auth login
#   bash deploy/musetalk/setup.sh
#
# Builds the worker image with Cloud Build (deploy/musetalk/, 30-60 min the first time: PyTorch, the MMLab packages
# and about 5 GB of weights, then a CPU smoke run of the worker) into the Artifact Registry repo the pipeline images
# use. The Vertex AI endpoint runner (lipsync.runner: vertex_endpoint, what the SD set uses) needs nothing more: the
# provider registers the image as a Vertex model the first time it runs. The Cloud Run job is created too, for
# projects with Cloud Run's L4 quota (lipsync.runner: cloudrun, lipsync.job: the name below).
set -euo pipefail

PROJECT="${GOOGLE_CLOUD_PROJECT:?set GOOGLE_CLOUD_PROJECT}"
REGION="${LIPSYNC_REGION:-us-central1}"                 # Cloud Run / Vertex AI L4 region
REPO="${ARTIFACT_REPO:-nraiorbit}"
JOB="${MUSETALK_JOB:-nraiorbit-musetalk}"
TAG="${MUSETALK_TAG:-v1}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/musetalk:${TAG}"
HERE="$(cd "$(dirname "$0")" && pwd)"

unset CLOUDSDK_AUTH_ACCESS_TOKEN          # a preset token would override the account gcloud is signed in with
gcloud config set project "$PROJECT" >/dev/null

echo "== enabling APIs"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
    aiplatform.googleapis.com storage.googleapis.com logging.googleapis.com

echo "== Artifact Registry ${REPO} (${REGION})"
gcloud artifacts repositories describe "$REPO" --location "$REGION" >/dev/null 2>&1 || \
    gcloud artifacts repositories create "$REPO" --repository-format docker --location "$REGION"

echo "== building ${IMAGE} with Cloud Build"
if ! gcloud artifacts docker images describe "$IMAGE" >/dev/null 2>&1; then
    gcloud builds submit "$HERE" --tag "$IMAGE" --machine-type e2-highcpu-32 --timeout 7200s --disk-size 200
fi

if [ "${MUSETALK_CLOUDRUN_JOB:-0}" = "1" ]; then
    SA="$(gcloud config get-value account 2>/dev/null)"
    case "$SA" in *gserviceaccount.com) ;; *) SA="${LIPSYNC_SERVICE_ACCOUNT:?set LIPSYNC_SERVICE_ACCOUNT (the job runs as it)}";; esac
    echo "== Cloud Run job ${JOB} (1x NVIDIA L4)"
    gcloud run jobs deploy "$JOB" --image "$IMAGE" --region "$REGION" \
        --gpu 1 --gpu-type nvidia-l4 --no-gpu-zonal-redundancy --cpu 4 --memory 16Gi \
        --task-timeout 3600s --max-retries 0 --service-account "$SA"
fi

echo "== done. Pipeline settings (projects/<slug>/pipeline.yaml, or quality: sd which carries them):"
cat <<EOF
providers:
  lipsync: musetalk_cloudrun
lipsync:
  runner: vertex_endpoint      # or cloudrun with MUSETALK_CLOUDRUN_JOB=1 above and job: ${JOB}
  image_tag: ${TAG}
  region: ${REGION}
EOF
