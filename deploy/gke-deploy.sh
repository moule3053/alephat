#!/usr/bin/env bash
# End-to-end GKE provisioning & single-chart Helm deployment for Alephat:
#   1. GKE cluster creation (Kata nested virtualization OR gVisor sandbox pool)
#   2. Workload Identity IAM & Google Cloud Secret Manager bootstrap (including Temporal mTLS certs)
#   3. Parallel Cloud Build image builds (platform, ui, slack-renderer, sandbox)
#   4. Single unified Helm release (`deploy/helm/alephat`) deploying Alephat in its own
#      namespace and every dependency in its own dedicated namespace:
#        - alephat              (API, Webhook, Temporal Harness + Slack Renderer, UI, ScaledObject)
#        - alephat-sandboxes    (SandboxTemplate, RBAC, NetworkPolicy)
#        - agent-sandbox-system (Agent Sandbox Controller v1.0.9)
#        - kata-system          (Kata Containers 4.3.1 + kata-qemu RuntimeClass)
#        - postgresql           (PostgreSQL 17 StatefulSet)
#        - temporal             (Temporal 1.32.2 mTLS Server + Namespace Job + Web UI)
#        - litellm              (LiteLLM 1.106.6 Gemini Coding Models + PostgreSQL)
#        - keda                 (KEDA 2.21.0 Operator + Metrics Server + Webhooks)
#        - external-secrets     (External Secrets Operator 2.14.0 + ClusterSecretStore)
#        - traefik              (Traefik v3.7.17 LoadBalancer + Let's Encrypt TLS)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CHART_DIR="${REPO_ROOT}/deploy/helm/alephat"

