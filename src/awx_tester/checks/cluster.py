"""Read-only checks of the Kubernetes cluster and the installation's workloads.

They evaluate the facts gathered once in ``environment.cluster``; they make no
extra calls to the cluster themselves.
"""

from __future__ import annotations

from ..model import Better
from ..registry import Skip, check
from .util import gib


def _cluster(ctx):
    data = ctx.env.get("cluster")
    if not data:
        raise Skip("no cluster information was collected")
    return data


def _installation(ctx):
    inst = _cluster(ctx).get("installation")
    if not inst:
        raise Skip("installation namespace not configured")
    return inst


@check("cluster.nodes", "Nodes are ready and sized", requires=("kube",), tags=("smoke", "sizing"))
def nodes(ctx, out):
    """All nodes are Ready without pressure; records capacity, allocation and usage."""
    c = _cluster(ctx)
    node_list, totals = c.get("nodes") or [], c.get("totals") or {}
    if not node_list:
        raise Skip("node list unavailable: " + "; ".join(c.get("errors", [])))
    for n in node_list:
        if not n["ready"]:
            out.fail(f"node {n['name']} is not Ready")
        if n["pressure"]:
            out.fail(f"node {n['name']} reports {', '.join(n['pressure'])}")
        if n["unschedulable"]:
            out.warn(f"node {n['name']} is cordoned")
        min_eph = ctx.opt("cluster", "warn_ephemeral_gib")
        if min_eph and n["ephemeral_storage_bytes"] and gib(n["ephemeral_storage_bytes"]) < min_eph:
            out.warn(f"node {n['name']} has only {gib(n['ephemeral_storage_bytes'])} GiB ephemeral storage "
                      f"(logs and emptyDirs); below {min_eph} GiB")
    cpu, mem = totals.get("cpu_allocatable") or 0, totals.get("memory_allocatable_bytes") or 0
    out.metric("nodes", totals.get("nodes"))
    out.metric("cpu_allocatable", cpu, "cores", Better.HIGHER)
    out.metric("memory_allocatable_gib", gib(mem), "GiB", Better.HIGHER)
    warn_pct = ctx.opt("cluster", "warn_requested_pct")
    if cpu and totals.get("cpu_requested") is not None:
        pct = 100 * totals["cpu_requested"] / cpu
        out.metric("cpu_requested_pct", pct, "%", Better.LOWER)
        out.threshold("cpu requested", round(pct), warn_pct, None, "%")
    if mem and totals.get("memory_requested_bytes") is not None:
        pct = 100 * totals["memory_requested_bytes"] / mem
        out.metric("memory_requested_pct", pct, "%", Better.LOWER)
        out.threshold("memory requested", round(pct), warn_pct, None, "%")
    usage = c.get("node_usage")
    if usage and cpu and mem:
        out.metric("cpu_used_pct", 100 * sum(u["cpu"] for u in usage.values()) / cpu, "%", Better.LOWER)
        out.metric("memory_used_pct", 100 * sum(u["memory_bytes"] for u in usage.values()) / mem, "%",
                   Better.LOWER)
    else:
        out.detail("metrics-server not available: no live usage")


@check("cluster.storage", "Storage is bound", requires=("kube", "namespace"))
def storage(ctx, out):
    """A default storage class exists and every claim of the installation is Bound."""
    c = _cluster(ctx)
    classes = c.get("storage_classes") or []
    if not any(sc["default"] for sc in classes):
        out.warn("no default storage class")
    pvcs = _installation(ctx).get("pvcs") or []
    for p in pvcs:
        if p["phase"] != "Bound":
            out.fail(f"claim {p['name']} is {p['phase']}")
    out.metric("claims", len(pvcs))
    out.metric("claimed_gib", gib(sum(p["capacity_bytes"] or p["requested_bytes"] for p in pvcs)), "GiB")


@check("cluster.installation", "Operator reports the installation healthy", requires=("kube", "namespace"),
       tags=("smoke",))
def installation(ctx, out):
    """The custom resource exists, its conditions are healthy and its workloads are fully available."""
    inst = _installation(ctx)
    cr = inst.get("custom_resource")
    if not cr:
        out.warn(f"no {ctx.platform.display} custom resource found in namespace {inst['name']}")
    else:
        out.attr("custom_resource", f"{cr['kind']}/{cr['name']}")
        out.attr("version", cr.get("version"))
        for cond in cr.get("conditions", []):
            if cond["type"] == "Failure" and cond["status"] == "True":
                out.fail(f"{cr['kind']} reports Failure: {cond.get('reason')}")
            if cond["type"] in ("Running", "Successful") and cond["status"] == "False":
                out.warn(f"{cr['kind']} condition {cond['type']} is False ({cond.get('reason')})")
    if inst.get("operator"):
        out.attr("operator_image", inst["operator"]["image"])
    for w in (inst.get("deployments") or []) + (inst.get("statefulsets") or []):
        if (w["replicas"] or 0) > w["ready_replicas"]:
            out.fail(f"{w['name']}: {w['ready_replicas']}/{w['replicas']} ready")
    for comp in ("web", "task"):
        replicas = sum(w["replicas"] or 0 for w in inst.get("deployments") or [] if w["component"] == comp)
        out.metric(f"{comp}_replicas", replicas, "", Better.NONE)


@check("cluster.pods", "Installation pods are running", requires=("kube", "namespace"))
def pods(ctx, out):
    """Long-running pods are Ready; restarts are counted and reported."""
    pods = _installation(ctx).get("pods") or []
    restarts = 0
    for p in pods:
        if p["component"] in ("job", "migration") or p["phase"] == "Succeeded":
            continue
        restarts += p["restarts"]
        if p["phase"] != "Running" or not p["ready"]:
            out.fail(f"pod {p['name']} is {p['phase']}{'' if p['ready'] else ', not ready'}")
        elif p["restarts"]:
            out.warn(f"pod {p['name']} restarted {p['restarts']} times")
    out.metric("pods", len(pods))
    out.metric("restarts", restarts, "", Better.LOWER)


@check("cluster.resources", "Installation resource requests and usage", requires=("kube", "namespace"),
       tags=("sizing",))
def resources(ctx, out):
    """Sums the requests and limits of the installation, and its live usage when metrics exist."""
    from ..cluster import parse_bytes, parse_cpu

    inst = _installation(ctx)
    cpu_req = mem_req = 0.0
    unbounded = []
    for w in (inst.get("deployments") or []) + (inst.get("statefulsets") or []):
        reps = w["replicas"] or 0
        for c in w["containers"]:
            cpu_req += reps * parse_cpu(c["requests"].get("cpu"))
            mem_req += reps * parse_bytes(c["requests"].get("memory"))
            if not c["limits"].get("memory") and w["component"] in ("web", "task"):
                unbounded.append(f"{w['name']}/{c['name']}")
    out.metric("cpu_requested", cpu_req, "cores")
    out.metric("memory_requested_gib", gib(mem_req), "GiB")
    if unbounded:
        out.detail("containers without a memory limit: " + ", ".join(unbounded))
    usage = inst.get("pod_usage")
    if usage:
        out.metric("cpu_used", sum(u["cpu"] for u in usage.values()), "cores", Better.LOWER)
        out.metric("memory_used_gib", gib(sum(u["memory_bytes"] for u in usage.values())), "GiB", Better.LOWER)
