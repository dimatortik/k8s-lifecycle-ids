#!/usr/bin/env bash
# Copy locally trained models into the running ml-service pod and call
# POST /admin/reload. /models is not a volume, so a pod restart reverts to
# the models baked into the image (rebuild the image to make them permanent).
#
# Usage:
#   bash scripts/utils/sync_models_to_pod.sh

set -euo pipefail

IDS_NS="${IDS_NS:-ids-system}"
DEPLOYMENT="${DEPLOYMENT:-ml-service}"
CONTAINER="${CONTAINER:-ml-service}"
REMOTE_MODELS_ROOT="${REMOTE_MODELS_ROOT:-/models}"
REMOTE_MODELS_DIR="${REMOTE_MODELS_DIR:-/models/onlineboutique}"
ML_SERVICE_URL="${ML_SERVICE_URL:-http://localhost:8080}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LOCAL_DIR="${REPO_ROOT}/models/onlineboutique"
LOCAL_BASELINE_PKL="${REPO_ROOT}/models/baseline.pkl"
LOCAL_BASELINE_PROVENANCE="${REPO_ROOT}/models/baseline.provenance.json"

log() { echo "[$(date +%H:%M:%S)] sync_models: $*"; }
fail() { echo "ERROR: $*" >&2; exit 1; }

command -v kubectl >/dev/null || fail "kubectl missing"
[[ -d "$LOCAL_DIR" ]] || fail "no local models dir: $LOCAL_DIR (did train_multi_model.sh run?)"

POD="$(kubectl get pods -n "$IDS_NS" \
    -l "app=${DEPLOYMENT}" \
    --field-selector=status.phase=Running \
    -o go-template='{{range .items}}{{$ready:="false"}}{{range .status.conditions}}{{if and (eq .type "Ready") (eq .status "True")}}{{$ready = "true"}}{{end}}{{end}}{{if eq $ready "true"}}{{.metadata.name}}{{"\n"}}{{end}}{{end}}' \
    | head -n1)"
[[ -n "$POD" ]] || fail "no Ready pod for app=${DEPLOYMENT} in ns=${IDS_NS}"
log "target pod: ${POD}"

PKL_COUNT="$(find "$LOCAL_DIR" -maxdepth 1 -name '*.pkl' -type f | wc -l | tr -d ' ')"
[[ "$PKL_COUNT" -gt 0 ]] || fail "no .pkl files under $LOCAL_DIR"
log "syncing ${PKL_COUNT} .pkl files → ${POD}:${REMOTE_MODELS_DIR}/"

kubectl exec -n "$IDS_NS" "$POD" -c "$CONTAINER" -- \
    mkdir -p "$REMOTE_MODELS_DIR" 2>/dev/null || true

log "sync_models: clearing stale .pkl files in remote ${REMOTE_MODELS_DIR}/"
kubectl exec -n "$IDS_NS" "$POD" -c "$CONTAINER" -- \
    sh -c "rm -f ${REMOTE_MODELS_DIR}/*.pkl" || {
        log "ERROR: failed to clear remote model dir; aborting sync"
        exit 1
    }

kubectl cp -n "$IDS_NS" -c "$CONTAINER" \
    "$LOCAL_DIR/." "${POD}:${REMOTE_MODELS_DIR}/"

REMOTE_LS="$(kubectl exec -n "$IDS_NS" "$POD" -c "$CONTAINER" -- \
    ls -1 "$REMOTE_MODELS_DIR" 2>/dev/null | grep -c '\.pkl$' || true)"
log "remote .pkl count after cp: ${REMOTE_LS}"
[[ "$REMOTE_LS" -eq "$PKL_COUNT" ]] || \
    fail "cp count mismatch: local=${PKL_COUNT} remote=${REMOTE_LS}"

if [[ -f "$LOCAL_BASELINE_PKL" && -f "$LOCAL_BASELINE_PROVENANCE" ]]; then
    log "syncing fleet baseline.pkl + provenance.json → ${POD}:${REMOTE_MODELS_ROOT}/"
    kubectl cp -n "$IDS_NS" -c "$CONTAINER" \
        "$LOCAL_BASELINE_PKL" \
        "${POD}:${REMOTE_MODELS_ROOT}/baseline.pkl"
    kubectl cp -n "$IDS_NS" -c "$CONTAINER" \
        "$LOCAL_BASELINE_PROVENANCE" \
        "${POD}:${REMOTE_MODELS_ROOT}/baseline.provenance.json"

    local_baseline_size=$(stat -f%z "$LOCAL_BASELINE_PKL" 2>/dev/null \
        || stat -c%s "$LOCAL_BASELINE_PKL")
    remote_baseline_size=$(kubectl exec -n "$IDS_NS" "$POD" -c "$CONTAINER" -- \
        stat -c%s "${REMOTE_MODELS_ROOT}/baseline.pkl" 2>/dev/null || echo "0")
    [[ "$local_baseline_size" == "$remote_baseline_size" ]] || \
        fail "baseline.pkl size mismatch: local=${local_baseline_size} remote=${remote_baseline_size}"

    remote_provenance=$(kubectl exec -n "$IDS_NS" "$POD" -c "$CONTAINER" -- \
        cat "${REMOTE_MODELS_ROOT}/baseline.provenance.json" 2>/dev/null \
        | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('workload',''))" \
        2>/dev/null || echo "")
    log "remote provenance workload: ${remote_provenance}"
    [[ "$remote_provenance" =~ ^online-boutique- ]] || \
        fail "remote provenance workload is not online-boutique-*: got '${remote_provenance}'"
else
    log "WARN: fleet baseline.pkl or provenance.json missing locally — the fallback model will be stale"
    log "       expected: $LOCAL_BASELINE_PKL"
    log "       expected: $LOCAL_BASELINE_PROVENANCE"
    fail "fleet artefacts missing — re-run train_multi_model.sh"
fi

log "POST ${ML_SERVICE_URL}/admin/reload"
if curl -fsS -m 5 -X POST "${ML_SERVICE_URL}/admin/reload" 2>/dev/null; then
    echo
    log "registry reloaded via port-forward."
else
    log "port-forward not reachable — falling back to kubectl exec"
    kubectl exec -n "$IDS_NS" "$POD" -c "$CONTAINER" -- \
        python3 -c "import urllib.request,sys; \
print(urllib.request.urlopen('http://localhost:8080/admin/reload', \
data=b'', timeout=5).read().decode())"
fi

log "DONE — verify keys with: curl ${ML_SERVICE_URL}/health"
