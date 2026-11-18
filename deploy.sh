#!/usr/bin/env bash
# Deploy to Cloud Run. Run once billing is linked to the project.
#   ./deploy.sh
set -euo pipefail

PROJECT=${PROJECT:-darglobal-bot-2026}
REGION=${REGION:-asia-south1}
SERVICE=${SERVICE:-darglobal-bot}
MODEL=${OPENROUTER_MODEL:-inclusionai/ling-3.2-flash:free}

[ -f .env ] && set -a && . ./.env && set +a
: "${OPENROUTER_API_KEY:?set OPENROUTER_API_KEY, or put it in .env}"

gcloud config set project "$PROJECT" >/dev/null

if ! gcloud billing projects describe "$PROJECT" \
      --format='value(billingEnabled)' 2>/dev/null | grep -qi true; then
  echo "Billing is not enabled on $PROJECT."
  echo "Link a billing account here, then re-run:"
  echo "  https://console.cloud.google.com/billing/linkedaccount?project=$PROJECT"
  exit 1
fi

echo "==> enabling APIs"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com

echo "==> storing the API key in Secret Manager"
# The key never reaches the image or the command line of the deploy.
if gcloud secrets describe openrouter-key >/dev/null 2>&1; then
  printf '%s' "$OPENROUTER_API_KEY" | gcloud secrets versions add openrouter-key --data-file=-
else
  printf '%s' "$OPENROUTER_API_KEY" | gcloud secrets create openrouter-key --data-file=-
fi

# A dedicated identity whose only permission is reading this one secret.
# The default compute service account is project Editor, so binding the key to
# it would let anything else in the project read the key too.
SA_NAME=darglobal-bot-run
SA="$SA_NAME@$PROJECT.iam.gserviceaccount.com"
gcloud iam service-accounts describe "$SA" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$SA_NAME" \
    --display-name="DarGlobal bot (Cloud Run runtime)"

gcloud secrets add-iam-policy-binding openrouter-key \
  --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor >/dev/null

echo "==> deploying"
gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --allow-unauthenticated \
  --service-account "$SA" \
  --memory 1Gi \
  --cpu 1 \
  --min-instances 0 \
  --max-instances 3 \
  --set-env-vars "OPENROUTER_MODEL=$MODEL" \
  --set-secrets "OPENROUTER_API_KEY=openrouter-key:latest"

URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')
echo
echo "live: $URL"
curl -fsS --max-time 30 "$URL/health" && echo
