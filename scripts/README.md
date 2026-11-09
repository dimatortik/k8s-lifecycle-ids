# scripts

## infra/ (GKE)
| Script | What |
|---|---|
| `gke_provision.sh` | GKE Standard cluster (3 × e2-standard-4, COS_CONTAINERD, Calico) + kube-prometheus-stack 65.5.1; `TEARDOWN=1` deletes it |
| `build_and_push.sh` | build the controller and ml-service images, push to Artifact Registry |
| `deploy_workload.sh` | deploy Tracee, ml-service, controller, monitors, Online Boutique (+ patch) |

All take `PROJECT=<gcp-project>`.

## experiments/
| Script | What |
|---|---|
| `train_multi_model.sh` / `.py` | collect steady-state windows and train the per-service models + fallback |
| `run_n10_cloud_trials.sh` | campaign orchestrator (all conditions, trials, resource collection) |
| `run_h2.py` | scoring driver for benign conditions (FPR) |
| `run_h3.py` | scoring driver for attack conditions (TPR) |
| `inject_attack_adversarial.py` | applies the attacker pod timed on the controller's `ThresholdRaised` Events |
| `_protected_filter.py` | which pods count as protected (deployment label) |

Main orchestrator settings: `N_TRIALS`, `CONDITIONS`, `INTERLEAVE_ORDER=1`
(alternate condition order between trials), `RESULTS_ROOT`, `TRIAL_MAX_SECS`.

## utils/
| Script | What |
|---|---|
| `lifecycle_tagger.py` | enrich Tracee events with pod UID, app label and lifecycle phase |
| `resource_profiler.py` | per-trial Prometheus collection (CPU/memory, controller latency, Tracee throughput) |
| `calculate_statistics.py` + `idsanalysis/` | FPR/TPR statistics (see `--help`) |
| `calculate_resource_statistics.py` | resource, latency and cost report per campaign |
| `sync_models_to_pod.sh` | copy freshly trained models into the running ml-service pod |
| `test_calculate_statistics.py` | unit tests for the statistics library |
| `replay_scores.py` | re-score every published window offline and check score, threshold and verdict |

## demo/
`run_demo.sh` (`make demo`) starts the ml-service locally and feeds `synthetic_events.py`
through `run_h2.py` with the phase rule off and on.

`fetch_data.sh` (`make data`) downloads and verifies the data release.