if [[ -f "${SCRIPT_DIR}/.env" ]]; then
  while IFS= read -r line || [[ -n "${line}" ]]; do
    [[ -z "${line}" || "${line}" =~ ^[[:space:]]*# || "${line}" != *"="* ]] && continue
    k="${line%%=*}"
    v="${line#*=}"
    v="${v#\"}"
    v="${v%\"}"
    v="${v#\'}"
    v="${v%\'}"
    case "${k}" in
      OPENAI_API_KEY|ANTHROPIC_API_KEY|GOOGLE_API_KEY|GEMINI_API_KEY|FIREWORKS_API_KEY|\
      GITHUB_APP_ID|GITHUB_APP_CLIENT_ID|GITHUB_APP_CLIENT_SECRET|GITHUB_APP_INSTALLATION_ID|\
      GITHUB_APP_PRIVATE_KEY|ALLOWED_GITHUB_USERS|ALLOWED_GITHUB_ORGS|GITHUB_WEBHOOK_SECRET|\
      SLACK_SIGNING_SECRET|SLACK_BOT_TOKEN|SLACK_APP_TOKEN|SLACK_APP_ID|SLACK_BOT_USER_ID|SLACK_BOT_USERNAME|SLACK_USER_MAP|\
      TOKEN_ENCRYPTION_KEY|DASHBOARD_JWT_SECRET|PLATFORM_API_TOKEN|PLATFORM_JWT_SECRET)
        if [[ -z "${!k:-}" && -n "${v}" ]]; then
          export "${k}=${v}"
        fi
        ;;
    esac
  done < "${SCRIPT_DIR}/.env"
fi

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
REGION="${REGION:-us-central1}"
ZONE="${ZONE:-}"
CLUSTER_NAME="${CLUSTER_NAME:-alephat}"
SANDBOX_RUNTIME="${SANDBOX_RUNTIME:-kata}"
NODE_SIZE="${NODE_SIZE:-n2-standard-4}"
NUM_NODES="${NUM_NODES:-3}"
MIN_NODES="${MIN_NODES:-2}"
MAX_NODES="${MAX_NODES:-6}"
K8S_VERSION="${K8S_VERSION:-latest}"
RELEASE_CHANNEL="${RELEASE_CHANNEL:-regular}"

KATA_VERSION="${KATA_VERSION:-4.3.1}"
LITELLM_VERSION="${LITELLM_VERSION:-1.106.6}"
USE_EXTERNAL_SECRETS="${USE_EXTERNAL_SECRETS:-true}"

AR_REPOSITORY="${AR_REPOSITORY:-alephat}"
RELEASE_NAME="${RELEASE_NAME:-alephat}"
PLATFORM_NAMESPACE="${PLATFORM_NAMESPACE:-alephat}"
SANDBOX_NAMESPACE="${SANDBOX_NAMESPACE:-alephat-sandboxes}"
POSTGRESQL_NAMESPACE="${POSTGRESQL_NAMESPACE:-postgresql}"
TEMPORAL_NAMESPACE_K8S="${TEMPORAL_NAMESPACE_K8S:-temporal}"
LITELLM_NAMESPACE="${LITELLM_NAMESPACE:-litellm}"
KATA_NAMESPACE="${KATA_NAMESPACE:-kata-system}"
KEDA_NAMESPACE="${KEDA_NAMESPACE:-keda}"
ESO_NAMESPACE="${ESO_NAMESPACE:-external-secrets}"
TRAEFIK_NAMESPACE="${TRAEFIK_NAMESPACE:-traefik}"

ENABLED_MODELS="${ENABLED_MODELS:-google:gemini-4-hydrogen,google:gemini-4-carbon,google:gemini-4-argon,google:gemini-3.8-flash,google:gemini-3.5-flash,google:gemini-3.1-pro-preview,google:gemini-2.5-pro,google:gemini-2.5-flash,anthropic:claude-opus-5-5,anthropic:claude-sonnet-5-5,anthropic:claude-haiku-5-5,anthropic:claude-opus-4-6,anthropic:claude-sonnet-4-6,anthropic:claude-sonnet-4-5,openai:gpt-6.1-terra,openai:gpt-6.1-sol,openai:gpt-6-luna,openai:gpt-6-astra,openai:gpt-5.5,openai:gpt-4.1,openai:o4-mini}"
DEFAULT_MODEL="${DEFAULT_MODEL:-}"

WITH_TRAEFIK=false
ALEPHAT_HOST="${ALEPHAT_HOST:-}"
TEMPORAL_UI_HOST="${TEMPORAL_UI_HOST:-}"
TEMPORAL_GRPC_HOST="${TEMPORAL_GRPC_HOST:-}"
TEMPORAL_UI_USER="${TEMPORAL_UI_USER:-admin}"
TEMPORAL_UI_PASSWORD="${TEMPORAL_UI_PASSWORD:-}"
DNS_DOMAIN="${DNS_DOMAIN:-}"
DNS_ZONE="${DNS_ZONE:-}"
ACME_EMAIL="${ACME_EMAIL:-}"
TRAEFIK_IP_NAME="${TRAEFIK_IP_NAME:-alephat-traefik}"

REUSE_CLUSTER=false
SKIP_BUILD=false
TEARDOWN=false
DRY_RUN=false

usage() {
  cat <<'EOF'
Usage: ./deploy/gke-deploy.sh [options]

Cluster & Sandbox Runtime Options:
  --project <id>                  GCP project ID (default: active gcloud project)
  --region <region>               GCP region (default: us-central1)
  --zone <zone>                   GCP zone (default: <region>-a)
  --cluster-name <name>           GKE cluster name (default: alephat)
  --sandbox-runtime <kata|gvisor> Sandbox isolation runtime (default: kata)
  --node-size <machine-type>      GKE machine type (default: n2-standard-4)
  --num-nodes <count>             Initial nodes in the primary pool (default: 3)
  --k8s-version <version>         GKE Kubernetes version (default: latest)
  --release-channel <channel>     GKE release channel (default: regular)
  --reuse-cluster                 Reuse an existing GKE cluster if present

Model & LiteLLM Options:
  --models <csv>                  Comma-separated model aliases to enable in LiteLLM
  --default-model <id>            Default model ID in alephat-config (default: first in --models)

Secrets & Ingress Options:
  --no-external-secrets           Use chart-managed Kubernetes Secrets instead of External Secrets + GCP Secret Manager
  --with-traefik                  Deploy Traefik LoadBalancer + Let's Encrypt HTTPS (Dashboard, Temporal UI, Temporal gRPC)
  --host <fqdn>                   Public hostname for Alephat Dashboard/Webhooks (e.g. alephat.example.com)
  --temporal-ui-host <fqdn>       Public hostname for Temporal Web UI (default: temporal.<base-domain>)
  --temporal-grpc-host <fqdn>     Public hostname for Temporal gRPC Frontend (default: temporal-grpc.<base-domain>)
  --dns-domain <domain>           Base DNS domain for Cloud DNS (e.g. example.com)
  --dns-zone <zone-name>          Cloud DNS managed zone name
  --acme-email <email>            Email for Let's Encrypt ACME registration
  --skip-build                    Skip Cloud Build image builds
  --teardown                      Delete the GKE cluster (and static IP if created)
  --dry-run                       Print resolved configuration and rendered Helm values without applying
  -h, --help                      Show this help text
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT_ID="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --zone) ZONE="$2"; shift 2 ;;
    --cluster-name) CLUSTER_NAME="$2"; shift 2 ;;
    --sandbox-runtime) SANDBOX_RUNTIME="$2"; shift 2 ;;
    --node-size|--machine-type) NODE_SIZE="$2"; shift 2 ;;
    --num-nodes) NUM_NODES="$2"; shift 2 ;;
    --k8s-version) K8S_VERSION="$2"; shift 2 ;;
    --release-channel) RELEASE_CHANNEL="$2"; shift 2 ;;
    --models) ENABLED_MODELS="$2"; shift 2 ;;
    --default-model) DEFAULT_MODEL="$2"; shift 2 ;;
    --kata-version) KATA_VERSION="$2"; shift 2 ;;
    --litellm-version) LITELLM_VERSION="$2"; shift 2 ;;
    --no-external-secrets) USE_EXTERNAL_SECRETS=false; shift ;;
    --with-traefik) WITH_TRAEFIK=true; shift ;;
    --host) ALEPHAT_HOST="$2"; shift 2 ;;
    --temporal-ui-host) TEMPORAL_UI_HOST="$2"; shift 2 ;;
    --temporal-grpc-host) TEMPORAL_GRPC_HOST="$2"; shift 2 ;;
    --dns-domain) DNS_DOMAIN="$2"; shift 2 ;;
    --dns-zone) DNS_ZONE="$2"; shift 2 ;;
    --acme-email) ACME_EMAIL="$2"; shift 2 ;;
    --reuse-cluster) REUSE_CLUSTER=true; shift ;;
    --skip-build) SKIP_BUILD=true; shift ;;
    --teardown) TEARDOWN=true; shift ;;
    --dry-run) DRY_RUN=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
