# Results

This is the record of the final experimental results. All tables are
generated from the campaign CSVs in the data release (see the README).

Source reports:
- Phase 1 (FPR): `experiments/results/main_campaign_phase1_20260527-011238/`
  (`phase1_per_rolled_pod_stats.md`, `resource_statistics.md`) and
  `analysis/reports/phase1_per_condition_fpr.md`
- Phase 2 (TPR): `experiments/results/main_campaign_phase2_20260527-094911/`
  (`phase2_per_attack_h3_FINAL.md`, `resource_statistics.md`)

---

## 1. Setup

| Dimension | Value |
|---|---|
| Platform | GKE Standard, 3 × e2-standard-4, COS_CONTAINERD, no autoscaling |
| Workload | Google Online Boutique (11 services) under Locust load (50 users) |
| Telemetry | Tracee 0.24.1, events `execve, openat, connect, futex`, `--scope=container` |
| Detector | Per-service IsolationForest + fleet-pooled fallback (`baseline.pkl`), `contamination=0.05` |
| Controller | Go pod reconciler (controller-runtime v0.23.3), per-`pod_uid` threshold, `WarmupTTL=60s`, `--high-threshold=-0.95` |
| Phase rule | `/score` uses θ = −0.95 for any window with `lifecycle_phase=running_warmup` (unless disabled) |
| H3 attack classes | **exfil** (TA0010), **reverse shell** (TA0002 + TA0011), **token theft / T1528** (TA0006) |
| Phase 1 (FPR) campaign | N = 10 × 5 conditions — `main_campaign_phase1_20260527-011238` |
| Phase 2 (TPR) campaign | N = 11–13 × 6 conditions — `main_campaign_phase2_20260527-094911` |

Scope notes:

1. **No cryptominer.** XMRig (RandomX) emits about 3 syscalls/s in the captured
   event set, below the benign Online Boutique floor, so it cannot be separated.
   Token theft (MITRE T1528) is the third attack class instead.
2. **HPA and multi-namespace rollouts are not evaluated.** `HPA_SCALEOUT` and
   `MULTI_ROLLOUT` are implemented in `run_n10_cloud_trials.sh` but were not run;
   they are future work.

---

## 2. Phase 1: false-positive suppression (H1 + H2)

Campaign `main_campaign_phase1_20260527-011238`: 50 trials (5 conditions × 10),
all `valid=true`, 0 retries, 0 aborts, 0 Tracee lost events.

**Conditions:** `BASELINE_IDLE`, `BLIND_STEADY`, `BLIND_ROLLOUT`,
`PHASE_ROLLOUT` (phase rule only), `PHASECTRL_ROLLOUT` (phase rule + controller).

### 2.1 Per-rolled-pod FPR (`phase1_per_rolled_pod_stats.md`)

| Condition | FPR_rolled_all (mean) | range | FPR_rolled_warmup (mean) | range |
|---|---:|---:|---:|---:|
| BLIND_ROLLOUT | 23.30% | [13.64%, 41.67%] | 63.89% | [0%, 100%] |
| PHASE_ROLLOUT | 14.63% | [0%, 44.44%] | 0.00% | [0%, 0%] |
| PHASECTRL_ROLLOUT | 5.49% | [0%, 17.65%] | 0.00% | [0%, 0%] |

- **Ordering holds** on rolled-pod FPR: BLIND 23.30% → PHASE 14.63% → PHASECTRL 5.49%.
- **Warmup windows.** Pooled over the 10 trials, 8 of 17 BLIND_ROLLOUT warmup
  windows were false positives (47%, 95% Wilson CI 26–69%). With the phase rule
  there were 0 of 18 (PHASE, CI 0–18%) and 0 of 16 (PHASECTRL, CI 0–19%). The
  per-trial median goes from 100% to 0%. A trial has only 0–4 warmup
  windows, so the per-trial value is close to 0% or 100%.
- **Where PHASECTRL gains over PHASE.** The phase rule only covers windows
  labelled `running_warmup`. The controller also keeps θ = −0.95 for 60 s after
  the pod becomes Ready (the deferred clear), which covers the settling period;
  PHASECTRL trials have 3–7 such `running_ready` windows at −0.95, the other
  conditions none.

### 2.2 Formal tests (paired one-sided Wilcoxon signed-rank, Holm-corrected, α = 0.05)

