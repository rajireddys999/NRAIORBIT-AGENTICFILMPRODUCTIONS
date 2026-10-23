#!/usr/bin/env bash
# One-time setup so GitHub Actions (.github/workflows/deploy.yml) can deploy to Google Cloud without any stored key:
# GitHub proves which workflow is running (OIDC), and Google lets only deploy.yml on the default branch of this
# repository act as a deployer account that can push images and point the job and the service at them. Nothing
# else changes: no secrets, no service settings. Safe to run again. Run it in Cloud Shell, as the project's owner:
#   bash deploy/github/setup.sh
set -euo pipefail

PROJECT="${PROJECT:-nraiorbit-agentic-film-product}"
REGION="${REGION:-us-central1}"
REPO_NAME="${REPO_NAME:-nraiorbit}"                                   # Artifact Registry repository
GITHUB_REPO="${GITHUB_REPO:-rajireddys999/NRAIORBIT-AGENTICFILMPRODUCTIONS}"
BRANCH="${BRANCH:-claude/telugu-ai-movie-generator-yhb6po}"
RUNNER_SA="nraiorbit-runner@${PROJECT}.iam.gserviceaccount.com"        # what the job and the service run as
DEPLOYER="nraiorbit-deployer"
DEPLOYER_SA="${DEPLOYER}@${PROJECT}.iam.gserviceaccount.com"
POOL="github"
PROVIDER="nraiorbit"

gcloud config set project "$PROJECT" >/dev/null
NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"

echo "1/5 APIs"
gcloud services enable iamcredentials.googleapis.com sts.googleapis.com artifactregistry.googleapis.com run.googleapis.com

echo "2/5 the deployer account and what it may do"
gcloud iam service-accounts describe "$DEPLOYER_SA" >/dev/null 2>&1 \
  || gcloud iam service-accounts create "$DEPLOYER" --display-name "NRAIORBIT deploys from GitHub Actions"
# push images to this one repository only
gcloud artifacts repositories add-iam-policy-binding "$REPO_NAME" --location "$REGION" \
  --member "serviceAccount:${DEPLOYER_SA}" --role roles/artifactregistry.writer --quiet >/dev/null
# point the job and the service at a new image
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member "serviceAccount:${DEPLOYER_SA}" --role roles/run.developer --condition None --quiet >/dev/null
# the job and the service keep running as the runner account: deploying them needs permission to use it, nothing more
gcloud iam service-accounts add-iam-policy-binding "$RUNNER_SA" \
  --member "serviceAccount:${DEPLOYER_SA}" --role roles/iam.serviceAccountUser --quiet >/dev/null

echo "3/5 the GitHub identity pool"
gcloud iam workload-identity-pools describe "$POOL" --location global >/dev/null 2>&1 \
  || gcloud iam workload-identity-pools create "$POOL" --location global --display-name "GitHub Actions"

echo "4/5 the provider: only deploy.yml on ${BRANCH} of ${GITHUB_REPO}"
# workflow_ref pins the very workflow file and the exact branch: another workflow in the repository (the coding
# agent's, a pull request's) or deploy.yml on any other branch (even one whose name starts the same) never gets the
# deployer's identity, though it runs in the same repository.
CONDITION="assertion.repository=='${GITHUB_REPO}' && assertion.workflow_ref=='${GITHUB_REPO}/.github/workflows/deploy.yml@refs/heads/${BRANCH}'"
MAPPING="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.workflow_ref=assertion.workflow_ref"
if gcloud iam workload-identity-pools providers describe "$PROVIDER" --location global --workload-identity-pool "$POOL" >/dev/null 2>&1; then
  gcloud iam workload-identity-pools providers update-oidc "$PROVIDER" --location global --workload-identity-pool "$POOL" \
    --attribute-mapping "$MAPPING" --attribute-condition "$CONDITION" >/dev/null
else
  gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" --location global --workload-identity-pool "$POOL" \
    --display-name "NRAIORBIT deploy.yml" --issuer-uri "https://token.actions.githubusercontent.com" \
    --attribute-mapping "$MAPPING" --attribute-condition "$CONDITION"
fi

echo "5/5 let that workflow act as the deployer"
gcloud iam service-accounts add-iam-policy-binding "$DEPLOYER_SA" --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/${NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.repository/${GITHUB_REPO}" \
  --quiet >/dev/null

cat <<DONE

Done. Add these three repository variables in GitHub (Settings > Secrets and variables > Actions > Variables):

  GCP_PROJECT        ${PROJECT}
  GCP_WIF_PROVIDER   projects/${NUMBER}/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}
  GCP_DEPLOYER_SA    ${DEPLOYER_SA}

Or from a terminal with the GitHub CLI:

  gh variable set GCP_PROJECT --repo ${GITHUB_REPO} --body "${PROJECT}"
  gh variable set GCP_WIF_PROVIDER --repo ${GITHUB_REPO} --body "projects/${NUMBER}/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}"
  gh variable set GCP_DEPLOYER_SA --repo ${GITHUB_REPO} --body "${DEPLOYER_SA}"
DONE
