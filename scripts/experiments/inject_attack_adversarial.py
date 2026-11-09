#!/usr/bin/env python3
"""Adversarial attack injector: times the attack to the controller's public K8s Events.

With --signal events (default) it watches `kubectl get events -w` in the
protected namespace for ThresholdRaised events, waits until no newer event has
arrived for ROLLING_WINDOW_SECS, then applies the attacker manifest after
ATTACKER_REACTION_S. --signal scores polls /scores/recent instead (an
all-knowing attacker, kept as an upper bound).

Exit codes: 0 injected, 2 bad arguments or cluster unreachable,
3 no threshold event within --patience (caller falls back to naive timing).
"""

from __future__ import annotations

import argparse
import calendar
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] inject_attack_adversarial: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


ROLLING_WINDOW_SECS = 5.0
ATTACKER_REACTION_S = 1.0
POLL_SECS = 0.25
DEFAULT_PATIENCE_SECS = 30.0


def fetch_scores_recent(
    api_url: str, namespace: Optional[str], label: Optional[str],
    pod_uid: Optional[str], window_secs: float, timeout: float = 5.0,
) -> dict:
    """One call to GET /scores/recent."""
    params = {"window_seconds": window_secs}
    if namespace:
        params["namespace"] = namespace
    if label:
        params["label"] = label
    if pod_uid:
        params["pod_uid"] = pod_uid
    url = f"{api_url.rstrip('/')}/scores/recent?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.load(resp)


_CRI_ENRICHMENT_GRACE_S = 5.0


