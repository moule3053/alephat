#!/usr/bin/env bash
# Provision a local k3s Kubernetes cluster and deploy the entire Alephat stack
# via the unified multi-namespace Helm chart (deploy/helm/alephat):
#   - On macOS (or Linux with --mode k3d): provisions real k3s inside Docker via k3d
#   - On Linux (--mode native, or auto when k3d is absent): installs native k3s
#
# All 4 Alephat images are built locally and imported directly into k3s containerd
# with zero cloud registry required.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CHART_DIR="${REPO_ROOT}/deploy/helm/alephat"
K3S_BASE_VALUES="${CHART_DIR}/values-k3s.yaml"

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
      TOKEN_ENCRYPTION_KEY|DASHBOARD_JWT_SECRET|PLATFORM_API_TOKEN|PLATFORM_JWT_SECRET|\
      DEFAULT_MODEL|ENABLED_MODELS|DEFAULT_SANDBOX_PROVIDER|\
      SUBSTRATE_API_ENDPOINT|SUBSTRATE_API_TLS|SUBSTRATE_API_SERVER_NAME|\
      SUBSTRATE_ROUTER_URL|SUBSTRATE_ROUTER_SERVER_NAME|SUBSTRATE_ATESPACE|SUBSTRATE_ACTOR_TEMPLATE|\
      SUBSTRATE_SANDBOX_PROTOCOL|SUBSTRATE_EGRESS_ALLOWED_HOSTS|SUBSTRATE_SUSPEND_ON_STOP)
        if [[ -z "${!k:-}" && -n "${v}" ]]; then
          export "${k}=${v}"
        fi
        ;;
    esac
  done < "${SCRIPT_DIR}/.env"
fi

K3S_MODE="${K3S_MODE:-auto}"                          # auto | k3d | native
CLUSTER_NAME="${CLUSTER_NAME:-alephat}"
RELEASE_NAME="${RELEASE_NAME:-alephat}"
PLATFORM_NAMESPACE="${PLATFORM_NAMESPACE:-alephat}"
K3S_AGENTS="${K3S_AGENTS:-0}"
K3S_IMAGE="${K3S_IMAGE:-rancher/k3s:v1.32.4-k3s1}"

HTTP_PORT="${HTTP_PORT:-8080}"
HTTPS_PORT="${HTTPS_PORT:-8443}"
TEMPORAL_PORT="${TEMPORAL_PORT:-7233}"

ALEPHAT_HOST="${ALEPHAT_HOST:-alephat.localhost}"
TEMPORAL_UI_HOST="${TEMPORAL_UI_HOST:-temporal.localhost}"
TEMPORAL_GRPC_HOST="${TEMPORAL_GRPC_HOST:-temporal-grpc.localhost}"

SANDBOX_RUNTIME="${SANDBOX_RUNTIME:-none}"            # none | kata | gvisor
DEFAULT_SANDBOX_PROVIDER="${DEFAULT_SANDBOX_PROVIDER:-agent_sandbox}"
API_AUTH_DISABLED="${API_AUTH_DISABLED:-false}"

ENABLED_MODELS="${ENABLED_MODELS:-google:gemini-4-hydrogen,google:gemini-4-carbon,google:gemini-4-argon,google:gemini-3.8-flash,google:gemini-3.5-flash,google:gemini-3.1-pro-preview,google:gemini-2.5-pro,google:gemini-2.5-flash,anthropic:claude-opus-5-5,anthropic:claude-sonnet-5-5,anthropic:claude-haiku-5-5,anthropic:claude-opus-4-6,anthropic:claude-sonnet-4-6,anthropic:claude-sonnet-4-5,openai:gpt-6.1-terra,openai:gpt-6.1-sol,openai:gpt-6-luna,openai:gpt-6-astra,openai:gpt-5.5,openai:gpt-4.1,openai:o4-mini}"
DEFAULT_MODEL="${DEFAULT_MODEL:-}"