Regenerate with:

```
python3 scripts/utils/calculate_statistics.py --per-condition-fpr \
  --results-root experiments/results/main_campaign_phase1_20260527-011238 \
  --trials 1,2,3,4,5,6,7,8,9,10
```

| Test | N | p (Holm) | r_rb | median Δ | Verdict |
|---|---:|---:|---:|---:|---|
| H2 — rolled FPR, PHASE | 9 | 0.0273 | 0.733 | +11.82 pp | **reject H0** |
| H2 — rolled FPR, PHASECTRL | 9 | 0.0078 | 1.000 | +18.18 pp | **reject H0** |
| H2 — warmup FPR, PHASE | 9 | 0.0234 | 1.000 | +100.00 pp | **reject H0** |
| H2 — warmup FPR, PHASECTRL | 9 | 0.0234 | 1.000 | +100.00 pp | **reject H0** |

All four reject H0. Three of four show perfect rank-biserial separation
(`r_rb = 1.0`: every paired trial in the expected direction); the fourth is
strongly positive (`r_rb = 0.733`).

### 2.3 H2c (no over-suppression): the verdict depends on the baseline

H2c tests whether the controller drives FPR more than δ_lower = 1 pp **below**
the baseline (`H0: median(FPR_s − FPR_b) ≤ −1 pp`):

| Baseline (FPR_b) | median(FPR_s − FPR_b) | 95% bootstrap CI | one-sided Wilcoxon p | H2c |
|---|---:|---:|---:|---|
| `BASELINE_IDLE` (Locust 1 user) | −4.74 pp | [−9.08, −1.50] | 0.997 | fails (apparent over-suppression) |
| `BLIND_STEADY` (Locust 50 users, same load) | **+2.54 pp** | [+0.65, +5.29] | **0.0029** | **confirmed** |

**The `BASELINE_IDLE` comparison is confounded.** The models are trained at
steady load (50 users). Idle traffic (1 user) is out of distribution for them,
so idle FPR is inflated (about 15–27% per trial, against 7–13% for
`BLIND_STEADY`). PHASECTRL looks "below" idle only because idle is high.

Against the same-load `BLIND_STEADY` baseline the controller keeps FPR at or
slightly above baseline (+2.54 pp), so **H2c holds: no over-suppression**. The
Phase-2 results (§3) agree: attacks are still detected.

Both rows were computed once from the campaign CSVs (per-trial aggregate FPR,
paired by trial, `_bootstrap_median_ci` and a one-sided Wilcoxon
non-inferiority test at δ_lower = 1 pp); there is no CLI mode for them.

---

## 3. Phase 2: true-positive preservation under attack (H3)

Campaign `main_campaign_phase2_20260527-094911`: 74 trials across 6 conditions
(N = 11–13 each), **332 malicious windows** in total. Per-attack TPR with a
Wilson 95% CI and a one-sided exact binomial test of H0: TPR ≥ 0.90. Windows
are pooled across trials because a trial has only 1–4 malicious windows.

### 3.1 Per-attack window-pooled TPR (`phase2_per_attack_h3_FINAL.md`)

| Condition | n_mal | n_anom | TPR | 95% Wilson CI | p (H0: TPR≥0.90) | N |
|---|---:|---:|---:|---|---:|---:|
| H3_EXFIL_NAIVE | 58 | 48 | 82.8% | [71.1%, 90.4%] | 0.061 | 13 |
| H3_EXFIL_ADV | 62 | 53 | 85.5% | [74.7%, 92.2%] | 0.163 | 13 |
| H3_TOKEN_NAIVE | 46 | 45 | 97.8% | [88.7%, 99.6%] | 0.992 | 13 |
| H3_TOKEN_ADV | 42 | 39 | 92.9% | [81.0%, 97.5%] | 0.805 | 11 |
| H3_RSHELL_NAIVE | 60 | 58 | 96.7% | [88.6%, 99.1%] | 0.986 | 11 |
| H3_RSHELL_ADV | 64 | 62 | 96.9% | [89.3%, 99.1%] | 0.990 | 13 |

**5 of 6 conditions cannot reject TPR ≥ 0.90.** EXFIL_NAIVE is borderline
(82.8%, p = 0.061; the CI upper bound touches 90%).

