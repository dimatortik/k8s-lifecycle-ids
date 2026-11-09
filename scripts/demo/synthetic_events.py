"""Emit ~20 s of synthetic, already-tagged Tracee events for the local demo.

A new cartservice pod is in warmup for the first 10 s (a few execve/openat/futex
events from several start-up processes), is quiet for 1 s and then serves steady
traffic; a frontend pod is steady throughout. Steady feature levels follow
the steady-state windows of the published campaign.
"""

import json
import sys
import time

DURATION_S = 19.8
WARMUP_S = 10.0
READY_S = 11.0  # 1 s gap keeps the two phases in separate 10 s windows
TICK_S = 0.1


def emit(pod_uid, pod_name, app, phase, syscall, process):
    sys.stdout.write(json.dumps({
        "eventName": syscall, "processName": process,
        "pod_uid": pod_uid, "pod_name": pod_name, "app_label": app, "lifecycle_phase": phase,
        "kubernetes": {"podName": pod_name, "podNamespace": "default"},
    }) + "\n")


def main() -> None:
    start = time.monotonic()
    tick = 0
    while time.monotonic() - start < DURATION_S:
        t = time.monotonic() - start
        # frontend: ~50 events/s, futex + openat, one process
        for i in range(5):
            emit("uid-frontend", "frontend-7d9f", "frontend", "running_ready",
                 "openat" if i == 0 and tick % 2 == 0 else "futex", "frontend")
        if t < WARMUP_S:
            # new cartservice pod starting: ~5 events/s, 3 syscalls, 4 processes
            if tick % 2 == 0:
                step = tick // 2
                emit("uid-cart-new", "cartservice-5c6b", "cartservice", "running_warmup",
                     ("execve", "openat", "futex")[step % 3], ("sh", "dotnet", "init", "cartservice")[step % 4])
        elif t >= READY_S:
            # same pod once Ready: ~20 events/s over 7 worker threads
            for i in range(2):
                emit("uid-cart-new", "cartservice-5c6b", "cartservice", "running_ready",
                     "futex", f"worker-{(tick * 2 + i) % 7}")
        sys.stdout.flush()
        tick += 1
        time.sleep(max(0.0, start + tick * TICK_S - time.monotonic()))


if __name__ == "__main__":
    main()
