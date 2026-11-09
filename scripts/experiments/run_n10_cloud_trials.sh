#!/usr/bin/env bash
# Campaign orchestrator (GKE + Online Boutique).
#
# For every trial and condition: reset state (attacker pods, threshold tables),
# set the Locust load and controller replicas, stream
# Tracee -> lifecycle_tagger.py -> run_h2.py / run_h3.py, trigger the rolling
# update (and the attack, for H3), then collect Prometheus metrics with
# resource_profiler.py. H2 trials whose /score error rate exceeds 5% are re-run.
#
# Conditions (Locust 50 users unless noted; rollout = frontend, cartservice,
# productcatalogservice):
#   BASELINE_IDLE      phase rule on,  controller on,  Locust 1 user, no rollout
#   BLIND_STEADY       phase rule off, controller off, no rollout
#   BLIND_ROLLOUT      phase rule off, controller off, rollout
#   PHASE_ROLLOUT      phase rule on,  controller off, rollout
#   PHASECTRL_ROLLOUT  phase rule on,  controller on,  rollout
#   H3_<EXFIL|TOKEN|RSHELL>_<NAIVE|ADV>
#                      PHASECTRL_ROLLOUT + attack pod, applied 30 s after the
#                      rollout (NAIVE) or timed on ThresholdRaised events (ADV)
#   HPA_SCALEOUT, MULTI_ROLLOUT are implemented but were not run.
#
# Prerequisites: gke_provision.sh, build_and_push.sh, deploy_workload.sh and
# train_multi_model.sh have been run; kubectl points at the cluster.
#
# Usage:
#   INTERLEAVE_ORDER=1 N_TRIALS=10 \
#   CONDITIONS="BASELINE_IDLE BLIND_STEADY BLIND_ROLLOUT PHASE_ROLLOUT PHASECTRL_ROLLOUT" \
#       bash scripts/experiments/run_n10_cloud_trials.sh

set -euo pipefail

N_TRIALS="${N_TRIALS:-10}"

DEFAULT_CONDITIONS=(
    BASELINE_IDLE
    BLIND_STEADY
    BLIND_ROLLOUT
    PHASE_ROLLOUT
    PHASECTRL_ROLLOUT
    H3_EXFIL_NAIVE
    H3_EXFIL_ADV
    H3_TOKEN_NAIVE
    H3_TOKEN_ADV
    H3_RSHELL_NAIVE
    H3_RSHELL_ADV
    HPA_SCALEOUT
    MULTI_ROLLOUT
)
if [[ -n "${CONDITIONS:-}" ]]; then
    # shellcheck disable=SC2206
    CONDITIONS_ARR=($CONDITIONS)
else
    CONDITIONS_ARR=("${DEFAULT_CONDITIONS[@]}")
fi

ONLINEBOUTIQUE_NS="${ONLINEBOUTIQUE_NS:-default}"
SECONDARY_NS="${SECONDARY_NS:-onlineboutique-b}"  # for MULTI_ROLLOUT
CONTROLLER_NS="${CONTROLLER_NS:-ids-system}"
TRACEE_NS="${TRACEE_NS:-tracee}"
TRACEE_DS="${TRACEE_DS:-tracee}"
LOCUST_DEPLOYMENT="${LOCUST_DEPLOYMENT:-loadgenerator}"

ROLLOUT_TARGETS=(
    "${ONLINEBOUTIQUE_NS}/frontend"
    "${ONLINEBOUTIQUE_NS}/cartservice"
    "${ONLINEBOUTIQUE_NS}/productcatalogservice"
)

LOCUST_IDLE_USERS=1
LOCUST_STEADY_USERS=50
LOCUST_PEAK_USERS=500

_RESOLVED_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_RESOLVED_REPO_ROOT="$(cd "${_RESOLVED_SCRIPT_DIR}/../.." && pwd)"
ATTACK_MANIFESTS_DIR="${ATTACK_MANIFESTS_DIR:-${_RESOLVED_REPO_ROOT}/k8s/scenarios}"
ATTACKER_POD_EXFIL="${ATTACKER_POD_EXFIL:-exfil-attacker}"
ATTACKER_POD_TOKEN="${ATTACKER_POD_TOKEN:-token-attacker}"
ATTACKER_POD_RSHELL="${ATTACKER_POD_RSHELL:-rshell-attacker}"

PROM_URL="${PROM_URL:-http://localhost:9091}"

ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-300s}"
STEADY_POLL_SECS=2
STEADY_POLL_MAX_ITERS=120          # 240s ceiling for OB to converge
INTER_TRIAL_COOLDOWN_SECS=20       # buffer Tracee + Prometheus drain

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
RESULTS_ROOT="${RESULTS_ROOT:-${REPO_ROOT}/experiments/results/cloud_trials}"
EXP_SCRIPTS="${REPO_ROOT}/scripts/experiments"
PROFILER="${REPO_ROOT}/scripts/utils/resource_profiler.py"
TAGGER="${REPO_ROOT}/scripts/utils/lifecycle_tagger.py"

mkdir -p "$RESULTS_ROOT"

declare -a CHILD_PIDS=()
_register_child() { CHILD_PIDS+=("$1"); }
_reap_children() {
    for pid in "${CHILD_PIDS[@]:-}"; do
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            kill -TERM "$pid" 2>/dev/null || true
        fi
    done
    CHILD_PIDS=()
}