All 332 malicious windows were labelled `running_ready` and scored at the
fleet-fallback threshold θ = −0.6412: attacker pods have no readiness probe
(Ready at once) and no `app` label (fallback model), and the controller does not
manage them. The warmup threshold therefore never applied to attack windows.

### 3.2 Naive vs adversarial timing (Fisher exact + Newcombe CI)

| Attack | TPR_naive | TPR_adv | Δ TPR | 95% CI (Newcombe) | Fisher p |
|---|---|---|---:|---:|---:|
| EXFIL | 82.8% (48/58) | 85.5% (53/62) | −2.7 pp | [−16.2%, 10.5%] | 0.804 |
| TOKEN | 97.8% (45/46) | 92.9% (39/42) | +5.0 pp | [−5.3%, 17.0%] | 0.344 |
| RSHELL | 96.7% (58/60) | 96.9% (62/64) | −0.2 pp | [−8.6%, 7.7%] | 1.000 |

**No significant TPR difference between naive and adversarial timing for any
attack class.** The adversary watches the public `ThresholdRaised` /
`ThresholdCleared` Kubernetes Events and times its injection on them.

---

## 4. Headline claims

1. **H1 (the FP spike exists):** without lifecycle context, 47% of warmup
   windows during rolling updates are false positives (8/17; per-trial median 100%).
2. **H2 (suppression works):** the phase rule removes warmup false positives
   (0/18 and 0/16); rolled-pod FPR drops 23.30% → 14.63% → 5.49%; all four
   Wilcoxon tests reject H0 under Holm correction; no over-suppression against
   the same-load baseline.
3. **H3 (true positives are kept):** 5 of 6 attack conditions hold TPR ≥ 0.90;
   adversarial timing gives no advantage in any attack class.

---

## 5. Limitations

- **EXFIL_NAIVE is borderline** (82.8%, CI touches 90%), the weakest result.
- **Few warmup windows.** Go services become Ready within 1–2 s, shorter than the
  10 s scoring window, so there are only 16–18 warmup windows per condition.
  The warmup claim rests on these pooled counts; the rolled-pod FPR rests on
  many more windows.
- **Suppression scope.** The phase rule applies to every `running_warmup`
  window, and the controller holds −0.95 for 60 s after Ready on protected pods.
  The attacks tested ran in separate pods. An attack *inside* a protected pod
  during its warmup or the 60 s hold was not tested and would likely be missed.
  A threshold relative to each model's `offset_` (between −0.544 and −0.641)
  instead of the absolute −0.95 is future work.
- **Window pooling in H3** treats windows from the same trial as independent, so
  the Wilson CIs and Fisher tests are somewhat optimistic.
- **Window phase labelling differs between drivers.** `run_h2.py` labels a
  window warmup if it contains any warmup event; `run_h3.py` uses the phase of
  the last event. Attack windows are all `running_ready`, so H3 is not affected.
- **Lost attacker windows.** A window with an empty `pod_uid` is rejected by
  `/score` (422) and not recorded; `run_h3.py` writes no validity file.
- **Near one-dimensional features.** With 4 syscalls and `--scope=container`,
  about 99% of Online Boutique events are `futex`, so the model effectively
  sees the per-pod futex rate. Adding `sched_yield` overflowed the
  kubectl-logs ingestion.
- **Small training sets.** Per-service n ≈ 100–200 < 256, so IsolationForest
  sub-sampling is effectively off. currencyservice and paymentservice have no
  own model and use the fallback.
- **Load generator.** Locust is not restarted between trials and keep-alive
  connections spread load unevenly over replicas.
- **Environment.** GKE used the rapid channel; the exact GKE/COS/kernel version
  was not recorded. Results will differ on other kernels or clusters.
- **Statistics library versions.** The exact Wilcoxon test with ties depends on
  the SciPy version.

---

## 6. Reproducing the tables

The tables above are recomputed from the published CSVs (no cluster needed):
the three `calculate_statistics.py` modes (`--per-condition-fpr`,
`--per-rolled-pod`, `--per-attack-h3`) and `calculate_resource_statistics.py`.
`make replay` additionally re-scores all 30,591 windows with the
published models and checks every score, threshold and verdict. See the README
for the exact commands. A new campaign needs the GKE pipeline in
`scripts/infra/` and `scripts/experiments/`.

System design: [architecture.md](architecture.md).
