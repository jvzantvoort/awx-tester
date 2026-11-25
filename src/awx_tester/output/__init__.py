"""Renderers. Each one takes a Report (or a Comparison) and nothing else."""

from __future__ import annotations


def fmt_value(value, unit: str = "") -> str:
    if value is None:
        return "–"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 1e12:
            text = str(int(value))
        elif abs(value) >= 100:
            text = f"{value:,.0f}"
        elif abs(value) >= 10:
            text = f"{value:.1f}"
        else:
            text = f"{value:.2f}"
    else:
        text = str(value)
    return f"{text} {unit}".strip() if unit and unit != "%" else f"{text}{unit}"


def fmt_delta(delta) -> str:
    if delta is None:
        return ""
    if delta == float("inf"):
        return "new"
    return f"{delta:+.1f}%"
