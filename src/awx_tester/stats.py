"""Small statistics helpers (no numpy dependency)."""

from __future__ import annotations

import math
from collections.abc import Sequence


def percentile(values: Sequence[float], pct: float) -> float | None:
    """Linear-interpolated percentile, ``pct`` in 0..100. None for an empty input."""
    if not values:
        return None
    data = sorted(values)
    if len(data) == 1:
        return float(data[0])
    rank = (pct / 100) * (len(data) - 1)
    lo, hi = math.floor(rank), math.ceil(rank)
    return data[lo] + (data[hi] - data[lo]) * (rank - lo)


def summarize(values: Sequence[float]) -> dict[str, float | None]:
    if not values:
        return {"count": 0, "min": None, "p50": None, "p95": None, "max": None, "mean": None}
    return {
        "count": len(values),
        "min": min(values),
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "max": max(values),
        "mean": sum(values) / len(values),
    }
