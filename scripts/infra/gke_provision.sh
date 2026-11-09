#!/usr/bin/env bash
# Create the GKE cluster used for the campaigns and install kube-prometheus-stack.
#
# GKE Standard (Tracee needs a privileged DaemonSet with hostPID), 3 x
# e2-standard-4 on COS_CONTAINERD, Calico network policy, Prometheus with a
# 5 s scrape interval. Idempotent; TEARDOWN=1 deletes the cluster.
#
# Usage:
#   PROJECT=<gcp-project> AUTO_TEARDOWN_HOURS=6 bash scripts/infra/gke_provision.sh
#   PROJECT=<gcp-project> TEARDOWN=1 bash scripts/infra/gke_provision.sh

set -euo pipefail

PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null || echo '')}"
REGION="${REGION:-us-central1}"
ZONE="${ZONE:-us-central1-a}"
CLUSTER_NAME="${CLUSTER_NAME:-ids-phase2}"
NODE_COUNT="${NODE_COUNT:-3}"
MACHINE_TYPE="${MACHINE_TYPE:-e2-standard-4}"        # 4 vCPU / 16 GiB
DISK_SIZE_GB="${DISK_SIZE_GB:-50}"                   # Tracee output is streamed, not stored
DISK_TYPE="${DISK_TYPE:-pd-standard}"                # standard, not SSD — cost
RELEASE_CHANNEL="${RELEASE_CHANNEL:-rapid}"          # GKE version was not pinned
IMAGE_TYPE="${IMAGE_TYPE:-COS_CONTAINERD}"           # Container-Optimized OS
NETWORK_POLICY="${NETWORK_POLICY:-true}"             # Calico

MONITORING_NS="${MONITORING_NS:-monitoring}"
PROM_CHART_VERSION="${PROM_CHART_VERSION:-65.5.1}"   # pin for repeatability
PROM_SCRAPE_INTERVAL="${PROM_SCRAPE_INTERVAL:-5s}"

KUBECTL_CONTEXT_PREFIX="${KUBECTL_CONTEXT_PREFIX:-gke_${PROJECT}_${ZONE}_${CLUSTER_NAME}}"

log()  { echo "[$(date +%H:%M:%S)] $*"; }
fail() { echo "ERROR: $*" >&2; exit 1; }

require_cmd() { command -v "$1" >/dev/null || fail "missing CLI: $1"; }

cluster_exists() {
    gcloud container clusters describe "$CLUSTER_NAME" \
        --zone "$ZONE" --project "$PROJECT" >/dev/null 2>&1
}

sweep_orphaned_disks() {
    require_cmd gcloud
    log "[sweep] scanning for orphaned PD disks in zone=${ZONE}..."

    local orphans
    orphans=$(gcloud compute disks list \
        --project "$PROJECT" \
        --filter="zone:(${ZONE}) AND -users:*" \
        --format='value(name)' 2>/dev/null || true)

    if [[ -z "$orphans" ]]; then
        log "[sweep] no orphaned disks found."
        return 0
    fi

    log "[sweep] orphaned disks queued for deletion:"
    echo "$orphans" | sed 's/^/    /'

    local d
    while IFS= read -r d; do
        [[ -z "$d" ]] && continue
        if gcloud compute disks delete "$d" \
            --project "$PROJECT" --zone "$ZONE" --quiet 2>/dev/null; then
            log "[sweep] deleted disk: $d"
        else
            log "[sweep] WARN: failed to delete disk: $d (continuing)"
        fi
    done <<< "$orphans"
}

if [[ "${TEARDOWN:-0}" == "1" ]]; then
    require_cmd gcloud
    [[ -n "$PROJECT" ]] || fail "PROJECT not set"
    log "TEARDOWN: deleting cluster ${CLUSTER_NAME} in ${ZONE} (project=${PROJECT})"
    if cluster_exists; then
        gcloud container clusters delete "$CLUSTER_NAME" \
            --zone "$ZONE" --project "$PROJECT" --quiet
        log "cluster deleted."
    else
        log "cluster already absent."
    fi
    sweep_orphaned_disks
    log "TEARDOWN complete.  Verify zero spend with: gcloud billing accounts list"
    exit 0
fi

require_cmd gcloud
require_cmd kubectl
require_cmd helm
[[ -n "$PROJECT" ]] || fail "set PROJECT or run: gcloud config set project <id>"
log "project=${PROJECT}  zone=${ZONE}  cluster=${CLUSTER_NAME}"
log "node pool: ${NODE_COUNT} × ${MACHINE_TYPE} (${IMAGE_TYPE}, ${DISK_SIZE_GB}GB ${DISK_TYPE})"

log "[1/5] enabling GCP APIs..."
gcloud services enable \
    container.googleapis.com \
    compute.googleapis.com \
    monitoring.googleapis.com \
    logging.googleapis.com \
    --project "$PROJECT" >/dev/null
log "APIs enabled."

ACTIVE_ACCOUNT="$(gcloud config get-value account 2>/dev/null || true)"
if [[ -n "$ACTIVE_ACCOUNT" ]]; then
    HAS_ADMIN="$(gcloud projects get-iam-policy "$PROJECT" \
        --flatten='bindings[].members' \
        --filter="bindings.members:${ACTIVE_ACCOUNT} AND bindings.role:roles/container.admin" \
        --format='value(bindings.role)' 2>/dev/null || true)"
    if [[ -z "$HAS_ADMIN" ]]; then
        log "WARN: ${ACTIVE_ACCOUNT} does not have roles/container.admin on ${PROJECT}"
        log "      cluster create will fail unless an equivalent role is bound."
    fi