cleanup_attacker_pods() {
    local ns="$1"
    for p in "$ATTACKER_POD_EXFIL" "$ATTACKER_POD_TOKEN" "$ATTACKER_POD_RSHELL"; do
        if kubectl get pod "$p" -n "$ns" >/dev/null 2>&1; then
            kubectl delete pod "$p" -n "$ns" \
                --grace-period=0 --force --wait=false >/dev/null 2>&1 || true
        fi
    done
}

_PF_ML_PID=0
_PF_PROM_PID=0
_PF_ML_LOG="/tmp/orchestrator-pf-mlservice-$$.log"
_PF_PROM_LOG="/tmp/orchestrator-pf-prom-$$.log"

_kill_existing_pf() {
    local pids
    pids=$(pgrep -f 'kubectl port-forward.*8080' 2>/dev/null || true)
    if [[ -n "$pids" ]]; then
        # shellcheck disable=SC2086
        kill -TERM $pids 2>/dev/null || true
    fi
    pids=$(pgrep -f 'kubectl port-forward.*9091' 2>/dev/null || true)
    if [[ -n "$pids" ]]; then
        # shellcheck disable=SC2086
        kill -TERM $pids 2>/dev/null || true
    fi
    sleep 1
}

_ensure_portforwards() {
    local trial_id="$1"
    _kill_existing_pf
    : > "$_PF_ML_LOG"
    : > "$_PF_PROM_LOG"

    kubectl port-forward -n ids-system svc/ml-service 8080:8080 \
        > "$_PF_ML_LOG" 2>&1 &
    _PF_ML_PID=$!
    kubectl port-forward -n monitoring svc/kps-prometheus 9091:9090 \
        > "$_PF_PROM_LOG" 2>&1 &
    _PF_PROM_PID=$!

    local i start_ns end_ns t_s now_ns_cmd='date +%s%N'
    if ! date +%s%N 2>/dev/null | grep -q N; then
        : # GNU date available
    else
        now_ns_cmd='python3 -c "import time;print(int(time.time()*1e9))"'
    fi
    start_ns=$(eval "$now_ns_cmd")

    sleep 3

    if ! kill -0 "$_PF_ML_PID" 2>/dev/null; then
        log "[ERROR] kubectl port-forward (ml-service) died immediately — see $_PF_ML_LOG"
        tail -20 "$_PF_ML_LOG" 2>/dev/null | sed 's/^/    /' >&2 || true
        exit 2
    fi
    if ! kill -0 "$_PF_PROM_PID" 2>/dev/null; then
        log "[ERROR] kubectl port-forward (prom) died immediately — see $_PF_PROM_LOG"
        tail -20 "$_PF_PROM_LOG" 2>/dev/null | sed 's/^/    /' >&2 || true
        exit 2
    fi

    for i in $(seq 1 30); do
        if curl -sSf -m 5 http://localhost:8080/health >/dev/null 2>&1; then
            end_ns=$(eval "$now_ns_cmd")
            t_s=$(awk "BEGIN{printf \"%.1f\", ($end_ns - $start_ns) / 1e9}")
            log "port-forward restart for trial_$(printf '%02d' "$trial_id"): ml-service PID=${_PF_ML_PID}, prom PID=${_PF_PROM_PID}, /health 200 OK after ${t_s}s (attempt ${i}/30)"
            if ! _assert_registry_populated "$trial_id"; then
                log "[ERROR] registry assertion failed for trial_${trial_id} — trial will be skipped (resume from clean state on relaunch)"
                return 1
            fi
            return 0
        fi
        sleep 1
    done

    end_ns=$(eval "$now_ns_cmd")
    t_s=$(awk "BEGIN{printf \"%.1f\", ($end_ns - $start_ns) / 1e9}")
    log "[ERROR] port-forward establishment timed out for trial_${trial_id} after ${t_s}s (30 attempts × 1s + 3s warmup)"
    log "  ml-service PF stderr (last 20 lines from $_PF_ML_LOG):"
    tail -20 "$_PF_ML_LOG" 2>/dev/null | sed 's/^/    /' >&2 || true
    log "  prom PF stderr (last 20 lines from $_PF_PROM_LOG):"
    tail -20 "$_PF_PROM_LOG" 2>/dev/null | sed 's/^/    /' >&2 || true
    exit 2
}

_EXPECTED_MIN_MODELS=8