SUBSTRATE_API_ENDPOINT="${SUBSTRATE_API_ENDPOINT:-api.ate-system.svc:443}"
SUBSTRATE_API_TLS="${SUBSTRATE_API_TLS:-true}"
SUBSTRATE_API_SERVER_NAME="${SUBSTRATE_API_SERVER_NAME:-api.ate-system.svc}"
SUBSTRATE_ROUTER_URL="${SUBSTRATE_ROUTER_URL:-https://atenet-router.ate-system.svc:443}"
SUBSTRATE_ROUTER_SERVER_NAME="${SUBSTRATE_ROUTER_SERVER_NAME:-atenet-router.ate-system.svc}"
SUBSTRATE_ATESPACE="${SUBSTRATE_ATESPACE:-alephat-sandboxes}"
SUBSTRATE_ACTOR_TEMPLATE="${SUBSTRATE_ACTOR_TEMPLATE:-alephat-sandbox}"
SUBSTRATE_SANDBOX_PROTOCOL="${SUBSTRATE_SANDBOX_PROTOCOL:-toolbox}"
SUBSTRATE_EGRESS_ALLOWED_HOSTS="${SUBSTRATE_EGRESS_ALLOWED_HOSTS:-github.com,api.github.com,objects.githubusercontent.com,pypi.org,files.pythonhosted.org,registry.npmjs.org}"
SUBSTRATE_SUSPEND_ON_STOP="${SUBSTRATE_SUSPEND_ON_STOP:-true}"

PLATFORM_IMAGE="${PLATFORM_IMAGE:-alephat-platform:local}"
UI_IMAGE="${UI_IMAGE:-alephat-ui:local}"
SLACK_RENDERER_IMAGE="${SLACK_RENDERER_IMAGE:-alephat-slack-renderer:local}"
SANDBOX_IMAGE="${SANDBOX_IMAGE:-alephat-sandbox:local}"

REUSE_CLUSTER=true
SKIP_BUILD=false
INSTALL_TOOLS=false
TEARDOWN=false
DRY_RUN=false

usage() {
  cat <<'EOF'
Usage: ./deploy/k3s-deploy.sh [options]

Provision local k3s (via k3d on macOS/Docker or native k3s on Linux) and deploy
the entire Alephat multi-namespace stack using deploy/helm/alephat.

Cluster & Runtime Options:
  --mode <auto|k3d|native>        k3s provisioning mode (default: auto; macOS=k3d, Linux=native/k3d)
  --cluster-name <name>           k3d cluster name (default: alephat)
  --agents <count>                Number of extra k3d agent nodes (default: 0; single-node k3s server)
  --k3s-image <image>             k3s container image for k3d (default: rancher/k3s:v1.32.4-k3s1)
  --sandbox-provider <provider>   DEFAULT_SANDBOX_PROVIDER: agent_sandbox | substrate | local (default: agent_sandbox)
  --sandbox-runtime <none|kata|gvisor> Sandbox RuntimeClass (default: none for local k3s)
  --install-k3d                   Auto-install k3d if missing (Homebrew on macOS / official script on Linux)

Port & Hostname Options:
  --http-port <port>              Local HTTP ingress port for Traefik (default: 8080)
  --https-port <port>             Local HTTPS ingress port for Traefik (default: 8443)
  --temporal-port <port>          Local Temporal gRPC mTLS port (default: 7233)
  --host <hostname>               Local dashboard hostname (default: alephat.localhost)
  --temporal-ui-host <hostname>   Local Temporal UI hostname (default: temporal.localhost)
  --temporal-grpc-host <hostname> Local Temporal gRPC hostname (default: temporal-grpc.localhost)
  --no-auth                       Set API_AUTH_DISABLED=true for passwordless local API testing

Model & Lifecycle Options:
  --models <csv>                  Comma-separated LiteLLM coding model aliases
  --default-model <id>            Default model ID (default: first in --models)
  --skip-build                    Skip local Docker image builds & import
  --recreate-cluster              Delete and recreate an existing k3d cluster before deploying
  --teardown                      Tear down the local k3s cluster
  --dry-run                       Print resolved configuration and validate Helm render without applying
  -h, --help                      Show this help text
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) K3S_MODE="$2"; shift 2 ;;
    --cluster-name) CLUSTER_NAME="$2"; shift 2 ;;
    --agents) K3S_AGENTS="$2"; shift 2 ;;
    --k3s-image) K3S_IMAGE="$2"; shift 2 ;;
    --sandbox-provider) DEFAULT_SANDBOX_PROVIDER="$2"; shift 2 ;;
    --sandbox-runtime) SANDBOX_RUNTIME="$2"; shift 2 ;;
    --http-port) HTTP_PORT="$2"; shift 2 ;;
    --https-port) HTTPS_PORT="$2"; shift 2 ;;
    --temporal-port) TEMPORAL_PORT="$2"; shift 2 ;;
    --host) ALEPHAT_HOST="$2"; shift 2 ;;
    --temporal-ui-host) TEMPORAL_UI_HOST="$2"; shift 2 ;;
    --temporal-grpc-host) TEMPORAL_GRPC_HOST="$2"; shift 2 ;;
    --no-auth) API_AUTH_DISABLED="true"; shift ;;
    --models) ENABLED_MODELS="$2"; shift 2 ;;
    --default-model) DEFAULT_MODEL="$2"; shift 2 ;;
    --install-k3d) INSTALL_TOOLS=true; shift ;;
    --skip-build) SKIP_BUILD=true; shift ;;
    --recreate-cluster) REUSE_CLUSTER=false; shift ;;
    --reuse-cluster) REUSE_CLUSTER=true; shift ;;
    --teardown) TEARDOWN=true; shift ;;
    --dry-run) DRY_RUN=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
