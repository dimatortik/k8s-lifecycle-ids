#!/usr/bin/env bash
# Deploy Tracee, the ml-service, the controller and Online Boutique.
#
# Online Boutique is then patched (kubectl patch, since its manifests do not
# expose these settings): protected=true label, RollingUpdate with
# maxSurge=1 / maxUnavailable=0, terminationGracePeriodSeconds=30.
#
# Usage:
#   PROJECT=<gcp-project> bash scripts/infra/deploy_workload.sh
#   SKIP_OB=1 ... | SKIP_PATCH=1 ...

set -euo pipefail

ONLINEBOUTIQUE_NS="${ONLINEBOUTIQUE_NS:-default}"
SECONDARY_NS="${SECONDARY_NS:-onlineboutique-b}"
IDS_NS="${IDS_NS:-ids-system}"

PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null || echo '')}"
REGION="${REGION:-us-central1}"
REPO_NAME="${REPO_NAME:-ids-phase2}"
TAG="${TAG:-phase2}"
# Same image URIs as build_and_push.sh; override with ML_SERVICE_IMAGE / IDS_CONTROLLER_IMAGE.
if [[ -z "$PROJECT" && ( -z "${ML_SERVICE_IMAGE:-}" || -z "${IDS_CONTROLLER_IMAGE:-}" ) ]]; then
    echo "ERROR: set PROJECT (or ML_SERVICE_IMAGE and IDS_CONTROLLER_IMAGE)" >&2; exit 1
fi
_AR="${REGION}-docker.pkg.dev/${PROJECT}/${REPO_NAME}"
ML_SERVICE_IMAGE="${ML_SERVICE_IMAGE:-${_AR}/ids-ml-service:${TAG}}"
IDS_CONTROLLER_IMAGE="${IDS_CONTROLLER_IMAGE:-${_AR}/ids-controller:${TAG}}"

# Upstream main; the exact revision used for the campaigns was not recorded.
OB_MANIFEST_URL="${OB_MANIFEST_URL:-https://raw.githubusercontent.com/GoogleCloudPlatform/microservices-demo/main/release/kubernetes-manifests.yaml}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

log() { echo "[$(date +%H:%M:%S)] $*"; }
fail() { echo "ERROR: $*" >&2; exit 1; }

for cmd in kubectl; do
    command -v "$cmd" >/dev/null || fail "missing CLI: $cmd"
done
kubectl cluster-info >/dev/null 2>&1 || fail "no kubectl cluster — run gke_provision.sh first"

log "[1/5] applying Tracee DaemonSet"
kubectl apply -f "${REPO_ROOT}/k8s/tracee/daemonset.yaml"
kubectl rollout status ds/tracee -n tracee --timeout=120s

kubectl get ns "$IDS_NS" >/dev/null 2>&1 \
    || kubectl create ns "$IDS_NS"
log "[2/5] applying ML service (image=${ML_SERVICE_IMAGE})"
kubectl apply -f - <<EOF
$(sed "s|image: ids-ml-service:phase2$|image: ${ML_SERVICE_IMAGE}|" \
    "${REPO_ROOT}/k8s/ml-service/deployment.yaml")
EOF
kubectl rollout status deployment/ml-service -n "$IDS_NS" --timeout=120s

log "[3/5] applying IDS controller (image=${IDS_CONTROLLER_IMAGE})"
kubectl apply -f - <<EOF
$(sed "s|image: ids-controller:phase2$|image: ${IDS_CONTROLLER_IMAGE}|" \
    "${REPO_ROOT}/k8s/ids-controller/deployment.yaml")
EOF
kubectl rollout status deployment/ids-controller -n "$IDS_NS" --timeout=120s

if kubectl get crd servicemonitors.monitoring.coreos.com >/dev/null 2>&1; then
    log "[3/5] applying ServiceMonitor for controller metrics"
    kubectl apply -f "${REPO_ROOT}/k8s/monitoring/controller-servicemonitor.yaml"
else
    log "WARN: ServiceMonitor CRD missing — re-run gke_provision.sh (Prometheus not installed)"
fi

if kubectl get crd podmonitors.monitoring.coreos.com >/dev/null 2>&1; then
    log "[3/5] applying PodMonitor for Tracee metrics"
    kubectl apply -f "${REPO_ROOT}/k8s/monitoring/tracee-podmonitor.yaml"
else
    log "WARN: PodMonitor CRD missing — Tracee drop-rate integrity gate non-binding"
fi

if [[ "${SKIP_OB:-0}" != "1" ]]; then
    log "[4/5] installing Online Boutique from raw manifest"
    log "    source: ${OB_MANIFEST_URL}"
    kubectl get ns "$ONLINEBOUTIQUE_NS" >/dev/null 2>&1 \
        || kubectl create ns "$ONLINEBOUTIQUE_NS"

    kubectl apply -n "$ONLINEBOUTIQUE_NS" -f "$OB_MANIFEST_URL"

    log "    waiting for OB deployments to become Available..."
    kubectl wait --for=condition=Available --timeout=10m \
        -n "$ONLINEBOUTIQUE_NS" deployment --all

    kubectl get ns "$SECONDARY_NS" >/dev/null 2>&1 \
        || kubectl create ns "$SECONDARY_NS"
    log "[4/5] secondary namespace ${SECONDARY_NS} ready (frontend deployed lazily by trial)"
fi

if [[ "${SKIP_PATCH:-0}" == "1" ]]; then
    log "[5/5] SKIP_PATCH=1 — leaving Online Boutique unmodified."
    exit 0
fi

log "[5/5] patching Online Boutique deployments (protected label, rollout strategy)"

read -r -d '' PATCH_DOC <<'PATCH' || true
{
  "metadata": {
    "annotations": { "ids-research.io/protected": "true" },
    "labels":      { "ids-research.io/protected": "true" }
  },
  "spec": {
    "strategy": {
      "type": "RollingUpdate",
      "rollingUpdate": { "maxSurge": 1, "maxUnavailable": 0 }
    },
    "template": {
      "metadata": {
        "labels": { "ids-research.io/protected": "true" }
      },
      "spec": {
        "terminationGracePeriodSeconds": 30
      }
    }
  }
}
PATCH

DEPLOYMENTS=()
while IFS= read -r d; do
    [[ -n "$d" ]] && DEPLOYMENTS+=("$d")
done < <(kubectl get deployments -n "$ONLINEBOUTIQUE_NS" \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')

if [[ ${#DEPLOYMENTS[@]} -eq 0 ]]; then
    log "WARN: no Helm-managed deployments found in ${ONLINEBOUTIQUE_NS}; "\
"either the chart labels differ or the install failed silently."
fi

for d in "${DEPLOYMENTS[@]}"; do
    [[ -z "$d" ]] && continue
    log "  patching deployment/${d}"
    kubectl patch deployment/"$d" -n "$ONLINEBOUTIQUE_NS" \
        --type=strategic --patch "$PATCH_DOC" >/dev/null
done

log "DONE.  Sanity checks:"
echo "  kubectl get pods -A | grep -E 'tracee|ids-|onlineboutique'"
echo "  kubectl get servicemonitor -n ${IDS_NS}"
echo "  kubectl -n ${IDS_NS} port-forward svc/ml-service 8080:8080 &"
echo "  kubectl -n monitoring port-forward svc/kps-kube-prometheus-stack-prometheus 9091:9090 &"