_query_registry_count() {
    local timeout="${1:-10}"
    local health
    health=$(curl -sSf -m "$timeout" http://localhost:8080/health 2>/dev/null || echo '{}')
    echo "$health" | python3 -c 'import json,sys
try: print(len(json.load(sys.stdin).get("per_service_models", [])))
except Exception: print(0)' 2>/dev/null || echo 0
}

_assert_registry_populated() {
    local trial_id="$1"
    local count i

    for i in 1 2 3; do
        count=$(_query_registry_count 10)
        count="${count:-0}"
        if [[ "$count" -ge "$_EXPECTED_MIN_MODELS" ]]; then
            if [[ "$i" -eq 1 ]]; then
                log "[registry] healthy: per_service_models=$count >= $_EXPECTED_MIN_MODELS"
            else
                log "[registry] healthy (attempt $i/3): per_service_models=$count >= $_EXPECTED_MIN_MODELS"
            fi
            return 0
        fi
        if [[ "$i" -lt 3 ]]; then
            log "[registry] attempt $i/3: per_service_models=$count < $_EXPECTED_MIN_MODELS — retrying in 5s"
            sleep 5
        fi
    done

    log "[registry] WARN: per_service_models=$count < $_EXPECTED_MIN_MODELS after 3 attempts — issuing /admin/reload"
    local reload_rc=""
    for i in 1 2 3; do
        reload_rc=$(curl -sSf -m 30 -X POST http://localhost:8080/admin/reload \
                    -o /dev/null -w '%{http_code}' 2>/dev/null || echo 000)
        if [[ "$reload_rc" == "200" ]]; then
            log "[registry] /admin/reload OK (attempt $i/3)"
            break
        fi
        log "[registry] /admin/reload attempt $i/3 returned HTTP $reload_rc"
        if [[ "$i" -lt 3 ]]; then
            sleep 5
        fi
    done
    if [[ "$reload_rc" != "200" ]]; then
        log "[ERROR] /admin/reload failed after 3 attempts (last HTTP=$reload_rc) for trial_${trial_id}"
        return 1
    fi

    for i in 1 2 3; do
        count=$(_query_registry_count 10)
        count="${count:-0}"
        if [[ "$count" -ge "$_EXPECTED_MIN_MODELS" ]]; then
            log "[registry] recovered after /admin/reload (re-assert $i/3): per_service_models=$count"
            return 0
        fi
        if [[ "$i" -lt 3 ]]; then
            sleep 3
        fi
    done

    log "[ERROR] registry remains depleted after /admin/reload: per_service_models=$count < $_EXPECTED_MIN_MODELS"
    log "  Investigate: on-disk /models/ contents, image bake, kubectl exec ml-service-* -- ls /models/onlineboutique/"
    return 1
}

_cleanup_portforwards() {
    if [[ "${_PF_ML_PID:-0}" -gt 0 ]] && kill -0 "$_PF_ML_PID" 2>/dev/null; then
        kill -TERM "$_PF_ML_PID" 2>/dev/null || true
        _PF_ML_PID=0
    fi
    if [[ "${_PF_PROM_PID:-0}" -gt 0 ]] && kill -0 "$_PF_PROM_PID" 2>/dev/null; then
        kill -TERM "$_PF_PROM_PID" 2>/dev/null || true
        _PF_PROM_PID=0
    fi
}

cleanup() {
    local rc=$?
    trap - INT TERM EXIT
    echo
    log "[cleanup] tearing down (rc=$rc)..."
    _reap_children
    cleanup_attacker_pods "$ONLINEBOUTIQUE_NS" || true
    cleanup_attacker_pods "$SECONDARY_NS" || true
    kubectl scale "deployment/ids-controller" -n "$CONTROLLER_NS" \
        --replicas=1 >/dev/null 2>&1 || true
    curl -sSf -m 3 -X POST http://localhost:8080/admin/clear_thresholds >/dev/null 2>&1 || true
    _cleanup_portforwards
    exit "$rc"
}
trap cleanup INT TERM EXIT

log() { echo "[$(date +%H:%M:%S)] $*"; }

set_locust_profile() {
    local profile="$1"
    local users
    case "$profile" in
        idle)   users="$LOCUST_IDLE_USERS" ;;
        steady) users="$LOCUST_STEADY_USERS" ;;
        peak)   users="$LOCUST_PEAK_USERS" ;;
        *)      echo "unknown locust profile: $profile" >&2; exit 1 ;;
    esac
    log "[locust] setting USERS=${users} (profile=${profile})"
    kubectl set env deployment/"$LOCUST_DEPLOYMENT" -n "$ONLINEBOUTIQUE_NS" \
        USERS="$users" >/dev/null
    kubectl rollout status deployment/"$LOCUST_DEPLOYMENT" \
        -n "$ONLINEBOUTIQUE_NS" --timeout=60s >/dev/null

    local ramp_secs=$((users + 15))
    log "[locust] waiting ${ramp_secs}s for ramp to ${users} users (RATE=1)"
    sleep "$ramp_secs"
}

set_controller_state() {
    local state="$1"   # off | on
    case "$state" in
        off)
            log "[controller] scaling ids-controller to 0 replicas"
            kubectl scale "deployment/ids-controller" -n "$CONTROLLER_NS" \
                --replicas=0 >/dev/null
            kubectl wait --for=delete pod -l app=ids-controller \
                -n "$CONTROLLER_NS" --timeout=60s >/dev/null 2>&1 || true
            ;;
        on)
            log "[controller] scaling ids-controller to 1 replica"
            kubectl scale "deployment/ids-controller" -n "$CONTROLLER_NS" \
                --replicas=1 >/dev/null
            kubectl rollout status "deployment/ids-controller" \
                -n "$CONTROLLER_NS" --timeout=120s >/dev/null
            sleep 5
            ;;
        *)
            log "[controller] unknown state: $state — leaving as-is"
            ;;
    esac
}