done

HOST_OS="$(uname -s)"
if [[ "${K3S_MODE}" == "auto" ]]; then
  if [[ "${HOST_OS}" == "Darwin" ]]; then
    K3S_MODE="k3d"
  elif command -v k3d >/dev/null 2>&1; then
    K3S_MODE="k3d"
  else
    K3S_MODE="native"
  fi
fi

if [[ "${K3S_MODE}" != "k3d" && "${K3S_MODE}" != "native" ]]; then
  echo "Error: --mode must be 'auto', 'k3d', or 'native' (got '${K3S_MODE}')." >&2
  exit 1
fi
if [[ "${K3S_MODE}" == "native" && "${HOST_OS}" == "Darwin" ]]; then
  echo "Error: Native k3s requires Linux; use --mode k3d on macOS." >&2
  exit 1
fi
if [[ "${SANDBOX_RUNTIME}" != "none" && "${SANDBOX_RUNTIME}" != "kata" && "${SANDBOX_RUNTIME}" != "gvisor" ]]; then
  echo "Error: --sandbox-runtime must be 'none', 'kata', or 'gvisor' (got '${SANDBOX_RUNTIME}')." >&2
  exit 1
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

if [[ "${K3S_MODE}" == "native" ]]; then
  DASHBOARD_ORIGIN="http://${ALEPHAT_HOST}"
  TEMPORAL_UI_URL="http://${TEMPORAL_UI_HOST}"
else
  if [[ "${HTTP_PORT}" == "80" ]]; then
    DASHBOARD_ORIGIN="http://${ALEPHAT_HOST}"
    TEMPORAL_UI_URL="http://${TEMPORAL_UI_HOST}"
  else
    DASHBOARD_ORIGIN="http://${ALEPHAT_HOST}:${HTTP_PORT}"
    TEMPORAL_UI_URL="http://${TEMPORAL_UI_HOST}:${HTTP_PORT}"
  fi
fi

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

