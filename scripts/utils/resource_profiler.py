#!/usr/bin/env python3
"""Per-trial Prometheus collector: CPU/memory, controller latency, Tracee throughput.

Queries Prometheus over the trial window and writes
<scenario>_resource.json (summary + cost projection) and one
<scenario>_resource_<series>.csv per series into the trial directory.

Usage:
    python3 scripts/utils/resource_profiler.py --prom-url http://localhost:9091 \\
        --trial-id 3 --scenario PHASECTRL_ROLLOUT --start <unix> --end <unix>
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path
from statistics import mean
from typing import Any


COST_PER_VCPU_MONTH_USD = 24.46
COST_PER_GB_RAM_MONTH_USD = 6.12
SIEM_PRICE_PER_GB_USD = {
    "datadog_cloud_siem": 0.30,
    "splunk_cloud_list":  4.93,
    "elastic_serverless_complete": 0.11,
}


class Prom:
    """Thin wrapper around the Prometheus HTTP API."""

    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def query_range(
        self, expr: str, start: int, end: int, step: str = "5s",
    ) -> list[dict[str, Any]]:
        """Returns the raw `result` array from /api/v1/query_range."""
        params = urllib.parse.urlencode(
            {"query": expr, "start": start, "end": end, "step": step}
        )
        url = f"{self.base_url}/api/v1/query_range?{params}"
        with urllib.request.urlopen(url, timeout=self.timeout) as resp:
            payload = json.load(resp)
        if payload.get("status") != "success":
            raise RuntimeError(
                f"Prometheus query failed: {payload.get('error', '<no error>')} "
                f"for expr={expr!r}"
            )
        return payload["data"]["result"]


@dataclass
class SeriesStats:
    """Summary statistics for one Prometheus series over a trial window."""
    metric: str
    unit: str
    samples: int = 0
    p50: float = 0.0
    p95: float = 0.0
    p99: float = 0.0
    max_: float = 0.0
    mean_: float = 0.0
    sum_: float = 0.0
    duration_s: int = 0
    labels: dict[str, str] = field(default_factory=dict)


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    k = (len(sorted_values) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    if lo == hi:
        return sorted_values[lo]
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)


def _summarize(
    raw: list[dict[str, Any]], metric: str, unit: str, duration_s: int,
) -> SeriesStats:
    """Collapses all returned series into one stats block."""
    if not raw:
        return SeriesStats(metric=metric, unit=unit, duration_s=duration_s)

    by_ts: dict[float, float] = {}
    label_union: dict[str, str] = {}
    for ser in raw:
        for k, v in ser.get("metric", {}).items():
            label_union.setdefault(k, v)
        for ts, v in ser.get("values", []):
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            by_ts[float(ts)] = by_ts.get(float(ts), 0.0) + fv

    if not by_ts:
        return SeriesStats(metric=metric, unit=unit, duration_s=duration_s,
                           labels=label_union)

    values = sorted(by_ts.values())
    return SeriesStats(
        metric=metric,
        unit=unit,
        samples=len(values),
        p50=_percentile(values, 0.50),
        p95=_percentile(values, 0.95),
        p99=_percentile(values, 0.99),
        max_=values[-1],
        mean_=mean(values),
        sum_=sum(values),
        duration_s=duration_s,
        labels=label_union,
    )


def _dump_csv(path: Path, raw: list[dict[str, Any]]) -> None:
    """Writes the raw range-query result so the analyst can re-plot later without re-running Prometheus."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "pod", "namespace", "container", "value"])
        for ser in raw:
            m = ser.get("metric", {})
            for ts, v in ser.get("values", []):
                w.writerow([ts, m.get("pod", ""), m.get("namespace", ""),
                            m.get("container", ""), v])


