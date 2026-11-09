#!/usr/bin/env bash
# Collect steady-state Tracee data and train the per-service models.
#
# Sets Locust to the training profile (steady = 50 users by default), streams
# Tracee -> lifecycle_tagger.py -> train_multi_model.py for DURATION_SECS, and
# syncs the resulting models into the running ml-service pod. No rollout runs
# during training: the models only see running_ready traffic.
#
# Usage:
#   bash scripts/experiments/train_multi_model.sh
#   DURATION_SECS=600 bash scripts/experiments/train_multi_model.sh   # short run

set -euo pipefail

DURATION_SECS="${DURATION_SECS:-3600}"
LOCUST_USERS_IDLE="${LOCUST_USERS_IDLE:-1}"
TRAINING_PROFILE="${TRAINING_PROFILE:-steady}"
LOCUST_USERS_STEADY="${LOCUST_USERS_STEADY:-50}"

ONLINEBOUTIQUE_NS="${ONLINEBOUTIQUE_NS:-default}"
LOCUST_DEPLOYMENT="${LOCUST_DEPLOYMENT:-loadgenerator}"
TRACEE_NS="${TRACEE_NS:-tracee}"
TRACEE_DS="${TRACEE_DS:-tracee}"

export MIN_ROWS_PER_SERVICE="${MIN_ROWS_PER_SERVICE:-100}"
export TARGET_ROWS_TOTAL="${TARGET_ROWS_TOTAL:-35000}"

OB_TRAIN_REPLICAS="${OB_TRAIN_REPLICAS:-3}"
OB_TRAIN_SCALE_TARGETS="${OB_TRAIN_SCALE_TARGETS:-frontend cartservice productcatalogservice currencyservice checkoutservice emailservice adservice paymentservice shippingservice}"
ORIGINAL_REPLICAS_FILE=""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
TAGGER="${REPO_ROOT}/scripts/utils/lifecycle_tagger.py"
TRAINER="${REPO_ROOT}/scripts/experiments/train_multi_model.py"

log() { echo "[$(date +%H:%M:%S)] $*"; }
fail() { echo "ERROR: $*" >&2; exit 1; }

for cmd in kubectl python3; do
    command -v "$cmd" >/dev/null || fail "missing CLI: $cmd"
done
if   command -v timeout  >/dev/null 2>&1; then TIMEOUT_CMD=timeout
elif command -v gtimeout >/dev/null 2>&1; then TIMEOUT_CMD=gtimeout
else fail "missing CLI: timeout/gtimeout — 'brew install coreutils' on macOS"
fi
kubectl cluster-info >/dev/null 2>&1 || fail "no cluster — run gke_provision.sh + deploy_workload.sh first"
[[ -f "$TAGGER"  ]] || fail "tagger missing: $TAGGER"
[[ -f "$TRAINER" ]] || fail "trainer missing: $TRAINER"

declare -a CHILD_PIDS=()
_register() { CHILD_PIDS+=("$1"); }
_reap() {
    for pid in "${CHILD_PIDS[@]:-}"; do
        kill -0 "$pid" 2>/dev/null && kill -TERM "$pid" 2>/dev/null || true
    done
    CHILD_PIDS=()
}
cleanup() {
    local rc=$?
    trap - INT TERM EXIT
    _reap
    if [[ -n "$ORIGINAL_REPLICAS_FILE" && -f "$ORIGINAL_REPLICAS_FILE" ]]; then
        log "[cleanup] restoring original replica counts"
        while IFS=' ' read -r svc orig; do
            [[ -z "$svc" ]] && continue
            kubectl scale deployment "$svc" -n "$ONLINEBOUTIQUE_NS" \
                --replicas="$orig" >/dev/null 2>&1 || true
        done < "$ORIGINAL_REPLICAS_FILE"
        rm -f "$ORIGINAL_REPLICAS_FILE"
    fi
    rm -f "${FIFO_RAW:-}" "${FIFO_TAGGED:-}"
    exit "$rc"
}
trap cleanup INT TERM EXIT