def inject_via_kubectl(manifest: Path, namespace: str) -> None:
    """`kubectl apply -f <manifest> -n <ns>`, then wait for Ready + grace."""
    cmd = ["kubectl", "apply", "-f", str(manifest), "-n", namespace]
    log.info("kubectl: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)

    pod = manifest.stem
    wait_cmd = ["kubectl", "wait", "--for=condition=Ready",
                f"pod/{pod}", "-n", namespace, "--timeout=90s"]
    log.info("kubectl: %s", " ".join(wait_cmd))
    rc = subprocess.run(wait_cmd).returncode
    if rc != 0:
        log.warning("kubectl wait returned rc=%d — proceeding with grace anyway", rc)
    log.info("post-Ready CRI-enrichment grace: %.1fs", _CRI_ENRICHMENT_GRACE_S)
    time.sleep(_CRI_ENRICHMENT_GRACE_S)


# Matches both ThresholdRaised and ThresholdCleared.
_THRESHOLD_REASON_RE = re.compile(r"threshold", re.IGNORECASE)


def wait_for_threshold_events_then_inject(
    namespace: str, manifest: Path, patience_secs: float,
) -> int:
    """Watch K8s Events for the protected namespace and inject when the controller's most recent ThresholdRaised event has been stable for one ROLLING_WINDOW_SECS."""
    log.info(
        "watching K8s Events  ns=%s  patience=%.1fs  reaction=%.1fs",
        namespace, patience_secs, ATTACKER_REACTION_S,
    )

    cmd = [
        "kubectl", "get", "events",
        "-n", namespace,
        "--watch-only",
        "--output=json",
    ]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    start = time.time()
    last_event_ts: Optional[float] = None
    seen_count = 0

    try:
        decoder = json.JSONDecoder()
        buf = ""
        assert proc.stdout is not None
        while True:
            now = time.time()
            if last_event_ts is None and (now - start) > patience_secs:
                log.warning(
                    "patience %.1fs elapsed without observing a "
                    "Threshold* event — abandoning adversarial timing",
                    patience_secs,
                )
                proc.terminate()
                return 3
            if last_event_ts is not None and (now - last_event_ts) >= ROLLING_WINDOW_SECS:
                log.info(
                    "threshold-raise events stable for %.1fs — committing  "
                    "last_event_ts=%.3f  seen=%d",
                    ROLLING_WINDOW_SECS, last_event_ts, seen_count,
                )
                break

            line = proc.stdout.readline()
            if not line:
                time.sleep(0.2)
                if proc.poll() is not None:
                    log.warning("kubectl events watcher exited; aborting")
                    return 2
                continue

            buf += line
            while True:
                buf = buf.lstrip()
                if not buf:
                    break
                try:
                    obj, idx = decoder.raw_decode(buf)
                    buf = buf[idx:]
                except json.JSONDecodeError:
                    break

                reason = (obj.get("reason") or "")
                if not _THRESHOLD_REASON_RE.search(reason):
                    continue
                ts_str = (obj.get("eventTime")
                          or obj.get("lastTimestamp")
                          or obj.get("firstTimestamp"))
                try:
                    ts = (calendar.timegm(time.strptime(ts_str[:19], "%Y-%m-%dT%H:%M:%S"))
                          if ts_str else time.time())
                except Exception:
                    ts = time.time()
                if last_event_ts is None or ts > last_event_ts:
                    log.info("threshold-event observed  reason=%s  ts=%.3f",
                             reason, ts)
                    last_event_ts = ts
                    seen_count += 1
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()

    log.info("attacker reaction sleep: %.2fs", ATTACKER_REACTION_S)
    time.sleep(ATTACKER_REACTION_S)
    inject_via_kubectl(manifest, namespace)
    log.info("attack injected at ts=%.3f", time.time())
    return 0


def wait_for_argmax_then_inject(
    api_url: str, namespace: str, label: Optional[str],
    pod_uid: Optional[str], manifest: Path, patience_secs: float,
) -> int:
    """Watch for the signal, wait, inject; returns the process exit code."""
    start = time.time()
    best_ts: Optional[float] = None
    best_score: Optional[float] = None
    last_better_ts = start

    log.info(
        "polling /scores/recent  ns=%s label=%s window=%.1fs reaction=%.1fs "
        "patience=%.1fs",
        namespace, label, ROLLING_WINDOW_SECS, ATTACKER_REACTION_S,
        patience_secs,
    )

    while True:
        now = time.time()
        if now - start > patience_secs and best_score is None:
            log.warning(
                "patience expired (%.1fs) with no observable benign score — "
                "abandoning adversarial timing", patience_secs,
            )
            return 3

        try:
            resp = fetch_scores_recent(
                api_url, namespace, label, pod_uid, ROLLING_WINDOW_SECS,
            )
        except Exception as exc:                              # noqa: BLE001
            log.warning("fetch failed: %s — retrying", exc)
            time.sleep(POLL_SECS)
            continue

        argmax = resp.get("argmax")
        if argmax is not None:
            s = float(argmax["score"])
            t = float(argmax["ts"])
            if best_score is None or s < best_score:
                log.info(
                    "new argmax  ts=%.3f score=%.4f pod=%s phase=%s",
                    t, s, argmax.get("pod_name", ""),
                    argmax.get("lifecycle_phase", ""),
                )
                best_score = s
                best_ts = t
                last_better_ts = now

        if best_score is not None and (now - last_better_ts) >= ROLLING_WINDOW_SECS:
            log.info(
                "argmax stable for %.1fs — committing  best_score=%.4f at ts=%.3f",
                ROLLING_WINDOW_SECS, best_score, best_ts,
            )
            break

        time.sleep(POLL_SECS)

    log.info("attacker reaction sleep: %.2fs", ATTACKER_REACTION_S)
    time.sleep(ATTACKER_REACTION_S)
    inject_via_kubectl(manifest, namespace)
    log.info("attack injected at ts=%.3f  Δ_to_argmax=%.3fs",
             time.time(), time.time() - (best_ts or time.time()))
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--class", dest="class_", required=True,
        choices=["exfil", "token", "rshell"],
        help="Attack class (informational; manifest path drives the actual injection)",
    )
    p.add_argument("--manifest", required=True, type=Path,
                   help="Path to the attacker K8s manifest")
    p.add_argument("--namespace", required=True,
                   help="Target namespace for the rollout being observed AND attacked")
    p.add_argument("--label", default=None,
                   help="Filter scores by label (e.g. 'app=frontend'); recommended")
    p.add_argument("--pod-uid", default=None,
                   help="Optional pod_uid filter (rarely used at injection time)")
    p.add_argument("--api-url", default=os.environ.get(
        "ML_SERVICE_URL", "http://localhost:8080"))
    p.add_argument("--patience-secs", type=float,
                   default=float(os.environ.get(
                       "ADV_PATIENCE_SECS", DEFAULT_PATIENCE_SECS)))
    p.add_argument("--signal", choices=["events", "scores"],
                   default=os.environ.get("ADV_SIGNAL_SOURCE", "events"),
                   help="What the attacker observes: K8s Events (default; "
                        "realistic public signal) or /scores/recent (ablation; "
                        "models a privileged in-cluster attacker).")
    args = p.parse_args(argv)

    if not args.manifest.exists():
        log.error("manifest not found: %s", args.manifest)
        return 2

    log.info(
        "adversarial-timing injection  class=%s manifest=%s ns=%s "
        "label=%s signal=%s",
        args.class_, args.manifest, args.namespace,
        args.label or "<any>", args.signal,
    )
    if args.signal == "events":
        return wait_for_threshold_events_then_inject(
            namespace=args.namespace, manifest=args.manifest,
            patience_secs=args.patience_secs,
        )
    return wait_for_argmax_then_inject(
        api_url=args.api_url, namespace=args.namespace,
        label=args.label, pod_uid=args.pod_uid,
        manifest=args.manifest, patience_secs=args.patience_secs,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
