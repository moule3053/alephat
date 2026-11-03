#!/usr/bin/env bash
# End-to-end GKE provisioning & deployment script for Alephat:
#   1. GKE cluster creation (Kata Containers with nested virtualization OR gVisor sandbox pool)
#   2. Agent Sandbox controller & CRDs (v1.0.9) + SandboxTemplate (kata-qemu or gvisor)
#   3. KEDA operator (2.21.0)
#   4. LiteLLM gateway (1.106.6) with configurable enabled models & Workload Identity
#   5. Cloud Build image builds (platform, ui, sandbox)
#   6. Alephat platform (PostgreSQL 17, NATS 2.12 JetStream, API, Webhook, UI, Harness + KEDA ScaledObject)
#   7. Optional Traefik ingress (42.3.0) with Let's Encrypt TLS & Cloud DNS

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ---------------------------------------------------------------------------
# Defaults (latest tested versions)
# ---------------------------------------------------------------------------
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
REGION="${REGION:-us-central1}"
ZONE="${ZONE:-}"
CLUSTER_NAME="${CLUSTER_NAME:-alephat}"
SANDBOX_RUNTIME="${SANDBOX_RUNTIME:-kata}"          # kata | gvisor
NODE_SIZE="${NODE_SIZE:-n2-standard-4}"
NUM_NODES="${NUM_NODES:-2}"
K8S_VERSION="${K8S_VERSION:-latest}"
RELEASE_CHANNEL="${RELEASE_CHANNEL:-regular}"

AGENT_SANDBOX_VERSION="${AGENT_SANDBOX_VERSION:-v1.0.9}"
KATA_VERSION="${KATA_VERSION:-4.3.1}"
LITELLM_VERSION="${LITELLM_VERSION:-1.106.6}"
KEDA_VERSION="${KEDA_VERSION:-2.21.0}"
TRAEFIK_VERSION="${TRAEFIK_VERSION:-42.3.0}"

AR_REPOSITORY="${AR_REPOSITORY:-alephat}"
PLATFORM_NAMESPACE="${PLATFORM_NAMESPACE:-alephat}"
SANDBOX_NAMESPACE="${SANDBOX_NAMESPACE:-alephat-sandboxes}"

ENABLED_MODELS="${ENABLED_MODELS:-google:gemini-4-argon,google_genai:gemini-4-argon,google:gemini-3.5-flash,gemini-5.6-flash,openai:gpt-4o-mini,anthropic:claude-3-5-sonnet}"
DEFAULT_MODEL="${DEFAULT_MODEL:-}"

WITH_TRAEFIK=false
ALEPHAT_HOST="${ALEPHAT_HOST:-}"
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
  --num-nodes <count>             Nodes in the primary/sandbox pool (default: 2)
  --k8s-version <version>         GKE Kubernetes version (default: latest)
  --release-channel <channel>     GKE release channel (default: regular)
  --reuse-cluster                 Reuse an existing GKE cluster if present

Model & LiteLLM Options:
  --models <csv>                  Comma-separated model aliases to enable in LiteLLM.
                                  Accepts known aliases (google:gemini-4-argon,
                                  google_genai:gemini-4-argon, google:gemini-3.5-flash,
                                  gemini-5.6-flash, openai:gpt-4o, openai:gpt-4o-mini,
                                  anthropic:claude-3-5-sonnet, anthropic:claude-sonnet-5)
                                  or custom alias=provider/model pairs.
  --default-model <id>            Default model ID in alephat-config (default: first in --models)

Component Versions:
  --agent-sandbox-version <ver>   kubernetes-sigs/agent-sandbox tag (default: v1.0.9)
  --kata-version <ver>            Kata Containers release tag (default: 4.3.1)
  --litellm-version <ver>         LiteLLM Helm chart & image version (default: 1.106.6)
  --keda-version <ver>            KEDA Helm chart version (default: 2.21.0)
  --traefik-version <ver>         Traefik Helm chart version (default: 42.3.0)