fi

if cluster_exists; then
    log "[3/5] cluster ${CLUSTER_NAME} already exists — skipping create."
else
    log "[3/5] creating GKE Standard cluster..."
    gcloud container clusters create "$CLUSTER_NAME" \
        --project "$PROJECT" \
        --zone "$ZONE" \
        --release-channel "$RELEASE_CHANNEL" \
        --num-nodes "$NODE_COUNT" \
        --machine-type "$MACHINE_TYPE" \
        --image-type "$IMAGE_TYPE" \
        --disk-type "$DISK_TYPE" \
        --disk-size "$DISK_SIZE_GB" \
        --enable-network-policy \
        --no-enable-autoscaling \
        --no-enable-master-authorized-networks \
        --logging=NONE \
        --monitoring=NONE \
        --addons=HorizontalPodAutoscaling \
        --quiet
    log "cluster created."
fi

log "[4/5] fetching credentials..."
gcloud container clusters get-credentials "$CLUSTER_NAME" \
    --zone "$ZONE" --project "$PROJECT"
kubectl config use-context "$KUBECTL_CONTEXT_PREFIX" >/dev/null 2>&1 || true
kubectl cluster-info >/dev/null
log "kubectl context active."

if [[ "${SKIP_HELM:-0}" == "1" ]]; then
    log "[5/5] SKIP_HELM=1 — skipping Prometheus install."
    exit 0
fi

log "[5/5] installing kube-prometheus-stack into ns=${MONITORING_NS}..."
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update >/dev/null

PROM_VALUES_FILE="$(mktemp -t prom-values.XXXX).yaml"
cat > "$PROM_VALUES_FILE" <<EOF
fullnameOverride: kps
prometheus:
  prometheusSpec:
    scrapeInterval: ${PROM_SCRAPE_INTERVAL}
    evaluationInterval: 30s
    retention: 24h
    retentionSize: "8GB"
    resources:
      requests:
        cpu: 200m
        memory: 800Mi
      limits:
        cpu: 1
        memory: 2Gi
    storageSpec:
      volumeClaimTemplate:
        spec:
          accessModes: ["ReadWriteOnce"]
          storageClassName: standard-rwo
          resources:
            requests:
              storage: 10Gi
    # Pick up every ServiceMonitor / PodMonitor in any namespace — needed
    # for the IDS controller's ServiceMonitor.
    serviceMonitorSelectorNilUsesHelmValues: false
    podMonitorSelectorNilUsesHelmValues: false
    ruleSelectorNilUsesHelmValues: false
nodeExporter:
  enabled: true
kube-state-metrics:
  enabled: true
prometheusOperator:
  resources:
    requests:
      cpu: 100m
      memory: 200Mi
grafana:
  enabled: false
alertmanager:
  enabled: false
EOF

kubectl get ns "$MONITORING_NS" >/dev/null 2>&1 \
    || kubectl create ns "$MONITORING_NS"

helm upgrade --install kps prometheus-community/kube-prometheus-stack \
    --namespace "$MONITORING_NS" \
    --version "$PROM_CHART_VERSION" \
    -f "$PROM_VALUES_FILE" \
    --wait --timeout 10m

rm -f "$PROM_VALUES_FILE"

log "Prometheus stack installed.  Port-forward with:"
echo "  kubectl -n ${MONITORING_NS} port-forward svc/kps-kube-prometheus-stack-prometheus 9091:9090"
log "Set PROM_URL=http://localhost:9091 before running run_n10_cloud_trials.sh"

if [[ "${AUTO_TEARDOWN_HOURS:-0}" -gt 0 ]]; then
    if ! command -v at >/dev/null 2>&1; then
        log "WARN: 'at' not installed — auto-teardown NOT scheduled."
        log "      On macOS: 'sudo launchctl load -w /System/Library/LaunchDaemons/com.apple.atrun.plist'"
        log "      On Linux: 'apt install at && systemctl enable --now atd'"
    else
        SELF="$(realpath "$0")"
        AT_SPEC="now + ${AUTO_TEARDOWN_HOURS} hours"
        AT_JOB_LOG="${HOME}/.cache/gke_auto_teardown_$(date +%s).log"
        mkdir -p "$(dirname "$AT_JOB_LOG")"
        echo "env TEARDOWN=1 PROJECT='${PROJECT}' ZONE='${ZONE}' \
CLUSTER_NAME='${CLUSTER_NAME}' bash '${SELF}' > '${AT_JOB_LOG}' 2>&1" \
            | at "$AT_SPEC" 2>&1 | tee -a "$AT_JOB_LOG"
        log "AUTO_TEARDOWN scheduled: $AT_SPEC"
        log "  cancel with: atq    (find job id)    then    atrm <id>"
        log "  log:         $AT_JOB_LOG"
    fi
elif [[ "${STRICT_AUTO_TEARDOWN:-1}" == "1" ]]; then
    log "─────────────────────────────────────────────────────────────────────"
    log "WARN: AUTO_TEARDOWN_HOURS is unset."
    log "      Cluster will run until manually torn down — at \$0.50/hr."
    log "      Recommended:  AUTO_TEARDOWN_HOURS=6 bash $0"
    log "      Override:     STRICT_AUTO_TEARDOWN=0 to silence this warning."
    log "─────────────────────────────────────────────────────────────────────"
fi

log "DONE.  Next step: deploy Online Boutique, controller, ML service."
log "REMEMBER: TEARDOWN=1 bash $0 when trials complete (cluster ~ \$3/h running idle)"
