#!/usr/bin/env bash
# The control room on Cloud Run with "Sign in with Google" in the page, open to the e-mail addresses in ALLOW only.
# CLIENT_ID is an OAuth web client id (Google Auth Platform → Clients) whose authorized JavaScript origin is the
# service URL printed at the end; make it once, re-run with it set. README.md explains the IAP alternative.
# Usage: PROJECT=… REGION=us-central1 BUCKET=… ALLOW=you@gmail.com CLIENT_ID=… deploy/ui/setup.sh
set -euo pipefail
: "${PROJECT:?}" "${REGION:=us-central1}" "${BUCKET:?}" "${ALLOW:?}" "${CLIENT_ID:=}"
IMAGE="$REGION-docker.pkg.dev/$PROJECT/nraiorbit/ui:latest"
SA="nraiorbit-runner@$PROJECT.iam.gserviceaccount.com"      # the runner's identity: bucket, secrets, job start

gcloud services enable run.googleapis.com cloudbuild.googleapis.com secretmanager.googleapis.com --project "$PROJECT"
gcloud secrets describe UI_SESSION_SECRET --project "$PROJECT" >/dev/null 2>&1 \
  || python3 -c "import secrets; print(secrets.token_hex(32), end='')" \
     | gcloud secrets create UI_SESSION_SECRET --project "$PROJECT" --data-file=- --replication-policy=automatic
gcloud builds submit --project "$PROJECT" --config deploy/ui/cloudbuild.yaml --substitutions _IMAGE="$IMAGE" .
for role in roles/run.viewer roles/logging.viewer; do
  gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" --role "$role" --quiet >/dev/null
done
gcloud run deploy nraiorbit-ui --project "$PROJECT" --region "$REGION" --image "$IMAGE" --service-account "$SA" \
  --allow-unauthenticated --no-iap --cpu 1 --memory 1Gi --min-instances 0 --max-instances 2 --timeout 120 \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=$PROJECT,GOOGLE_CLOUD_LOCATION=$REGION,UI_ROOT=gs://$BUCKET,UI_ALLOWED_EMAILS=$ALLOW,UI_JOB=nraiorbit-runner,UI_GOOGLE_CLIENT_ID=$CLIENT_ID" \
  --set-secrets "GOOGLE_APPLICATION_CREDENTIALS_JSON=GOOGLE_APPLICATION_CREDENTIALS_JSON:latest,UI_SESSION_SECRET=UI_SESSION_SECRET:latest"
URL=$(gcloud run services describe nraiorbit-ui --project "$PROJECT" --region "$REGION" --format "value(status.url)")
echo "$URL"
[ -n "$CLIENT_ID" ] || echo "now make the OAuth web client with authorized JavaScript origin $URL and re-run with CLIENT_ID=<id>"
