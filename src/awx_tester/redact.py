"""Strip identifying names from a report so it can be shared and compared with others.

Hostnames, node names, ingress hosts, the API URL and the kube context are
replaced by stable placeholders (the same input always maps to the same
placeholder within a report) and IPv4 addresses are masked. Sizing, versions and
measurements are kept, since those are what make reports comparable.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from .model import Report

IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

# Too generic to identify anyone, and replacing them would clobber unrelated values.
GENERIC = {"default", "local", "localhost", "kubernetes", "admin", "master", "node", "cluster", "awx"}

# Descriptive fields that never identify a site, left untouched so reports stay comparable.
SAFE_KEYS = {"distribution", "kubernetes_version", "kubelet_version", "container_runtime", "os_image", "kernel",
             "architecture", "instance_type", "provisioner", "storage_class", "version"}


def _sensitive_strings(d: dict[str, Any]) -> dict[str, str]:
    mapping: dict[str, str] = {}

    def add(value: Any, kind: str) -> None:
        if isinstance(value, str) and len(value) >= 3 and value.lower() not in GENERIC and value not in mapping:
            mapping[value] = f"<{kind}-{sum(1 for v in mapping.values() if v.startswith('<' + kind)) + 1}>"

    target = d.get("target", {})
    url = target.get("url") or ""
    if url:
        add(urlparse(url).hostname, "host")
    add(target.get("kube_context"), "context")
    env = d.get("environment", {})
    cluster = env.get("cluster") or {}
    add(cluster.get("context"), "context")
    for n in cluster.get("nodes") or []:
        add(n.get("name"), "node")
    for i in (cluster.get("installation") or {}).get("ingresses") or []:
        for h in i.get("hosts") or []:
            add(h, "host")
    for i in (env.get("platform") or {}).get("instances") or []:
        add(i.get("node"), "instance")
    for r in d.get("results", []):
        for i in (r.get("attributes") or {}).get("instances") or []:
            if isinstance(i, dict):
                add(i.get("hostname"), "instance")
    return mapping


def redact(report: Report) -> Report:
    d = report.to_dict()
    mapping = _sensitive_strings(d)
    # Whole tokens only: "k3s" as a node name must not touch "k3s-awx-test" or "v1.31+k3s1".
    # Longest first, so a full hostname wins over a shorter name inside it.
    patterns = [(re.compile(r"(?<![\w.-])" + re.escape(raw) + r"(?![\w-]|\.\w)"), placeholder)
                for raw, placeholder in sorted(mapping.items(), key=lambda kv: -len(kv[0]))]

    def scrub(value: Any, key: str = "") -> Any:
        if isinstance(value, str):
            if key in SAFE_KEYS:
                return value
            for pattern, placeholder in patterns:
                value = pattern.sub(placeholder, value)
            return IPV4.sub("<ip>", value)
        if isinstance(value, dict):
            return {(scrub(k) if isinstance(k, str) else k): scrub(v, k if isinstance(k, str) else "")
                    for k, v in value.items()}
        if isinstance(value, list):
            return [scrub(v, key) for v in value]
        return value

    clean = scrub(d)
    clean["target"]["labels"] = d["target"].get("labels", {})  # chosen by the user, kept as given
    clean["target"]["url"] = "<redacted>" if d["target"].get("url") else None
    clean["run"]["redacted"] = True
    return Report.from_dict(clean)