clear_threshold_tables() {
    local trial_id="$1"
    local condition="$2"
    local resp
    resp="$(curl -sSf -m 5 -X POST http://localhost:8080/admin/clear_thresholds 2>/dev/null || echo '')"
    if [[ -n "$resp" ]]; then
        local n_pod n_label
        n_pod="$(echo "$resp" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("cleared_pod_entries", 0))' 2>/dev/null || echo "?")"
        n_label="$(echo "$resp" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("cleared_label_entries", 0))' 2>/dev/null || echo "?")"
        log "[trial:${trial_id}:${condition}] cleared n_pod=${n_pod} n_label=${n_label} from threshold tables"
    else
        log "[trial:${trial_id}:${condition}] WARN: /admin/clear_thresholds unreachable — proceeding without clear"
    fi
}

wait_for_steady_state() {
    local ns="$1"
    local i terminating
    for i in $(seq 1 "$STEADY_POLL_MAX_ITERS"); do
        terminating=$(kubectl get pods -n "$ns" \
            -o jsonpath='{range .items[?(@.metadata.deletionTimestamp)]}{.metadata.name}{"\n"}{end}' \
            2>/dev/null || true)
        if [[ -z "$terminating" ]]; then
            return 0
        fi
        sleep "$STEADY_POLL_SECS"
    done
    log "[steady] WARNING: timed out waiting on ns=$ns; proceeding"
}

trigger_rollout_targets() {
    local ns_filter="$1"   # "primary" | "both"
    for target in "${ROLLOUT_TARGETS[@]}"; do
        local ns="${target%%/*}" name="${target##*/}"
        kubectl rollout restart "deployment/${name}" -n "$ns" >/dev/null
    done
    if [[ "$ns_filter" == "both" ]]; then
        kubectl rollout restart "deployment/frontend" -n "$SECONDARY_NS" >/dev/null
    fi
}

enable_hpa() {
    log "[hpa] enabling HPA on frontend (1→10 replicas, CPU 50%)"
    kubectl autoscale deployment/frontend -n "$ONLINEBOUTIQUE_NS" \
        --min=1 --max=10 --cpu-percent=50 >/dev/null 2>&1 || true
}
disable_hpa() {
    kubectl delete hpa/frontend -n "$ONLINEBOUTIQUE_NS" \
        --ignore-not-found=true >/dev/null 2>&1 || true
}

inject_attack() {
    local class="$1"     # exfil | token | rshell
    local timing="$2"    # naive | adv
    local ns="$3"
    local manifest inject_log attacker_pod
    case "$class" in
        exfil)  manifest="${ATTACK_MANIFESTS_DIR}/exfil-attacker.yaml"
                attacker_pod="$ATTACKER_POD_EXFIL"  ;;
        token)  manifest="${ATTACK_MANIFESTS_DIR}/token-attacker.yaml"
                attacker_pod="$ATTACKER_POD_TOKEN"  ;;
        rshell) manifest="${ATTACK_MANIFESTS_DIR}/rshell-attacker.yaml"
                attacker_pod="$ATTACKER_POD_RSHELL" ;;
        *)      echo "unknown attack class: $class" >&2; return 1 ;;
    esac
    inject_log="${RESULTS_ROOT}/inject_${class}_${timing}_$(date +%s).log"

    if [[ "$timing" == "naive" ]]; then
        log "[attack] scheduling naive injection (+30s): $manifest"
        nohup bash -c "
            set +e
            echo \"[\$(date +%H:%M:%S.%N)] inject: sleep 30 start\"
            sleep 30
            echo \"[\$(date +%H:%M:%S.%N)] inject: sleep done, applying $manifest\"
            kubectl apply -f '$manifest' -n '$ns'
            echo \"[\$(date +%H:%M:%S.%N)] inject: kubectl apply rc=\$?\"
            echo \"[\$(date +%H:%M:%S.%N)] inject: waiting for pod/${attacker_pod} Ready\"
            kubectl wait --for=condition=Ready 'pod/${attacker_pod}' -n '$ns' --timeout=90s
            echo \"[\$(date +%H:%M:%S.%N)] inject: kubectl wait rc=\$? (sleeping 5s for CRI enrichment)\"
            sleep 5
            echo \"[\$(date +%H:%M:%S.%N)] inject: post-wait grace complete\"
        " </dev/null >"$inject_log" 2>&1 &
        disown $!
        log "[attack] naive injection detached (log: ${inject_log})"
    else
        log "[attack] scheduling adversarial injection: class=$class ns=$ns"
        nohup bash -c "
            echo \"[\$(date +%H:%M:%S.%N)] inject: adversarial wrapper start\"
            python3 '${EXP_SCRIPTS}/inject_attack_adversarial.py' \
                --class '$class' --namespace '$ns' --manifest '$manifest'
            echo \"[\$(date +%H:%M:%S.%N)] inject: adversarial rc=\$?\"
        " </dev/null >"$inject_log" 2>&1 &
        disown $!
        log "[attack] adversarial injection detached (log: ${inject_log})"
    fi
}