Ingress & Lifecycle Options:
  --with-traefik                  Deploy Traefik LoadBalancer + Let's Encrypt HTTPS
  --host <fqdn>                   Public hostname for Traefik (e.g. alephat.example.com)
  --dns-domain <domain>           Base DNS domain for Cloud DNS (e.g. example.com)
  --dns-zone <zone-name>          Cloud DNS managed zone name
  --acme-email <email>            Email for Let's Encrypt ACME registration
  --skip-build                    Skip Cloud Build image builds
  --teardown                      Delete the GKE cluster (and static IP if created)
  --dry-run                       Print resolved configuration and rendered manifests without applying
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
    --agent-sandbox-version) AGENT_SANDBOX_VERSION="$2"; shift 2 ;;
    --kata-version) KATA_VERSION="$2"; shift 2 ;;
    --litellm-version) LITELLM_VERSION="$2"; shift 2 ;;
    --keda-version) KEDA_VERSION="$2"; shift 2 ;;
    --traefik-version) TRAEFIK_VERSION="$2"; shift 2 ;;
    --with-traefik) WITH_TRAEFIK=true; shift ;;
    --host) ALEPHAT_HOST="$2"; shift 2 ;;
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

if [[ -z "${ZONE}" ]]; then
  ZONE="${REGION}-a"
fi

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
  RUNTIME_CLASS_NAME="kata-qemu"
else
  RUNTIME_CLASS_NAME="gvisor"
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
SANDBOX_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPOSITORY}/alephat-sandbox:latest"

