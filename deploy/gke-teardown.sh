#!/usr/bin/env bash
# Teardown script for an Alephat GKE deployment:
#   - Deletes the GKE cluster
#   - Deletes the reserved Traefik regional static IP (if present)
#   - Optionally deletes the Cloud DNS A record (--host & --dns-zone)
#   - Optionally deletes the Artifact Registry repository (--delete-images)
#   - Optionally deletes the LiteLLM GCP Service Account (--delete-iam)

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
REGION="${REGION:-us-central1}"
ZONE="${ZONE:-}"
CLUSTER_NAME="${CLUSTER_NAME:-alephat}"
AR_REPOSITORY="${AR_REPOSITORY:-alephat}"
TRAEFIK_IP_NAME="${TRAEFIK_IP_NAME:-alephat-traefik}"
ALEPHAT_HOST="${ALEPHAT_HOST:-}"
DNS_ZONE="${DNS_ZONE:-}"

DELETE_IMAGES=false
DELETE_IAM=false
DELETE_SECRETS=false
DRY_RUN=false

usage() {
  cat <<'EOF'
Usage: ./deploy/gke-teardown.sh [options]

Options:
  --project <id>             GCP project ID (default: active gcloud project)
  --region <region>          GCP region (default: us-central1)
  --zone <zone>              GCP zone (default: <region>-a)
  --cluster-name <name>      GKE cluster name (default: alephat)
  --traefik-ip-name <name>   Static IP address name (default: alephat-traefik)
  --host <fqdn>              Public hostname whose Cloud DNS A record should be deleted
  --dns-zone <zone-name>     Cloud DNS managed zone name
  --ar-repository <name>     Artifact Registry repository name (default: alephat)
  --delete-images            Also delete the Artifact Registry repository and images
  --delete-iam               Also delete the alephat-litellm and alephat-eso GCP service accounts
  --delete-secrets           Also delete the alephat-* secrets in Google Cloud Secret Manager
  --dry-run                  Print teardown actions without deleting resources
  -h, --help                 Show this help text
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT_ID="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --zone) ZONE="$2"; shift 2 ;;
    --cluster-name) CLUSTER_NAME="$2"; shift 2 ;;
    --traefik-ip-name) TRAEFIK_IP_NAME="$2"; shift 2 ;;
    --host) ALEPHAT_HOST="$2"; shift 2 ;;
    --dns-zone) DNS_ZONE="$2"; shift 2 ;;
    --ar-repository) AR_REPOSITORY="$2"; shift 2 ;;
    --delete-images) DELETE_IMAGES=true; shift ;;
    --delete-iam) DELETE_IAM=true; shift ;;
    --delete-secrets) DELETE_SECRETS=true; shift ;;
    --dry-run) DRY_RUN=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
done

if [[ -z "${ZONE}" ]]; then
  ZONE="${REGION}-a"
fi

if [[ -z "${PROJECT_ID}" && "${DRY_RUN}" != "true" ]]; then
  echo "Error: --project <gcp-project-id> is required." >&2
  exit 1
fi
PROJECT_ID="${PROJECT_ID:-your-gcp-project-id}"

echo "=================================================================="
echo "Alephat GKE Teardown Plan"
echo "=================================================================="
echo "Project ID:             ${PROJECT_ID}"
echo "Region / Zone:          ${REGION} / ${ZONE}"
echo "Cluster Name:           ${CLUSTER_NAME}"
echo "Traefik Static IP:      ${TRAEFIK_IP_NAME}"
echo "DNS Record / Zone:      ${ALEPHAT_HOST:-<none>} / ${DNS_ZONE:-<none>}"
echo "Delete Images (${AR_REPOSITORY}): ${DELETE_IMAGES}"
echo "Delete IAM SA:          ${DELETE_IAM}"
echo "Delete GSM Secrets:     ${DELETE_SECRETS}"
echo "=================================================================="

if [[ "${DRY_RUN}" == "true" ]]; then
  echo "[dry-run] Would delete GKE cluster '${CLUSTER_NAME}' in '${ZONE}'."
  echo "[dry-run] Would delete static IP '${TRAEFIK_IP_NAME}' in '${REGION}' if present."
  [[ -n "${ALEPHAT_HOST}" && -n "${DNS_ZONE}" ]] && echo "[dry-run] Would delete DNS A record '${ALEPHAT_HOST%.}.' in zone '${DNS_ZONE}'."
  [[ "${DELETE_IMAGES}" == "true" ]] && echo "[dry-run] Would delete Artifact Registry repository '${AR_REPOSITORY}' in '${REGION}'."
  [[ "${DELETE_IAM}" == "true" ]] && echo "[dry-run] Would delete service accounts 'alephat-litellm@${PROJECT_ID}.iam.gserviceaccount.com' and 'alephat-eso@${PROJECT_ID}.iam.gserviceaccount.com'."
  [[ "${DELETE_SECRETS}" == "true" ]] && echo "[dry-run] Would delete alephat-* secrets in Google Cloud Secret Manager."
  exit 0