render_k3s_overrides() {
  cat <<EOF
sandboxRuntime:
  type: ${SANDBOX_RUNTIME}
  kata:
    k8sDistribution: k3s

agentSandbox:
  enabled: true
  sandboxImage: ${SANDBOX_IMAGE}
  imagePullPolicy: IfNotPresent

substrate:
  enabled: $([[ "${DEFAULT_SANDBOX_PROVIDER}" == "substrate" ]] && echo "true" || echo "false")
  apiEndpoint: "${SUBSTRATE_API_ENDPOINT}"
  apiTls: "${SUBSTRATE_API_TLS}"
  apiServerName: "${SUBSTRATE_API_SERVER_NAME}"
  routerUrl: "${SUBSTRATE_ROUTER_URL}"
  routerServerName: "${SUBSTRATE_ROUTER_SERVER_NAME}"
  atespace: "${SUBSTRATE_ATESPACE}"
  actorTemplate: "${SUBSTRATE_ACTOR_TEMPLATE}"
  protocol: "${SUBSTRATE_SANDBOX_PROTOCOL}"
  egressAllowedHosts: "${SUBSTRATE_EGRESS_ALLOWED_HOSTS}"
  suspendOnStop: "${SUBSTRATE_SUSPEND_ON_STOP}"

litellm:
  enabled: true
  defaultModel: ${DEFAULT_MODEL}
  models:
$(render_litellm_models_yaml)

alephat:
  platformImage: ${PLATFORM_IMAGE}
  uiImage: ${UI_IMAGE}
  slackRendererImage: ${SLACK_RENDERER_IMAGE}
  imagePullPolicy: IfNotPresent
  config:
    defaultSandboxProvider: "${DEFAULT_SANDBOX_PROVIDER}"
    apiAuthDisabled: "${API_AUTH_DISABLED}"
    dashboardBaseUrl: "${DASHBOARD_ORIGIN}"

ingress:
  enabled: true
  host: ${ALEPHAT_HOST}
  temporalUiHost: ${TEMPORAL_UI_HOST}
  temporalGrpcHost: ${TEMPORAL_GRPC_HOST}
  tls:
    enabled: false
  temporalUiBasicAuth:
    enabled: false

secrets:
  platformApiToken: "${PLATFORM_API_TOKEN:-}"
  platformJwtSecret: "${PLATFORM_JWT_SECRET:-}"
  githubWebhookSecret: "${GITHUB_WEBHOOK_SECRET:-}"
  dashboardJwtSecret: "${DASHBOARD_JWT_SECRET:-}"
  tokenEncryptionKey: "${TOKEN_ENCRYPTION_KEY:-}"
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
}

echo "=================================================================="
echo "Alephat Local k3s Deployment Configuration"
echo "=================================================================="
echo "Host OS / k3s Mode:     ${HOST_OS} / ${K3S_MODE}"
echo "Cluster / Release:      ${CLUSTER_NAME} / ${RELEASE_NAME}"
echo "Helm Chart:             ${CHART_DIR}"
echo "Sandbox Provider:       ${DEFAULT_SANDBOX_PROVIDER}"
echo "Sandbox Runtime:        ${SANDBOX_RUNTIME}"
echo "Default Model:          ${DEFAULT_MODEL}"
echo "Dashboard URL:          ${DASHBOARD_ORIGIN}"
echo "Temporal Web UI URL:    ${TEMPORAL_UI_URL}"
echo "Temporal gRPC (mTLS):   ${TEMPORAL_GRPC_HOST}:${TEMPORAL_PORT}"
echo "=================================================================="

if [[ "${DRY_RUN}" == "true" ]]; then
  TMP_DRY_VALUES="$(mktemp)"
  trap 'rm -f "${TMP_DRY_VALUES}"' EXIT
  render_k3s_overrides > "${TMP_DRY_VALUES}"
  echo "--- Rendered Local k3s Override Values ---"
  cat "${TMP_DRY_VALUES}"
  helm template "${RELEASE_NAME}" "${CHART_DIR}" \
    --namespace "${PLATFORM_NAMESPACE}" \
    --values "${K3S_BASE_VALUES}" \
    --values "${TMP_DRY_VALUES}" >/dev/null
  echo "--- Helm Template Verification (k3s): OK ---"
  exit 0
fi