run_trial() {
    local trial_id="$1"
    local condition="$2"
    local retry_count=0
    local _MAX_VALIDITY_RETRIES=8

    while true; do
        retry_count=$((retry_count + 1))

        local trial_dir
        trial_dir="$(printf '%s/trial_%02d' "$RESULTS_ROOT" "$trial_id")"
        mkdir -p "$trial_dir"

        echo
        echo "──────────────────────────────────────────────────────────────────"
        log "TRIAL ${trial_id}/${N_TRIALS}  •  Condition: ${condition}  •  attempt ${retry_count}/${_MAX_VALIDITY_RETRIES}"
        echo "──────────────────────────────────────────────────────────────────"

    cleanup_attacker_pods "$ONLINEBOUTIQUE_NS"
    cleanup_attacker_pods "$SECONDARY_NS"
    disable_hpa
    wait_for_steady_state "$ONLINEBOUTIQUE_NS"

    local locust_profile="steady"
    local rollout_mode="none"      # none | primary | both
    local hpa_mode="off"           # off | on
    local attack_class=""
    local attack_timing=""
    local experiment_script="${EXP_SCRIPTS}/run_h2.py"
    local apply_phase_auth="true"
    local controller_mode="on"

    case "$condition" in
        BASELINE_IDLE)
            locust_profile="idle" ;;
        BLIND_STEADY)
            apply_phase_auth="false" ; controller_mode="off" ;;
        BLIND_ROLLOUT)
            rollout_mode="primary"
            apply_phase_auth="false" ; controller_mode="off" ;;
        PHASE_ROLLOUT)
            rollout_mode="primary"
            apply_phase_auth="true"  ; controller_mode="off" ;;
        PHASECTRL_ROLLOUT)
            rollout_mode="primary"
            apply_phase_auth="true"  ; controller_mode="on" ;;
        H3_EXFIL_NAIVE)
            rollout_mode="primary"  ; experiment_script="${EXP_SCRIPTS}/run_h3.py"
            attack_class="exfil"    ; attack_timing="naive" ;;
        H3_EXFIL_ADV)
            rollout_mode="primary"  ; experiment_script="${EXP_SCRIPTS}/run_h3.py"
            attack_class="exfil"    ; attack_timing="adv" ;;
        H3_TOKEN_NAIVE)
            rollout_mode="primary"  ; experiment_script="${EXP_SCRIPTS}/run_h3.py"
            attack_class="token"    ; attack_timing="naive" ;;
        H3_TOKEN_ADV)
            rollout_mode="primary"  ; experiment_script="${EXP_SCRIPTS}/run_h3.py"
            attack_class="token"    ; attack_timing="adv" ;;
        H3_RSHELL_NAIVE)
            rollout_mode="primary"  ; experiment_script="${EXP_SCRIPTS}/run_h3.py"
            attack_class="rshell"   ; attack_timing="naive" ;;
        H3_RSHELL_ADV)
            rollout_mode="primary"  ; experiment_script="${EXP_SCRIPTS}/run_h3.py"
            attack_class="rshell"   ; attack_timing="adv" ;;
        HPA_SCALEOUT)
            locust_profile="peak"   ; hpa_mode="on" ;;
        MULTI_ROLLOUT)
            rollout_mode="both" ;;
        *) echo "unknown condition: $condition" >&2; return 1 ;;
    esac

    set_locust_profile "$locust_profile"
    if [[ "$hpa_mode" == "on" ]]; then enable_hpa; fi

    clear_threshold_tables "$trial_id" "$condition"
    set_controller_state "$controller_mode"

    local trial_start trial_end
    trial_start="$(date +%s)"

    if [[ "$rollout_mode" != "none" ]]; then
        trigger_rollout_targets "$rollout_mode"
    fi
    if [[ -n "$attack_class" ]]; then
        inject_attack "$attack_class" "$attack_timing" "$ONLINEBOUTIQUE_NS"
    fi

    local results_path="${trial_dir}/${condition}_raw_data.csv"
    run_tracee_pipeline "$experiment_script" "$trial_id" "$results_path" "$apply_phase_auth" "$condition"

    trial_end="$(date +%s)"

    cleanup_attacker_pods "$ONLINEBOUTIQUE_NS"
    cleanup_attacker_pods "$SECONDARY_NS"
    if [[ "$hpa_mode" == "on" ]]; then disable_hpa; fi
    if [[ "$controller_mode" == "off" ]]; then
        set_controller_state on
    fi

    log "[profile] resource_profiler.py for window [${trial_start}, ${trial_end}]"
    python3 "$PROFILER" \
        --prom-url      "$PROM_URL" \
        --trial-id      "$trial_id" \
        --scenario      "$condition" \
        --start         "$trial_start" \
        --end           "$trial_end" \
        --controller-ns "$CONTROLLER_NS" \
        --results-root  "$RESULTS_ROOT"

    local trial_dir
    trial_dir="$(printf '%s/trial_%02d' "$RESULTS_ROOT" "$trial_id")"
    local profile_json="${trial_dir}/${condition}_resource.json"
    local lost_threshold="${LOST_EVENTS_THRESHOLD_PCT:-5}"
    if [[ -f "$profile_json" ]]; then
        local drop_pct instrumented
        drop_pct="$(python3 -c "
