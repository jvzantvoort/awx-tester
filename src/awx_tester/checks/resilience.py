"""Disruptive checks: delete pods of the installation and measure how it recovers.

They only run with ``--level disruptive`` against a target whose ``max_level``
is ``disruptive`` and after the target name was confirmed. Each check first
verifies the installation is healthy, so one failure does not cascade into the
next.
"""

from __future__ import annotations

import time

from ..api import ApiError
from ..model import Better, Level
from ..registry import Skip, check

D = Level.DISRUPTIVE


def _api_up(ctx) -> bool:
    try:
        ctx.api.request("GET", "ping/", auth=False, timeout=5)
        return True
    except ApiError:
        return False


def _workloads_ready(ctx, component: str) -> bool:
    kube = ctx.kube
    ns = kube.namespace
    items = list(kube.apps.list_namespaced_deployment(ns).items) + list(
        kube.apps.list_namespaced_stateful_set(ns).items)
    from ..cluster import _belongs_to, classify_component

    found = False
    for w in items:
        if kube.instance and not _belongs_to(w, kube.instance):
            continue
        if classify_component(w.metadata.name, w.metadata.labels) != component:
            continue
        found = True
        want = w.spec.replicas or 0
        if (w.status.ready_replicas or 0) < want or (w.status.updated_replicas or 0) < want:
            return False
    return found


def _control_capacity_ok(ctx) -> bool:
    try:
        items = list(ctx.api.iter_pages("instances/"))
    except ApiError:
        return False
    return any(i.get("enabled") and i.get("capacity") and i.get("node_type") in ("control", "hybrid")
               and i.get("node_state") in (None, "ready") for i in items)


def _replaced(ctx, pod) -> bool:
    """The deleted pod is gone, or (StatefulSets reuse names) came back as a new object."""
    from kubernetes.client.exceptions import ApiException

    try:
        current = ctx.kube.core.read_namespaced_pod(pod.metadata.name, ctx.kube.namespace)
    except ApiException as exc:
        if exc.status == 404:
            return True
        raise
    return current.metadata.uid != pod.metadata.uid


def _pick_pod(ctx, component: str):
    if not _workloads_ready(ctx, component) or not _api_up(ctx):
        raise Skip(f"the {component} workload is not healthy before the test; not disrupting it")
    pods = [p for p in ctx.kube.pods(component) if p.status.phase == "Running"]
    if not pods:
        raise Skip(f"no running {component} pod found")
    return sorted(pods, key=lambda p: p.metadata.name)[0]


def _disrupt_and_recover(ctx, out, component: str, extra_ready=None) -> dict:
    """Delete one pod of ``component`` and poll until the API, the workloads and ``extra_ready`` recover."""
    pod = _pick_pod(ctx, component)
    poll = float(ctx.opt("chaos", "poll_s"))
    timeout = float(ctx.opt("chaos", "recovery_timeout_s"))
    out.attr("deleted_pod", pod.metadata.name)
    ctx.log(f"deleting pod {pod.metadata.name}")
    start = time.monotonic()
    ctx.kube.core.delete_namespaced_pod(pod.metadata.name, ctx.kube.namespace)

    down = 0.0
    first_down = last_down = None
    api_back = workloads_back = None
    while True:
        now = time.monotonic()
        if now - start > timeout:
            out.fail(f"no full recovery within {timeout:.0f}s")
            break
        up = _api_up(ctx)
        if not up:
            # count the real time spent, including a ping that hung until its timeout
            down += time.monotonic() - now + poll
            first_down = first_down if first_down is not None else now - start
            last_down = time.monotonic() - start
        elif api_back is None and first_down is not None:
            api_back = now - start
        if workloads_back is None and _replaced(ctx, pod) and _workloads_ready(ctx, component):
            workloads_back = now - start
        if up and workloads_back is not None and (extra_ready is None or extra_ready()):
            break
        time.sleep(poll)
    recovery = time.monotonic() - start
    out.metric("recovery_s", recovery, "s", Better.LOWER, "pod deleted to fully healthy")
    out.metric("api_unavailable_s", down, "s", Better.LOWER, "time ping did not answer")
    out.metric("workload_ready_s", workloads_back, "s", Better.LOWER, "pod deleted to replacement ready")
    if last_down is not None:
        out.attr("api_outage_window_s", [round(first_down, 1), round(last_down, 1)])
    return {"recovery_s": recovery, "api_down_s": down}


def _job_after(ctx, out, label: str) -> None:
    ws = ctx.workspace()
    start = time.monotonic()
    job = ws.launch_adhoc(ctx)
    final = ctx.api.wait_job(job["url"], timeout=float(ctx.opt("jobs", "timeout_s")))
    out.metric(f"{label}_s", time.monotonic() - start, "s", Better.LOWER, "launch to finish after recovery")
    if final["status"] != "successful":
        out.fail(f"job after recovery ended {final['status']}")


@check("chaos.web_restart", "Recover from losing a web pod", level=D, requires=("api", "kube", "namespace"),
       tags=("resilience",))
def web_restart(ctx, out):
    """Deletes one web pod; measures API downtime and time until the replacement is ready."""
    _disrupt_and_recover(ctx, out, "web")


@check("chaos.task_restart", "Recover from losing a task pod", level=D,
       requires=("api", "auth", "kube", "namespace"), depends=("func.workspace",), tags=("resilience",))
def task_restart(ctx, out):
    """Deletes one task pod; waits for control capacity to return and proves a job runs again."""
    _disrupt_and_recover(ctx, out, "task", extra_ready=lambda: _control_capacity_ok(ctx))
    _job_after(ctx, out, "job_after_recovery")


@check("chaos.job_during_task_restart", "A running job when the task pod dies", level=D,
       requires=("api", "auth", "kube", "namespace"), depends=("func.workspace",), tags=("resilience",))
def job_during_task_restart(ctx, out):
    """Starts a 60s job, deletes the task pod while it runs and records how the job and platform end up."""
    ws = ctx.workspace()
    job = ws.launch_adhoc(ctx, module="command", args="sleep 60")
    deadline = time.monotonic() + float(ctx.opt("jobs", "timeout_s"))
    while ctx.api.get(job["url"])["status"] != "running":
        if time.monotonic() > deadline:
            out.fail("the job never started running")
            return
        time.sleep(1)
    time.sleep(5)
    _disrupt_and_recover(ctx, out, "task", extra_ready=lambda: _control_capacity_ok(ctx))
    start = time.monotonic()
    try:
        final = ctx.api.wait_job(job["url"], timeout=float(ctx.opt("chaos", "recovery_timeout_s")))
    except TimeoutError:
        out.fail("the interrupted job never reached a final state")
        return
    out.attr("interrupted_job_status", final["status"])
    out.metric("interrupted_job_settled_s", time.monotonic() - start, "s", Better.LOWER)
    out.info(f"interrupted job ended '{final['status']}'")


@check("chaos.postgres_restart", "Recover from losing the managed database", level=D,
       requires=("api", "auth", "kube", "namespace"), depends=("func.workspace",), tags=("resilience",))
def postgres_restart(ctx, out):
    """Deletes the operator-managed PostgreSQL pod; skipped for external databases."""
    if not ctx.kube.pods("postgres"):
        raise Skip("no managed PostgreSQL pod (external database)")
    _disrupt_and_recover(ctx, out, "postgres", extra_ready=lambda: _control_capacity_ok(ctx))
    _job_after(ctx, out, "job_after_recovery")
