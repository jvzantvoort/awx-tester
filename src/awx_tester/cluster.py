"""Read-only collection of Kubernetes facts: cluster sizing and the installation's configuration.

Everything here only lists and gets. A section that cannot be read (missing RBAC,
no metrics-server) is recorded under ``errors`` instead of aborting the run.
"""

from __future__ import annotations

import re
from typing import Any

from .config import KubeTarget
from .platforms import Platform

_CPU_RE = re.compile(r"^([0-9.]+)([a-zA-Z]*)$")
_MEM_SUFFIX = {
    "": 1, "k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12, "P": 10**15, "E": 10**18,
    "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60, "m": 1e-3,
}


def parse_cpu(q: str | int | float | None) -> float:
    """Kubernetes CPU quantity to cores ('250m' -> 0.25, '4' -> 4.0, '1500000n' -> 0.0015)."""
    if q is None or q == "":
        return 0.0
    m = _CPU_RE.match(str(q))
    if not m:
        raise ValueError(f"bad cpu quantity {q!r}")
    value, unit = float(m.group(1)), m.group(2)
    return value * {"": 1, "m": 1e-3, "u": 1e-6, "n": 1e-9}[unit]


def parse_bytes(q: str | int | float | None) -> float:
    """Kubernetes memory/storage quantity to bytes ('1Gi' -> 1073741824)."""
    if q is None or q == "":
        return 0.0
    s = str(q)
    m = re.match(r"^([0-9.]+(?:[eE][0-9]+)?)([a-zA-Z]*)$", s)
    if not m:
        raise ValueError(f"bad quantity {q!r}")
    return float(m.group(1)) * _MEM_SUFFIX[m.group(2)]


def classify_component(name: str, labels: dict[str, str] | None) -> str:
    labels = labels or {}
    key = labels.get("app.kubernetes.io/name") or name
    checks = (
        ("automation-job", "job"),
        ("operator", "operator"),
        ("postgres", "postgres"),
        ("migration", "migration"),
        ("redis", "redis"),
        ("gateway", "gateway"),
        ("hub", "hub"),
        ("eda", "eda"),
        ("lightspeed", "lightspeed"),
    )
    for needle, component in checks:
        if needle in key or needle in name:
            return component
    for suffix in ("web", "task", "ee", "rsyslog"):
        if key.endswith(f"-{suffix}") or f"-{suffix}-" in name:
            return suffix
    return "other"


def detect_distribution(git_version: str, api_groups: set[str]) -> str:
    v = git_version.lower()
    if "config.openshift.io" in api_groups:
        return "openshift"
    for marker, name in (("k3s", "k3s"), ("rke2", "rke2"), ("-eks-", "eks"), ("gke", "gke"),
                         ("aks", "aks"), ("+vmware", "tkg"), ("microk8s", "microk8s")):
        if marker in v:
            return name
    return "kubernetes"


class Kube:
    """Thin wrapper around the Kubernetes client for one context and namespace."""

    def __init__(self, target: KubeTarget):
        from kubernetes import client, config

        config.load_kube_config(config_file=target.kubeconfig, context=target.context)
        _, active = config.list_kube_config_contexts(config_file=target.kubeconfig)
        self.context = target.context or (active or {}).get("name")
        self.namespace = target.namespace
        self.instance = target.instance
        self.client = client
        self.core = client.CoreV1Api()
        self.apps = client.AppsV1Api()
        self.custom = client.CustomObjectsApi()
        self.storage = client.StorageV1Api()
        self.net = client.NetworkingV1Api()
        self.version_api = client.VersionApi()
        self.apis = client.ApisApi()

    # -- helpers used by checks ---------------------------------------------------

    def find_instance(self, platform: Platform) -> tuple[Any, dict[str, Any]] | None:
        """Return (custom resource definition, object) for the installation in the namespace."""
        from kubernetes.client.exceptions import ApiException

        if not self.namespace:
            return None
        for cr in platform.custom_resources:
            try:
                items = self.custom.list_namespaced_custom_object(
                    cr.group, cr.version, self.namespace, cr.plural).get("items", [])
            except ApiException as exc:
                if exc.status in (403, 404):
                    continue
                raise
            if self.instance:
                items = [i for i in items if i["metadata"]["name"] == self.instance]
            if len(items) == 1:
                return cr, items[0]
            if len(items) > 1:
                names = ", ".join(i["metadata"]["name"] for i in items)
                raise RuntimeError(
                    f"{len(items)} {cr.kind} objects in {self.namespace} ({names}); "
                    "set kubernetes.instance in the target")
        return None

    def pods(self, component: str | None = None) -> list[Any]:
        pods = self.core.list_namespaced_pod(self.namespace).items
        if self.instance:
            pods = [p for p in pods if _belongs_to(p, self.instance)]
        if component:
            pods = [p for p in pods if classify_component(p.metadata.name, p.metadata.labels) == component]
        return pods