import json, sys
try:
    d = json.load(open('$profile_json'))
    t = d.get('tracee_integrity', {})
    print(f\"{t.get('drop_rate_pct_estimate', 0):.2f}\\t{t.get('instrumented', False)}\")
except Exception as e:
    print('0\\tFalse')
" 2>/dev/null)"
        local pct="${drop_pct%%$'\t'*}"
        instrumented="${drop_pct##*$'\t'}"
        local exceeded
        exceeded="$(awk -v a="$pct" -v b="$lost_threshold" 'BEGIN{print (a+0>b+0)?1:0}')"
        if [[ "$instrumented" == "True" && "$exceeded" == "1" ]]; then
            log "[integrity] WARN: Tracee drop rate ${pct}% > ${lost_threshold}% — trial flagged"
            log "[integrity]       file: ${profile_json}"
            log "[integrity]       this trial MUST be re-run before inclusion in final stats"
            touch "${trial_dir}/${condition}_DROP_RATE_EXCEEDED"
            if [[ "${ABORT_ON_HIGH_DROP_RATE:-0}" == "1" ]]; then
                fail_integrity "tracee_drop_rate"
            fi
        elif [[ "$instrumented" == "False" ]]; then
            log "[integrity] NOTE: Tracee drop counter not instrumented (no abort possible)"
        else
            log "[integrity] OK: Tracee drop rate ${pct}% ≤ ${lost_threshold}%"
        fi
    fi

    wait_for_steady_state "$ONLINEBOUTIQUE_NS"
    log "[trial:${condition}] cooldown ${INTER_TRIAL_COOLDOWN_SECS}s..."
    sleep "$INTER_TRIAL_COOLDOWN_SECS"

    if [[ "$experiment_script" == *run_h2.py ]]; then
        if [[ ! -d "$trial_dir" ]]; then
            log "[trial:${trial_id}:${condition}] CATASTROPHIC: trial dir missing entirely"
            log "  expected: $trial_dir"
            log "  run_h2.py crashed before creating its output dir, or"
            log "  the dir was destroyed externally.  ABORT."
            exit 3
        fi

        local validity_path="${trial_dir}/${condition}_validity.json"
        local valid_status reason
        if [[ ! -f "$validity_path" ]]; then
            valid_status="missing"
            reason="validity_json_absent (SIGTERM race)"
            log "[trial:${trial_id}:${condition}] attempt ${retry_count} → validity.json missing, treating as retryable"
        else
            local valid
            valid="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['valid'])" \
                     "$validity_path" 2>/dev/null || echo "ParseError")"
            reason="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['reason'])" \
                      "$validity_path" 2>/dev/null || echo "ParseError")"
            if [[ "$valid" == "True" ]]; then
                log "[trial:${trial_id}:${condition}] attempt ${retry_count} → valid=true"
                return 0
            fi
            valid_status="false"
        fi

        local archive_ts archive_dir
        archive_ts="$(date +%Y%m%d_%H%M%S)"
        archive_dir="${trial_dir}_${condition}_invalid_${archive_ts}"
        if [[ "$valid_status" == "missing" ]]; then
            log "[trial:${trial_id}:${condition}] attempt ${retry_count} → archived as $(basename "$archive_dir") (validity.json absent)"
        else
            log "[trial:${trial_id}:${condition}] attempt ${retry_count} → valid=false, reason=\"${reason}\", archived as $(basename "$archive_dir")"
        fi
        mkdir -p "$archive_dir"
        shopt -s nullglob
        local -a matched_files=( "${trial_dir}/${condition}_"* )
        shopt -u nullglob
        if [[ ${#matched_files[@]} -gt 0 ]]; then
            mv "${matched_files[@]}" "$archive_dir/"
        else
            log "[trial:${trial_id}:${condition}] no files to archive (run_h2 crashed pre-write); empty archive dir created as attempt evidence"
        fi

        if [[ "$retry_count" -ge "$_MAX_VALIDITY_RETRIES" ]]; then
            log "[trial:${trial_id}:${condition}] ABORT — ${_MAX_VALIDITY_RETRIES} attempts failed validity gate"
            log "  diagnostic dump (archived attempts for this trial_id, condition):"
            local d archive_validity archive_reason
            for d in "${trial_dir}_${condition}"_invalid_*; do
                [[ -d "$d" ]] || continue
                archive_validity="${d}/${condition}_validity.json"
                if [[ -f "$archive_validity" ]]; then
                    archive_reason="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['reason'])" \
                                      "$archive_validity" 2>/dev/null || echo "?")"
                    log "    $d  reason=\"${archive_reason}\""
                else
                    log "    $d  (no validity.json in archive — atomic-write race or pre-write crash)"
                fi
            done
            exit 4
        fi
        log "[trial:${trial_id}:${condition}] retrying — attempt $((retry_count + 1))/${_MAX_VALIDITY_RETRIES}"
    else
        log "[trial:${trial_id}:${condition}] attempt ${retry_count} → no validity.json (non-h2 script — accepted as-is)"
        return 0
    fi
    done  # while true (retry loop)
}

fail_integrity() {
    log "[integrity] ABORT — ${1}.  Set ABORT_ON_HIGH_DROP_RATE=0 to soft-flag instead."
    exit 78  # EX_CONFIG — config-time precondition violated
}

run_tracee_pipeline() {
    local script="$1"
    local trial_id="$2"
    local results_path="$3"
    local apply_phase_auth="${4:-true}"
    local condition="${5:-}"

    local fifo_raw fifo_tagged
    fifo_raw="$(mktemp -u)".raw.fifo
    fifo_tagged="$(mktemp -u)".tagged.fifo
    mkfifo "$fifo_raw" "$fifo_tagged"

    local trial_max="${TRIAL_MAX_SECS:-270}"
    if   command -v timeout  >/dev/null 2>&1; then local TO=timeout
    elif command -v gtimeout >/dev/null 2>&1; then local TO=gtimeout
    else log "WARN: no timeout binary — trial may hang on Phase-1 scripts"; local TO=""
    fi
    export SKIP_INTERNAL_INJECT=1

    local -a cond_args=()
    if [[ -n "$condition" && "$script" == *run_h2.py ]]; then
        cond_args=(--condition "$condition")
    fi

    if [[ -n "$TO" ]]; then
        "$TO" --preserve-status --kill-after=30s "${trial_max}s" \
            python3 "$script" \
                --trial-id "$trial_id" \
                --results-path "$results_path" \
                --apply-phase-authoritative "$apply_phase_auth" \
                ${cond_args[@]+"${cond_args[@]}"} \
                < "$fifo_tagged" &
    else
        python3 "$script" \
            --trial-id "$trial_id" \
            --results-path "$results_path" \
            --apply-phase-authoritative "$apply_phase_auth" \
            ${cond_args[@]+"${cond_args[@]}"} \
            < "$fifo_tagged" &
    fi
    local py_pid=$!
    _register_child "$py_pid"

    python3 "$TAGGER" < "$fifo_raw" > "$fifo_tagged" &
    local tag_pid=$!
    _register_child "$tag_pid"

    ( kubectl logs -n "$TRACEE_NS" -l app=tracee \
        --max-log-requests=10 -f --since=1s 2>/dev/null > "$fifo_raw" ) &
    local kc_pid=$!
    _register_child "$kc_pid"

    set +e
    wait "$py_pid"
    local py_rc=$?
    set -e
    kill -TERM "$tag_pid" 2>/dev/null || true
    kill -TERM "$kc_pid"  2>/dev/null || true
    wait "$tag_pid" 2>/dev/null || true
    wait "$kc_pid"  2>/dev/null || true
    rm -f "$fifo_raw" "$fifo_tagged"
    CHILD_PIDS=()

    case "$py_rc" in
        0|124|143|137)
            [[ "$py_rc" -ne 0 ]] && log "[run] trial time-box hit (rc=$py_rc) — treating as success"
            return 0 ;;
        *)
            log "[run] WARNING: $script exited with rc=$py_rc"
            return "$py_rc" ;;
    esac
}

