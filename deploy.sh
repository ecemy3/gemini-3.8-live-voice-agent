#!/usr/bin/env bash
# Cloud Run'a yayın. Kullanım: ./deploy.sh
# Gereken: gcloud girişi, faturalandırması açık proje (.env'deki GOOGLE_CLOUD_PROJECT).
set -euo pipefail
cd "$(dirname "$0")"
set -a; source .env; set +a

PROJECT="${GOOGLE_CLOUD_PROJECT:?GOOGLE_CLOUD_PROJECT yok}"
REGION="${DEPLOY_REGION:-us-central1}"
SERVICE="${DEPLOY_SERVICE:-live-asistan}"
SA_NAME="live-asistan-run"
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
BUILD_SA_NAME="live-asistan-build"
BUILD_SA="${BUILD_SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
PUBLIC_ORIGINS="${PUBLIC_ORIGINS:-https://ecem.dev,https://www.ecem.dev}"

echo "==> API'ler etkinleştiriliyor"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  aiplatform.googleapis.com --project "$PROJECT"

echo "==> Servis hesabı (yalnızca Vertex AI kullanıcısı)"
gcloud iam service-accounts describe "$SA" --project "$PROJECT" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$SA_NAME" --project "$PROJECT" --display-name "Live asistan (Cloud Run)"
gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" \
  --role roles/aiplatform.user --condition=None >/dev/null

echo "==> Derleme servis hesabı (yalnızca roles/run.builder)"
# Yeni projelerde Cloud Build varsayılan hesabının kaynak koduna izni yok; varsayılan
# hesaba geniş yetki vermek yerine ayrı, en az yetkili bir derleme hesabı kullanıyoruz.
gcloud iam service-accounts describe "$BUILD_SA" --project "$PROJECT" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$BUILD_SA_NAME" --project "$PROJECT" --display-name "Live asistan (Cloud Build)"
# Yeni hesap IAM'e birkaç saniyede yayılır; hazır olana kadar tekrar dene.
for i in 1 2 3 4 5 6; do
  gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$BUILD_SA" \
    --role roles/run.builder --condition=None >/dev/null 2>&1 && break
  echo "   hesap henüz yayılmadı, bekleniyor ($i)"; sleep 10
done
# Deploy eden kullanıcı derleme hesabını kullanabilmeli (actAs)
gcloud iam service-accounts add-iam-policy-binding "$BUILD_SA" --project "$PROJECT" \
  --member "user:$(gcloud config get-value account 2>/dev/null)" --role roles/iam.serviceAccountUser >/dev/null

# Tam demo için erişim kodu: yoksa üret, yalnızca yerel .deploy-secrets dosyasına yaz.
if [[ ! -f .deploy-secrets ]]; then
  umask 077
  echo "DEMO_ACCESS_CODE=$(python3 -c 'import secrets; print(secrets.token_urlsafe(9))')" > .deploy-secrets
fi
source .deploy-secrets

PROJECT_NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
RUN_URL="https://${SERVICE}-${PROJECT_NUMBER}.${REGION}.run.app"
ORIGINS="${PUBLIC_ORIGINS},${RUN_URL}"

# gcloud --set-env-vars virgülü ayırıcı sayar; ^@^ ile ayırıcıyı @ yapıyoruz.
ENV_VARS="^@^GOOGLE_GENAI_USE_VERTEXAI=TRUE@GOOGLE_CLOUD_PROJECT=${PROJECT}"
ENV_VARS+="@LIVE_LOCATION=${LIVE_LOCATION:-us-central1}@FLASH_LOCATION=${FLASH_LOCATION:-us}"
ENV_VARS+="@TRANSCRIBE_LOCATION=${TRANSCRIBE_LOCATION:-global}@ALLOWED_ORIGINS=${ORIGINS}"
ENV_VARS+="@REQUIRE_ORIGIN=true@DEMO_ACCESS_CODE=${DEMO_ACCESS_CODE}"
ENV_VARS+="@MAX_CONCURRENT_SESSIONS=${MAX_CONCURRENT_SESSIONS:-6}"
ENV_VARS+="@PUBLIC_DAILY_SESSIONS_PER_IP=${PUBLIC_DAILY_SESSIONS_PER_IP:-10}@LOG_LEVEL=INFO"

echo "==> Cloud Run'a yükleniyor ($SERVICE @ $REGION)"
# max-instances=1: bellek içi oturum sınırları global kalsın ve maliyet tavanı olsun.
# timeout=3600: WebSocket bağlantıları için. session-affinity: aynı istemci aynı örneğe.
gcloud run deploy "$SERVICE" --source . --project "$PROJECT" --region "$REGION" \
  --service-account "$SA" --build-service-account "projects/${PROJECT}/serviceAccounts/${BUILD_SA}" \
  --allow-unauthenticated \
  --min-instances 0 --max-instances 1 --concurrency 20 --cpu 1 --memory 512Mi \
  --timeout 3600 --session-affinity --set-env-vars "$ENV_VARS" --quiet

echo
echo "Yayında: ${RUN_URL}"
echo "Widget : <script src=\"${RUN_URL}/static/widget/ecem-widget.js\" data-bottom=\"84\" defer></script>"
echo "Tam demo erişim kodu .deploy-secrets dosyasında."
