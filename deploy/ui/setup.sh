#!/usr/bin/env bash
# The control room on Cloud Run behind IAP, open to the e-mail addresses in ALLOW only.
# A project without an organization also needs a custom OAuth client made once in the console (README.md).
# Usage: PROJECT=… REGION=us-central1 BUCKET=… ALLOW=you@gmail.com deploy/ui/setup.sh
set -euo pipefail
: "${PROJECT:?}" "${REGION:=us-central1}" "${BUCKET:?}" "${ALLOW:?}"
IMAGE="$REGION-docker.pkg.dev/$PROJECT/nraiorbit/ui:latest"
SA="nraiorbit-runner@$PROJECT.iam.gserviceaccount.com"      # the runner's identity: bucket, secrets, job start
NUMBER=$(gcloud projects describe "$PROJECT" --format="value(projectNumber)")

gcloud services enable iap.googleapis.com run.googleapis.com cloudbuild.googleapis.com --project "$PROJECT"
gcloud builds submit --project "$PROJECT" --config deploy/ui/cloudbuild.yaml --substitutions _IMAGE="$IMAGE" .
for role in roles/run.viewer roles/logging.viewer; do
  gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" --role "$role" --quiet >/dev/null
done
gcloud run deploy nraiorbit-ui --project "$PROJECT" --region "$REGION" --image "$IMAGE" --service-account "$SA" \
  --no-allow-unauthenticated --iap --cpu 1 --memory 1Gi --min-instances 0 --max-instances 2 --timeout 120 \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=$PROJECT,GOOGLE_CLOUD_LOCATION=$REGION,UI_ROOT=gs://$BUCKET,UI_ALLOWED_EMAILS=$ALLOW,UI_JOB=nraiorbit-runner" \
  --set-secrets "GOOGLE_APPLICATION_CREDENTIALS_JSON=GOOGLE_APPLICATION_CREDENTIALS_JSON:latest"
# IAP's own service agent must be able to reach the service; the people in ALLOW get through IAP
gcloud run services add-iam-policy-binding nraiorbit-ui --project "$PROJECT" --region "$REGION" \
  --member "serviceAccount:service-$NUMBER@gcp-sa-iap.iam.gserviceaccount.com" --role roles/run.invoker --quiet >/dev/null
IFS=, read -ra PEOPLE <<< "$ALLOW"
for who in "${PEOPLE[@]}"; do
  gcloud iap web add-iam-policy-binding --project "$PROJECT" --resource-type=cloud-run --region "$REGION" \
    --service nraiorbit-ui --member "user:$who" --role roles/iap.httpsResourceAccessor
done
gcloud run services describe nraiorbit-ui --project "$PROJECT" --region "$REGION" --format "value(status.url)"