def build_queries(
    ml_pod_regex: str,
    controller_pod_regex: str,
    tracee_pod_regex: str,
    controller_ns: str,
    rate_window: str = "30s",
) -> dict[str, str]:
    return {
        "ml_service_cpu_cores": (
            f'sum by (pod) (rate(container_cpu_usage_seconds_total'
            f'{{pod=~"{ml_pod_regex}", container!="POD", container!=""}}'
            f'[{rate_window}]))'
        ),
        "ml_service_mem_bytes": (
            f'sum by (pod) (container_memory_working_set_bytes'
            f'{{pod=~"{ml_pod_regex}", container!="POD", container!=""}})'
        ),
        "controller_cpu_cores": (
            f'sum by (pod) (rate(container_cpu_usage_seconds_total'
            f'{{pod=~"{controller_pod_regex}", container!="POD", container!=""}}'
            f'[{rate_window}]))'
        ),
        "controller_mem_bytes": (
            f'sum by (pod) (container_memory_working_set_bytes'
            f'{{pod=~"{controller_pod_regex}", container!="POD", container!=""}})'
        ),
        "tctrl_p95_seconds": (
            f'histogram_quantile(0.95, sum by (le) '
            f'(rate(ids_controller_reconcile_latency_seconds_bucket[{rate_window}])))'
        ),
        "tctrl_p99_seconds": (
            f'histogram_quantile(0.99, sum by (le) '
            f'(rate(ids_controller_reconcile_latency_seconds_bucket[{rate_window}])))'
        ),
        "tctrl_e2e_p95_seconds": (
            f'histogram_quantile(0.95, sum by (le) '
            f'(rate(ids_controller_e2e_ready_latency_seconds_bucket'
            f'{{transition="ready"}}[{rate_window}])))'
        ),
        "tctrl_e2e_p99_seconds": (
            f'histogram_quantile(0.99, sum by (le) '
            f'(rate(ids_controller_e2e_ready_latency_seconds_bucket'
            f'{{transition="ready"}}[{rate_window}])))'
        ),
        "tracee_egress_bytes_per_sec": (
            f'sum by (pod) (rate(container_network_transmit_bytes_total'
            f'{{pod=~"{tracee_pod_regex}"}}[{rate_window}]))'
        ),
        "tracee_lost_events_per_sec": (
            f'sum (rate(tracee_ebpf_lostevents_total'
            f'{{pod=~"{tracee_pod_regex}"}}[{rate_window}]))'
        ),
        "tracee_events_per_sec": (
            f'sum (rate(tracee_ebpf_events_total'
            f'{{pod=~"{tracee_pod_regex}"}}[{rate_window}]))'
        ),
    }


