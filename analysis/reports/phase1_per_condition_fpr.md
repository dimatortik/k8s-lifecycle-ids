## Per-condition rolled-pod FPR (N=10 trials)

| Condition | FPR_rolled_all mean | range | FPR_rolled_warmup mean | range |
|---|---:|---:|---:|---:|
| BLIND_ROLLOUT | 23.30% | [13.64%, 41.67%] | 63.89% | [0.00%, 100.00%] |
| PHASE_ROLLOUT | 14.63% | [0.00%, 44.44%] | 0.00% | [0.00%, 0.00%] |
| PHASECTRL_ROLLOUT | 5.49% | [0.00%, 17.65%] | 0.00% | [0.00%, 0.00%] |

## H2 formal tests — paired one-sided Wilcoxon (baseline BLIND_ROLLOUT > treatment), Holm-corrected α=0.05

| Test | N | W | p (raw) | p (Holm) | r_rb | median Δ | Verdict |
|---|---:|---:|---:|---:|---:|---:|---|
| H2 — rolled FPR, PHASE | 9 | 39.00 | 0.0273 | 0.0273 | +0.733 | +11.82 pp | **reject H0** |
| H2 — rolled FPR, PHASECTRL | 9 | 45.00 | 0.0020 | 0.0078 | +1.000 | +18.18 pp | **reject H0** |
| H2 — warmup FPR, PHASE | 9 | 28.00 | 0.0078 | 0.0234 | +1.000 | +100.00 pp | **reject H0** |
| H2 — warmup FPR, PHASECTRL | 9 | 28.00 | 0.0078 | 0.0234 | +1.000 | +100.00 pp | **reject H0** |

> Pairing is by trial; pairs where either condition has no rolled pods (FPR undefined) are dropped. Wilcoxon method is pinned (exact for N≤50). Reproduces RESULTS.md §2.2.