#!/usr/bin/env bash
# Local demo, no cluster: start the ml-service with the published models, stream
# synthetic tagged events through run_h2.py twice (phase rule off, then on) and
# show how the new pod's warmup window is scored.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PYTHON="${PYTHON:-python3}"
PORT=8080
OUT="${REPO_ROOT}/build/demo"
mkdir -p "$OUT"

[[ -f "${REPO_ROOT}/models/baseline.pkl" ]] || { echo "run 'make data' first" >&2; exit 1; }
if (exec 3<>"/dev/tcp/127.0.0.1/${PORT}") 2>/dev/null; then
    echo "port ${PORT} is busy (the drivers expect the ml-service there)" >&2; exit 1
fi

(cd "${REPO_ROOT}/ml-service" && IDS_MODELS_DIR=../models exec "$PYTHON" -m uvicorn app.main:app \
    --port "$PORT" --workers 1 --log-level warning) > "${OUT}/ml-service.log" 2>&1 &
ML_PID=$!
trap 'kill "$ML_PID" 2>/dev/null || true' EXIT
for _ in $(seq 1 30); do
    curl -fs "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1 && break
    sleep 0.5
done

for phase_rule in false true; do
    echo
    echo "=== phase rule $([[ $phase_rule == true ]] && echo ON || echo OFF) (about 20 s)"
    "$PYTHON" "${REPO_ROOT}/scripts/demo/synthetic_events.py" \
      | IDS_PROTECTED_PREFIXES=cartservice,frontend "$PYTHON" "${REPO_ROOT}/scripts/experiments/run_h2.py" \
          --apply-phase-authoritative "$phase_rule" --condition DEMO \
          --results-path "${OUT}/phase_rule_${phase_rule}_raw_data.csv" > "${OUT}/phase_rule_${phase_rule}.log" 2>&1
    grep -E "^W" "${OUT}/phase_rule_${phase_rule}.log" | sed 's/^/  /'
done
echo
echo "The new cartservice pod's first window (running_warmup) is flagged with the phase rule"
echo "off and scored against -0.95, so not flagged, with it on. Output: ${OUT}/"
