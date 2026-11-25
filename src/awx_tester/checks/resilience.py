"""Disruptive checks: delete pods of the installation and measure how it recovers.

They only run with ``--level disruptive`` against a target whose ``max_level``
is ``disruptive`` and after the target name was confirmed. Each check first
verifies the installation is healthy, so one failure does not cascade into the
next.

With ``chaos.repeat`` (``--repeat N``) every check disrupts and recovers N times.
Metrics keep their names and report the median, so reports stay comparable with
single runs; ``<metric>_max`` adds the worst repetition and the attribute
``samples`` keeps every value.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable

from ..api import ApiError
from ..model import Better, Level, Status
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


class Samples:
    """Measurements of all repetitions of one check, emitted as medians at the end."""

    def __init__(self) -> None:
        self.values: dict[str, list[float]] = {}
        self.meta: dict[str, tuple[str, Better, str]] = {}

    def add(self, name: str, value: float | None, unit: str = "s", better: Better = Better.LOWER,
            description: str = "") -> None:
        if value is None:
            return
        self.values.setdefault(name, []).append(float(value))
        self.meta[name] = (unit, better, description)

    def emit(self, out) -> None:
        for name, values in self.values.items():
            unit, better, description = self.meta[name]
            n = len(values)
            out.metric(name, statistics.median(values), unit, better,
                       f"{description} (median of {n})" if n > 1 else description)
            if n > 1:
                out.metric(f"{name}_max", max(values), unit, better, f"{description} (worst of {n})")
        out.attr("samples", {name: [round(v, 3) for v in values] for name, values in self.values.items()})


def _repeat(ctx, out, once: Callable[[Samples, int], None]) -> None:
    """Run one disruption ``chaos.repeat`` times; stop at the first failed repetition."""
    n = max(1, int(ctx.opt("chaos", "repeat") or 1))
    settle = float(ctx.opt("chaos", "settle_s") or 0)
    samples = Samples()
    done = 0
    try:
        for i in range(n):
            if i:
                ctx.log(f"repetition {i + 1}/{n} in {settle:g}s")
                time.sleep(settle)
            try:
                once(samples, i)
            except Skip as exc:
                if i == 0:
                    raise
                out.fail(f"repetition {i + 1}: {exc}")
                break
            done += 1
            if out.status in (Status.FAIL, Status.ERROR):
                if i + 1 < n:
                    out.info(f"stopped after repetition {i + 1} of {n}")
                break
    finally:
        out.attr("repetitions", done)
        samples.emit(out)


def _disrupt_and_recover(ctx, out, s: Samples, component: str, extra_ready=None) -> None:
    """Delete one pod of ``component`` and poll until the API, the workloads and ``extra_ready`` recover."""
    pod = _pick_pod(ctx, component)
    poll = float(ctx.opt("chaos", "poll_s"))
    timeout = float(ctx.opt("chaos", "recovery_timeout_s"))
    out.attributes.setdefault("deleted_pods", []).append(pod.metadata.name)
    ctx.log(f"deleting pod {pod.metadata.name}")
    start = time.monotonic()
    ctx.kube.core.delete_namespaced_pod(pod.metadata.name, ctx.kube.namespace)

    down = 0.0
    first_down = last_down = None
    workloads_back = None
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
        if workloads_back is None and _replaced(ctx, pod) and _workloads_ready(ctx, component):
            workloads_back = now - start
        if up and workloads_back is not None and (extra_ready is None or extra_ready()):
            break
        time.sleep(poll)
    s.add("recovery_s", time.monotonic() - start, description="pod deleted to fully healthy")
    s.add("api_unavailable_s", down, description="time ping did not answer")
    s.add("workload_ready_s", workloads_back, description="pod deleted to replacement ready")
    if last_down is not None:
        out.attributes.setdefault("api_outage_windows_s", []).append([round(first_down, 1), round(last_down, 1)])


def _job_after(ctx, out, s: Samples, label: str) -> None:
    ws = ctx.workspace()
    start = time.monotonic()
    job = ws.launch_adhoc(ctx)
    final = ctx.api.wait_job(job["url"], timeout=float(ctx.opt("jobs", "timeout_s")))
    s.add(f"{label}_s", time.monotonic() - start, description="launch to finish after recovery")
    if final["status"] != "successful":
        out.fail(f"job after recovery ended {final['status']}")


@check("chaos.web_restart", "Recover from losing a web pod", level=D, requires=("api", "kube", "namespace"),
       tags=("resilience",))
def web_restart(ctx, out):
    """Deletes one web pod; measures API downtime and time until the replacement is ready."""
    _repeat(ctx, out, lambda s, i: _disrupt_and_recover(ctx, out, s, "web"))


@check("chaos.task_restart", "Recover from losing a task pod", level=D,
       requires=("api", "auth", "kube", "namespace"), depends=("func.workspace",), tags=("resilience",))
def task_restart(ctx, out):
    """Deletes one task pod; waits for control capacity to return and proves a job runs again."""
    def once(s, i):
        _disrupt_and_recover(ctx, out, s, "task", extra_ready=lambda: _control_capacity_ok(ctx))
        _job_after(ctx, out, s, "job_after_recovery")

    _repeat(ctx, out, once)


@check("chaos.job_during_task_restart", "A running job when the task pod dies", level=D,
       requires=("api", "auth", "kube", "namespace"), depends=("func.workspace",), tags=("resilience",))
def job_during_task_restart(ctx, out):
    """Starts a 60s job, deletes the task pod while it runs and records how the job and platform end up."""
    outcomes: list[str] = []

    def once(s, i):
        ws = ctx.workspace()
        job = ws.launch_adhoc(ctx, module="command", args="sleep 60")
        deadline = time.monotonic() + float(ctx.opt("jobs", "timeout_s"))
        while ctx.api.get(job["url"])["status"] != "running":
            if time.monotonic() > deadline:
                out.fail("the job never started running")
                return
            time.sleep(1)
        time.sleep(5)
        _disrupt_and_recover(ctx, out, s, "task", extra_ready=lambda: _control_capacity_ok(ctx))
        start = time.monotonic()
        try:
            final = ctx.api.wait_job(job["url"], timeout=float(ctx.opt("chaos", "recovery_timeout_s")))
        except TimeoutError:
            out.fail("the interrupted job never reached a final state")
            return
        outcomes.append(final["status"])
        s.add("interrupted_job_settled_s", time.monotonic() - start)

    try:
        _repeat(ctx, out, once)
    finally:
        out.attr("interrupted_job_statuses", outcomes)
        if outcomes:
            counts = {st: outcomes.count(st) for st in dict.fromkeys(outcomes)}
            out.info("interrupted jobs ended " + ", ".join(f"'{st}' x{c}" for st, c in counts.items()))


@check("chaos.postgres_restart", "Recover from losing the managed database", level=D,
       requires=("api", "auth", "kube", "namespace"), depends=("func.workspace",), tags=("resilience",))
def postgres_restart(ctx, out):
    """Deletes the operator-managed PostgreSQL pod; skipped for external databases."""
    if not ctx.kube.pods("postgres"):
        raise Skip("no managed PostgreSQL pod (external database)")

    def once(s, i):
        _disrupt_and_recover(ctx, out, s, "postgres", extra_ready=lambda: _control_capacity_ok(ctx))
        _job_after(ctx, out, s, "job_after_recovery")

    _repeat(ctx, out, once)
