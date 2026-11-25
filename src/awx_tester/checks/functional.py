"""Mutating functional checks.

They build their own objects (named ``awx-tester-<run id>-...``), run real jobs
against ``localhost`` inside the execution environment, and delete everything
again when the run ends (unless ``--keep-objects``). They do add entries to the
job history and the activity stream.
"""

from __future__ import annotations

import contextlib
import time
from datetime import datetime, timedelta, timezone

from ..api import ApiError
from ..model import Better, Level
from ..registry import Skip, check
from ..stats import summarize
from .util import job_timings, parse_ts, stdout_tail

M = Level.MUTATING


def wait_and_judge(ctx, out, job, kind: str, timeout_key=("jobs", "timeout_s")) -> dict:
    """Wait for a unified job, record its timings and fail the check unless it succeeded."""
    seen_start = time.monotonic()
    final = ctx.api.wait_job(job["url"], timeout=ctx.opt(*timeout_key))
    observed = time.monotonic() - seen_start
    t = job_timings(final)
    out.metric(f"{kind}_queue_s", t["queue_s"], "s", Better.LOWER, "created to started")
    out.metric(f"{kind}_run_s", t["run_s"], "s", Better.LOWER, "started to finished")
    out.metric(f"{kind}_observed_s", observed, "s", Better.LOWER, "launch to terminal state, as polled")
    if final["status"] != "successful":
        out.fail(f"{kind} {final['id']} ended {final['status']}: {final.get('job_explanation') or ''}".strip())
        tail = stdout_tail(ctx.api, final)
        if tail:
            out.detail(tail)
    elif t["queue_s"] is not None:
        out.threshold(f"{kind} queue time", round(t["queue_s"], 1), ctx.opt("jobs", "warn_queue_s"),
                      ctx.opt("jobs", "fail_queue_s"), "s")
    return final


def wait_events(ctx, out, job: dict, kind: str) -> int:
    """Wait until the callback receiver has stored all events; record the lag and the count."""
    start = time.monotonic()
    deadline = start + 300
    current = job
    while not current.get("event_processing_finished", True) and time.monotonic() < deadline:
        time.sleep(0.5)
        current = ctx.api.get(job["url"])
    out.metric(f"{kind}_event_lag_s", time.monotonic() - start, "s", Better.LOWER,
               "job finished to all events stored")
    if not current.get("event_processing_finished", True):
        out.warn("event processing did not finish within 300s")
    related = current["related"]
    events = ctx.api.count(related.get("job_events") or related["events"])
    out.metric(f"{kind}_events", events)
    if events == 0:
        out.fail(f"{kind} {job['id']} stored no events")
    return events


@check("func.workspace", "Create organization, inventory and credential", level=M,
       requires=("api", "auth"), tags=("smoke",))
def workspace(ctx, out):
    """Creates the objects the other mutating checks run in; times the creation."""
    start = time.perf_counter()
    ws = ctx.workspace()
    out.metric("create_s", time.perf_counter() - start, "s", Better.LOWER)
    out.attr("organization", ws.organization["name"])
    out.attr("inventory", ws.inventory["name"])


@check("func.crud", "Create, read, update and delete hosts", level=M, requires=("api", "auth"),
       depends=("func.workspace",))
def crud(ctx, out):
    """Write-path latency: creates N hosts one by one, updates, lists and deletes them."""
    ws = ctx.workspace()
    n = int(ctx.opt("crud", "hosts"))
    inv = ctx.create("inventories/", {"name": f"{ctx.name_prefix}-crud", "organization": ws.organization["id"]})
    create, update, delete = [], [], []
    hosts = []
    for i in range(n):
        t = ctx.api.request("POST", f"inventories/{inv['id']}/hosts/",
                            json={"name": f"host-{i:04d}", "variables": "role: test"})
        create.append(t.elapsed * 1000)
        hosts.append(t.data)
    for h in hosts:
        update.append(ctx.api.request("PATCH", h["url"], json={"description": "updated"}).elapsed * 1000)
    listing = ctx.api.request("GET", f"inventories/{inv['id']}/hosts/", params={"page_size": 200})
    if listing.data.get("count") != n:
        out.fail(f"expected {n} hosts, listed {listing.data.get('count')}")
    for h in hosts:
        delete.append(ctx.api.request("DELETE", h["url"]).elapsed * 1000)
    for name, values in (("create", create), ("update", update), ("delete", delete)):
        s = summarize(values)
        out.metric(f"{name}_p50_ms", s["p50"], "ms", Better.LOWER)
        out.metric(f"{name}_p95_ms", s["p95"], "ms", Better.LOWER)
    out.metric("list_ms", listing.elapsed * 1000, "ms", Better.LOWER)
    out.attr("hosts", n)


@check("func.adhoc", "Run an ad hoc command", level=M, requires=("api", "auth"),
       depends=("func.workspace",), tags=("smoke",))
def adhoc(ctx, out):
    """Runs the ping module against localhost in the default execution environment."""
    ws = ctx.workspace()
    job = ws.launch_adhoc(ctx)
    final = wait_and_judge(ctx, out, job, "adhoc")
    if final["status"] == "successful":
        wait_events(ctx, out, final, "adhoc")
    out.attr("execution_node", final.get("execution_node"))
    out.attr("controller_node", final.get("controller_node"))


