#!/usr/bin/env bash
# One-time setup of the runner on Google Cloud: image, secrets, the Cloud Run job, a 15-minute schedule.
# Usage: PROJECT=nraiorbit-agentic-film-product REGION=us-central1 BUCKET=nraiorbit-agentic-film-product-media ./setup.sh
set -euo pipefail
: "${PROJECT:?}" "${REGION:=us-central1}" "${BUCKET:?}"
IMAGE="$REGION-docker.pkg.dev/$PROJECT/nraiorbit/runner:latest"
SA="nraiorbit-runner@$PROJECT.iam.gserviceaccount.com"

gcloud services enable run.googleapis.com cloudbuild.googleapis.com cloudscheduler.googleapis.com \
  secretmanager.googleapis.com aiplatform.googleapis.com --project "$PROJECT"

# 1. the image (built from the repository root)
gcloud builds submit --project "$PROJECT" --tag "$IMAGE" --file deploy/service/Dockerfile . 2>/dev/null \
  || gcloud builds submit --project "$PROJECT" --config deploy/service/cloudbuild.yaml --substitutions _IMAGE="$IMAGE" .

# 2. a service account with just what the runner needs
gcloud iam service-accounts create nraiorbit-runner --project "$PROJECT" --display-name "nraiorbit runner" 2>/dev/null || true
for role in roles/aiplatform.user roles/storage.objectAdmin roles/secretmanager.secretAccessor roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" --role "$role" --quiet >/dev/null
done

# 3. secrets: create each once with `printf %s "$VALUE" | gcloud secrets create NAME --data-file=-`
#    SYNC_API_KEY, ELEVENLABS_API_KEY, GOOGLE_APPLICATION_CREDENTIALS_JSON (a key for a signing-capable SA),
#    YOUTUBE_TOKEN_JSON (made once on a laptop with `nraiorbit release youtube <slug> --dry-run` then the OAuth flow)
SECRETS="SYNC_API_KEY=SYNC_API_KEY:latest,ELEVENLABS_API_KEY=ELEVENLABS_API_KEY:latest,GOOGLE_APPLICATION_CREDENTIALS_JSON=GOOGLE_APPLICATION_CREDENTIALS_JSON:latest"

# 4. the job: 4 vCPU, 8 GiB, up to 6 hours per tick, one task
gcloud run jobs deploy nraiorbit-runner --project "$PROJECT" --region "$REGION" --image "$IMAGE" \
  --service-account "$SA" --cpu 4 --memory 8Gi --task-timeout 6h --max-retries 0 --tasks 1 \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=$PROJECT,GOOGLE_CLOUD_LOCATION=$REGION" \
  --set-secrets "$SECRETS" \
  --args="--stories,gs://$BUCKET/queue/stories.csv,--references,gs://$BUCKET/queue/references.csv,--bucket,$BUCKET"

# 5. every 15 minutes; a tick that finds nothing to do exits in seconds
gcloud scheduler jobs create http nraiorbit-runner-tick --project "$PROJECT" --location "$REGION" \
  --schedule "*/15 * * * *" --http-method POST \
  --uri "https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT/jobs/nraiorbit-runner:run" \
  --oauth-service-account-email "$SA" 2>/dev/null || echo "scheduler job exists"

echo "done. Queue a story:   nraiorbit service add-story <slug> gs://$BUCKET/stories/<slug>.md --stories gs://$BUCKET/queue/stories.csv --budget 150 --gates ask --publish private"
echo "      Queue a reference: nraiorbit service add-reference <youtube url> --references gs://$BUCKET/queue/references.csv --learn all"
