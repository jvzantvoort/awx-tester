"""Mutating load checks: they launch many jobs to measure throughput."""

from __future__ import annotations

import time

from ..api import TERMINAL_JOB_STATES, ApiError
from ..model import Better, Level
from ..registry import check
from ..stats import summarize
from .util import job_timings

M = Level.MUTATING


def wait_all(ctx, urls: list[str], timeout: float) -> list[dict]:
    pending = dict.fromkeys(urls)
    deadline = time.monotonic() + timeout
    while any(v is None for v in pending.values()):
        if time.monotonic() > deadline:
            raise TimeoutError(f"{sum(v is None for v in pending.values())} jobs not finished after {timeout:.0f}s")
        for url, done in pending.items():
            if done is None:
                job = ctx.api.get(url)
                if job["status"] in TERMINAL_JOB_STATES:
                    pending[url] = job
        time.sleep(1)
    return list(pending.values())


@check("perf.job_throughput", "Concurrent job throughput", level=M, requires=("api", "auth"),
       depends=("func.workspace",), tags=("performance", "load"))
def job_throughput(ctx, out):
    """Launches N ad hoc commands at once; measures queue time, run time and jobs per minute."""
    ws = ctx.workspace()
    n = int(ctx.opt("job_throughput", "jobs"))
    start = time.monotonic()
    urls, launch_ms = [], []
    for _ in range(n):
        t0 = time.perf_counter()
        job = ws.launch_adhoc(ctx)
        launch_ms.append((time.perf_counter() - t0) * 1000)
        urls.append(job["url"])
    launched_s = time.monotonic() - start
    jobs = wait_all(ctx, urls, float(ctx.opt("job_throughput", "timeout_s")))
    wall = time.monotonic() - start
    queue = [t["queue_s"] for t in map(job_timings, jobs) if t["queue_s"] is not None]
    run = [t["run_s"] for t in map(job_timings, jobs) if t["run_s"] is not None]
    failed = [j for j in jobs if j["status"] != "successful"]
    q, r, lms = summarize(queue), summarize(run), summarize(launch_ms)
    out.attr("jobs", n)
    out.attr("timeline", [
        {"id": j["id"], "status": j["status"], "created": j.get("created"), "started": j.get("started"),
         "finished": j.get("finished"), "execution_node": j.get("execution_node") or None,
         **{k: (round(v, 2) if v is not None else None) for k, v in job_timings(j).items()}}
        for j in jobs
    ])
    out.metric("jobs_per_minute", 60 * n / wall if wall else 0, "jobs/min", Better.HIGHER)
    out.metric("wall_s", wall, "s", Better.LOWER)
    out.metric("launch_p95_ms", lms["p95"], "ms", Better.LOWER)
    out.metric("launch_total_s", launched_s, "s", Better.LOWER)
    out.metric("queue_p50_s", q["p50"], "s", Better.LOWER)
    out.metric("queue_p95_s", q["p95"], "s", Better.LOWER)
    out.metric("queue_max_s", q["max"], "s", Better.LOWER)
    out.metric("run_p50_s", r["p50"], "s", Better.LOWER)
    out.metric("run_p95_s", r["p95"], "s", Better.LOWER)
    out.metric("failed", len(failed), "", Better.LOWER)
    for j in failed[:5]:
        out.fail(f"job {j['id']} ended {j['status']}")
    if q["p95"] is not None:
        out.threshold("queue p95", round(q["p95"], 1), ctx.opt("jobs", "warn_queue_s"),
                      ctx.opt("jobs", "fail_queue_s"), "s")


@check("perf.event_throughput", "Job event processing throughput", level=M, requires=("api", "auth"),
       depends=("func.workspace",), tags=("performance", "load"))
def event_throughput(ctx, out):
    """One ad hoc command over N local hosts; measures how fast its events are stored."""
    ws = ctx.workspace()
    n = int(ctx.opt("event_throughput", "hosts"))
    inv = ctx.create("inventories/", {"name": f"{ctx.name_prefix}-events", "organization": ws.organization["id"]})
    hosts = [{"name": f"local-{i:04d}", "variables": ws.LOCAL_VARS} for i in range(n)]
    try:
        ctx.api.post("bulk/host_create/", {"inventory": inv["id"], "hosts": hosts})
    except ApiError as exc:
        if exc.status not in (404, 405):
            raise
        for h in hosts:
            ctx.api.post(f"inventories/{inv['id']}/hosts/", h)
    job = ws.launch_adhoc(ctx, inventory_id=inv["id"], forks=int(ctx.opt("event_throughput", "forks")))
    final = ctx.api.wait_job(job["url"], timeout=float(ctx.opt("event_throughput", "timeout_s")))
    if final["status"] != "successful":
        out.fail(f"ad hoc command {final['id']} ended {final['status']}")
    finished_at = time.monotonic()
    while not final.get("event_processing_finished", True) and time.monotonic() - finished_at < 600:
        time.sleep(0.5)
        final = ctx.api.get(job["url"])
    lag = time.monotonic() - finished_at
    events = ctx.api.count(final["related"]["events"])
    t = job_timings(final)
    window = (t["run_s"] or 0) + lag
    out.attr("hosts", n)
    out.metric("events", events)
    out.metric("events_per_s", events / window if window else 0, "events/s", Better.HIGHER)
    out.metric("run_s", t["run_s"], "s", Better.LOWER)
    out.metric("event_lag_s", lag, "s", Better.LOWER, "job finished to all events stored")
    if events < n:
        out.fail(f"only {events} events stored for {n} hosts")