preflight() {
    log "preflight: checking dependencies and cluster..."
    command -v kubectl >/dev/null || { echo "kubectl not found" >&2; exit 1; }
    command -v python3 >/dev/null || { echo "python3 not found" >&2; exit 1; }
    command -v curl >/dev/null    || { echo "curl not found"    >&2; exit 1; }

    kubectl get ns "$ONLINEBOUTIQUE_NS" >/dev/null 2>&1 \
        || { echo "namespace ${ONLINEBOUTIQUE_NS} missing" >&2; exit 1; }
    kubectl get "ds/${TRACEE_DS}" -n "$TRACEE_NS" >/dev/null 2>&1 \
        || { echo "ds/${TRACEE_DS} not found in ns ${TRACEE_NS}" >&2; exit 1; }
    kubectl get "deployment/${LOCUST_DEPLOYMENT}" -n "$ONLINEBOUTIQUE_NS" >/dev/null 2>&1 \
        || { echo "locust deployment ${LOCUST_DEPLOYMENT} missing" >&2; exit 1; }

    if ! curl -sSf -m 3 http://localhost:8080/health >/dev/null 2>&1; then
        echo "ML service not reachable at http://localhost:8080" >&2; exit 1
    fi
    if ! curl -sSf -m 3 "${PROM_URL}/-/ready" >/dev/null 2>&1; then
        echo "Prometheus not reachable at ${PROM_URL}" >&2; exit 1
    fi

    [[ -x "$PROFILER" || -f "$PROFILER" ]] \
        || { echo "profiler missing: $PROFILER" >&2; exit 1; }

    log "preflight OK."
}

