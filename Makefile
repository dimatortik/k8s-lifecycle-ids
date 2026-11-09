SHELL  := /bin/bash
PYTHON ?= python3
P1     := experiments/results/main_campaign_phase1_20260527-011238
P2     := experiments/results/main_campaign_phase2_20260527-094911
OUT    := build/stats
TRIALS_P1 := 1,2,3,4,5,6,7,8,9,10
TRIALS_P2 := 1,2,3,4,5,6,7,8,9,10,11,12,13,14,15
H3_CONDITIONS := H3_EXFIL_NAIVE,H3_EXFIL_ADV,H3_TOKEN_NAIVE,H3_TOKEN_ADV,H3_RSHELL_NAIVE,H3_RSHELL_ADV

.PHONY: help data stats replay demo test vet gke-up build deploy train campaign gke-down

help:
	@echo "Local (no cluster):"
	@echo "  make data      download and unpack the campaign data + models"
	@echo "  make stats     recompute the result tables and diff them against the shipped reports"
	@echo "  make replay    re-score all 30,591 published windows with the published models"
	@echo "  make demo      local ml-service + synthetic rollout, phase rule off vs on (~45 s)"
	@echo "  make test      Python and Go tests"
	@echo "  make vet       gofmt, go vet, go build for the controller"
	@echo "GKE (needs PROJECT=<gcp-project>, costs money):"
	@echo "  make gke-up build deploy train campaign gke-down"

data:
	bash scripts/fetch_data.sh

stats:
	@test -d $(P1) || { echo "run 'make data' first"; exit 1; }
	@mkdir -p $(OUT)
	$(PYTHON) scripts/utils/calculate_statistics.py --per-condition-fpr \
	    --results-root $(P1) --trials $(TRIALS_P1) --output $(OUT)/phase1_per_condition_fpr.md > /dev/null
	$(PYTHON) scripts/utils/calculate_statistics.py --per-rolled-pod \
	    --results-root $(P1) --trials $(TRIALS_P1) \
	    --conditions BASELINE_IDLE,BLIND_STEADY,BLIND_ROLLOUT,PHASE_ROLLOUT,PHASECTRL_ROLLOUT \
	    --output $(OUT)/phase1_per_rolled_pod_stats.md > /dev/null
	$(PYTHON) scripts/utils/calculate_statistics.py --per-attack-h3 \
	    --results-root $(P2) --trials $(TRIALS_P2) --output $(OUT)/phase2_per_attack_h3.md > /dev/null
	$(PYTHON) scripts/utils/calculate_resource_statistics.py --no-fix --campaign-root $(P1) \
	    --output $(OUT)/phase1_resource_statistics.md \
	    --per-trial-csv $(OUT)/phase1_resource_per_trial_metrics.csv > /dev/null 2>&1
	$(PYTHON) scripts/utils/calculate_resource_statistics.py --no-fix --campaign-root $(P2) \
	    --conditions $(H3_CONDITIONS) --trials $(TRIALS_P2) \
	    --output $(OUT)/phase2_resource_statistics.md \
	    --per-trial-csv $(OUT)/phase2_resource_per_trial_metrics.csv > /dev/null 2>&1
	@echo "--- comparing with the shipped reports"
	cmp $(OUT)/phase1_per_condition_fpr.md analysis/reports/phase1_per_condition_fpr.md
	cmp $(OUT)/phase1_per_rolled_pod_stats.md $(P1)/phase1_per_rolled_pod_stats.md
	cmp $(OUT)/phase2_per_attack_h3.md $(P2)/phase2_per_attack_h3_FINAL.md
	cmp $(OUT)/phase1_resource_per_trial_metrics.csv $(P1)/resource_per_trial_metrics.csv
	cmp $(OUT)/phase2_resource_per_trial_metrics.csv $(P2)/resource_per_trial_metrics.csv
	@# lines 1-3 of the resource reports differ only in the --no-fix note
	diff <(tail -n +4 $(OUT)/phase1_resource_statistics.md) <(tail -n +4 $(P1)/resource_statistics.md)
	diff <(tail -n +4 $(OUT)/phase2_resource_statistics.md) <(tail -n +4 $(P2)/resource_statistics.md)
	@echo "OK: all tables match the shipped reports"

replay:
	@test -d $(P1) || { echo "run 'make data' first"; exit 1; }
	$(PYTHON) scripts/utils/replay_scores.py $(P1) $(P2)

demo:
	PYTHON=$(PYTHON) bash scripts/demo/run_demo.sh

test:
	$(PYTHON) -m pytest -q -p no:cacheprovider scripts/utils/test_calculate_statistics.py
	cd ml-service && IDS_MODELS_DIR=../models $(PYTHON) -m pytest -q -p no:cacheprovider app/
	cd controller && go test ./...

vet:
	cd controller && test -z "$$(gofmt -l .)" && go vet ./... && go build ./...

gke-up:
	AUTO_TEARDOWN_HOURS=$${AUTO_TEARDOWN_HOURS:-6} bash scripts/infra/gke_provision.sh

build:
	bash scripts/infra/build_and_push.sh

deploy:
	bash scripts/infra/deploy_workload.sh

train:
	bash scripts/experiments/train_multi_model.sh

campaign:
	INTERLEAVE_ORDER=1 bash scripts/experiments/run_n10_cloud_trials.sh

gke-down:
	TEARDOWN=1 bash scripts/infra/gke_provision.sh