if [[ "${TEARDOWN}" == "true" ]]; then
  exec "${SCRIPT_DIR}/k3s-teardown.sh" --mode "${K3S_MODE}" --cluster-name "${CLUSTER_NAME}"
fi

for cmd in docker kubectl helm jq; do
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    echo "Error: required command '${cmd}' is not installed." >&2
    exit 1
  fi
done

if [[ -z "${DOCKER_HOST:-}" ]]; then
  RESOLVED_DOCKER_HOST="$(docker context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null || true)"
  if [[ -n "${RESOLVED_DOCKER_HOST}" ]]; then
    export DOCKER_HOST="${RESOLVED_DOCKER_HOST}"
  fi
fi

# ---------------------------------------------------------------------------
# 1. Provision Local k3s Cluster
# ---------------------------------------------------------------------------
if [[ "${K3S_MODE}" == "k3d" ]]; then
  if ! command -v k3d >/dev/null 2>&1; then
    if [[ "${INSTALL_TOOLS}" == "true" ]]; then
      if [[ "${HOST_OS}" == "Darwin" ]] && command -v brew >/dev/null 2>&1; then
        echo "Installing k3d via Homebrew..."
        brew install k3d
      else
        echo "Installing k3d via official install script..."
        curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash
      fi
    else
      echo "Error: 'k3d' is required for --mode k3d. Install it (e.g. 'brew install k3d') or re-run with --install-k3d." >&2
      exit 1
    fi
  fi

  if k3d cluster list -o json | jq -e --arg c "${CLUSTER_NAME}" '.[] | select(.name == $c)' >/dev/null 2>&1; then
    if [[ "${REUSE_CLUSTER}" != "true" ]]; then
      echo "Deleting existing k3d cluster '${CLUSTER_NAME}'..."
      k3d cluster delete "${CLUSTER_NAME}"
    else
      echo "Reusing existing k3d cluster '${CLUSTER_NAME}'..."
    fi
  fi

  if ! k3d cluster list -o json | jq -e --arg c "${CLUSTER_NAME}" '.[] | select(.name == $c)' >/dev/null 2>&1; then
    echo "Creating k3d (k3s) cluster '${CLUSTER_NAME}' with Traefik disabled so Alephat's Traefik owns :${HTTP_PORT}/:${HTTPS_PORT}/:${TEMPORAL_PORT}..."
    k3d cluster create "${CLUSTER_NAME}" \
      --image "${K3S_IMAGE}" \
      --agents "${K3S_AGENTS}" \
      --k3s-arg "--disable=traefik@server:0" \
      -p "${HTTP_PORT}:80@loadbalancer" \
      -p "${HTTPS_PORT}:443@loadbalancer" \
      -p "${TEMPORAL_PORT}:7233@loadbalancer" \
      --wait
  fi

  kubectl config use-context "k3d-${CLUSTER_NAME}" >/dev/null
else
  if ! command -v k3s >/dev/null 2>&1; then
    echo "Installing native k3s on Linux (with bundled Traefik disabled)..."
    curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="server --disable=traefik --write-kubeconfig-mode=644" sh -
  else
    echo "Reusing existing native k3s installation..."
  fi
  export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"
fi

echo "Waiting for k3s node(s) to become Ready..."
kubectl wait --for=condition=Ready nodes --all --timeout=3m

