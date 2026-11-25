"""Flatten a report's environment into comparable key/value pairs.

The fingerprint is what a comparison lines up to explain *why* two runs differ:
platform version, cluster size, operator settings, data volume.
"""

from __future__ import annotations

from typing import Any

from .model import Report

GIB = 2**30


def _flatten(prefix: str, value: Any, out: dict[str, Any], depth: int = 0) -> None:
    if isinstance(value, dict) and depth < 4:
        for k in sorted(value):
            _flatten(f"{prefix}.{k}", value[k], out, depth + 1)
    elif isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
        out[prefix] = f"[{len(value)} items]"
    else:
        out[prefix] = value


def fingerprint(report: Report) -> dict[str, Any]:
    env = report.environment or {}
    fp: dict[str, Any] = {}

    p = env.get("platform") or {}
    fp["platform.type"] = p.get("display") or p.get("type") or report.target.get("type")
    fp["platform.version"] = p.get("version")
    if p.get("gateway_version"):
        fp["platform.gateway_version"] = p["gateway_version"]
    fp["platform.api_prefix"] = p.get("api_prefix")
    instances = p.get("instances") or []
    fp["platform.instances"] = len(instances)
    fp["platform.capacity"] = sum(i.get("capacity") or 0 for i in instances)
    for k, v in sorted((p.get("scale") or {}).items()):
        fp[f"scale.{k}"] = v
    for k, v in sorted((p.get("settings") or {}).items()):
        fp[f"setting.{k}"] = v if not isinstance(v, (list, dict)) else str(v)

    c = env.get("cluster") or {}
    if c:
        totals = c.get("totals") or {}
        nodes = c.get("nodes") or []
        fp["cluster.distribution"] = c.get("distribution")
        fp["cluster.kubernetes_version"] = c.get("kubernetes_version")
        fp["cluster.nodes"] = totals.get("nodes")
        fp["cluster.cpu_allocatable"] = totals.get("cpu_allocatable")
        if totals.get("memory_allocatable_bytes") is not None:
            fp["cluster.memory_allocatable_gib"] = round(totals["memory_allocatable_bytes"] / GIB, 1)
        if totals.get("ephemeral_storage_bytes") is not None:
            fp["cluster.ephemeral_storage_gib"] = round(totals["ephemeral_storage_bytes"] / GIB, 1)
        fp["cluster.node_os"] = ", ".join(sorted({n.get("os_image") or "?" for n in nodes})) or None
        fp["cluster.kernel"] = ", ".join(sorted({n.get("kernel") or "?" for n in nodes})) or None
        fp["cluster.container_runtime"] = ", ".join(sorted({n.get("container_runtime") or "?" for n in nodes})) or None
        fp["cluster.instance_types"] = ", ".join(sorted({n["instance_type"] for n in nodes
                                                         if n.get("instance_type")})) or None
        default_sc = [s["name"] for s in c.get("storage_classes") or [] if s.get("default")]
        fp["cluster.default_storage_class"] = ", ".join(default_sc) or None
        fp["cluster.metrics_server"] = c.get("node_usage") is not None

        inst = c.get("installation") or {}
        cr = inst.get("custom_resource") or {}
        if cr:
            fp["install.kind"] = cr.get("kind")
            fp["install.cr_version"] = cr.get("version")
            spec: dict[str, Any] = {}
            _flatten("install.spec", cr.get("spec") or {}, spec)
            fp.update(spec)
        if inst.get("operator"):
            fp["install.operator_image"] = inst["operator"].get("image")
        for w in (inst.get("deployments") or []) + (inst.get("statefulsets") or []):
            comp = w["component"]
            if comp in ("operator", "other"):
                continue
            fp[f"install.{comp}.replicas"] = w.get("replicas")
            for cont in w.get("containers") or []:
                key = f"install.{comp}.{cont['name']}"
                fp[f"{key}.image"] = cont.get("image")
                req, lim = cont.get("requests") or {}, cont.get("limits") or {}
                if req or lim:
                    fp[f"{key}.resources"] = (
                        f"req cpu={req.get('cpu', '-')} mem={req.get('memory', '-')}; "
                        f"lim cpu={lim.get('cpu', '-')} mem={lim.get('memory', '-')}"
                    )
        if inst:
            claimed = sum((pvc.get("capacity_bytes") or pvc.get("requested_bytes") or 0)
                          for pvc in inst.get("pvcs") or [])
            fp["install.storage_gib"] = round(claimed / GIB, 1)
    return {k: v for k, v in fp.items() if v is not None}


SIZING_KEYS = (
    "platform.type", "platform.version", "platform.gateway_version", "platform.instances", "platform.capacity",
    "cluster.distribution", "cluster.kubernetes_version", "cluster.nodes", "cluster.cpu_allocatable",
    "cluster.memory_allocatable_gib", "cluster.ephemeral_storage_gib", "cluster.node_os",
    "cluster.container_runtime", "cluster.default_storage_class", "install.kind", "install.cr_version",
    "install.operator_image", "install.web.replicas", "install.task.replicas", "install.storage_gib",
)


def sizing_summary(report: Report) -> dict[str, Any]:
    fp = fingerprint(report)
    return {k: fp[k] for k in SIZING_KEYS if k in fp}