render_litellm_model_list() {
  for entry in "${MODEL_ENTRIES[@]}"; do
    entry="$(echo "${entry}" | xargs)"
    [[ -z "${entry}" ]] && continue
    local alias target api_key_line=""
    if [[ "${entry}" == *"="* ]]; then
      alias="${entry%%=*}"
      target="${entry#*=}"
    else
      alias="${entry}"
      case "${alias}" in
        google:gemini-4-argon|google_genai:gemini-4-argon|gemini-4-argon)
          target="gemini/gemini-4-argon"
          api_key_line="        api_key: os.environ/GEMINI_API_KEY"
          ;;
        google:gemini-3.5-flash|google_genai:gemini-3.5-flash)
          target="gemini/gemini-3.5-flash"
          api_key_line="        api_key: os.environ/GEMINI_API_KEY"
          ;;
        gemini-5.6-flash)
          target="vertex_ai/gemini-2.5-flash"
          ;;
        openai:*)
          target="openai/${alias#openai:}"
          api_key_line="        api_key: os.environ/OPENAI_API_KEY"
          ;;
        anthropic:claude-3-5-sonnet)
          target="anthropic/claude-sonnet-4-5-20250929"
          api_key_line="        api_key: os.environ/ANTHROPIC_API_KEY"
          ;;
        anthropic:*)
          target="anthropic/${alias#anthropic:}"
          api_key_line="        api_key: os.environ/ANTHROPIC_API_KEY"
          ;;
        fireworks:*)
          target="fireworks_ai/${alias#fireworks:}"
          api_key_line="        api_key: os.environ/FIREWORKS_API_KEY"
          ;;
        *)
          target="${alias}"
          ;;
      esac
    fi
    if [[ -z "${api_key_line}" ]]; then
      case "${target}" in
        openai/*) api_key_line="        api_key: os.environ/OPENAI_API_KEY" ;;
        anthropic/*) api_key_line="        api_key: os.environ/ANTHROPIC_API_KEY" ;;
        gemini/*) api_key_line="        api_key: os.environ/GEMINI_API_KEY" ;;
        fireworks_ai/*) api_key_line="        api_key: os.environ/FIREWORKS_API_KEY" ;;
      esac
    fi
    echo "    - model_name: ${alias}"
    echo "      litellm_params:"
    echo "        model: ${target}"
    if [[ -n "${api_key_line}" ]]; then
      echo "${api_key_line}"
    fi
  done
}

render_sandbox_template() {
  cat <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: ${SANDBOX_NAMESPACE}
---
apiVersion: extensions.agents.x-k8s.io/v1beta1
kind: SandboxTemplate
metadata:
  name: python-runtime-template
  namespace: ${SANDBOX_NAMESPACE}
spec:
  podTemplate:
    metadata:
      labels:
        app.kubernetes.io/name: alephat-python-sandbox
    spec:
      runtimeClassName: ${RUNTIME_CLASS_NAME}
      restartPolicy: OnFailure
      automountServiceAccountToken: false
EOF
  if [[ "${SANDBOX_RUNTIME}" == "kata" ]]; then
    cat <<'EOF'
      nodeSelector:
        cloud.google.com/gke-os-distribution: ubuntu
        kubernetes.io/arch: amd64
EOF
  else
    cat <<'EOF'
      nodeSelector:
        kubernetes.io/arch: amd64
EOF
  fi
  cat <<EOF
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
      containers:
        - name: runtime
          image: ${SANDBOX_IMAGE}
          ports:
            - name: toolbox
              containerPort: 8888
          resources:
            requests:
              cpu: 250m
              memory: 512Mi
            limits:
              cpu: "2"
              memory: 2Gi
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
EOF
}

render_litellm_values() {
  cat <<EOF
fullnameOverride: alephat-platform-litellm

replicaCount: 1

image:
  repository: ghcr.io/berriai/litellm-database
  tag: v${LITELLM_VERSION#v}
  pullPolicy: IfNotPresent

masterkeySecretName: litellm-secrets
masterkeySecretKey: masterkey

environmentSecrets:
  - litellm-provider-keys

proxy_config:
  model_list:
$(render_litellm_model_list)
  general_settings:
    master_key: os.environ/PROXY_MASTER_KEY
  litellm_settings:
    drop_params: true

db:
  useExisting: false
  deployStandalone: true

postgresql:
  global:
    security:
      allowInsecureImages: true
  image:
    repository: bitnamilegacy/postgresql
  auth:
    username: litellm
    database: litellm
    existingSecret: litellm-postgres
    secretKeys:
      adminPasswordKey: postgres-password
      userPasswordKey: password
  primary:
    persistence:
      enabled: true
      storageClass: standard-rwo
      size: 20Gi

redis:
  enabled: false

resources:
  requests:
    cpu: 500m
    memory: 1Gi
  limits:
    cpu: "2"
    memory: 4Gi
EOF
}

echo "=================================================================="
echo "Alephat GKE Deployment Configuration"
echo "=================================================================="
echo "Project ID:             ${PROJECT_ID}"
echo "Region / Zone:          ${REGION} / ${ZONE}"
echo "Cluster Name:           ${CLUSTER_NAME} (k8s: ${K8S_VERSION}, channel: ${RELEASE_CHANNEL})"
echo "Sandbox Runtime:        ${SANDBOX_RUNTIME} (RuntimeClass: ${RUNTIME_CLASS_NAME})"
echo "Node Size / Count:      ${NODE_SIZE} x ${NUM_NODES}"
echo "Agent Sandbox Version:  ${AGENT_SANDBOX_VERSION}"
echo "Kata Version:           ${KATA_VERSION}"
echo "LiteLLM Version:        ${LITELLM_VERSION}"
echo "KEDA Version:           ${KEDA_VERSION}"
echo "Traefik Version:        ${TRAEFIK_VERSION} (enabled: ${WITH_TRAEFIK})"
echo "Enabled Models:         ${ENABLED_MODELS}"
echo "Default Model:          ${DEFAULT_MODEL}"
echo "=================================================================="

if [[ "${DRY_RUN}" == "true" ]]; then
  echo ""
  echo "--- Rendered SandboxTemplate (${SANDBOX_RUNTIME}) ---"
  render_sandbox_template
  echo ""
  echo "--- Rendered LiteLLM Values (v${LITELLM_VERSION#v}) ---"
  render_litellm_values
  exit 0
fi

if [[ "${TEARDOWN}" == "true" ]]; then
  echo "Tearing down GKE cluster '${CLUSTER_NAME}' in zone '${ZONE}'..."
  gcloud container clusters delete "${CLUSTER_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}" --quiet
  if gcloud compute addresses describe "${TRAEFIK_IP_NAME}" --project "${PROJECT_ID}" --region "${REGION}" >/dev/null 2>&1; then
    gcloud compute addresses delete "${TRAEFIK_IP_NAME}" --project "${PROJECT_ID}" --region "${REGION}" --quiet
  fi
  echo "Teardown complete."
  exit 0
fi

# ---------------------------------------------------------------------------
# 1. Enable required Google Cloud APIs
# ---------------------------------------------------------------------------
gcloud config set project "${PROJECT_ID}"
gcloud services enable \
  aiplatform.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  compute.googleapis.com \
  container.googleapis.com \
  dns.googleapis.com \
  iamcredentials.googleapis.com

# ---------------------------------------------------------------------------
# 2. Provision GKE Cluster & Sandbox Runtime (Kata or gVisor)
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
      --machine-type "${NODE_SIZE}" \
      --image-type COS_CONTAINERD \
      --enable-dataplane-v2 \
      --enable-ip-alias \
      --workload-pool="${PROJECT_ID}.svc.id.goog" \
      --workload-metadata=GKE_METADATA
  fi
fi

gcloud container clusters get-credentials "${CLUSTER_NAME}" --zone "${ZONE}"

if [[ "${SANDBOX_RUNTIME}" == "kata" ]]; then
  echo "Installing Kata Containers ${KATA_VERSION} and registering RuntimeClass/${RUNTIME_CLASS_NAME}..."
  kubectl apply -f "https://raw.githubusercontent.com/kata-containers/kata-containers/${KATA_VERSION}/tools/packaging/kata-deploy/kata-rbac/base/kata-rbac.yaml"
  kubectl apply -f "https://raw.githubusercontent.com/kata-containers/kata-containers/${KATA_VERSION}/tools/packaging/kata-deploy/kata-deploy/base/kata-deploy.yaml"
  kubectl -n kube-system rollout status daemonset/kata-deploy --timeout=10m
  cat <<EOF | kubectl apply -f -
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: ${RUNTIME_CLASS_NAME}
handler: ${RUNTIME_CLASS_NAME}
scheduling:
  nodeSelector:
    kubernetes.io/os: linux
EOF
else
  if ! gcloud container node-pools describe sandbox-pool --cluster "${CLUSTER_NAME}" --zone "${ZONE}" >/dev/null 2>&1; then
    echo "Creating gVisor node pool 'sandbox-pool'..."
    gcloud container node-pools create sandbox-pool \
      --cluster "${CLUSTER_NAME}" \
      --zone "${ZONE}" \
      --num-nodes "${NUM_NODES}" \
      --machine-type "${NODE_SIZE}" \
      --image-type COS_CONTAINERD \
      --sandbox type=gvisor \
      --workload-metadata=GKE_METADATA
  fi
  kubectl get runtimeclass gvisor
fi

# ---------------------------------------------------------------------------
# 3. Install Agent Sandbox Controller & CRDs
# ---------------------------------------------------------------------------
echo "Installing Agent Sandbox ${AGENT_SANDBOX_VERSION}..."
kubectl apply -f \
  "https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${AGENT_SANDBOX_VERSION}/sandbox-with-extensions.yaml"
kubectl wait --for=condition=Established crd/sandboxes.agents.x-k8s.io --timeout=2m
kubectl wait --for=condition=Established crd/sandboxtemplates.extensions.agents.x-k8s.io --timeout=2m

# ---------------------------------------------------------------------------
# 4. Install KEDA
# ---------------------------------------------------------------------------
echo "Installing KEDA ${KEDA_VERSION}..."
helm repo add kedacore https://kedacore.github.io/charts --force-update
helm repo update kedacore
helm upgrade --install keda kedacore/keda \
  --namespace keda \
  --create-namespace \
  --version "${KEDA_VERSION}"
kubectl -n keda rollout status deployment/keda-operator --timeout=5m

# ---------------------------------------------------------------------------
# 5. Deploy LiteLLM Gateway
# ---------------------------------------------------------------------------
kubectl create namespace "${PLATFORM_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -

LITELLM_MASTER_KEY="${LITELLM_MASTER_KEY:-sk-$(openssl rand -hex 24)}"
LITELLM_DB_PASSWORD="${LITELLM_DB_PASSWORD:-$(openssl rand -hex 24)}"

if ! kubectl -n "${PLATFORM_NAMESPACE}" get secret litellm-secrets >/dev/null 2>&1; then
  kubectl -n "${PLATFORM_NAMESPACE}" create secret generic litellm-secrets \
    --from-literal=masterkey="${LITELLM_MASTER_KEY}"
fi
if ! kubectl -n "${PLATFORM_NAMESPACE}" get secret litellm-provider-keys >/dev/null 2>&1; then
  kubectl -n "${PLATFORM_NAMESPACE}" create secret generic litellm-provider-keys \
    --from-literal=OPENAI_API_KEY="${OPENAI_API_KEY:-}" \
    --from-literal=ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}" \
    --from-literal=GEMINI_API_KEY="${GEMINI_API_KEY:-${GOOGLE_API_KEY:-}}" \
    --from-literal=FIREWORKS_API_KEY="${FIREWORKS_API_KEY:-}"
fi
if ! kubectl -n "${PLATFORM_NAMESPACE}" get secret litellm-postgres >/dev/null 2>&1; then
  kubectl -n "${PLATFORM_NAMESPACE}" create secret generic litellm-postgres \
    --from-literal=postgres-password="${LITELLM_DB_PASSWORD}" \
    --from-literal=password="${LITELLM_DB_PASSWORD}"
fi

LITELLM_GSA="alephat-litellm@${PROJECT_ID}.iam.gserviceaccount.com"
gcloud iam service-accounts describe "${LITELLM_GSA}" >/dev/null 2>&1 || \
  gcloud iam service-accounts create alephat-litellm --display-name="Alephat LiteLLM Vertex AI"
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${LITELLM_GSA}" \
  --role=roles/aiplatform.user \
  --condition=None >/dev/null
gcloud iam service-accounts add-iam-policy-binding "${LITELLM_GSA}" \
  --role=roles/iam.workloadIdentityUser \
  --member="serviceAccount:${PROJECT_ID}.svc.id.goog[${PLATFORM_NAMESPACE}/alephat-litellm]" >/dev/null

kubectl -n "${PLATFORM_NAMESPACE}" create serviceaccount alephat-litellm \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "${PLATFORM_NAMESPACE}" annotate serviceaccount alephat-litellm \
  "iam.gke.io/gcp-service-account=${LITELLM_GSA}" --overwrite

TMP_LITELLM_VALUES="$(mktemp)"
trap 'rm -f "${TMP_LITELLM_VALUES}"' EXIT
render_litellm_values > "${TMP_LITELLM_VALUES}"

echo "Deploying LiteLLM ${LITELLM_VERSION}..."
helm upgrade --install alephat-platform \
  oci://ghcr.io/berriai/litellm-helm \
  --namespace "${PLATFORM_NAMESPACE}" \
  --version "${LITELLM_VERSION#v}" \
  --values "${TMP_LITELLM_VALUES}" \
  --set-string postgresql.auth.password="${LITELLM_DB_PASSWORD}" \
  --set-string postgresql.auth.postgresPassword="${LITELLM_DB_PASSWORD}" \
  --set serviceAccount.name=alephat-litellm \
  --set-string envVars.VERTEXAI_PROJECT="${PROJECT_ID}" \
  --set-string envVars.VERTEXAI_LOCATION="${REGION}"
kubectl -n "${PLATFORM_NAMESPACE}" rollout status deployment/alephat-platform-litellm --timeout=10m

# ---------------------------------------------------------------------------
# 6. Ensure Platform Secrets Exist
# ---------------------------------------------------------------------------
if ! kubectl -n "${PLATFORM_NAMESPACE}" get secret alephat-postgres >/dev/null 2>&1; then
  kubectl -n "${PLATFORM_NAMESPACE}" create secret generic alephat-postgres \
    --from-literal=password="${ALEPHAT_DB_PASSWORD:-$(openssl rand -hex 24)}"
fi
if ! kubectl -n "${PLATFORM_NAMESPACE}" get secret alephat-secrets >/dev/null 2>&1; then
  kubectl -n "${PLATFORM_NAMESPACE}" create secret generic alephat-secrets \
    --from-literal=PLATFORM_API_TOKEN="${PLATFORM_API_TOKEN:-$(openssl rand -hex 32)}" \
    --from-literal=PLATFORM_JWT_SECRET="${PLATFORM_JWT_SECRET:-$(openssl rand -hex 32)}" \
    --from-literal=GITHUB_WEBHOOK_SECRET="${GITHUB_WEBHOOK_SECRET:-$(openssl rand -hex 32)}"
fi
if ! kubectl -n "${PLATFORM_NAMESPACE}" get secret alephat-oauth >/dev/null 2>&1; then
  kubectl -n "${PLATFORM_NAMESPACE}" create secret generic alephat-oauth \
    --from-literal=ALLOWED_GITHUB_USERS="${ALLOWED_GITHUB_USERS:-}" \
    --from-literal=GITHUB_APP_ID="${GITHUB_APP_ID:-}" \
    --from-literal=GITHUB_APP_CLIENT_ID="${GITHUB_APP_CLIENT_ID:-}" \
    --from-literal=GITHUB_APP_CLIENT_SECRET="${GITHUB_APP_CLIENT_SECRET:-}" \
    --from-literal=GITHUB_APP_INSTALLATION_ID="${GITHUB_APP_INSTALLATION_ID:-}" \
    --from-literal=GITHUB_APP_PRIVATE_KEY="${GITHUB_APP_PRIVATE_KEY:-}" \
    --from-literal=DASHBOARD_JWT_SECRET="${DASHBOARD_JWT_SECRET:-$(openssl rand -hex 32)}" \
    --from-literal=TOKEN_ENCRYPTION_KEY="${TOKEN_ENCRYPTION_KEY:-$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n')}"
fi

# ---------------------------------------------------------------------------
# 7. Build & Push Container Images
# ---------------------------------------------------------------------------
if [[ "${SKIP_BUILD}" != "true" ]]; then
  gcloud artifacts repositories describe "${AR_REPOSITORY}" --location "${REGION}" >/dev/null 2>&1 || \
    gcloud artifacts repositories create "${AR_REPOSITORY}" --repository-format docker --location "${REGION}"

  gcloud builds submit "${REPO_ROOT}" \
    --config "${REPO_ROOT}/cloudbuild.platform.yaml" \
    --substitutions="_PLATFORM_IMAGE=${PLATFORM_IMAGE}"
  gcloud builds submit "${REPO_ROOT}" \
    --config "${REPO_ROOT}/cloudbuild.ui.yaml" \
    --substitutions="_UI_IMAGE=${UI_IMAGE}"
  gcloud builds submit "${REPO_ROOT}" \
    --config "${REPO_ROOT}/cloudbuild.sandbox.yaml" \
    --substitutions="_SANDBOX_IMAGE=${SANDBOX_IMAGE}"
fi

# ---------------------------------------------------------------------------
# 8. Deploy SandboxTemplate, Platform Workloads, Harness, and KEDA Autoscaler
# ---------------------------------------------------------------------------
kubectl apply -f "${REPO_ROOT}/deploy/k8s/agent-sandbox-rbac.yaml"
kubectl apply -f "${REPO_ROOT}/deploy/k8s/agent-sandbox-networkpolicy.yaml"
render_sandbox_template | kubectl apply -f -

TMP_KUSTOMIZE="$(mktemp -d)"
trap 'rm -rf "${TMP_LITELLM_VALUES}" "${TMP_KUSTOMIZE}"' EXIT
cp "${REPO_ROOT}"/deploy/k8s/*.yaml "${TMP_KUSTOMIZE}/"
(
  cd "${TMP_KUSTOMIZE}"
  kustomize edit set image \
    "us-central1-docker.pkg.dev/your-gcp-project-id/alephat/alephat-platform=${PLATFORM_IMAGE}" \
    "us-central1-docker.pkg.dev/your-gcp-project-id/alephat/alephat-ui=${UI_IMAGE}"
)
kubectl apply -k "${TMP_KUSTOMIZE}"

kubectl -n "${PLATFORM_NAMESPACE}" patch configmap alephat-config \
  --type merge \
  --patch "{\"data\":{\"DEFAULT_MODEL\":\"${DEFAULT_MODEL}\",\"LITELLM_API_KEY\":\"${LITELLM_MASTER_KEY}\"}}"

kubectl -n "${PLATFORM_NAMESPACE}" wait --for=condition=complete job/alephat-migrate --timeout=5m
kubectl -n "${PLATFORM_NAMESPACE}" rollout status deployment/alephat-api --timeout=10m
kubectl -n "${PLATFORM_NAMESPACE}" rollout status deployment/alephat-webhook --timeout=10m
kubectl -n "${PLATFORM_NAMESPACE}" rollout status deployment/alephat-ui --timeout=10m
kubectl -n "${PLATFORM_NAMESPACE}" rollout status deployment/alephat-harness --timeout=10m

kubectl apply -f "${REPO_ROOT}/deploy/k8s/harness-keda.yaml"

# ---------------------------------------------------------------------------
# 9. Optional Traefik Public Ingress & TLS
# ---------------------------------------------------------------------------
if [[ "${WITH_TRAEFIK}" == "true" ]]; then
  if [[ -z "${ALEPHAT_HOST}" || -z "${ACME_EMAIL}" ]]; then
    echo "Error: --with-traefik requires --host <fqdn> and --acme-email <email>." >&2
    exit 1
  fi
  gcloud compute addresses describe "${TRAEFIK_IP_NAME}" --region "${REGION}" >/dev/null 2>&1 || \
    gcloud compute addresses create "${TRAEFIK_IP_NAME}" --region "${REGION}" --network-tier PREMIUM
  TRAEFIK_IP="$(gcloud compute addresses describe "${TRAEFIK_IP_NAME}" --region "${REGION}" --format='value(address)')"

  helm repo add traefik https://traefik.github.io/charts --force-update
  helm repo update traefik
  helm upgrade --install traefik traefik/traefik \
    --namespace traefik \
    --create-namespace \
    --version "${TRAEFIK_VERSION}" \
    --values "${REPO_ROOT}/deploy/k8s/traefik-values.yaml" \
    --set-string "service.spec.loadBalancerIP=${TRAEFIK_IP}" \
    --set-string "certificatesResolvers.letsencrypt.acme.email=${ACME_EMAIL}" \
    --wait

  if [[ -n "${DNS_ZONE}" && -n "${DNS_DOMAIN}" ]]; then
    gcloud dns managed-zones describe "${DNS_ZONE}" >/dev/null 2>&1 || \
      gcloud dns managed-zones create "${DNS_ZONE}" \
        --dns-name="${DNS_DOMAIN%.}." \
        --description="Alephat public DNS zone"
    gcloud dns record-sets describe "${ALEPHAT_HOST%.}." --zone "${DNS_ZONE}" --type A >/dev/null 2>&1 || \
      gcloud dns record-sets create "${ALEPHAT_HOST%.}." \
        --zone "${DNS_ZONE}" \
        --type A \
        --ttl 300 \
        --rrdatas "${TRAEFIK_IP}"
  fi

  sed "s/alephat\.example\.com/${ALEPHAT_HOST}/g" \
    "${REPO_ROOT}/deploy/k8s/traefik-ingressroute.yaml" | kubectl apply -f -

  ALEPHAT_ORIGIN="https://${ALEPHAT_HOST}"
  kubectl -n "${PLATFORM_NAMESPACE}" patch configmap alephat-config \
    --type merge \
    --patch "{\"data\":{\"DASHBOARD_BASE_URL\":\"${ALEPHAT_ORIGIN}\",\"DASHBOARD_API_BASE_URL\":\"${ALEPHAT_ORIGIN}\",\"DASHBOARD_ALLOWED_ORIGINS\":\"${ALEPHAT_ORIGIN}\",\"CORS_ORIGINS\":\"${ALEPHAT_ORIGIN}\"}}"
  kubectl -n "${PLATFORM_NAMESPACE}" rollout restart deployment/alephat-api deployment/alephat-webhook deployment/alephat-harness
fi

echo "Deployment complete!"
kubectl -n "${PLATFORM_NAMESPACE}" get pods,svc,scaledobject
kubectl -n "${SANDBOX_NAMESPACE}" get sandboxtemplate