# ---------------------------------------------------------------------------
# 2. Build & Import Local Container Images into k3s
# ---------------------------------------------------------------------------
if [[ "${SKIP_BUILD}" != "true" ]]; then
  echo "Building local Alephat images (platform, ui, slack-renderer, sandbox)..."
  docker build --network=host -f "${REPO_ROOT}/deploy/Dockerfile.harness" -t "${PLATFORM_IMAGE}" "${REPO_ROOT}"
  docker build --network=host -f "${REPO_ROOT}/deploy/Dockerfile.ui" -t "${UI_IMAGE}" "${REPO_ROOT}"
  docker build --network=host -f "${REPO_ROOT}/deploy/Dockerfile.slack-renderer" -t "${SLACK_RENDERER_IMAGE}" "${REPO_ROOT}"
  docker build --network=host -f "${REPO_ROOT}/deploy/Dockerfile.sandbox" -t "${SANDBOX_IMAGE}" "${REPO_ROOT}"
  docker image prune -f >/dev/null 2>&1 || true

  echo "Importing images into local k3s containerd..."
  if [[ "${K3S_MODE}" == "k3d" ]]; then
    for img in "${PLATFORM_IMAGE}" "${UI_IMAGE}" "${SLACK_RENDERER_IMAGE}" "${SANDBOX_IMAGE}"; do
      k3d image import "${img}" -c "${CLUSTER_NAME}"
    done
  else
    for img in "${PLATFORM_IMAGE}" "${UI_IMAGE}" "${SLACK_RENDERER_IMAGE}" "${SANDBOX_IMAGE}"; do
      docker save "${img}" | sudo k3s ctr images import -
    done
  fi
fi

# ---------------------------------------------------------------------------
# 3. Deploy Full Alephat Ecosystem via Unified Helm Chart
# ---------------------------------------------------------------------------
TMP_K3S_VALUES="$(mktemp)"
trap 'rm -f "${TMP_K3S_VALUES}"' EXIT
render_k3s_overrides > "${TMP_K3S_VALUES}"

echo "Deploying unified Helm chart '${CHART_DIR}' onto local k3s..."
# Note: omit Helm --wait because Helm v3's ReadyChecker looks up cross-namespace PVCs
# in the release namespace (helm/helm#10075); we verify every namespace rollout below.
helm upgrade --install "${RELEASE_NAME}" "${CHART_DIR}" \
  --namespace "${PLATFORM_NAMESPACE}" \
  --create-namespace \
  --values "${K3S_BASE_VALUES}" \
  --values "${TMP_K3S_VALUES}" \
  --timeout 15m

if [[ "${DEFAULT_SANDBOX_PROVIDER}" == "substrate" ]]; then
  kubectl label namespace "${PLATFORM_NAMESPACE}" ate.dev/api-access=true --overwrite
fi

echo "Waiting for workloads across all namespaces to become Ready..."
kubectl -n postgresql rollout status statefulset/alephat-postgresql --timeout=5m
kubectl -n temporal rollout status deployment/temporal --timeout=10m
kubectl -n temporal rollout status deployment/temporal-ui --timeout=5m
kubectl -n litellm rollout status statefulset/litellm-postgresql --timeout=5m
kubectl -n litellm rollout status deployment/litellm --timeout=10m
kubectl -n agent-sandbox-system rollout status deployment/agent-sandbox-controller --timeout=5m
kubectl -n keda rollout status deployment/keda-operator --timeout=5m
kubectl -n traefik rollout status deployment/traefik --timeout=5m
kubectl -n "${PLATFORM_NAMESPACE}" rollout status deployment/alephat-api --timeout=10m
kubectl -n "${PLATFORM_NAMESPACE}" rollout status deployment/alephat-webhook --timeout=10m
kubectl -n "${PLATFORM_NAMESPACE}" rollout status deployment/alephat-ui --timeout=5m
kubectl -n "${PLATFORM_NAMESPACE}" rollout status deployment/alephat-harness --timeout=10m

echo ""
echo "=================================================================="
echo "Alephat Local k3s Deployment Ready!"
echo "=================================================================="
echo "Dashboard & API:      ${DASHBOARD_ORIGIN}"
echo "Temporal Web UI:      ${TEMPORAL_UI_URL}"
echo "Temporal gRPC (mTLS): ${TEMPORAL_GRPC_HOST}:${TEMPORAL_PORT}"
echo "Sandbox Provider:     ${DEFAULT_SANDBOX_PROVIDER} (runtime: ${SANDBOX_RUNTIME})"
echo "=================================================================="
kubectl -n "${PLATFORM_NAMESPACE}" get pods,svc,scaledobject