done

[[ -z "${ZONE}" ]] && ZONE="${REGION}-a"

if [[ "${SANDBOX_RUNTIME}" != "kata" && "${SANDBOX_RUNTIME}" != "gvisor" ]]; then
  echo "Error: --sandbox-runtime must be 'kata' or 'gvisor' (got '${SANDBOX_RUNTIME}')." >&2
  exit 1
fi

if [[ "${SANDBOX_RUNTIME}" == "kata" ]]; then
  case "${NODE_SIZE}" in
    e2-*|n2d-*|t2a-*|t2d-*)
      echo "Error: Kata requires nested virtualization on Intel N2/C2/C3 nodes; '${NODE_SIZE}' is not supported for Kata." >&2
      exit 1
      ;;
  esac
fi

IFS=',' read -r -a MODEL_ENTRIES <<< "${ENABLED_MODELS}"
if [[ ${#MODEL_ENTRIES[@]} -eq 0 ]]; then
  echo "Error: --models must include at least one model." >&2
  exit 1
fi

if [[ -z "${DEFAULT_MODEL}" ]]; then
  FIRST_ENTRY="${MODEL_ENTRIES[0]}"
  DEFAULT_MODEL="${FIRST_ENTRY%%=*}"
fi

if [[ -z "${PROJECT_ID}" && "${DRY_RUN}" != "true" ]]; then
  echo "Error: --project <gcp-project-id> is required (or set via 'gcloud config set project')." >&2
  exit 1
fi
PROJECT_ID="${PROJECT_ID:-your-gcp-project-id}"

PLATFORM_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPOSITORY}/alephat-platform:latest"
UI_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPOSITORY}/alephat-ui:latest"
SLACK_RENDERER_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPOSITORY}/alephat-slack-renderer:latest"
SANDBOX_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPOSITORY}/alephat-sandbox:latest"

