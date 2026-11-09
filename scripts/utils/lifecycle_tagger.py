#!/usr/bin/env python3
"""Enrich Tracee events with pod identity and Kubernetes lifecycle phase.

Reads Tracee JSON lines from stdin and adds pod_uid, pod_name, app_label and
lifecycle_phase (running_warmup | running_ready | terminating | unknown),
using a background Pod watch as the source of truth. Writes one event per
line to stdout.

Usage:
    kubectl logs -n tracee ds/tracee -f \\
        | python3 scripts/utils/lifecycle_tagger.py \\
        | python3 scripts/experiments/run_h2.py --trial-id 1
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple


logger = logging.getLogger("lifecycle_tagger")
logger.setLevel(os.environ.get("LIFECYCLE_TAGGER_LOG_LEVEL", "INFO"))
_handler = logging.StreamHandler(sys.stderr)
_handler.setFormatter(logging.Formatter("[lifecycle_tagger] %(levelname)s %(message)s"))
logger.addHandler(_handler)


@dataclass
class PodInfo:
    uid: str
    namespace: str
    name: str
    phase: str
    ready: bool
    deletion_ts: Optional[str]
    container_ids: Tuple[str, ...] = ()
    app_label: str = ""


def _derive_lifecycle_phase(p: PodInfo) -> str:
    # Same rule as the controller: Pending, or Running but not every container
    # Ready, is warmup.
    if p.deletion_ts is not None:
        return "terminating"
    if p.phase == "Running" and p.ready:
        return "running_ready"
    if p.phase in ("Pending", "Running"):
        return "running_warmup"
    return "unknown"


class PodCache:
    """Thread-safe in-memory pod cache."""

    def __init__(self) -> None:
        self._by_uid: Dict[str, PodInfo] = {}
        self._by_ns_name: Dict[Tuple[str, str], str] = {}
        self._by_name: Dict[str, str] = {}
        self._by_container_id: Dict[str, str] = {}
        self._lock = threading.RLock()
        self.initial_sync_done = threading.Event()

    def upsert(self, p: PodInfo) -> None:
        with self._lock:
            old = self._by_uid.get(p.uid)
            if old is not None:
                for cid in old.container_ids:
                    if self._by_container_id.get(cid) == p.uid:
                        self._by_container_id.pop(cid, None)
            self._by_uid[p.uid] = p
            self._by_ns_name[(p.namespace, p.name)] = p.uid
            self._by_name[p.name] = p.uid
            for cid in p.container_ids:
                self._by_container_id[cid] = p.uid

    def delete(self, uid: str) -> None:
        with self._lock:
            p = self._by_uid.pop(uid, None)
            if p is None:
                return
            if self._by_ns_name.get((p.namespace, p.name)) == uid:
                self._by_ns_name.pop((p.namespace, p.name), None)
            if self._by_name.get(p.name) == uid:
                self._by_name.pop(p.name, None)
            for cid in p.container_ids:
                if self._by_container_id.get(cid) == uid:
                    self._by_container_id.pop(cid, None)

    def get_by_uid(self, uid: str) -> Optional[PodInfo]:
        with self._lock:
            return self._by_uid.get(uid)

    def get_by_ns_name(self, namespace: str, name: str) -> Optional[PodInfo]:
        with self._lock:
            uid = self._by_ns_name.get((namespace, name))
            return self._by_uid.get(uid) if uid else None

    def get_by_name(self, name: str) -> Optional[PodInfo]:
        with self._lock:
            uid = self._by_name.get(name)
            return self._by_uid.get(uid) if uid else None

    def get_by_container_id(self, container_id: str) -> Optional[PodInfo]:
        """Look up a pod by container ID (available before Tracee's own K8s enrichment)."""
        with self._lock:
            uid = self._by_container_id.get(container_id)
            return self._by_uid.get(uid) if uid else None


def _container_statuses_ready(status_obj) -> bool:
    """Returns True iff every container reports ready=True."""
    statuses = getattr(status_obj, "container_statuses", None) or []
    if not statuses:
        return False
    return all(bool(getattr(cs, "ready", False)) for cs in statuses)


_CRI_PREFIX_RE = re.compile(r"^[a-z0-9]+://")


def _normalize_container_id(cid: str) -> str:
    """K8s reports containerID as `containerd://<hex>` or `docker://<hex>`."""
    if not cid:
        return ""
    return _CRI_PREFIX_RE.sub("", cid).strip()


def _extract_container_ids(status) -> Tuple[str, ...]:
    """Pull all containerIDs from a pod status (init + main containers)."""
    if status is None:
        return ()
    out: list[str] = []
    for attr in ("container_statuses", "init_container_statuses",
                 "ephemeral_container_statuses"):
        statuses = getattr(status, attr, None) or []
        for cs in statuses:
            cid = _normalize_container_id(str(getattr(cs, "container_id", "") or ""))
            if cid:
                out.append(cid)
    return tuple(dict.fromkeys(out))


def _build_pod_info(pod) -> PodInfo:
    meta = pod.metadata
    status = pod.status
    raw_labels = getattr(meta, "labels", None) or {}
    app_label = str(raw_labels.get("app", "")).strip().lower()
    return PodInfo(
        uid=str(meta.uid),
        namespace=str(meta.namespace),
        name=str(meta.name),
        phase=str(status.phase) if status and status.phase else "Unknown",
        ready=_container_statuses_ready(status) if status else False,
        deletion_ts=str(meta.deletion_timestamp) if meta.deletion_timestamp else None,
        container_ids=_extract_container_ids(status),
        app_label=app_label,
    )


def _run_watcher(cache: PodCache, namespace: Optional[str]) -> None:
    """Long-lived informer-style loop on Pods."""
    try:
        from kubernetes import client, config, watch as k8s_watch
    except ImportError:
        logger.error(
            "kubernetes python client not installed — pip install kubernetes. "
            "Falling back to pass-through mode (all events tagged 'unknown').",
        )
        cache.initial_sync_done.set()
        return

    try:
        config.load_incluster_config()
        logger.info("loaded in-cluster kubeconfig")
    except Exception:
        try:
            config.load_kube_config()
            logger.info("loaded ~/.kube/config")
        except Exception as exc:
            logger.error("could not load any kubeconfig: %s", exc)
            cache.initial_sync_done.set()
            return

    v1 = client.CoreV1Api()

    while True:
        try:
            list_kwargs = {"watch": False}
            if namespace:
                pod_list = v1.list_namespaced_pod(namespace=namespace, **list_kwargs)
            else:
                pod_list = v1.list_pod_for_all_namespaces(**list_kwargs)
            for pod in pod_list.items:
                cache.upsert(_build_pod_info(pod))
            resource_version = pod_list.metadata.resource_version
            cache.initial_sync_done.set()
            logger.info(
                "initial pod LIST complete  count=%d  rv=%s",
                len(pod_list.items), resource_version,
            )

            w = k8s_watch.Watch()
            stream_kwargs = {
                "resource_version": resource_version,
                "timeout_seconds": 300,
            }
            if namespace:
                stream = w.stream(v1.list_namespaced_pod, namespace, **stream_kwargs)
            else:
                stream = w.stream(v1.list_pod_for_all_namespaces, **stream_kwargs)

            for event in stream:
                etype = event.get("type")
                pod = event.get("object")
                if pod is None or pod.metadata is None or pod.metadata.uid is None:
                    continue
                if etype in ("ADDED", "MODIFIED"):
                    cache.upsert(_build_pod_info(pod))
                elif etype == "DELETED":
                    cache.delete(str(pod.metadata.uid))
        except Exception as exc:
            logger.warning("watcher error (will resync): %s", exc)
            time.sleep(2.0)


def _resolve_pod(event: dict, cache: PodCache) -> Optional[PodInfo]:
    """Resolve the event's pod: Tracee pod UID, then (namespace, name), then container ID."""
    k8s = event.get("kubernetes") or {}
    pod_uid = k8s.get("podUID") or k8s.get("podUid") or k8s.get("pod_uid")
    if pod_uid:
        info = cache.get_by_uid(str(pod_uid))
        if info is not None:
            return info
        # The UID is known but the watch has not delivered the pod yet:
        # keep the UID, phase stays unknown.
        ns = str(k8s.get("podNamespace") or "")
        name = str(k8s.get("podName") or "")
        return PodInfo(uid=str(pod_uid), namespace=ns, name=name,
                       phase="Unknown", ready=False, deletion_ts=None,
                       app_label="")

    ns = k8s.get("podNamespace") or ""
    name = k8s.get("podName") or ""
    if ns and name:
        info = cache.get_by_ns_name(str(ns), str(name))
        if info is not None:
            return info

    # Tracee fills `kubernetes.*` asynchronously, so a fresh container's first
    # events may have no pod fields; its container ID is set from the cgroup
    # immediately and the K8s API reports it as soon as the container starts.
    cid = (event.get("containerId")
           or (event.get("container") or {}).get("id")
           or "")
    if cid:
        info = cache.get_by_container_id(_normalize_container_id(str(cid)))
        if info is not None:
            return info

    if os.environ.get("IDS_ENABLE_HOSTNAME_FALLBACK") == "1":
        host = event.get("hostName") or ""
        if host:
            info = cache.get_by_name(str(host))
            if info is not None:
                return info

    return None


def enrich(event: dict, cache: PodCache) -> dict:
    """Pure-function enrichment used by the main loop and unit tests."""
    info = _resolve_pod(event, cache)
    if info is None:
        event["pod_uid"] = ""
        event["pod_name"] = ""
        event["lifecycle_phase"] = "unknown"
        event["app_label"] = ""
    else:
        event["pod_uid"] = info.uid
        event["pod_name"] = info.name
        event["lifecycle_phase"] = _derive_lifecycle_phase(info)
        event["app_label"] = info.app_label
    return event


def _parse_args() -> Tuple[Optional[str], float]:
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--namespace", default=None,
                   help="Limit watch to this namespace (default: all namespaces).")
    p.add_argument("--initial-sync-timeout", type=float, default=10.0,
                   help="Seconds to wait for the initial pod LIST before "
                        "starting to consume stdin (default: 10).")
    args = p.parse_args()
    return args.namespace, args.initial_sync_timeout