def _belongs_to(obj: Any, instance: str) -> bool:
    labels = obj.metadata.labels or {}
    if labels.get("app.kubernetes.io/part-of") == instance:
        return True
    if labels.get("app.kubernetes.io/instance", "").startswith(instance):
        return True
    return obj.metadata.name.startswith(instance + "-")


def _resources(container: Any) -> dict[str, Any]:
    res = container.resources
    return {
        "requests": dict((res.requests or {}) if res else {}),
        "limits": dict((res.limits or {}) if res else {}),
    }


def _workload(obj: Any) -> dict[str, Any]:
    spec = obj.spec.template.spec
    status = obj.status
    return {
        "name": obj.metadata.name,
        "component": classify_component(obj.metadata.name, obj.metadata.labels),
        "replicas": obj.spec.replicas,
        "ready_replicas": status.ready_replicas or 0,
        "containers": [
            {"name": c.name, "image": c.image, **_resources(c)} for c in spec.containers
        ],
        "init_containers": [
            {"name": c.name, "image": c.image} for c in (spec.init_containers or [])
        ],
    }


def collect_cluster(kube: Kube, platform: Platform) -> dict[str, Any]:
    from kubernetes.client.exceptions import ApiException

    out: dict[str, Any] = {"context": kube.context, "errors": []}

    def guard(section: str, fn):
        try:
            return fn()
        except ApiException as exc:
            out["errors"].append(f"{section}: HTTP {exc.status} {exc.reason}")
        except Exception as exc:  # noqa: BLE001 - we report, not crash, on partial data
            out["errors"].append(f"{section}: {exc}")
        return None

    # -- cluster-wide ---------------------------------------------------------------
    version = guard("version", lambda: kube.version_api.get_code())
    groups = guard("api groups", lambda: {g.name for g in kube.apis.get_api_versions().groups}) or set()
    if version:
        out["kubernetes_version"] = version.git_version
        out["distribution"] = detect_distribution(version.git_version, groups)

    def nodes():
        result = []
        for n in kube.core.list_node().items:
            labels = n.metadata.labels or {}
            info = n.status.node_info
            conds = {c.type: c.status for c in (n.status.conditions or [])}
            cap, alloc = n.status.capacity or {}, n.status.allocatable or {}
            result.append({
                "name": n.metadata.name,
                "roles": sorted(k.split("/", 1)[1] for k in labels
                                if k.startswith("node-role.kubernetes.io/")) or ["worker"],
                "ready": conds.get("Ready") == "True",
                "pressure": sorted(t for t, s in conds.items() if t != "Ready" and s == "True"),
                "unschedulable": bool(n.spec.unschedulable),
                "taints": len(n.spec.taints or []),
                "kubelet_version": info.kubelet_version,
                "os_image": info.os_image,
                "kernel": info.kernel_version,
                "container_runtime": info.container_runtime_version,
                "architecture": info.architecture,
                "instance_type": labels.get("node.kubernetes.io/instance-type"),
                "zone": labels.get("topology.kubernetes.io/zone"),
                "cpu_capacity": parse_cpu(cap.get("cpu")),
                "cpu_allocatable": parse_cpu(alloc.get("cpu")),
                "memory_capacity_bytes": parse_bytes(cap.get("memory")),
                "memory_allocatable_bytes": parse_bytes(alloc.get("memory")),
                "ephemeral_storage_bytes": parse_bytes(alloc.get("ephemeral-storage")),
                "pods_allocatable": int(alloc.get("pods", 0) or 0),
            })
        return result

    node_list = guard("nodes", nodes) or []
    out["nodes"] = node_list
    out["totals"] = {
        "nodes": len(node_list),
        "ready_nodes": sum(1 for n in node_list if n["ready"]),
        "cpu_allocatable": round(sum(n["cpu_allocatable"] for n in node_list), 3),
        "memory_allocatable_bytes": sum(n["memory_allocatable_bytes"] for n in node_list),
        "ephemeral_storage_bytes": sum(n["ephemeral_storage_bytes"] for n in node_list),
    }

    def requested():
        cpu = mem = 0.0
        for p in kube.core.list_pod_for_all_namespaces(field_selector="status.phase=Running").items:
            for c in p.spec.containers:
                req = (c.resources.requests or {}) if c.resources else {}
                cpu += parse_cpu(req.get("cpu"))
                mem += parse_bytes(req.get("memory"))
        return {"cpu_requested": round(cpu, 3), "memory_requested_bytes": mem}

    out["totals"].update(guard("cluster requests", requested) or {})

    out["storage_classes"] = guard("storage classes", lambda: [
        {
            "name": sc.metadata.name,
            "provisioner": sc.provisioner,
            "default": (sc.metadata.annotations or {}).get(
                "storageclass.kubernetes.io/is-default-class") == "true",
            "reclaim_policy": sc.reclaim_policy,
            "binding_mode": sc.volume_binding_mode,
            "expansion": bool(sc.allow_volume_expansion),
        }
        for sc in kube.storage.list_storage_class().items
    ]) or []
    out["ingress_classes"] = guard("ingress classes", lambda: [
        ic.metadata.name for ic in kube.net.list_ingress_class().items]) or []

    def node_usage():
        data = kube.custom.list_cluster_custom_object("metrics.k8s.io", "v1beta1", "nodes")
        return {
            i["metadata"]["name"]: {
                "cpu": parse_cpu(i["usage"]["cpu"]),
                "memory_bytes": parse_bytes(i["usage"]["memory"]),
            }
            for i in data.get("items", [])
        }

    out["node_usage"] = guard("node metrics", node_usage)

    # -- the installation's namespace --------------------------------------------------
    if not kube.namespace:
        out["errors"].append("namespace: not configured, skipping installation details")
        return out

    ns: dict[str, Any] = {"name": kube.namespace}
    out["installation"] = ns

    found = guard("custom resource", lambda: kube.find_instance(platform))
    if found:
        cr, obj = found
        kube.instance = kube.instance or obj["metadata"]["name"]
        status = obj.get("status") or {}
        ns["custom_resource"] = {
            "api_version": f"{cr.group}/{cr.version}",
            "kind": cr.kind,
            "name": obj["metadata"]["name"],
            "spec": _strip_secrets(obj.get("spec") or {}),
            "version": status.get("version"),
            "conditions": [
                {"type": c.get("type"), "status": c.get("status"), "reason": c.get("reason")}
                for c in status.get("conditions", [])
            ],
        }
    ns["instance"] = kube.instance

    ns["deployments"] = guard("deployments", lambda: [
        _workload(d) for d in kube.apps.list_namespaced_deployment(kube.namespace).items
        if not kube.instance or _belongs_to(d, kube.instance)
        or classify_component(d.metadata.name, d.metadata.labels) == "operator"
    ]) or []
    ns["statefulsets"] = guard("statefulsets", lambda: [
        _workload(s) for s in kube.apps.list_namespaced_stateful_set(kube.namespace).items
        if not kube.instance or _belongs_to(s, kube.instance)
    ]) or []
    operator = next((d for d in ns["deployments"] if d["component"] == "operator"), None)
    if operator:
        image = next((c["image"] for c in operator["containers"] if "operator" in c["image"]),
                     operator["containers"][0]["image"])
        ns["operator"] = {"deployment": operator["name"], "image": image,
                          "version": image.rsplit(":", 1)[-1] if ":" in image else None}

    def pods():
        result = []
        for p in kube.pods():
            statuses = p.status.container_statuses or []
            result.append({
                "name": p.metadata.name,
                "component": classify_component(p.metadata.name, p.metadata.labels),
                "phase": p.status.phase,
                "ready": bool(statuses) and all(s.ready for s in statuses),
                "restarts": sum(s.restart_count for s in statuses),
                "node": p.spec.node_name,
                "started": p.status.start_time.isoformat() if p.status.start_time else None,
            })
        return result

    ns["pods"] = guard("pods", pods) or []
    ns["pvcs"] = guard("persistent volume claims", lambda: [
        {
            "name": c.metadata.name,
            "storage_class": c.spec.storage_class_name,
            "requested_bytes": parse_bytes((c.spec.resources.requests or {}).get("storage")),
            "capacity_bytes": parse_bytes((c.status.capacity or {}).get("storage")),
            "phase": c.status.phase,
            "access_modes": c.spec.access_modes,
        }
        for c in kube.core.list_namespaced_persistent_volume_claim(kube.namespace).items
    ]) or []
    ns["ingresses"] = guard("ingresses", lambda: [
        {
            "name": i.metadata.name,
            "class": i.spec.ingress_class_name,
            "hosts": [r.host for r in (i.spec.rules or []) if r.host],
            "tls": bool(i.spec.tls),
        }
        for i in kube.net.list_namespaced_ingress(kube.namespace).items
    ]) or []

    def pod_usage():
        data = kube.custom.list_namespaced_custom_object("metrics.k8s.io", "v1beta1", kube.namespace, "pods")
        return {
            i["metadata"]["name"]: {
                "cpu": round(sum(parse_cpu(c["usage"]["cpu"]) for c in i["containers"]), 4),
                "memory_bytes": sum(parse_bytes(c["usage"]["memory"]) for c in i["containers"]),
            }
            for i in data.get("items", [])
        }

    ns["pod_usage"] = guard("pod metrics", pod_usage)
    return out


_SECRET_KEY = re.compile(r"(password|secret|token|key|credential|cert)", re.I)


def _strip_secrets(spec: Any) -> Any:
    """Drop values whose key looks sensitive. Secret *names* are kept, values never reach a report."""
    if isinstance(spec, dict):
        clean = {}
        for k, v in spec.items():
            if _SECRET_KEY.search(k) and not isinstance(v, (dict, list, bool)) and not k.endswith("_secret"):
                clean[k] = "<redacted>"
            else:
                clean[k] = _strip_secrets(v)
        return clean
    if isinstance(spec, list):
        return [_strip_secrets(v) for v in spec]
    return spec