def _project_costs(
    ml_cpu_p95: float, ml_mem_bytes_p95: float,
    ctl_cpu_p95: float, ctl_mem_bytes_p95: float,
    tracee_egress_total_bytes: float, duration_s: int,
) -> dict[str, Any]:
    """Turns p95 resource numbers into $/month estimates."""
    def _arch_a_cost_usd(cpu_cores: float, mem_bytes: float) -> float:
        return cpu_cores * COST_PER_VCPU_MONTH_USD + \
            (mem_bytes / (1024 ** 3)) * COST_PER_GB_RAM_MONTH_USD

    arch_a_ml_usd = _arch_a_cost_usd(ml_cpu_p95, ml_mem_bytes_p95)
    arch_a_ctl_usd = _arch_a_cost_usd(ctl_cpu_p95, ctl_mem_bytes_p95)
    arch_a_total_usd = arch_a_ml_usd + arch_a_ctl_usd

    gb_in_trial = tracee_egress_total_bytes / (1024 ** 3)
    gb_per_month = gb_in_trial * (30 * 86400 / max(duration_s, 1))
    arch_b = {
        siem: round(gb_per_month * unit_price, 4)
        for siem, unit_price in SIEM_PRICE_PER_GB_USD.items()
    }

    return {
        "duration_s": duration_s,
        "arch_a_monthly_usd_list_price": {
            "ml_service": round(arch_a_ml_usd, 4),
            "controller": round(arch_a_ctl_usd, 4),
            "total": round(arch_a_total_usd, 4),
        },
        "arch_b_counterfactual_monthly_usd_list_price": {
            "gross_egress_gb_per_month_estimate": round(gb_per_month, 4),
            **arch_b,
        },
        "pricing_notes": {
            "vcpu_month_usd": COST_PER_VCPU_MONTH_USD,
            "gb_ram_month_usd": COST_PER_GB_RAM_MONTH_USD,
            "siem_per_gb_usd": SIEM_PRICE_PER_GB_USD,
            "source": "GCP e2-standard-4 us-central1 list, May 2026",
        },
    }


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--prom-url", required=True,
                   help="Base URL of Prometheus (e.g. http://localhost:9091)")
    p.add_argument("--trial-id", required=True, type=int)
    p.add_argument("--scenario", required=True,
                   help="One of the 12 Phase-2 conditions, e.g. H2_ROLLOUT_CTL")
    p.add_argument("--start", required=True, type=int,
                   help="Unix epoch (seconds) — trial start")
    p.add_argument("--end", required=True, type=int,
                   help="Unix epoch (seconds) — trial end")

    p.add_argument("--ml-pod-regex", default=os.environ.get(
        "ML_POD_REGEX", "ml-service.*"))
    p.add_argument("--controller-pod-regex", default=os.environ.get(
        "CONTROLLER_POD_REGEX", "ids-controller.*"))
    p.add_argument("--tracee-pod-regex", default=os.environ.get(
        "TRACEE_POD_REGEX", "tracee.*"))
    p.add_argument("--controller-ns", default=os.environ.get(
        "CONTROLLER_NS", "ids-system"))

    p.add_argument("--results-root", default=os.environ.get(
        "RESULTS_ROOT",
        str(Path(__file__).resolve().parents[2] / "experiments" / "results" / "trials"),
    ))
    p.add_argument("--step", default="5s",
                   help="Prometheus query_range step (default 5s)")
    p.add_argument("--rate-window", default="30s",
                   help="Window for rate() (default 30s — 6× step)")
    args = p.parse_args(argv)

    if args.end <= args.start:
        print(f"error: --end ({args.end}) must be > --start ({args.start})",
              file=sys.stderr)
        return 2
    duration_s = args.end - args.start

    trial_dir = Path(args.results_root) / f"trial_{args.trial_id:02d}"
    trial_dir.mkdir(parents=True, exist_ok=True)

    prom = Prom(args.prom_url)
    queries = build_queries(
        ml_pod_regex=args.ml_pod_regex,
        controller_pod_regex=args.controller_pod_regex,
        tracee_pod_regex=args.tracee_pod_regex,
        controller_ns=args.controller_ns,
        rate_window=args.rate_window,
    )

    units = {
        "ml_service_cpu_cores": "cores",
        "ml_service_mem_bytes": "bytes",
        "controller_cpu_cores": "cores",
        "controller_mem_bytes": "bytes",
        "tctrl_p95_seconds": "seconds",
        "tctrl_p99_seconds": "seconds",
        "tctrl_e2e_p95_seconds": "seconds",
        "tctrl_e2e_p99_seconds": "seconds",
        "tracee_egress_bytes_per_sec": "bytes_per_sec",
        "tracee_lost_events_per_sec": "events_per_sec",
        "tracee_events_per_sec": "events_per_sec",
    }
    summary: dict[str, Any] = {
        "trial_id": args.trial_id,
        "scenario": args.scenario,
        "start": args.start,
        "end": args.end,
        "duration_s": duration_s,
        "prom_url": args.prom_url,
        "collected_at": int(time.time()),
        "series": {},
    }

    for name, expr in queries.items():
        try:
            raw = prom.query_range(expr, args.start, args.end, step=args.step)
        except Exception as exc:                            # noqa: BLE001
            print(f"[profiler] WARN: query {name!r} failed: {exc}",
                  file=sys.stderr)
            summary["series"][name] = {
                "error": str(exc), "promql": expr, "unit": units[name],
            }
            continue

        stats = _summarize(raw, metric=name, unit=units[name],
                           duration_s=duration_s)
        summary["series"][name] = {**asdict(stats), "promql": expr}

        csv_path = trial_dir / f"{args.scenario}_resource_{name}.csv"
        _dump_csv(csv_path, raw)

    def _stat(name: str, key: str) -> float:
        s = summary["series"].get(name, {})
        return float(s.get(key, 0.0)) if "error" not in s else 0.0

    egress_total = _stat("tracee_egress_bytes_per_sec", "mean_") * duration_s
    summary["cost_projection"] = _project_costs(
        ml_cpu_p95=_stat("ml_service_cpu_cores",  "p95"),
        ml_mem_bytes_p95=_stat("ml_service_mem_bytes", "p95"),
        ctl_cpu_p95=_stat("controller_cpu_cores", "p95"),
        ctl_mem_bytes_p95=_stat("controller_mem_bytes", "p95"),
        tracee_egress_total_bytes=egress_total,
        duration_s=duration_s,
    )
    summary["cost_projection"]["aggregation_notes"] = {
        "resource_sizing_statistic": "p95",
        "egress_integration_statistic": "mean",
        "egress_integration_formula":
            "total_bytes = mean(rate_bytes_per_sec) × duration_s; "
            "extrapolated to month via × (30*86400/duration_s).",
    }

    lost_per_sec_mean = _stat("tracee_lost_events_per_sec", "mean_")
    lost_samples = summary["series"].get("tracee_lost_events_per_sec", {}).get("samples", 0)
    direct_captured_per_sec_mean = _stat("tracee_events_per_sec", "mean_")
    direct_samples = summary["series"].get("tracee_events_per_sec", {}).get("samples", 0)
    if direct_samples > 0 and direct_captured_per_sec_mean > 0:
        captured_per_sec_mean = direct_captured_per_sec_mean
        captured_source = "tracee_ebpf_events_total (direct)"
    else:
        captured_per_sec_mean = (
            _stat("tracee_egress_bytes_per_sec", "mean_") / 200.0
            if _stat("tracee_egress_bytes_per_sec", "mean_") > 0 else 0.0
        )
        captured_source = "byte-rate proxy (~200 B/event)"
    drop_pct = (
        100.0 * lost_per_sec_mean / (lost_per_sec_mean + captured_per_sec_mean)
        if (lost_per_sec_mean + captured_per_sec_mean) > 0 else 0.0
    )
    summary["tracee_integrity"] = {
        "lost_events_per_sec_mean": lost_per_sec_mean,
        "captured_events_per_sec_mean": captured_per_sec_mean,
        "captured_events_source": captured_source,
        "drop_rate_pct_estimate": drop_pct,
        "instrumented": lost_samples > 0,
        "note": (
            "Metric series: tracee_ebpf_lostevents_total (no underscore "
            "between 'lost' and 'events' — exported by Tracee 0.24.x via "
            "--server=metrics).  Direct events_total denominator is "
            "preferred over the byte-rate proxy when available."
        ),
    }

    json_path = trial_dir / f"{args.scenario}_resource.json"
    json_path.write_text(json.dumps(summary, indent=2, sort_keys=True))
    print(f"[profiler] wrote {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
