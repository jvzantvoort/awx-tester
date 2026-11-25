"""Read-only API performance checks. They add request load but change nothing."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from ..api import ApiError
from ..model import Better
from ..registry import check
from ..stats import summarize


def _slug(endpoint: str) -> str:
    return endpoint.split("?")[0].strip("/").replace("/", "_") or "root"


@check("perf.api_latency", "API read latency", requires=("api", "auth"), tags=("performance",))
def api_latency(ctx, out):
    """Sequential GETs of common list endpoints; p50/p95/max per endpoint and overall."""
    iterations = int(ctx.opt("api_latency", "iterations"))
    warmup = int(ctx.opt("api_latency", "warmup"))
    overall: list[float] = []
    errors = 0
    rows = []
    for endpoint in ctx.opt("api_latency", "endpoints"):
        samples: list[float] = []
        for i in range(warmup + iterations):
            try:
                timed = ctx.api.request("GET", endpoint)
            except ApiError as exc:
                if exc.status in (403, 404):
                    out.detail(f"{endpoint}: skipped ({exc.status})")
                    break
                errors += 1
                out.detail(f"{endpoint}: {exc}")
                continue
            if i >= warmup:
                samples.append(timed.elapsed * 1000)
        if not samples:
            continue
        s = summarize(samples)
        overall.extend(samples)
        rows.append({"endpoint": endpoint, **{k: round(v, 1) for k, v in s.items() if v is not None}})
        out.metric(f"{_slug(endpoint)}_p95_ms", s["p95"], "ms", Better.LOWER)
    out.attr("endpoints", rows)
    s = summarize(overall)
    out.metric("p50_ms", s["p50"], "ms", Better.LOWER)
    out.metric("p95_ms", s["p95"], "ms", Better.LOWER)
    out.metric("max_ms", s["max"], "ms", Better.LOWER)
    out.metric("requests", s["count"])
    out.metric("errors", errors, "", Better.LOWER)
    if errors:
        out.fail(f"{errors} requests failed")
    if s["p95"] is not None:
        out.threshold("p95 latency", round(s["p95"]), ctx.opt("api_latency", "warn_p95_ms"),
                      ctx.opt("api_latency", "fail_p95_ms"), "ms")


@check("perf.api_concurrency", "API throughput under concurrent reads", requires=("api", "auth"),
       tags=("performance", "load"))
def api_concurrency(ctx, out):
    """N workers issue GETs for a fixed time; requests per second, latency and error rate."""
    workers = int(ctx.opt("api_concurrency", "workers"))
    duration = float(ctx.opt("api_concurrency", "duration_s"))
    endpoints = list(ctx.opt("api_concurrency", "endpoints"))
    lock = threading.Lock()
    latencies: list[float] = []
    errors: list[str] = []
    stop_at = time.monotonic() + duration

    def worker(n: int) -> None:
        api = ctx.api.clone()
        i = n
        while time.monotonic() < stop_at:
            endpoint = endpoints[i % len(endpoints)]
            i += 1
            try:
                timed = api.request("GET", endpoint)
                with lock:
                    latencies.append(timed.elapsed * 1000)
            except ApiError as exc:
                with lock:
                    errors.append(f"{endpoint}: {exc.status}")

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(worker, range(workers)))
    elapsed = time.monotonic() - started

    total = len(latencies) + len(errors)
    s = summarize(latencies)
    error_rate = 100.0 * len(errors) / total if total else 0.0
    out.attr("workers", workers)
    out.attr("duration_s", duration)
    out.metric("throughput_rps", len(latencies) / elapsed if elapsed else 0, "req/s", Better.HIGHER)
    out.metric("p50_ms", s["p50"], "ms", Better.LOWER)
    out.metric("p95_ms", s["p95"], "ms", Better.LOWER)
    out.metric("requests", total)
    out.metric("error_rate_pct", error_rate, "%", Better.LOWER)
    for e in sorted(set(errors))[:10]:
        out.detail(e)
    if total == 0:
        out.fail("no requests completed")
    out.threshold("error rate", round(error_rate, 2), ctx.opt("api_concurrency", "warn_error_rate_pct"),
                  ctx.opt("api_concurrency", "fail_error_rate_pct"), "%")