@check("func.project_sync", "Sync a project from source control", level=M, requires=("api", "auth"),
       depends=("func.workspace",))
def project_sync(ctx, out):
    """Creates a git project and waits for its update. Skipped when project.scm_url is empty."""
    url = ctx.opt("project", "scm_url")
    if not url:
        raise Skip("project.scm_url is empty")
    ws = ctx.workspace()
    project = ctx.create("projects/", {
        "name": f"{ctx.name_prefix}-project", "organization": ws.organization["id"],
        "scm_type": "git", "scm_url": url, "scm_branch": ctx.opt("project", "scm_branch") or "",
    })
    ctx.shared["project"] = project
    deadline = time.monotonic() + 60
    update = None
    while update is None and time.monotonic() < deadline:
        update = ctx.api.find_one(project["related"]["project_updates"], order_by="-id")
        if update is None:
            time.sleep(1)
    if update is None:
        out.fail("no project update started within 60s")
        return
    ctx.track(update["url"], f"project update {update['id']}")
    wait_and_judge(ctx, out, update, "sync", ("project", "timeout_s"))
    playbooks = ctx.api.get(project["related"]["playbooks"])
    wanted = ctx.opt("project", "playbook")
    out.attr("playbooks", playbooks)
    if wanted not in (playbooks or []):
        out.fail(f"playbook {wanted!r} not found in the project")


def _job_template(ctx):
    project = ctx.shared.get("project")
    if not project:
        raise Skip("no project (func.project_sync did not run or failed)")
    if "job_template" not in ctx.shared:
        ws = ctx.workspace()
        ctx.shared["job_template"] = ctx.create("job_templates/", {
            "name": f"{ctx.name_prefix}-jt", "inventory": ws.inventory["id"], "project": project["id"],
            "playbook": ctx.opt("project", "playbook"),
        })
    return ctx.shared["job_template"]


@check("func.job_template", "Launch a job template", level=M, requires=("api", "auth"),
       depends=("func.project_sync",), tags=("smoke",))
def job_template(ctx, out):
    """Launches a playbook job and checks that its events were stored."""
    jt = _job_template(ctx)
    launched = ctx.api.post(jt["related"]["launch"])
    job = ctx.api.get(launched["url"])
    ctx.track(job["url"], f"job {job['id']}")
    final = wait_and_judge(ctx, out, job, "job")
    if final["status"] == "successful":
        wait_events(ctx, out, final, "job")


@check("func.workflow", "Run a two-node workflow", level=M, requires=("api", "auth"),
       depends=("func.job_template",))
def workflow(ctx, out):
    """A workflow that runs the job template, then runs it again on success."""
    jt = _job_template(ctx)
    ws = ctx.workspace()
    wf = ctx.create("workflow_job_templates/", {
        "name": f"{ctx.name_prefix}-workflow", "organization": ws.organization["id"]})
    first = ctx.api.post(wf["related"]["workflow_nodes"], {"unified_job_template": jt["id"]})
    ctx.api.post(first["related"]["success_nodes"], {"unified_job_template": jt["id"]})
    launched = ctx.api.post(wf["related"]["launch"])
    wf_job = ctx.api.get(launched["url"])
    ctx.track(wf_job["url"], f"workflow job {wf_job['id']}")
    final = wait_and_judge(ctx, out, wf_job, "workflow")
    spawned = 0
    for node in ctx.api.iter_pages(final["related"]["workflow_nodes"]):
        job = (node.get("summary_fields") or {}).get("job")
        if job:
            spawned += 1
            ctx.track(f"jobs/{job['id']}/", f"job {job['id']}")
    out.metric("jobs_spawned", spawned)
    if final["status"] == "successful" and spawned != 2:
        out.fail(f"expected 2 spawned jobs, found {spawned}")


@check("func.schedule", "A schedule fires on time", level=M, requires=("api", "auth"),
       depends=("func.job_template",), tags=("slow",))
def schedule(ctx, out):
    """Schedules the job template a short time ahead and measures how late the scheduler starts it."""
    jt = _job_template(ctx)
    lead = int(ctx.opt("schedule", "lead_s"))
    when = (datetime.now(timezone.utc) + timedelta(seconds=lead)).replace(microsecond=0)
    rrule = f"DTSTART:{when.strftime('%Y%m%dT%H%M%SZ')} RRULE:FREQ=MINUTELY;INTERVAL=1;COUNT=1"
    sched = ctx.create(jt["related"]["schedules"], {"name": f"{ctx.name_prefix}-schedule", "rrule": rrule})
    out.attr("rrule", rrule)
    deadline = time.monotonic() + lead + int(ctx.opt("schedule", "timeout_s"))
    job = None
    while job is None and time.monotonic() < deadline:
        job = ctx.api.find_one("jobs/", schedule=sched["id"])
        if job is None:
            time.sleep(2)
    if job is None:
        out.fail("the schedule did not launch a job in time")
        return
    ctx.track(job["url"], f"job {job['id']}")
    delay = (parse_ts(job["created"]) - when).total_seconds()
    out.metric("schedule_delay_s", delay, "s", Better.LOWER, "scheduled time to job created")
    wait_and_judge(ctx, out, job, "scheduled_job")
    with contextlib.suppress(ApiError):
        ctx.api.delete(sched["url"])
