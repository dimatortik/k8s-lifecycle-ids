#!/usr/bin/env bash
# Build the controller and ml-service images and push them to Artifact Registry.
#
# Usage:
#   PROJECT=<gcp-project> bash scripts/infra/build_and_push.sh
#   PROJECT=<gcp-project> bash scripts/infra/build_and_push.sh --update-manifests

set -euo pipefail

PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null || echo '')}"
REGION="${REGION:-us-central1}"            # match cluster region — minimises pull latency + egress
REPO_NAME="${REPO_NAME:-ids-phase2}"
TAG="${TAG:-phase2}"

CONTROLLER_IMAGE_NAME="${CONTROLLER_IMAGE_NAME:-ids-controller}"
ML_SERVICE_IMAGE_NAME="${ML_SERVICE_IMAGE_NAME:-ids-ml-service}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

log()  { echo "[$(date +%H:%M:%S)] $*"; }
fail() { echo "ERROR: $*" >&2; exit 1; }

for cmd in gcloud docker; do
    command -v "$cmd" >/dev/null || fail "missing CLI: $cmd"
done
[[ -n "$PROJECT" ]] || fail "PROJECT not set"
docker info >/dev/null 2>&1 || fail "docker daemon not reachable"

AR_HOST="${REGION}-docker.pkg.dev"
AR_REPO_PATH="${AR_HOST}/${PROJECT}/${REPO_NAME}"
CTL_URI="${AR_REPO_PATH}/${CONTROLLER_IMAGE_NAME}:${TAG}"
ML_URI="${AR_REPO_PATH}/${ML_SERVICE_IMAGE_NAME}:${TAG}"

log "project=${PROJECT}  region=${REGION}  repo=${REPO_NAME}  tag=${TAG}"
log "controller URI: ${CTL_URI}"
log "ml-service URI: ${ML_URI}"

log "[1/5] ensuring artifactregistry.googleapis.com is enabled"
gcloud services enable artifactregistry.googleapis.com --project "$PROJECT" >/dev/null

if gcloud artifacts repositories describe "$REPO_NAME" \
        --location "$REGION" --project "$PROJECT" >/dev/null 2>&1; then
    log "[2/5] AR repo ${REPO_NAME} exists — skipping create."
else
    log "[2/5] creating AR repo ${REPO_NAME} (Docker format)"
    gcloud artifacts repositories create "$REPO_NAME" \
        --project "$PROJECT" \
        --location "$REGION" \
        --repository-format=docker \
        --description="IDS component images (ids-controller, ids-ml-service)" \
        --quiet
fi

log "[3/5] configuring docker credential helper for ${AR_HOST}"
gcloud auth configure-docker "$AR_HOST" --quiet

if [[ "${SKIP_BUILD:-0}" == "1" ]]; then
    log "[4/5] SKIP_BUILD=1 — leaving images untouched."
else
    log "[4/5] docker buildx — controller"
    docker buildx build \
        --platform linux/amd64 \
        --tag "$CTL_URI" \
        --push \
        --file "${REPO_ROOT}/controller/Dockerfile" \
        "${REPO_ROOT}/controller"

    log "[4/5] docker buildx — ml-service (context=repo root so baseline.pkl is in scope)"
    docker buildx build \
        --platform linux/amd64 \
        --tag "$ML_URI" \
        --push \
        --file "${REPO_ROOT}/ml-service/Dockerfile" \
        "${REPO_ROOT}"
fi

if [[ "${1:-}" == "--update-manifests" ]]; then
    log "[5/5] patching K8s manifests to reference AR URIs"
    sed -i.bak \
        -e "s|image: ids-controller:phase2$|image: ${CTL_URI}|" \
        "${REPO_ROOT}/k8s/ids-controller/deployment.yaml"
    sed -i.bak \
        -e "s|image: ids-ml-service:phase2$|image: ${ML_URI}|" \
        "${REPO_ROOT}/k8s/ml-service/deployment.yaml"
    rm -f "${REPO_ROOT}/k8s/ids-controller/deployment.yaml.bak" \
          "${REPO_ROOT}/k8s/ml-service/deployment.yaml.bak"
    log "    patched k8s/ids-controller/deployment.yaml"
    log "    patched k8s/ml-service/deployment.yaml"
else
    log "[5/5] skipping manifest rewrite — pass --update-manifests to enable"
    echo
    log "To wire these URIs into the deploy script, either:"
    log "    (a) re-run with --update-manifests, OR"
    log "    (b) export IDS_CONTROLLER_IMAGE=${CTL_URI}"
    log "        export ML_SERVICE_IMAGE=${ML_URI}"
    log "        bash scripts/infra/deploy_workload.sh"
fi

log "DONE."
log "Pushed images:"
echo "    ${CTL_URI}"
echo "    ${ML_URI}"
