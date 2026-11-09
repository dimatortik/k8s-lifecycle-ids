#!/usr/bin/env python3
"""Campaign-level resource, latency and cost statistics.

Aggregates the per-trial <COND>_resource*.{json,csv} and score_telemetry.csv
files into one markdown report. Without --no-fix it first recomputes the
summary fields of each <COND>_resource.json from its CSVs (the collector
propagated NaN from warm-up scrapes).

Example:
    python3 scripts/utils/calculate_resource_statistics.py --no-fix \\
        --campaign-root experiments/results/main_campaign_phase1_20260527-011238 \\
        --per-trial-csv /tmp/resource_per_trial_metrics.csv
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from idsanalysis.campaign import discover_conditions as _discover_conditions
from idsanalysis.campaign import discover_trials as _discover_trials


_RESOURCE_METRICS = (
    "controller_cpu_cores",
    "controller_mem_bytes",
    "ml_service_cpu_cores",
    "ml_service_mem_bytes",
    "tracee_events_per_sec",
    "tracee_egress_bytes_per_sec",
    "tracee_lost_events_per_sec",
    "tctrl_p95_seconds",
    "tctrl_p99_seconds",
    "tctrl_e2e_p95_seconds",
    "tctrl_e2e_p99_seconds",
)


def _warn_unreadable(path, exc) -> None:
    print(f"WARN: skipping unreadable {path}: {exc}", file=sys.stderr)


def _stats_from_values(values: np.ndarray) -> dict:
    """Compute mean/p50/p95/p99/max/sum on non-NaN entries."""
    valid = values[~np.isnan(values)]
    n = int(valid.size)
    if n == 0:
        return {
            "samples_non_nan": 0,
            "mean_": float("nan"), "p50": float("nan"),
            "p95":   float("nan"), "p99": float("nan"),
            "max_":  float("nan"), "sum_": float("nan"),
        }
    return {
        "samples_non_nan": n,
        "mean_": float(valid.mean()),
        "p50":   float(np.percentile(valid, 50)),
        "p95":   float(np.percentile(valid, 95)),
        "p99":   float(np.percentile(valid, 99)),
        "max_":  float(valid.max()),
        "sum_":  float(valid.sum()),
    }


def _stats_from_csv(df: pd.DataFrame) -> dict:
    """Compute stats from a resource-CSV, summing across pods per timestamp for multi-label series (DaemonSets, replicated workloads)."""
    if "value" not in df.columns:
        return _stats_from_values(np.array([], dtype=float))
    if "ts" in df.columns and len(df) > 0 and df["ts"].nunique() < len(df):
        agg = df.groupby("ts")["value"].sum(min_count=1)
        return _stats_from_values(agg.to_numpy(dtype=float))
    return _stats_from_values(df["value"].to_numpy(dtype=float))


def fix_resource_json(trial_dir: Path, condition: str) -> dict:
    """Recompute summary stats in {COND}_resource.json from companion CSVs."""
    json_path = trial_dir / f"{condition}_resource.json"
    if not json_path.exists():
        return {}

    blob = json.loads(json_path.read_text())
    series = blob.get("series", {})

    diffs: dict = {}
    for metric, entry in series.items():
        csv_path = trial_dir / f"{condition}_resource_{metric}.csv"
        if not csv_path.exists():
            continue
        try:
            df = pd.read_csv(csv_path)
        except Exception as exc:
            _warn_unreadable(csv_path, exc)
            continue
        if "value" not in df.columns:
            continue

        new = _stats_from_csv(df)

        diffs[metric] = {
            "before": {k: entry.get(k) for k in ("mean_", "p50", "p95", "p99", "max_")},
            "after":  {k: new[k]       for k in ("mean_", "p50", "p95", "p99", "max_")},
            "samples_non_nan": new["samples_non_nan"],
        }

        entry["mean_"] = new["mean_"]
        entry["p50"]   = new["p50"]
        entry["p95"]   = new["p95"]
        entry["p99"]   = new["p99"]
        entry["max_"]  = new["max_"]
        entry["sum_"]  = new["sum_"]
        entry["samples_non_nan"] = new["samples_non_nan"]

    blob["_recomputed_at"] = datetime.now(timezone.utc).isoformat()
    blob["_recomputed_note"] = (
        "Summary stats (mean_, p50/p95/p99, max_, sum_) recomputed from "
        "non-NaN rows of {COND}_resource_<metric>.csv by "
        "scripts/utils/calculate_resource_statistics.py.  The original "
        "collector used NaN-propagating arithmetic so warm-up scrapes of "
        "histogram metrics (tctrl_p95, tctrl_e2e_p95) yielded NaN "
        "summaries despite valid data in the CSV.  Field samples_non_nan "
        "is the number of CSV rows that contributed to the new summary."
    )
    json_path.write_text(json.dumps(blob, indent=2, allow_nan=True))
    return diffs


def aggregate_resource(campaign_root: Path,
                       trials: list[int],
                       conditions: list[str]) -> dict:
    """Pool non-NaN values from `{COND}_resource_<metric>.csv` across trials."""
    out: dict = {}
    for cond in conditions:
        out[cond] = {}
        for metric in _RESOURCE_METRICS:
            pooled: list[float] = []
            trials_with_data = 0
            for tid in trials:
                csv_path = (campaign_root / f"trial_{tid:02d}" /
                            f"{cond}_resource_{metric}.csv")
                if not csv_path.exists():
                    continue
                try:
                    df = pd.read_csv(csv_path)
                except Exception as exc:
                    _warn_unreadable(csv_path, exc)
                    continue
                if "value" not in df.columns:
                    continue
                if "ts" in df.columns and len(df) > 0 and df["ts"].nunique() < len(df):
                    series_vals = df.groupby("ts")["value"].sum(min_count=1)
                else:
                    series_vals = df["value"]
                vals = series_vals.dropna().to_numpy(dtype=float)
                if vals.size > 0:
                    pooled.extend(vals.tolist())
                    trials_with_data += 1
            if not pooled:
                out[cond][metric] = None
                continue
            arr = np.asarray(pooled, dtype=float)
            out[cond][metric] = {
                "n":        int(arr.size),
                "mean":     float(arr.mean()),
                "p50":      float(np.percentile(arr, 50)),
                "p95":      float(np.percentile(arr, 95)),
                "p99":      float(np.percentile(arr, 99)),
                "max":      float(arr.max()),
                "n_trials": trials_with_data,
            }
    return out


def aggregate_score_telemetry(campaign_root: Path,
                              trials: list[int],
                              conditions: list[str]) -> dict:
    """Pool response_ms (status==ok) across trials per condition."""
    out: dict = {}
    for cond in conditions:
        pooled_ms: list[float] = []
        n_timeout = n_error = n_total = 0
        for tid in trials:
            csv_path = (campaign_root / f"trial_{tid:02d}" /
                        f"{cond}_score_telemetry.csv")
            if not csv_path.exists():
                continue
            try:
                df = pd.read_csv(csv_path)
            except Exception as exc:
                _warn_unreadable(csv_path, exc)
                continue
            n_total += len(df)
            if not {"status", "response_ms"} <= set(df.columns):
                continue
            ok_ms = df.loc[df["status"] == "ok", "response_ms"].dropna()
            pooled_ms.extend(ok_ms.tolist())
            n_timeout += int((df["status"] == "timeout").sum())
            n_error   += int((df["status"] == "error").sum())
        if not pooled_ms:
            out[cond] = None
            continue
        arr = np.asarray(pooled_ms, dtype=float)
        out[cond] = {
            "n_ok":      int(arr.size),
            "n_timeout": n_timeout,
            "n_error":   n_error,
            "n_total":   n_total,
            "mean":      float(arr.mean()),
            "p50":       float(np.percentile(arr, 50)),
            "p95":       float(np.percentile(arr, 95)),
            "p99":       float(np.percentile(arr, 99)),
            "max":       float(arr.max()),
        }
    return out


def aggregate_cost(campaign_root: Path,
                   trials: list[int],
                   conditions: list[str]) -> dict:
    """Aggregate `cost_projection` blocks across trials per condition."""
    def _summ(xs: list) -> Optional[dict]:
        arr = np.asarray([x for x in xs if x is not None], dtype=float)
        if arr.size == 0:
            return None
        return {
            "mean": float(arr.mean()),
            "min":  float(arr.min()),
            "max":  float(arr.max()),
            "n":    int(arr.size),
        }

    out: dict = {}
    for cond in conditions:
        arch_a_total: list = []
        ctrl_cost: list = []
        ml_cost: list = []
        datadog: list = []
        elastic: list = []
        splunk: list = []
        egress_gb: list = []
        durations: list = []
        for tid in trials:
            jpath = (campaign_root / f"trial_{tid:02d}" /
                     f"{cond}_resource.json")
            if not jpath.exists():
                continue
            try:
                blob = json.loads(jpath.read_text())
            except Exception as exc:
                _warn_unreadable(jpath, exc)
                continue
            cp = blob.get("cost_projection", {}) or {}
            a = cp.get("arch_a_monthly_usd_list_price", {}) or {}
            b = cp.get("arch_b_counterfactual_monthly_usd_list_price", {}) or {}

            arch_a_total.append(a.get("total"))
            ctrl_cost.append(a.get("controller"))
            ml_cost.append(a.get("ml_service"))
            datadog.append(b.get("datadog_cloud_siem"))
            elastic.append(b.get("elastic_serverless_complete"))
            splunk.append(b.get("splunk_cloud_list"))
            egress_gb.append(b.get("gross_egress_gb_per_month_estimate"))
            durations.append(cp.get("duration_s"))

        out[cond] = {
            "arch_a_total":        _summ(arch_a_total),
            "arch_a_controller":   _summ(ctrl_cost),
            "arch_a_ml_service":   _summ(ml_cost),
            "datadog":             _summ(datadog),
            "elastic":             _summ(elastic),
            "splunk":              _summ(splunk),
            "egress_gb_per_month": _summ(egress_gb),
            "duration_s":          _summ(durations),
        }
    return out


def compute_e2e_cap_rates(campaign_root: Path,
                          trials: list[int],
                          conditions: list[str],
                          cap_seconds: float = 30.0) -> dict:
    out: dict = {}
    for cond in conditions:
        pooled: list[float] = []
        for tid in trials:
            csv_path = (campaign_root / f"trial_{tid:02d}" /
                        f"{cond}_resource_tctrl_e2e_p95_seconds.csv")
            if not csv_path.exists():
                continue
            try:
                df = pd.read_csv(csv_path)
            except Exception as exc:
                _warn_unreadable(csv_path, exc)
                continue
            if "value" not in df.columns:
                continue
            pooled.extend(df["value"].dropna().tolist())
        if not pooled:
            out[cond] = None
            continue
        arr = np.asarray(pooled, dtype=float)
        capped = int((arr >= cap_seconds).sum())
        out[cond] = {
            "n":            int(arr.size),
            "n_capped":     capped,
            "cap_rate":     capped / arr.size,
            "p50_below_cap": float(np.percentile(arr[arr < cap_seconds], 50))
                if (arr < cap_seconds).any() else None,
            "p95_below_cap": float(np.percentile(arr[arr < cap_seconds], 95))
                if (arr < cap_seconds).any() else None,
        }
    return out


def _fmt_cores(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x:.4f}"

def _fmt_mb(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x / 1e6:.1f}"

def _fmt_mbs(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x / 1e6:.2f}"

def _fmt_ms_from_s(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x * 1000:.2f}"

def _fmt_int(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x:,.0f}"

def _fmt_pct(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x * 100:.2f}%"

def _fmt_usd(x: Optional[float]) -> str:
    return "n/a" if x is None else f"${x:,.2f}"

def _get(d: Optional[dict], k: str) -> Optional[float]:
    return None if d is None else d.get(k)


def render_markdown(campaign_root: Path,
                    trials: list[int],
                    conditions: list[str],
                    res: dict,
                    score: dict,
                    cost: dict,
                    e2e_caps: dict,
                    fixes_total: int) -> str:
    L: list[str] = []
    L.append(f"# Resource, latency & cost statistics — {campaign_root.name}")
    L.append("")
    L.append(
        f"Trials analysed: **{len(trials)}**  •  conditions: "
        f"{', '.join(conditions)}  •  resource.json files recomputed: "
        f"**{fixes_total}**"
    )
    L.append("")
    L.append("> Pooled across all trials per condition.  All percentiles are "
             "computed on the **non-NaN** entries of the per-scrape CSV "
             "streams (`*_resource_<metric>.csv`), eliminating the "
             "NaN-propagation bug in the original `mean_` summaries.")
    L.append("")

    L.append("## 1. Compute footprint — controller & ML-service")
    L.append("")
    L.append("| Condition | ctrl CPU mean (cores) | ctrl CPU p95 | ctrl RAM p95 (MB) | ml CPU mean | ml CPU p95 | ml RAM p95 (MB) |")
    L.append("|---|---:|---:|---:|---:|---:|---:|")
    for c in conditions:
        ctrl_cpu = res[c].get("controller_cpu_cores")
        ctrl_mem = res[c].get("controller_mem_bytes")
        ml_cpu   = res[c].get("ml_service_cpu_cores")
        ml_mem   = res[c].get("ml_service_mem_bytes")
        L.append(
            f"| {c} | "
            f"{_fmt_cores(_get(ctrl_cpu, 'mean'))} | "
            f"{_fmt_cores(_get(ctrl_cpu, 'p95'))} | "
            f"{_fmt_mb(_get(ctrl_mem, 'p95'))} | "
            f"{_fmt_cores(_get(ml_cpu, 'mean'))} | "
            f"{_fmt_cores(_get(ml_cpu, 'p95'))} | "
            f"{_fmt_mb(_get(ml_mem, 'p95'))} |"
        )
    L.append("")

    L.append("## 2. Tracee data plane — throughput & integrity")
    L.append("")
    L.append("| Condition | events/s mean | events/s p95 | lost events/s mean | drop rate |")
    L.append("|---|---:|---:|---:|---:|")
    for c in conditions:
        ev   = res[c].get("tracee_events_per_sec")
        lost = res[c].get("tracee_lost_events_per_sec")
        ev_mean = _get(ev, "mean")
        lost_mean = _get(lost, "mean")
        drop = None
        if ev_mean and lost_mean is not None and ev_mean > 0:
            drop = lost_mean / (ev_mean + lost_mean)
        L.append(
            f"| {c} | "
            f"{_fmt_int(ev_mean)} | "
            f"{_fmt_int(_get(ev, 'p95'))} | "
            f"{_fmt_int(lost_mean)} | "
            f"{_fmt_pct(drop)} |"
        )
    L.append("")

    L.append("## 3. Tracee egress (Arch-B counterfactual input)")
    L.append("")
    L.append("| Condition | egress MB/s mean | egress MB/s p95 | egress MB/s max | n samples |")
    L.append("|---|---:|---:|---:|---:|")
    for c in conditions:
        eg = res[c].get("tracee_egress_bytes_per_sec")
        L.append(
            f"| {c} | "
            f"{_fmt_mbs(_get(eg, 'mean'))} | "
            f"{_fmt_mbs(_get(eg, 'p95'))} | "
            f"{_fmt_mbs(_get(eg, 'max'))} | "
            f"{_get(eg, 'n') if eg else 'n/a'} |"
        )
    L.append("")

    L.append("## 4. Controller latency `T_ctrl` (reconcile loop)")
    L.append("")
    L.append("Internal Reconcile() wall-time, p95/p99 of per-scrape-window "
             "histogram quantile.  SLO: p95 ≤ 200 ms.")
    L.append("")
    L.append("| Condition | tctrl p95 mean (ms) | tctrl p95 p95 (ms) | tctrl p99 mean (ms) | n samples |")
    L.append("|---|---:|---:|---:|---:|")
    for c in conditions:
        p95 = res[c].get("tctrl_p95_seconds")
        p99 = res[c].get("tctrl_p99_seconds")
        L.append(
            f"| {c} | "
            f"{_fmt_ms_from_s(_get(p95, 'mean'))} | "
            f"{_fmt_ms_from_s(_get(p95, 'p95'))} | "
            f"{_fmt_ms_from_s(_get(p99, 'mean'))} | "
            f"{_get(p95, 'n') if p95 else 'n/a'} |"
        )
    L.append("")

    L.append("## 5. End-to-end ready latency (includes K8s watch-lag)")
    L.append("")
    L.append("Histogram is capped at 30 s — `cap rate` is the share of scrape "
             "windows whose per-window p95 hit the cap (≥30 s).  "
             "`p50/p95 < cap` are computed on the sub-cap remainder.")
    L.append("")
    L.append("| Condition | e2e p95 mean (ms) | cap rate | p50 < cap (ms) | p95 < cap (ms) | n samples |")
    L.append("|---|---:|---:|---:|---:|---:|")
    for c in conditions:
        e2e = res[c].get("tctrl_e2e_p95_seconds")
        cap = e2e_caps.get(c)
        L.append(
            f"| {c} | "
            f"{_fmt_ms_from_s(_get(e2e, 'mean'))} | "
            f"{_fmt_pct(_get(cap, 'cap_rate'))} | "
            f"{_fmt_ms_from_s(_get(cap, 'p50_below_cap'))} | "
            f"{_fmt_ms_from_s(_get(cap, 'p95_below_cap'))} | "
            f"{_get(cap, 'n') if cap else 'n/a'} |"
        )
    L.append("")

    L.append("## 6. ML-service scoring latency (`response_ms`)")
    L.append("")
    L.append("Pooled across `*_score_telemetry.csv` rows with status == 'ok'.")
    L.append("")
    L.append("| Condition | n_ok | n_timeout | n_error | mean (ms) | p50 | p95 | p99 | max |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for c in conditions:
        s = score.get(c)
        if s is None:
            L.append(f"| {c} | — | — | — | — | — | — | — | — |")
            continue
        L.append(
            f"| {c} | {s['n_ok']:,} | {s['n_timeout']:,} | {s['n_error']:,} | "
            f"{s['mean']:.1f} | {s['p50']:.1f} | {s['p95']:.1f} | "
            f"{s['p99']:.1f} | {s['max']:.1f} |"
        )
    L.append("")

    L.append("## 7. Cost projection (USD/month, mean ± range across trials)")
    L.append("")
    L.append("Arch A = compute footprint of own controller + ml-service on "
             "GCP e2-standard-4 list price.  Arch B = counterfactual SIEM "
             "ingest of the same Tracee egress at vendor list prices.")
    L.append("")
    L.append("| Condition | Arch A total | ctrl | ml-service | Egress GB/mo | Datadog | Elastic | Splunk |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for c in conditions:
        cc = cost.get(c, {})

        def _row(key: str, fmt) -> str:
            d = cc.get(key)
            if d is None:
                return "n/a"
            return f"{fmt(d['mean'])} [{fmt(d['min'])}–{fmt(d['max'])}]"

        L.append(
            f"| {c} | "
            f"{_row('arch_a_total',        _fmt_usd)} | "
            f"{_row('arch_a_controller',   _fmt_usd)} | "
            f"{_row('arch_a_ml_service',   _fmt_usd)} | "
            f"{_row('egress_gb_per_month', lambda x: f'{x:,.0f}')} | "
            f"{_row('datadog',             _fmt_usd)} | "
            f"{_row('elastic',             _fmt_usd)} | "
            f"{_row('splunk',              _fmt_usd)} |"
        )
    L.append("")

    return "\n".join(L)


def write_per_trial_csv(campaign_root: Path,
                        trials: list[int],
                        conditions: list[str],
                        path: Path) -> None:
    """Per-(trial,condition,metric) flat CSV of recomputed summary stats."""
    rows: list[dict] = []
    for tid in trials:
        for c in conditions:
            jpath = (campaign_root / f"trial_{tid:02d}" /
                     f"{c}_resource.json")
            if not jpath.exists():
                continue
            try:
                blob = json.loads(jpath.read_text())
            except Exception as exc:
                _warn_unreadable(jpath, exc)
                continue
            series = blob.get("series", {})
            for metric, entry in series.items():
                rows.append({
                    "trial_id":         tid,
                    "condition":        c,
                    "metric":           metric,
                    "unit":             entry.get("unit"),
                    "samples":          entry.get("samples"),
                    "samples_non_nan":  entry.get("samples_non_nan"),
                    "mean_":            entry.get("mean_"),
                    "p50":              entry.get("p50"),
                    "p95":              entry.get("p95"),
                    "p99":              entry.get("p99"),
                    "max_":             entry.get("max_"),
                    "sum_":             entry.get("sum_"),
                })
            sc_path = (campaign_root / f"trial_{tid:02d}" /
                       f"{c}_score_telemetry.csv")
            if sc_path.exists():
                try:
                    sdf = pd.read_csv(sc_path)
                except Exception as exc:
                    _warn_unreadable(sc_path, exc)
                    sdf = None
                if sdf is not None and {"status", "response_ms"} <= set(sdf.columns):
                    ok = sdf.loc[sdf["status"] == "ok", "response_ms"].dropna().to_numpy()
                    if ok.size > 0:
                        rows.append({
                            "trial_id":         tid,
                            "condition":        c,
                            "metric":           "score_response_ms",
                            "unit":             "ms",
                            "samples":          int(len(sdf)),
                            "samples_non_nan":  int(ok.size),
                            "mean_":            float(ok.mean()),
                            "p50":              float(np.percentile(ok, 50)),
                            "p95":              float(np.percentile(ok, 95)),
                            "p99":              float(np.percentile(ok, 99)),
                            "max_":             float(ok.max()),
                            "sum_":             float(ok.sum()),
                        })
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--campaign-root", type=Path, required=True,
                   help="Campaign directory containing trial_NN/ subdirs.")
    p.add_argument("--output", type=Path, default=None,
                   help="Markdown output path.  Default: <campaign-root>/"
                        "resource_statistics.md (also printed to stdout).")
    p.add_argument("--per-trial-csv", type=Path, default=None,
                   help="Per-trial flat CSV path.  Default: <campaign-root>/"
                        "resource_per_trial_metrics.csv.")
    p.add_argument("--no-fix", action="store_true",
                   help="Skip the in-place NaN-summary fix.  By default the "
                        "script rewrites every {COND}_resource.json with "
                        "summaries recomputed from non-NaN CSV rows.")
    p.add_argument("--conditions", type=str, default=None,
                   help="Comma-separated condition names (default: "
                        "auto-discovered from trial_01/*_raw_data.csv).")
    p.add_argument("--trials", type=str, default=None,
                   help="Comma-separated trial ids (default: auto-discovered).")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    root = args.campaign_root.resolve()
    if not root.exists():
        sys.exit(f"ERROR: campaign root not found: {root}")

    conditions = (
        [c.strip() for c in args.conditions.split(",") if c.strip()]
        if args.conditions else _discover_conditions(root)
    )
    trials = (
        [int(t.strip()) for t in args.trials.split(",") if t.strip()]
        if args.trials else _discover_trials(root)
    )
    if not conditions:
        sys.exit(f"ERROR: no conditions discovered under {root}")
    if not trials:
        sys.exit(f"ERROR: no trial_NN/ directories discovered under {root}")

    print(f"[info] campaign:   {root}", file=sys.stderr)
    print(f"[info] trials:     {trials}", file=sys.stderr)
    print(f"[info] conditions: {conditions}", file=sys.stderr)

    fixes_total = 0
    if not args.no_fix:
        nan_repaired = 0
        for tid in trials:
            tdir = root / f"trial_{tid:02d}"
            for c in conditions:
                diffs = fix_resource_json(tdir, c)
                if diffs:
                    fixes_total += 1
                    for metric, info in diffs.items():
                        b_mean = info["before"].get("mean_")
                        a_mean = info["after"].get("mean_")
                        if b_mean != b_mean and a_mean == a_mean:
                            nan_repaired += 1
        print(f"[info] resource.json files recomputed: {fixes_total}",
              file=sys.stderr)
        print(f"[info] NaN→value summary repairs:      {nan_repaired}",
              file=sys.stderr)
    else:
        print("[info] --no-fix: skipping in-place JSON recomputation",
              file=sys.stderr)

    res      = aggregate_resource(root, trials, conditions)
    score    = aggregate_score_telemetry(root, trials, conditions)
    cost     = aggregate_cost(root, trials, conditions)
    e2e_caps = compute_e2e_cap_rates(root, trials, conditions)

    md = render_markdown(root, trials, conditions, res, score, cost,
                         e2e_caps, fixes_total)
    print(md)

    out_path = args.output if args.output is not None \
        else root / "resource_statistics.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md)
    print(f"\n[wrote] {out_path}", file=sys.stderr)

    csv_path = args.per_trial_csv if args.per_trial_csv is not None \
        else root / "resource_per_trial_metrics.csv"
    write_per_trial_csv(root, trials, conditions, csv_path)
    print(f"[wrote] {csv_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