def main() -> None:
    namespace, initial_sync_timeout = _parse_args()

    cache = PodCache()
    t = threading.Thread(target=_run_watcher, args=(cache, namespace),
                         daemon=True, name="lifecycle-tagger-watcher")
    t.start()

    if not cache.initial_sync_done.wait(timeout=initial_sync_timeout):
        logger.warning(
            "initial pod sync did not complete within %.1fs — proceeding "
            "anyway; early events may be tagged 'unknown'.",
            initial_sync_timeout,
        )

    n_total = 0
    n_unknown = 0
    n_pod_events = 0
    n_pod_resolved = 0
    n_resolved_no_label = 0

    try:
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                sys.stdout.write(raw + "\n")
                sys.stdout.flush()
                continue

            # Resolution rate is measured only over events Tracee attributes
            # to a pod; host processes legitimately have no pod.
            tracee_pod_name = ""
            k8s_block = event.get("kubernetes") or {}
            if isinstance(k8s_block, dict):
                tracee_pod_name = str(k8s_block.get("podName") or "")
            event = enrich(event, cache)
            n_total += 1
            if event["lifecycle_phase"] == "unknown":
                n_unknown += 1
            if tracee_pod_name:
                n_pod_events += 1
                if event.get("pod_uid") and event["lifecycle_phase"] != "unknown":
                    n_pod_resolved += 1
                    if not event.get("app_label"):
                        n_resolved_no_label += 1

            sys.stdout.write(json.dumps(event, separators=(",", ":")) + "\n")
            sys.stdout.flush()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    finally:
        if n_total > 0:
            unknown_pct = 100.0 * n_unknown / n_total
            level = logger.warning if unknown_pct > 5.0 else logger.info
            level(
                "stream summary: events=%d  unknown=%d (%.1f%%)",
                n_total, n_unknown, unknown_pct,
            )
            if n_pod_events > 0:
                resolved_pct = 100.0 * n_pod_resolved / n_pod_events
                pod_unresolved = n_pod_events - n_pod_resolved
                pod_level = (logger.warning if resolved_pct < 90.0
                             else logger.info)
                pod_level(
                    "pod-event resolution: pod_events=%d  resolved=%d "
                    "(%.1f%%)  unresolved=%d",
                    n_pod_events, n_pod_resolved, resolved_pct,
                    pod_unresolved,
                )
                if n_pod_resolved > 0:
                    no_label_pct = 100.0 * n_resolved_no_label / n_pod_resolved
                    nl_level = (logger.warning if no_label_pct > 5.0
                                else logger.info)
                    nl_level(
                        "resolved-no-label: %d / %d  (%.1f%%) — rows that "
                        "resolved to a pod with empty `app` label; these "
                        "route to the registry fallback model.",
                        n_resolved_no_label, n_pod_resolved, no_label_pct,
                    )
            else:
                logger.warning(
                    "pod-event resolution: NO Tracee podName fields seen "
                    "in stream — enrichment pipeline broken upstream?"
                )


if __name__ == "__main__":
    main()
