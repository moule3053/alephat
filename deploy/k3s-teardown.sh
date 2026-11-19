#!/usr/bin/env bash
# Tear down a local k3s / k3d Alephat deployment.

set -euo pipefail

K3S_MODE="${K3S_MODE:-auto}"
CLUSTER_NAME="${CLUSTER_NAME:-alephat}"
RELEASE_NAME="${RELEASE_NAME:-alephat}"
PLATFORM_NAMESPACE="${PLATFORM_NAMESPACE:-alephat}"
HELM_ONLY=false
DRY_RUN=false

usage() {
  cat <<'EOF'
Usage: ./deploy/k3s-teardown.sh [options]

Options:
  --mode <auto|k3d|native>   k3s mode to tear down (default: auto)
  --cluster-name <name>      k3d cluster name (default: alephat)
  --helm-only                Uninstall only the Helm release & namespaces, keeping k3s running
  --dry-run                  Print teardown actions without deleting anything
  -h, --help                 Show this help text
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) K3S_MODE="$2"; shift 2 ;;
    --cluster-name) CLUSTER_NAME="$2"; shift 2 ;;
    --helm-only) HELM_ONLY=true; shift ;;
    --dry-run) DRY_RUN=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
done

HOST_OS="$(uname -s)"
if [[ "${K3S_MODE}" == "auto" ]]; then
  if [[ "${HOST_OS}" == "Darwin" ]] || command -v k3d >/dev/null 2>&1; then
    K3S_MODE="k3d"
  else
    K3S_MODE="native"
  fi
fi

if [[ "${DRY_RUN}" == "true" ]]; then
  echo "[dry-run] Mode=${K3S_MODE}, cluster=${CLUSTER_NAME}, helm_only=${HELM_ONLY}"
  exit 0
fi

if [[ "${HELM_ONLY}" == "true" ]]; then
  echo "Uninstalling Helm release '${RELEASE_NAME}' from namespace '${PLATFORM_NAMESPACE}'..."
  helm uninstall "${RELEASE_NAME}" --namespace "${PLATFORM_NAMESPACE}" --ignore-not-found || true
  for ns in alephat alephat-sandboxes alephat-substrate agent-sandbox-system kata-system postgresql temporal litellm keda external-secrets traefik; do
    kubectl delete namespace "${ns}" --ignore-not-found --wait=false || true
  done
  echo "Helm uninstall complete."
  exit 0
fi

if [[ "${K3S_MODE}" == "k3d" ]]; then
  if [[ -z "${DOCKER_HOST:-}" ]] && command -v docker >/dev/null 2>&1; then
    RESOLVED_DOCKER_HOST="$(docker context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null || true)"
    [[ -n "${RESOLVED_DOCKER_HOST}" ]] && export DOCKER_HOST="${RESOLVED_DOCKER_HOST}"
  fi
  if command -v k3d >/dev/null 2>&1; then
    echo "Deleting k3d cluster '${CLUSTER_NAME}'..."
    k3d cluster delete "${CLUSTER_NAME}" || true
  else
    echo "k3d not found; nothing to delete."
  fi
else
  if [[ -x /usr/local/bin/k3s-uninstall.sh ]]; then
    echo "Uninstalling native k3s..."
    sudo /usr/local/bin/k3s-uninstall.sh
  else
    echo "/usr/local/bin/k3s-uninstall.sh not found; skipping native k3s uninstall."
  fi
fi

echo "Local k3s teardown complete."