case "$TRAINING_PROFILE" in
    idle)   _users="$LOCUST_USERS_IDLE" ;;
    steady) _users="$LOCUST_USERS_STEADY" ;;
    *)      fail "unknown TRAINING_PROFILE='$TRAINING_PROFILE' (use idle|steady)" ;;
esac
log "[1/3] setting Locust to ${TRAINING_PROFILE} (USERS=${_users})"
kubectl set env "deployment/${LOCUST_DEPLOYMENT}" -n "$ONLINEBOUTIQUE_NS" \
    USERS="$_users" >/dev/null
kubectl rollout status "deployment/${LOCUST_DEPLOYMENT}" \
    -n "$ONLINEBOUTIQUE_NS" --timeout=120s

log "[1/3] waiting for steady state..."
for _ in $(seq 1 60); do
    terminating="$(kubectl get pods -n "$ONLINEBOUTIQUE_NS" \
        -o jsonpath='{range .items[?(@.metadata.deletionTimestamp)]}{.metadata.name}{"\n"}{end}' \
        2>/dev/null || true)"
    [[ -z "$terminating" ]] && break
    sleep 2
done

ORIGINAL_REPLICAS_FILE="$(mktemp -t c2_replicas.XXXXXX)"
log "[1.5/3] scaling up training-target services to ${OB_TRAIN_REPLICAS} replicas"
for svc in $OB_TRAIN_SCALE_TARGETS; do
    current="$(kubectl get deployment "$svc" -n "$ONLINEBOUTIQUE_NS" \
        -o jsonpath='{.spec.replicas}' 2>/dev/null || echo "1")"
    echo "$svc $current" >> "$ORIGINAL_REPLICAS_FILE"
    kubectl scale deployment "$svc" -n "$ONLINEBOUTIQUE_NS" \
        --replicas="$OB_TRAIN_REPLICAS" >/dev/null
done
for svc in $OB_TRAIN_SCALE_TARGETS; do
    kubectl rollout status "deployment/${svc}" \
        -n "$ONLINEBOUTIQUE_NS" --timeout=120s
done
log "[1.5/3] waiting for post-scale steady state..."
for _ in $(seq 1 60); do
    terminating="$(kubectl get pods -n "$ONLINEBOUTIQUE_NS" \
        -o jsonpath='{range .items[?(@.metadata.deletionTimestamp)]}{.metadata.name}{"\n"}{end}' \
        2>/dev/null || true)"
    [[ -z "$terminating" ]] && break
    sleep 2
done

FIFO_RAW="$(mktemp -u)".raw.fifo
FIFO_TAGGED="$(mktemp -u)".tagged.fifo
mkfifo "$FIFO_RAW" "$FIFO_TAGGED"

log "[2/3] starting capture pipeline for ${DURATION_SECS}s"

python3 "$TRAINER" < "$FIFO_TAGGED" &
TRAINER_PID=$!
_register "$TRAINER_PID"

python3 "$TAGGER" < "$FIFO_RAW" > "$FIFO_TAGGED" &
TAGGER_PID=$!
_register "$TAGGER_PID"

( "$TIMEOUT_CMD" "${DURATION_SECS}s" \
    kubectl logs -n "$TRACEE_NS" -l app=tracee \
        --max-log-requests=10 -f --since=1s 2>/dev/null \
    > "$FIFO_RAW" ) &
KUBECTL_PID=$!
_register "$KUBECTL_PID"

log "[3/3] capturing for up to ${DURATION_SECS}s"
set +e
wait "$TRAINER_PID"
TRAINER_RC=$?
set -e

kill -TERM "$TAGGER_PID"  2>/dev/null || true
kill -TERM "$KUBECTL_PID" 2>/dev/null || true
wait "$TAGGER_PID"  2>/dev/null || true
wait "$KUBECTL_PID" 2>/dev/null || true
rm -f "$FIFO_RAW" "$FIFO_TAGGED"
CHILD_PIDS=()

if [[ "$TRAINER_RC" -ne 0 ]]; then
    log "trainer exited rc=${TRAINER_RC}"
    exit "$TRAINER_RC"
fi

log "[sync] handing off to sync_models_to_pod.sh"
bash "${REPO_ROOT}/scripts/utils/sync_models_to_pod.sh"

log "DONE — models trained and synced to pod."