_compute_trial_offset() {
    local max_existing=0 d num basename
    if [[ -d "$RESULTS_ROOT" ]]; then
        for d in "${RESULTS_ROOT}"/trial_*; do
            [[ -d "$d" ]] || continue
            basename=$(basename "$d")
            num=${basename#trial_}
            [[ "$num" =~ ^[0-9]+$ ]] || continue
            num=$((10#$num))
            (( num > max_existing )) && max_existing=$num
        done
    fi
    echo $((max_existing + 1))
}

main() {
    _ensure_portforwards 0
    preflight

    if [[ "${SKIP_PREWARMUP:-0}" != "1" ]]; then
        log "[prewarmup] starting 30-min Locust warmup window (skip with SKIP_PREWARMUP=1)"
        set_locust_profile steady
        local prewarm_start=$(date +%s)
        local prewarm_target=1800   # 30 minutes
        while true; do
            local elapsed=$(( $(date +%s) - prewarm_start ))
            if (( elapsed >= prewarm_target )); then
                log "[prewarmup] done — elapsed ${elapsed}s"
                break
            fi
            local remaining=$(( prewarm_target - elapsed ))
            log "[prewarmup] ${elapsed}s / ${prewarm_target}s — remaining ${remaining}s"
            sleep 60
        done
    else
        log "[prewarmup] SKIP_PREWARMUP=1 — skipping warmup (for fast iteration only)"
    fi

    local trial_start trial_end
    if [[ -n "${TRIAL_OFFSET:-}" ]]; then
        trial_start="$TRIAL_OFFSET"
    else
        trial_start="$(_compute_trial_offset)"
    fi
    trial_end=$((trial_start + N_TRIALS - 1))

    if [[ "${REVERSE_ORDER:-0}" == "1" ]]; then
        _reversed=()
        for ((i=${#CONDITIONS_ARR[@]}-1; i>=0; i--)); do
            _reversed+=("${CONDITIONS_ARR[$i]}")
        done
        CONDITIONS_ARR=("${_reversed[@]}")
        log "[ablation] REVERSE_ORDER=1: condition order reversed"
    fi

    log "Campaign: ${N_TRIALS} trials × ${#CONDITIONS_ARR[@]} conditions"
    log "Trial id range: ${trial_start}..${trial_end} (next free after scanning ${RESULTS_ROOT})"
    log "Conditions: ${CONDITIONS_ARR[*]}"
    log "Results root: ${RESULTS_ROOT}"

    local trial condition trial_seq
    trial_seq=0
    for trial in $(seq "$trial_start" "$trial_end"); do
        trial_seq=$((trial_seq + 1))
        _ensure_portforwards "$trial"
        echo
        echo "##################################################################"
        log "TRIAL BLOCK ${trial_seq} / ${N_TRIALS}  (trial_id=${trial})"
        echo "##################################################################"

        local trial_conditions
        if [[ "${INTERLEAVE_ORDER:-0}" == "1" ]]; then
            if (( trial_seq % 2 == 1 )); then
                trial_conditions=("${CONDITIONS_ARR[@]}")
                log "[interleave] trial_seq=${trial_seq} (trial_id=${trial}): FORWARD order"
            else
                trial_conditions=()
                for ((i=${#CONDITIONS_ARR[@]}-1; i>=0; i--)); do
                    trial_conditions+=("${CONDITIONS_ARR[$i]}")
                done
                log "[interleave] trial_seq=${trial_seq} (trial_id=${trial}): REVERSE order"
            fi
        else
            trial_conditions=("${CONDITIONS_ARR[@]}")
        fi

        for condition in "${trial_conditions[@]}"; do
            local trial_dir_check
            trial_dir_check="$(printf '%s/trial_%02d' "$RESULTS_ROOT" "$trial")"
            local existing_csv="${trial_dir_check}/${condition}_raw_data.csv"
            if [[ -s "$existing_csv" ]]; then
                local existing_rows
                existing_rows=$(awk 'NR>2 && !/^#/' "$existing_csv" 2>/dev/null | wc -l | tr -d ' ')
                local MIN_RESUME_ROWS="${MIN_RESUME_ROWS:-100}"
                if [[ "$existing_rows" -ge "$MIN_RESUME_ROWS" ]]; then
                    log "[resume] SKIP trial_${trial}/${condition} — existing CSV has ${existing_rows} rows (≥${MIN_RESUME_ROWS})"
                    continue
                else
                    log "[resume] RE-RUN trial_${trial}/${condition} — existing CSV has only ${existing_rows} rows (<${MIN_RESUME_ROWS}); treating as partial"
                fi
            fi

            if ! _ensure_portforwards "$trial"; then
                log "[trial-failure] trial_${trial}/${condition} — PF/registry setup failed; skipping (will be re-run on resume)"
                continue
            fi

            if ! run_trial "$trial" "$condition"; then
                log "[trial-failure] trial_${trial}/${condition} returned non-zero — continuing to next condition (re-run by relaunching with the same RESULTS_ROOT)"
            fi
        done
    done

    echo
    log "All ${N_TRIALS} × ${#CONDITIONS_ARR[@]} = $(( N_TRIALS * ${#CONDITIONS_ARR[@]} )) trials complete."
    log "Per-trial outputs: ${RESULTS_ROOT}/trial_NN/<CONDITION>_{raw_data.csv,resource.json}"
    echo
    log "Next step:"
    echo "    python3 scripts/utils/calculate_statistics.py --cloud"

    if [[ "${TEARDOWN_AFTER_CAMPAIGN:-0}" == "1" ]]; then
        local provisioner="${REPO_ROOT}/scripts/infra/gke_provision.sh"
        if [[ ! -x "$provisioner" && ! -f "$provisioner" ]]; then
            log "WARN: TEARDOWN_AFTER_CAMPAIGN=1 but $provisioner not found"
            return
        fi
        log "──────────────────────────────────────────────────────────"
        log "TEARDOWN_AFTER_CAMPAIGN=1 — destroying cluster (and orphans)"
        log "──────────────────────────────────────────────────────────"
        TEARDOWN=1 bash "$provisioner"
    fi
}

main "$@"
