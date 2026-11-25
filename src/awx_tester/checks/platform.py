"""Read-only functional checks of the controller itself."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..api import ApiError
from ..model import Better
from ..registry import Skip, check
from ..stats import summarize
from .util import gib, job_timings

HOP = "hop"


@check("platform.ping", "API answers ping", requires=("api",), tags=("smoke",))
def ping(ctx, out):
    """The unauthenticated ping endpoint responds and reports a version and instances."""
    timed = ctx.api.request("GET", "ping/", auth=False)
    data = timed.data or {}
    out.metric("latency_ms", timed.elapsed * 1000, "ms", Better.NONE, "single request; see perf.api_latency")
    out.attr("version", data.get("version"))
    out.attr("ha", data.get("ha"))
    out.attr("active_node", data.get("active_node"))
    instances = data.get("instances", [])
    out.metric("instances", len(instances), "", Better.NONE)
    if not data.get("version"):
        out.fail("ping did not report a version")
    for label, path in ctx.platform.extra_pings:
        try:
            extra = ctx.api.request("GET", path, auth=False)
            out.attr(f"{label}_version", (extra.data or {}).get("version"))
            out.metric(f"{label}_latency_ms", extra.elapsed * 1000, "ms", Better.NONE, "single request")
        except ApiError as exc:
            out.detail(f"{label} ping unavailable: {exc.status}")


@check("platform.auth", "Credentials are accepted", requires=("api", "auth"), tags=("smoke",))
def auth(ctx, out):
    """The configured user can log in; records its privileges."""
    me = ctx.env.get("platform", {}).get("me")
    if not me:
        raise Skip("no user information")
    out.attr("username", me.get("username"))
    out.attr("is_superuser", me.get("is_superuser"))
    out.attr("is_system_auditor", me.get("is_system_auditor"))
    if not (me.get("is_superuser") or me.get("is_system_auditor")):
        out.warn("user is neither superuser nor system auditor; instance and settings checks are limited")


@check("platform.instances", "Instances are healthy", requires=("api", "auth", "superuser"), tags=("smoke",))
def instances(ctx, out):
    """Every enabled instance is ready, has capacity and reports no health errors."""
    items = list(ctx.api.iter_pages("instances/"))
    if not items:
        out.fail("no instances registered")
        return
    versions = set()
    rows = []
    for i in items:
        versions.add(i.get("version"))
        rows.append({
            k: i.get(k) for k in ("hostname", "node_type", "node_state", "enabled", "capacity",
                                  "consumed_capacity", "cpu", "memory", "forks", "version",
                                  "last_health_check", "errors")
        })
        if not i.get("enabled"):
            out.detail(f"{i['hostname']} is disabled")
            continue
        if i.get("node_state") not in (None, "ready", "installed"):
            out.fail(f"{i['hostname']} is {i.get('node_state')}")
        if i.get("errors"):
            out.fail(f"{i['hostname']}: {i['errors']}")
        if i.get("node_type") != HOP and not i.get("capacity"):
            out.fail(f"{i['hostname']} has no capacity")
    out.attr("instances", rows)
    out.metric("instances", len(items))
    out.metric("capacity_total", sum(i.get("capacity") or 0 for i in items), "forks", Better.HIGHER)
    out.metric("cpu_total", sum(float(i.get("cpu") or 0) for i in items), "cores")
    out.metric("memory_total_gib", gib(sum(float(i.get("memory") or 0) for i in items)), "GiB")
    if len(versions) > 1:
        out.warn(f"instances run different versions: {sorted(str(v) for v in versions)}")


@check("platform.instance_groups", "Instance groups have capacity", requires=("api", "auth"))
def instance_groups(ctx, out):
    """Every non-container instance group with instances has free capacity."""
    groups = list(ctx.api.iter_pages("instance_groups/"))
    rows = []
    for g in groups:
        rows.append({k: g.get(k) for k in (
            "name", "is_container_group", "instances", "capacity", "consumed_capacity",
            "percent_capacity_remaining", "jobs_running", "jobs_total", "max_concurrent_jobs", "max_forks")})
        if not g.get("is_container_group") and g.get("instances") and not g.get("capacity"):
            out.fail(f"instance group {g['name']} has instances but no capacity")
        if g.get("name") in ("default", "controlplane"):
            out.metric(f"{g['name']}_capacity", g.get("capacity"), "forks", Better.HIGHER)
            out.metric(f"{g['name']}_remaining_pct", g.get("percent_capacity_remaining"), "%", Better.HIGHER)
    out.attr("instance_groups", rows)
    out.metric("groups", len(groups))
    if not groups:
        out.fail("no instance groups")


@check("platform.execution_environments", "Execution environments are defined", requires=("api", "auth"))
def execution_environments(ctx, out):
    """At least one execution environment exists; records their images."""
    try:
        items = list(ctx.api.iter_pages("execution_environments/"))
    except ApiError as exc:
        if exc.status == 404:
            raise Skip("this version has no execution environments") from exc
        raise
    out.attr("execution_environments", [
        {"name": e.get("name"), "image": e.get("image"), "pull": e.get("pull"), "managed": e.get("managed")}
        for e in items
    ])
    out.metric("execution_environments", len(items))
    if not items:
        out.fail("no execution environments")


@check("platform.job_history", "Recent job outcomes and queue times", requires=("api", "auth"))
def job_history(ctx, out):
    """Outcomes and queue times of jobs that finished in the last N days. Read-only."""
    days = ctx.opt("job_history", "days")
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    counts = {}
    for status in ("successful", "failed", "error", "canceled"):
        counts[status] = ctx.api.count("unified_jobs/", {"finished__gte": since, "status": status})
    total = sum(counts.values())
    out.attr("window_days", days)
    out.attr("counts", counts)
    out.metric("jobs_finished", total)
    if total == 0:
        out.info(f"no jobs finished in the last {days} days")
        return
    failure_rate = 100.0 * (counts["failed"] + counts["error"]) / total
    out.metric("failure_rate_pct", failure_rate, "%", Better.LOWER)
    out.metric("system_errors", counts["error"], "", Better.LOWER)
    if counts["error"]:
        out.warn(f"{counts['error']} jobs ended in 'error' (a platform problem, not a playbook failure)")
    out.threshold("failure rate", round(failure_rate, 1), ctx.opt("job_history", "warn_failure_rate_pct"),
                  None, "%")

    sample = ctx.opt("job_history", "sample")
    queue, run = [], []
    for job in ctx.api.iter_pages("jobs/", {"finished__gte": since, "order_by": "-finished"}, limit=sample):
        t = job_timings(job)
        if t["queue_s"] is not None:
            queue.append(t["queue_s"])
        if t["run_s"] is not None:
            run.append(t["run_s"])
    q, r = summarize(queue), summarize(run)
    out.metric("queue_p50_s", q["p50"], "s", Better.LOWER)
    out.metric("queue_p95_s", q["p95"], "s", Better.LOWER)
    out.metric("run_p50_s", r["p50"], "s", Better.NONE)
    if q["p95"] is not None:
        out.threshold("queue p95", round(q["p95"], 1), ctx.opt("job_history", "warn_queue_p95_s"), None, "s")


@check("platform.metrics", "Prometheus metrics endpoint", requires=("api", "auth", "superuser"))
def metrics(ctx, out):
    """The /metrics endpoint answers; records the unlabelled gauges it exposes."""
    try:
        text = ctx.api.get("metrics/", headers={"Accept": "text/plain"})
    except ApiError as exc:
        if exc.status in (403, 404):
            raise Skip(f"metrics endpoint not available ({exc.status})") from exc
        raise
    if not isinstance(text, str):
        out.fail("metrics endpoint did not return text")
        return
    gauges = {}
    for line in text.splitlines():
        if line.startswith("#") or "{" in line or " " not in line:
            continue
        name, value = line.split(" ", 1)
        try:
            gauges[name] = float(value.split()[0])
        except ValueError:
            continue
    out.attr("gauges", gauges)
    for name in ("awx_pending_jobs_total", "awx_running_jobs_total", "awx_database_connections_total"):
        if name in gauges:
            out.metric(name, gauges[name])
