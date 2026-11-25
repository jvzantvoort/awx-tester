from __future__ import annotations

import pytest

from awx_tester.model import Better, CheckResult, Level, Metric, Report, Status


def make_report(name: str = "lab", version: str = "24.6.1", p95: float = 100.0, status: Status = Status.PASS,
                nodes: int = 3, options: dict | None = None) -> Report:
    return Report(
        tool={"name": "awx-tester", "version": "0.1.0"},
        run={"id": f"run-{name}-{version}", "started": "2026-11-25T10:00:00+00:00",
             "finished": "2026-11-25T10:05:00+00:00", "duration_s": 300, "level": "readonly",
             "checks": ["platform.ping", "perf.api_latency"],
             "options": options or {"api_latency": {"iterations": 10}}},
        target={"name": name, "type": "awx", "url": "https://awx.lab.example.com", "namespace": "awx",
                "kube_context": "lab-admin", "labels": {"env": "lab"}},
        environment={
            "platform": {"type": "awx", "display": "AWX", "version": version, "api_prefix": "/api/v2/",
                         "instances": [{"node": "awx-task-abc", "capacity": 100}],
                         "scale": {"hosts": 1200}},
            "cluster": {
                "context": "lab-admin", "distribution": "k3s", "kubernetes_version": "v1.31.0+k3s1",
                "nodes": [{"name": f"node{i}.lab.example.com", "ready": True, "pressure": [], "unschedulable": False,
                           "os_image": "Rocky Linux 9", "kernel": "5.14", "container_runtime": "containerd://1.7",
                           "cpu_allocatable": 4, "memory_allocatable_bytes": 16 * 2**30,
                           "ephemeral_storage_bytes": 50 * 2**30, "roles": ["worker"]} for i in range(nodes)],
                "totals": {"nodes": nodes, "cpu_allocatable": 4.0 * nodes,
                           "memory_allocatable_bytes": 16 * 2**30 * nodes},
                "storage_classes": [{"name": "local-path", "default": True}],
                "installation": {"name": "awx", "ingresses": [{"hosts": ["awx.lab.example.com"]}],
                                 "pvcs": [], "deployments": [], "statefulsets": []},
            },
        },
        results=[
            CheckResult("platform.ping", "API answers ping", "platform", Level.READONLY, status=Status.PASS,
                        metrics=[Metric("latency_ms", 20, "ms", Better.LOWER)],
                        attributes={"active_node": "awx-web-1 at 10.1.2.3"}),
            CheckResult("perf.api_latency", "API read latency", "perf", Level.READONLY, status=status,
                        message="" if status is Status.PASS else "slow",
                        metrics=[Metric("p95_ms", p95, "ms", Better.LOWER), Metric("requests", 130)]),
        ],
    )


@pytest.fixture
def report() -> Report:
    return make_report()
