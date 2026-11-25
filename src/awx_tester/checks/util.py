"""Helpers shared by checks."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def job_timings(job: dict[str, Any]) -> dict[str, float | None]:
    """Queue time (created -> started) and run time (started -> finished) in seconds."""
    created, started, finished = (parse_ts(job.get(k)) for k in ("created", "started", "finished"))
    queue = (started - created).total_seconds() if created and started else None
    run = (finished - started).total_seconds() if started and finished else job.get("elapsed")
    total = (finished - created).total_seconds() if created and finished else None
    return {"queue_s": queue, "run_s": run, "total_s": total}


def stdout_tail(api: Any, job: dict[str, Any], lines: int = 20) -> str:
    try:
        text = api.get(job["related"]["stdout"], params={"format": "txt"})
    except Exception:  # noqa: BLE001 - best effort diagnostics
        return ""
    if not isinstance(text, str):
        return ""
    return "\n".join(text.splitlines()[-lines:])


def gib(value: float | None) -> float | None:
    return None if value is None else round(value / 2**30, 2)
