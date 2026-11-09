"""Markdown number formatters for the statistics reports."""

from __future__ import annotations

from typing import Optional


def fmt_metric(x: Optional[float], pct: bool = True) -> str:
    if x is None:
        return "n/a"
    return f"{x*100:.2f}%" if pct else f"{x:.4f}"


def fmt_p(p: float) -> str:
    if p != p:
        return "n/a"
    if p < 1e-4:
        return f"{p:.2e}"
    return f"{p:.4f}"


def fmt_rb(r: Optional[float]) -> str:
    if r is None or r != r:
        return "n/a"
    return f"{r:+.3f}"


def fmt_ci(low: Optional[float], high: Optional[float]) -> str:
    if low is None or high is None or low != low or high != high:
        return "n/a"
    return f"[{low*100:+.2f}%, {high*100:+.2f}%]"
