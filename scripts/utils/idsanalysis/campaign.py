"""Campaign-directory discovery (trial ids + condition names)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence


def discover_trials(root: Path) -> list[int]:
    """Trial ids (ints) parsed from trial_NN/ subdirs, ascending by dir name."""
    out: list[int] = []
    for d in sorted(root.iterdir()):
        if d.is_dir() and d.name.startswith("trial_"):
            try:
                out.append(int(d.name.removeprefix("trial_")))
            except ValueError:
                pass
    return out


def discover_conditions(root: Path,
                        order: Optional[Sequence[str]] = None) -> list[str]:
    """Condition names from `*_raw_data.csv` across all trial dirs (sorted, or by `order`)."""
    found_set: set[str] = set()
    for d in sorted(root.iterdir()):
        if d.is_dir() and d.name.startswith("trial_"):
            found_set.update(p.name.removesuffix("_raw_data.csv")
                             for p in d.glob("*_raw_data.csv"))
    found = sorted(found_set)
    if order is None:
        return found
    ordered = [c for c in order if c in found]
    ordered += [c for c in found if c not in order]
    return ordered