fi

# 1. Delete Cloud DNS A record if specified
if [[ -n "${ALEPHAT_HOST}" && -n "${DNS_ZONE}" ]]; then
  if gcloud dns record-sets describe "${ALEPHAT_HOST%.}." --project "${PROJECT_ID}" --zone "${DNS_ZONE}" --type A >/dev/null 2>&1; then
    echo "Deleting Cloud DNS A record '${ALEPHAT_HOST%.}.' from zone '${DNS_ZONE}'..."
    gcloud dns record-sets delete "${ALEPHAT_HOST%.}." \
      --project "${PROJECT_ID}" \
      --zone "${DNS_ZONE}" \
      --type A \
      --quiet
  fi
fi

# 2. Delete GKE Cluster
if gcloud container clusters describe "${CLUSTER_NAME}" --project "${PROJECT_ID}" --zone "${ZONE}" >/dev/null 2>&1; then
  echo "Deleting GKE cluster '${CLUSTER_NAME}' in zone '${ZONE}'..."
  gcloud container clusters delete "${CLUSTER_NAME}" \
    --project "${PROJECT_ID}" \
    --zone "${ZONE}" \
    --quiet
else
  echo "GKE cluster '${CLUSTER_NAME}' not found in zone '${ZONE}'; skipping."
fi

# 3. Delete Static External IP
if gcloud compute addresses describe "${TRAEFIK_IP_NAME}" --project "${PROJECT_ID}" --region "${REGION}" >/dev/null 2>&1; then
  echo "Deleting regional static IP '${TRAEFIK_IP_NAME}' in '${REGION}'..."
  gcloud compute addresses delete "${TRAEFIK_IP_NAME}" \
    --project "${PROJECT_ID}" \
    --region "${REGION}" \
    --quiet
fi

# 4. Optionally Delete Artifact Registry Repository
if [[ "${DELETE_IMAGES}" == "true" ]]; then
  if gcloud artifacts repositories describe "${AR_REPOSITORY}" --project "${PROJECT_ID}" --location "${REGION}" >/dev/null 2>&1; then
    echo "Deleting Artifact Registry repository '${AR_REPOSITORY}' in '${REGION}'..."
    gcloud artifacts repositories delete "${AR_REPOSITORY}" \
      --project "${PROJECT_ID}" \
      --location "${REGION}" \
      --quiet
  fi
fi

# 5. Optionally Delete LiteLLM & ESO GCP Service Accounts
if [[ "${DELETE_IAM}" == "true" ]]; then
  LITELLM_GSA="alephat-litellm@${PROJECT_ID}.iam.gserviceaccount.com"
  if gcloud iam service-accounts describe "${LITELLM_GSA}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
    echo "Removing IAM policy binding and deleting service account '${LITELLM_GSA}'..."
    gcloud projects remove-iam-policy-binding "${PROJECT_ID}" \
      --member="serviceAccount:${LITELLM_GSA}" \
      --role=roles/aiplatform.user \
      --condition=None >/dev/null 2>&1 || true
    gcloud iam service-accounts delete "${LITELLM_GSA}" \
      --project "${PROJECT_ID}" \
      --quiet
  fi

  ESO_GSA="alephat-eso@${PROJECT_ID}.iam.gserviceaccount.com"
  if gcloud iam service-accounts describe "${ESO_GSA}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
    echo "Removing IAM policy binding and deleting service account '${ESO_GSA}'..."
    gcloud projects remove-iam-policy-binding "${PROJECT_ID}" \
      --member="serviceAccount:${ESO_GSA}" \
      --role=roles/secretmanager.secretAccessor \
      --condition=None >/dev/null 2>&1 || true
    gcloud iam service-accounts delete "${ESO_GSA}" \
      --project "${PROJECT_ID}" \
      --quiet
  fi
fi

# 6. Optionally Delete Google Cloud Secret Manager Secrets
if [[ "${DELETE_SECRETS}" == "true" ]]; then
  for secret in \
    litellm-secrets \
    litellm-postgres \
    litellm-provider-keys \
    alephat-postgres \
    alephat-secrets \
    alephat-oauth; do
    if gcloud secrets describe "${secret}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
      echo "Deleting Google Cloud Secret Manager secret '${secret}'..."
      gcloud secrets delete "${secret}" --project "${PROJECT_ID}" --quiet
    fi
  done
fi

echo "Teardown complete."