render_litellm_models_yaml() {
  for entry in "${MODEL_ENTRIES[@]}"; do
    entry="$(echo "${entry}" | xargs)"
    [[ -z "${entry}" ]] && continue
    local alias target api_key_env=""
    if [[ "${entry}" == *"="* ]]; then
      alias="${entry%%=*}"
      target="${entry#*=}"
    else
      alias="${entry}"
      case "${alias}" in
        gemini-5.6-flash) target="vertex_ai/gemini-2.5-flash" ;;
        google:*|google_genai:*|gemini:*)
          target="gemini/${alias#*:}"
          api_key_env="GEMINI_API_KEY"
          ;;
        gemini-*)
          target="gemini/${alias}"
          api_key_env="GEMINI_API_KEY"
          ;;
        openai:*)
          target="openai/${alias#openai:}"
          api_key_env="OPENAI_API_KEY"
          ;;
        anthropic:claude-3-5-sonnet|anthropic:claude-sonnet-4-5)
          target="anthropic/claude-sonnet-4-5-20250929"
          api_key_env="ANTHROPIC_API_KEY"
          ;;
        anthropic:*)
          target="anthropic/${alias#anthropic:}"
          api_key_env="ANTHROPIC_API_KEY"
          ;;
        fireworks:*)
          target="fireworks_ai/${alias#fireworks:}"
          api_key_env="FIREWORKS_API_KEY"
          ;;
        *) target="${alias}" ;;
      esac
    fi
    if [[ -z "${api_key_env}" ]]; then
      case "${target}" in
        openai/*) api_key_env="OPENAI_API_KEY" ;;
        anthropic/*) api_key_env="ANTHROPIC_API_KEY" ;;
        gemini/*) api_key_env="GEMINI_API_KEY" ;;
        fireworks_ai/*) api_key_env="FIREWORKS_API_KEY" ;;
      esac
    fi
    echo "    - alias: ${alias}"
    echo "      target: ${target}"
    echo "      apiKeyEnv: \"${api_key_env}\""
  done
}

render_helm_values() {
  local traefik_ip="${1:-}"
  local resolved_host="${ALEPHAT_HOST:-alephat.example.com}"
  local resolved_ui_host="${TEMPORAL_UI_HOST:-temporal.example.com}"
  local resolved_grpc_host="${TEMPORAL_GRPC_HOST:-temporal-grpc.example.com}"
  local resolved_acme="${ACME_EMAIL:-admin@example.com}"

  cat <<EOF
namespaces:
  create: true
  alephat: ${PLATFORM_NAMESPACE}
  postgresql: ${POSTGRESQL_NAMESPACE}
  temporal: ${TEMPORAL_NAMESPACE_K8S}
  litellm: ${LITELLM_NAMESPACE}
  sandboxes: ${SANDBOX_NAMESPACE}
  kata: ${KATA_NAMESPACE}
  keda: ${KEDA_NAMESPACE}
  externalSecrets: ${ESO_NAMESPACE}
  traefik: ${TRAEFIK_NAMESPACE}

sandboxRuntime:
  type: ${SANDBOX_RUNTIME}
  kata:
    version: "${KATA_VERSION}"

agentSandbox:
  enabled: true
  sandboxImage: ${SANDBOX_IMAGE}

externalSecrets:
  enabled: ${USE_EXTERNAL_SECRETS}
  gcp:
    projectID: ${PROJECT_ID}
    clusterLocation: ${ZONE}
    clusterName: ${CLUSTER_NAME}
    serviceAccountEmail: alephat-eso@${PROJECT_ID}.iam.gserviceaccount.com

litellm:
  enabled: true
  image: ghcr.io/berriai/litellm-database:v${LITELLM_VERSION#v}
  gcpServiceAccountEmail: alephat-litellm@${PROJECT_ID}.iam.gserviceaccount.com
  vertexProject: ${PROJECT_ID}
  vertexLocation: ${REGION}
  defaultModel: ${DEFAULT_MODEL}
  models:
$(render_litellm_models_yaml)

alephat:
  platformImage: ${PLATFORM_IMAGE}
  uiImage: ${UI_IMAGE}
  slackRendererImage: ${SLACK_RENDERER_IMAGE}

traefik:
  enabled: ${WITH_TRAEFIK}
  loadBalancerIP: "${traefik_ip}"
  acme:
    email: ${resolved_acme}

ingress:
  enabled: ${WITH_TRAEFIK}
  host: ${resolved_host}
  temporalUiHost: ${resolved_ui_host}
  temporalGrpcHost: ${resolved_grpc_host}
EOF
  if [[ "${USE_EXTERNAL_SECRETS}" != "true" ]]; then
    cat <<EOF

secrets:
  providerKeys:
    openaiApiKey: "${OPENAI_API_KEY:-}"
    anthropicApiKey: "${ANTHROPIC_API_KEY:-}"
    geminiApiKey: "${GEMINI_API_KEY:-${GOOGLE_API_KEY:-}}"
    fireworksApiKey: "${FIREWORKS_API_KEY:-}"
  githubApp:
    appId: "${GITHUB_APP_ID:-}"
    clientId: "${GITHUB_APP_CLIENT_ID:-}"
    clientSecret: "${GITHUB_APP_CLIENT_SECRET:-}"
    installationId: "${GITHUB_APP_INSTALLATION_ID:-}"
    privateKey: $(jq -nc --arg v "${GITHUB_APP_PRIVATE_KEY:-}" '$v')
    allowedUsers: "${ALLOWED_GITHUB_USERS:-}"
    allowedOrgs: "${ALLOWED_GITHUB_ORGS:-}"
  slack:
    signingSecret: "${SLACK_SIGNING_SECRET:-}"
    botToken: "${SLACK_BOT_TOKEN:-}"
    appToken: "${SLACK_APP_TOKEN:-}"
    userMap: $(jq -nc --arg v "${SLACK_USER_MAP:-}" '$v')
    appId: "${SLACK_APP_ID:-}"
    botUserId: "${SLACK_BOT_USER_ID:-}"
    botUsername: "${SLACK_BOT_USERNAME:-}"
EOF
  fi
}

echo "=================================================================="
echo "Alephat Unified Helm Deployment Configuration"
echo "=================================================================="
echo "Project ID:             ${PROJECT_ID}"
echo "Region / Zone:          ${REGION} / ${ZONE}"
echo "Cluster Name:           ${CLUSTER_NAME} (k8s: ${K8S_VERSION}, channel: ${RELEASE_CHANNEL})"
echo "Helm Chart:             ${CHART_DIR}"
echo "Sandbox Runtime:        ${SANDBOX_RUNTIME}"
echo "Node Size / Count:      ${NODE_SIZE} x ${NUM_NODES} (autoscale ${MIN_NODES}-${MAX_NODES})"
echo "External Secrets:       ${USE_EXTERNAL_SECRETS}"
echo "Traefik Ingress:        ${WITH_TRAEFIK}"
echo "Enabled Models:         ${ENABLED_MODELS}"
echo "Default Model:          ${DEFAULT_MODEL}"
echo "=================================================================="

if [[ "${DRY_RUN}" == "true" ]]; then
  TMP_DRY_VALUES="$(mktemp)"
  trap 'rm -f "${TMP_DRY_VALUES}"' EXIT
  render_helm_values "" > "${TMP_DRY_VALUES}"
  echo "--- Rendered Helm Values ---"
  cat "${TMP_DRY_VALUES}"
  helm template "${RELEASE_NAME}" "${CHART_DIR}" \
    --namespace "${PLATFORM_NAMESPACE}" \
    --values "${TMP_DRY_VALUES}" >/dev/null
  echo "--- Helm Template Verification: OK ---"
  exit 0
fi

if [[ "${TEARDOWN}" == "true" ]]; then
  exec "${SCRIPT_DIR}/gke-teardown.sh" \
    --project "${PROJECT_ID}" \
    --region "${REGION}" \
    --zone "${ZONE}" \
    --cluster-name "${CLUSTER_NAME}"
fi

# ---------------------------------------------------------------------------
# 1. Enable required Google Cloud APIs & Reserve Ingress IP
# ---------------------------------------------------------------------------
gcloud config set project "${PROJECT_ID}"
gcloud services enable \
  aiplatform.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  compute.googleapis.com \
  container.googleapis.com \
  dns.googleapis.com \
  iamcredentials.googleapis.com \
  secretmanager.googleapis.com

TRAEFIK_IP=""
if [[ "${WITH_TRAEFIK}" == "true" ]]; then
  if [[ -z "${ACME_EMAIL}" ]]; then
    ACME_EMAIL="$(gcloud config get-value account 2>/dev/null || true)"
  fi
  if [[ -z "${ACME_EMAIL}" ]]; then
    echo "Error: --with-traefik requires --acme-email <email> (or an active gcloud account)." >&2
    exit 1
  fi
  gcloud compute addresses describe "${TRAEFIK_IP_NAME}" --region "${REGION}" >/dev/null 2>&1 || \
    gcloud compute addresses create "${TRAEFIK_IP_NAME}" --region "${REGION}" --network-tier PREMIUM
  TRAEFIK_IP="$(gcloud compute addresses describe "${TRAEFIK_IP_NAME}" --region "${REGION}" --format='value(address)')"

  if [[ -z "${ALEPHAT_HOST}" || "${ALEPHAT_HOST}" == "auto" ]]; then
    BASE_SSLIP="${TRAEFIK_IP//./-}.sslip.io"
    ALEPHAT_HOST="alephat.${BASE_SSLIP}"
    TEMPORAL_UI_HOST="${TEMPORAL_UI_HOST:-temporal.${BASE_SSLIP}}"
    TEMPORAL_GRPC_HOST="${TEMPORAL_GRPC_HOST:-temporal-grpc.${BASE_SSLIP}}"
  else
    HOST_SUFFIX="${ALEPHAT_HOST#*.}"
    [[ "${HOST_SUFFIX}" == "${ALEPHAT_HOST}" ]] && HOST_SUFFIX="${ALEPHAT_HOST}"
    TEMPORAL_UI_HOST="${TEMPORAL_UI_HOST:-temporal.${HOST_SUFFIX}}"
    TEMPORAL_GRPC_HOST="${TEMPORAL_GRPC_HOST:-temporal-grpc.${HOST_SUFFIX}}"
  fi
  echo "Reserved Traefik IP ${TRAEFIK_IP}:"
  echo "  Alephat UI / Webhooks: https://${ALEPHAT_HOST}"
  echo "  Temporal Web UI:       https://${TEMPORAL_UI_HOST}"
  echo "  Temporal gRPC (mTLS):  ${TEMPORAL_GRPC_HOST}:7233"
fi

# ---------------------------------------------------------------------------
# 2. Provision GKE Cluster
# ---------------------------------------------------------------------------
if gcloud container clusters describe "${CLUSTER_NAME}" --zone "${ZONE}" >/dev/null 2>&1; then
  if [[ "${REUSE_CLUSTER}" != "true" ]]; then
    echo "Error: Cluster '${CLUSTER_NAME}' already exists in ${ZONE}. Pass --reuse-cluster to reuse it." >&2
    exit 1
  fi
  echo "Reusing existing GKE cluster '${CLUSTER_NAME}'..."
else
  if [[ "${SANDBOX_RUNTIME}" == "kata" ]]; then
    echo "Creating GKE Standard cluster '${CLUSTER_NAME}' with Ubuntu containerd + nested virtualization for Kata..."
    gcloud container clusters create "${CLUSTER_NAME}" \
      --zone "${ZONE}" \
      --release-channel "${RELEASE_CHANNEL}" \
      --cluster-version "${K8S_VERSION}" \
      --num-nodes "${NUM_NODES}" \
      --enable-autoscaling \
      --min-nodes "${MIN_NODES}" \
      --max-nodes "${MAX_NODES}" \
      --machine-type "${NODE_SIZE}" \
      --image-type UBUNTU_CONTAINERD \
      --enable-dataplane-v2 \
      --enable-ip-alias \
      --enable-nested-virtualization \
      --workload-pool="${PROJECT_ID}.svc.id.goog" \
      --workload-metadata=GKE_METADATA
  else
    echo "Creating GKE Standard cluster '${CLUSTER_NAME}' with COS_CONTAINERD + gVisor sandbox pool..."
    gcloud container clusters create "${CLUSTER_NAME}" \
      --zone "${ZONE}" \
      --release-channel "${RELEASE_CHANNEL}" \
      --cluster-version "${K8S_VERSION}" \
      --num-nodes "${NUM_NODES}" \
      --enable-autoscaling \
      --min-nodes "${MIN_NODES}" \
      --max-nodes "${MAX_NODES}" \
      --machine-type "${NODE_SIZE}" \
      --image-type COS_CONTAINERD \
      --enable-dataplane-v2 \
      --enable-ip-alias \
      --workload-pool="${PROJECT_ID}.svc.id.goog" \
      --workload-metadata=GKE_METADATA
    gcloud container node-pools describe sandbox-pool --cluster "${CLUSTER_NAME}" --zone "${ZONE}" >/dev/null 2>&1 || \
      gcloud container node-pools create sandbox-pool \
        --cluster "${CLUSTER_NAME}" \
        --zone "${ZONE}" \
        --num-nodes "${NUM_NODES}" \
        --machine-type "${NODE_SIZE}" \
        --image-type COS_CONTAINERD \
        --sandbox type=gvisor \
        --workload-metadata=GKE_METADATA
  fi
fi

gcloud container clusters get-credentials "${CLUSTER_NAME}" --zone "${ZONE}"

# ---------------------------------------------------------------------------
# 3. Configure Workload Identity IAM & Google Cloud Secret Manager Secrets
# ---------------------------------------------------------------------------
ensure_gsm_secret() {
  local name="$1"
  local value="$2"
  if ! gcloud secrets describe "${name}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud secrets create "${name}" --project "${PROJECT_ID}" --replication-policy="automatic" >/dev/null
    printf '%s' "${value}" | gcloud secrets versions add "${name}" --project "${PROJECT_ID}" --data-file=- >/dev/null
  else
    local existing=""
    existing="$(gcloud secrets versions access latest --secret="${name}" --project "${PROJECT_ID}" 2>/dev/null || true)"
    if [[ -z "${existing}" && -n "${value}" ]]; then
      printf '%s' "${value}" | gcloud secrets versions add "${name}" --project "${PROJECT_ID}" --data-file=- >/dev/null
    fi
  fi
}

generate_temporal_tls_json() {
  local cert_dir
  cert_dir="$(mktemp -d)"
  openssl ecparam -name prime256v1 -genkey -noout -out "${cert_dir}/ca.key"
  openssl req -x509 -new -key "${cert_dir}/ca.key" -sha256 -days 3650 \
    -subj "/CN=Alephat Temporal Root CA" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -out "${cert_dir}/ca.crt" >/dev/null 2>&1

  local extra_san=""
  [[ -n "${TEMPORAL_GRPC_HOST}" ]] && extra_san=",DNS:${TEMPORAL_GRPC_HOST}"
  cat > "${cert_dir}/server.ext" <<EXT
basicConstraints=CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth,clientAuth
subjectAltName=DNS:temporal,DNS:temporal.${TEMPORAL_NAMESPACE_K8S},DNS:temporal.${TEMPORAL_NAMESPACE_K8S}.svc,DNS:temporal.${TEMPORAL_NAMESPACE_K8S}.svc.cluster.local,DNS:localhost,IP:127.0.0.1${extra_san}
EXT
  openssl ecparam -name prime256v1 -genkey -noout -out "${cert_dir}/server.key"
  openssl req -new -key "${cert_dir}/server.key" \
    -subj "/CN=temporal.${TEMPORAL_NAMESPACE_K8S}.svc.cluster.local" \
    -out "${cert_dir}/server.csr" >/dev/null 2>&1
  openssl x509 -req -in "${cert_dir}/server.csr" \
    -CA "${cert_dir}/ca.crt" -CAkey "${cert_dir}/ca.key" -CAcreateserial \
    -days 3650 -sha256 -extfile "${cert_dir}/server.ext" \
    -out "${cert_dir}/server.crt" >/dev/null 2>&1

  cat > "${cert_dir}/client.ext" <<EXT
basicConstraints=CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=clientAuth
EXT
  openssl ecparam -name prime256v1 -genkey -noout -out "${cert_dir}/tls.key"
  openssl req -new -key "${cert_dir}/tls.key" \
    -subj "/CN=alephat-temporal-client" \
    -out "${cert_dir}/tls.csr" >/dev/null 2>&1
  openssl x509 -req -in "${cert_dir}/tls.csr" \
    -CA "${cert_dir}/ca.crt" -CAkey "${cert_dir}/ca.key" -CAcreateserial \
    -days 3650 -sha256 -extfile "${cert_dir}/client.ext" \
    -out "${cert_dir}/tls.crt" >/dev/null 2>&1

  jq -nc \
    --arg ca "$(cat "${cert_dir}/ca.crt")" \
    --arg scrt "$(cat "${cert_dir}/server.crt")" \
    --arg skey "$(cat "${cert_dir}/server.key")" \
    --arg ccrt "$(cat "${cert_dir}/tls.crt")" \
    --arg ckey "$(cat "${cert_dir}/tls.key")" \
    '{"ca.crt": $ca, "server.crt": $scrt, "server.key": $skey, "tls.crt": $ccrt, "tls.key": $ckey}'
  rm -rf "${cert_dir}"
}

LITELLM_GSA="alephat-litellm@${PROJECT_ID}.iam.gserviceaccount.com"
gcloud iam service-accounts describe "${LITELLM_GSA}" --project "${PROJECT_ID}" >/dev/null 2>&1 || \
  gcloud iam service-accounts create alephat-litellm --project "${PROJECT_ID}" --display-name="Alephat LiteLLM Vertex AI"
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${LITELLM_GSA}" \
  --role=roles/aiplatform.user \
  --condition=None >/dev/null
gcloud iam service-accounts add-iam-policy-binding "${LITELLM_GSA}" \
  --project "${PROJECT_ID}" \
  --role=roles/iam.workloadIdentityUser \
  --member="serviceAccount:${PROJECT_ID}.svc.id.goog[${LITELLM_NAMESPACE}/litellm]" >/dev/null

if [[ "${USE_EXTERNAL_SECRETS}" == "true" ]]; then
  ESO_GSA="alephat-eso@${PROJECT_ID}.iam.gserviceaccount.com"
  gcloud iam service-accounts describe "${ESO_GSA}" --project "${PROJECT_ID}" >/dev/null 2>&1 || \
    gcloud iam service-accounts create alephat-eso --project "${PROJECT_ID}" --display-name="Alephat External Secrets Operator"
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${ESO_GSA}" \
    --role=roles/secretmanager.secretAccessor \
    --condition=None >/dev/null
  gcloud iam service-accounts add-iam-policy-binding "${ESO_GSA}" \
    --project "${PROJECT_ID}" \
    --role=roles/iam.workloadIdentityUser \
    --member="serviceAccount:${PROJECT_ID}.svc.id.goog[${ESO_NAMESPACE}/alephat-eso]" >/dev/null

  echo "Ensuring JSON secrets exist in Google Cloud Secret Manager..."
  ensure_gsm_secret "litellm-secrets" "$(jq -nc --arg masterkey "${LITELLM_MASTER_KEY:-sk-$(openssl rand -hex 24)}" '{masterkey: $masterkey}')"
  RESOLVED_LITELLM_DB="${LITELLM_DB_PASSWORD:-$(openssl rand -hex 24)}"
  ensure_gsm_secret "litellm-postgres" "$(jq -nc --arg pw "${RESOLVED_LITELLM_DB}" '{password: $pw, "postgres-password": $pw}')"
  ensure_gsm_secret "litellm-provider-keys" "$(jq -nc \
    --arg openai "${OPENAI_API_KEY:-}" \
    --arg anthropic "${ANTHROPIC_API_KEY:-}" \
    --arg gemini "${GEMINI_API_KEY:-${GOOGLE_API_KEY:-}}" \
    --arg fireworks "${FIREWORKS_API_KEY:-}" \
    '{OPENAI_API_KEY: $openai, ANTHROPIC_API_KEY: $anthropic, GEMINI_API_KEY: $gemini, FIREWORKS_API_KEY: $fireworks}')"
  ensure_gsm_secret "alephat-postgres" "$(jq -nc --arg pw "${ALEPHAT_DB_PASSWORD:-$(openssl rand -hex 24)}" '{password: $pw}')"
  ensure_gsm_secret "alephat-secrets" "$(jq -nc \
    --arg token "${PLATFORM_API_TOKEN:-$(openssl rand -hex 32)}" \
    --arg jwt "${PLATFORM_JWT_SECRET:-$(openssl rand -hex 32)}" \
    --arg webhook "${GITHUB_WEBHOOK_SECRET:-$(openssl rand -hex 32)}" \
    --arg slack_secret "${SLACK_SIGNING_SECRET:-}" \
    --arg slack_token "${SLACK_BOT_TOKEN:-}" \
    --arg slack_app_token "${SLACK_APP_TOKEN:-}" \
    --arg slack_user_map "${SLACK_USER_MAP:-}" \
    --arg slack_app_id "${SLACK_APP_ID:-}" \
    --arg slack_bot_user_id "${SLACK_BOT_USER_ID:-}" \
    --arg slack_bot_username "${SLACK_BOT_USERNAME:-}" \
    '{
      PLATFORM_API_TOKEN: $token,
      PLATFORM_JWT_SECRET: $jwt,
      GITHUB_WEBHOOK_SECRET: $webhook,
      SLACK_SIGNING_SECRET: $slack_secret,
      SLACK_BOT_TOKEN: $slack_token,
      SLACK_APP_TOKEN: $slack_app_token,
      SLACK_USER_MAP: $slack_user_map,
      SLACK_APP_ID: $slack_app_id,
      SLACK_BOT_USER_ID: $slack_bot_user_id,
      SLACK_BOT_USERNAME: $slack_bot_username
    }')"
  ensure_gsm_secret "alephat-oauth" "$(jq -nc \
    --arg app_id "${GITHUB_APP_ID:-}" \
    --arg client_id "${GITHUB_APP_CLIENT_ID:-}" \
    --arg client_secret "${GITHUB_APP_CLIENT_SECRET:-}" \
    --arg installation_id "${GITHUB_APP_INSTALLATION_ID:-}" \
    --arg private_key "${GITHUB_APP_PRIVATE_KEY:-}" \
    --arg allowed_users "${ALLOWED_GITHUB_USERS:-}" \
    --arg allowed_orgs "${ALLOWED_GITHUB_ORGS:-}" \
    --arg dashboard_jwt "${DASHBOARD_JWT_SECRET:-$(openssl rand -hex 32)}" \
    --arg token_enc "${TOKEN_ENCRYPTION_KEY:-$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n')}" \
    '{
      GITHUB_APP_ID: $app_id,
      GITHUB_APP_CLIENT_ID: $client_id,
      GITHUB_APP_CLIENT_SECRET: $client_secret,
      GITHUB_APP_INSTALLATION_ID: $installation_id,
      GITHUB_APP_PRIVATE_KEY: $private_key,
      ALLOWED_GITHUB_USERS: $allowed_users,
      ALLOWED_GITHUB_ORGS: $allowed_orgs,
      DASHBOARD_JWT_SECRET: $dashboard_jwt,
      TOKEN_ENCRYPTION_KEY: $token_enc
    }')"

  # Regenerate temporal-tls if missing or if it lacks temporal.<ns>.svc.cluster.local SAN
  REGEN_TLS=false
  if ! gcloud secrets describe temporal-tls --project "${PROJECT_ID}" >/dev/null 2>&1; then
    REGEN_TLS=true
  else
    EXISTING_SAN="$(gcloud secrets versions access latest --secret=temporal-tls --project "${PROJECT_ID}" | jq -r '."server.crt"' | openssl x509 -noout -ext subjectAltName 2>/dev/null || true)"
    if [[ "${EXISTING_SAN}" != *"DNS:temporal.${TEMPORAL_NAMESPACE_K8S}.svc.cluster.local"* ]]; then
      REGEN_TLS=true
    fi
  fi
  if [[ "${REGEN_TLS}" == "true" ]]; then
    echo "Generating Temporal mTLS certificates for namespace '${TEMPORAL_NAMESPACE_K8S}' in Google Cloud Secret Manager..."
    if ! gcloud secrets describe temporal-tls --project "${PROJECT_ID}" >/dev/null 2>&1; then
      ensure_gsm_secret "temporal-tls" "$(generate_temporal_tls_json)"
    else
      generate_temporal_tls_json | gcloud secrets versions add temporal-tls --project "${PROJECT_ID}" --data-file=- >/dev/null
    fi
  fi

  RESOLVED_UI_PW="${TEMPORAL_UI_PASSWORD:-$(openssl rand -hex 16)}"
  ensure_gsm_secret "temporal-ui-auth" "$(jq -nc --arg u "${TEMPORAL_UI_USER}:$(openssl passwd -apr1 "${RESOLVED_UI_PW}")" '{users: $u}')"
fi

# ---------------------------------------------------------------------------
# 4. Build & Push Container Images in Parallel
# ---------------------------------------------------------------------------
if [[ "${SKIP_BUILD}" != "true" ]]; then
  gcloud artifacts repositories describe "${AR_REPOSITORY}" --location "${REGION}" >/dev/null 2>&1 || \
    gcloud artifacts repositories create "${AR_REPOSITORY}" --repository-format docker --location "${REGION}"

  echo "Submitting Cloud Builds for platform, ui, slack-renderer, and sandbox images in parallel..."
  BUILD_PIDS=()
  gcloud builds submit "${REPO_ROOT}" --config "${REPO_ROOT}/cloudbuild.platform.yaml" --substitutions="_PLATFORM_IMAGE=${PLATFORM_IMAGE}" &
  BUILD_PIDS+=($!)
  gcloud builds submit "${REPO_ROOT}" --config "${REPO_ROOT}/cloudbuild.ui.yaml" --substitutions="_UI_IMAGE=${UI_IMAGE}" &
  BUILD_PIDS+=($!)
  gcloud builds submit "${REPO_ROOT}" --config "${REPO_ROOT}/cloudbuild.slack-renderer.yaml" --substitutions="_SLACK_RENDERER_IMAGE=${SLACK_RENDERER_IMAGE}" &
  BUILD_PIDS+=($!)
  gcloud builds submit "${REPO_ROOT}" --config "${REPO_ROOT}/cloudbuild.sandbox.yaml" --substitutions="_SANDBOX_IMAGE=${SANDBOX_IMAGE}" &
  BUILD_PIDS+=($!)

  for bpid in "${BUILD_PIDS[@]}"; do
    wait "${bpid}"
  done
fi

# ---------------------------------------------------------------------------
# 5. Optional Cloud DNS Records (before ACME challenge)
# ---------------------------------------------------------------------------
if [[ "${WITH_TRAEFIK}" == "true" && -n "${DNS_ZONE}" && -n "${DNS_DOMAIN}" ]]; then
  gcloud dns managed-zones describe "${DNS_ZONE}" >/dev/null 2>&1 || \
    gcloud dns managed-zones create "${DNS_ZONE}" \
      --dns-name="${DNS_DOMAIN%.}." \
      --description="Alephat public DNS zone"
  for fqdn in "${ALEPHAT_HOST}" "${TEMPORAL_UI_HOST}" "${TEMPORAL_GRPC_HOST}"; do
    gcloud dns record-sets describe "${fqdn%.}." --zone "${DNS_ZONE}" --type A >/dev/null 2>&1 || \
      gcloud dns record-sets create "${fqdn%.}." \
        --zone "${DNS_ZONE}" \
        --type A \
        --ttl 300 \
        --rrdatas "${TRAEFIK_IP}"
  done
fi

# ---------------------------------------------------------------------------
# 6. Deploy Alephat & All Dependencies via Unified Multi-Namespace Helm Chart
# ---------------------------------------------------------------------------
TMP_HELM_VALUES="$(mktemp)"
trap 'rm -f "${TMP_HELM_VALUES}"' EXIT
render_helm_values "${TRAEFIK_IP}" > "${TMP_HELM_VALUES}"

echo "Deploying unified Helm chart '${CHART_DIR}' across isolated namespaces..."
helm upgrade --install "${RELEASE_NAME}" "${CHART_DIR}" \
  --namespace "${PLATFORM_NAMESPACE}" \
  --create-namespace \
  --values "${TMP_HELM_VALUES}" \
  --wait \
  --timeout 15m

echo "Deployment complete!"
kubectl -n "${PLATFORM_NAMESPACE}" get pods,svc,scaledobject
