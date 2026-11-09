"""Admission filter shared by run_h2.py and run_h3.py (which pods are protected)."""

from __future__ import annotations

import os
import subprocess
import sys

# Protected deployments carry this label; their pods are matched by the
# "<deployment>-" name prefix.
DEFAULT_SELECTOR = "ids-research.io/protected=true"
DEFAULT_NS       = "default"


def resolve_protected_prefixes() -> set[str]:
    """Returns the set of deployment names that match the protection selector."""
    override = os.environ.get("IDS_PROTECTED_PREFIXES", "").strip()
    if override:
        prefixes = {p.strip() for p in override.split(",") if p.strip()}
        print(f"[filter] using IDS_PROTECTED_PREFIXES override: {sorted(prefixes)}",
              file=sys.stderr)
        return prefixes

    selector = os.environ.get("IDS_PROTECTED_SELECTOR", DEFAULT_SELECTOR)
    raw_ns = os.environ.get("IDS_TARGET_NS", DEFAULT_NS).strip()
    if raw_ns == "*":
        namespaces = ["__ALL__"]
    else:
        namespaces = [n.strip() for n in raw_ns.split(",") if n.strip()]

    prefixes: set[str] = set()
    for ns in namespaces:
        try:
            base_cmd = ["kubectl", "get", "deployments", "-l", selector,
                        "-o", "jsonpath={range .items[*]}{.metadata.name}{\"\\n\"}{end}"]
            if ns == "__ALL__":
                cmd = base_cmd + ["--all-namespaces"]
            else:
                cmd = base_cmd + ["-n", ns]
            out = subprocess.check_output(cmd, text=True, timeout=10)
        except FileNotFoundError:
            allow_empty = os.environ.get("IDS_ALLOW_EMPTY_PREFIXES") == "1"
            msg = "kubectl not on PATH"
            if allow_empty:
                print(f"[filter] WARN: {msg}; admit-all enabled via "
                      f"IDS_ALLOW_EMPTY_PREFIXES=1", file=sys.stderr)
                return set()
            else:
                print(f"[filter] ERROR: {msg}; set IDS_ALLOW_EMPTY_PREFIXES=1 "
                      f"to override (NOT recommended for trials)", file=sys.stderr)
                sys.exit(2)
        except subprocess.TimeoutExpired:
            print(f"[filter] WARN: kubectl timed out (ns={ns}); "
                  "continuing with partial set", file=sys.stderr)
            continue
        except subprocess.CalledProcessError as exc:
            print(f"[filter] WARN: kubectl get deployments failed in ns={ns} "
                  f"(rc={exc.returncode}); continuing with partial set",
                  file=sys.stderr)
            continue
        prefixes.update(line.strip() for line in out.splitlines() if line.strip())

    allow_empty = os.environ.get("IDS_ALLOW_EMPTY_PREFIXES") == "1"

    if not prefixes:
        msg = (f"no deployments match selector '{selector}' in ns={raw_ns}")
        if allow_empty:
            print(f"[filter] WARN: {msg}; admit-all enabled via "
                  f"IDS_ALLOW_EMPTY_PREFIXES=1", file=sys.stderr)
            return set()
        else:
            print(f"[filter] ERROR: {msg}; set IDS_ALLOW_EMPTY_PREFIXES=1 "
                  f"to override (NOT recommended for trials)", file=sys.stderr)
            sys.exit(2)

    print(f"[filter] resolved {len(prefixes)} protected prefixes via "
          f"selector '{selector}' across ns=[{raw_ns}]: {sorted(prefixes)}",
          file=sys.stderr)
    return prefixes


def matches_protected(pod_name: str, prefixes: set[str]) -> bool:
    """True iff pod_name starts with `<prefix>-` for some prefix in the set, OR if the set is empty (admit-all fallback)."""
    if not prefixes:
        return True
    return any(pod_name.startswith(p + "-") for p in prefixes)
