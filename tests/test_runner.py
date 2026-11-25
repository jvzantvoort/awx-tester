"""Runs the read-only API checks against a mocked controller, without Kubernetes."""

import json
import re

import responses

from awx_tester.config import target_from_dict
from awx_tester.context import merge_options
from awx_tester.model import Level, Status
from awx_tester.registry import select
from awx_tester.runner import Runner

BASE = "https://awx.example.com"
PING = {"version": "24.6.1", "ha": False, "active_node": "awx-web",
        "instances": [{"node": "awx-task", "node_type": "control", "capacity": 50}], "instance_groups": []}


def page(results):
    return {"count": len(results), "next": None, "previous": None, "results": results}


ROUTES = [
    (r"ping/$", PING),
    (r"me/$", page([{"username": "admin", "is_superuser": True}])),
    (r"config/$", {"version": "24.6.1", "license_info": {}}),
    (r"settings/\w+/$", {"MAX_FORKS": 200}),
    (r"instances/$", page([{
        "hostname": "awx-task", "node_type": "control", "node_state": "ready", "enabled": True,
        "capacity": 50, "cpu": "4", "memory": 8 * 2**30, "version": "24.6.1", "errors": ""}])),
    (r"instance_groups/$", page([{"name": "controlplane", "instances": 1, "capacity": 50,
                                  "percent_capacity_remaining": 100, "is_container_group": False}])),
    (r"execution_environments/$", page([{"name": "AWX EE", "image": "quay.io/ansible/awx-ee"}])),
    (r"metrics/$", "# HELP x\nawx_pending_jobs_total 0\nawx_running_jobs_total 2\n"),
    (r"jobs/$", page([{"created": "2026-11-25T10:00:00Z", "started": "2026-11-25T10:00:02Z",
                       "finished": "2026-11-25T10:00:12Z"}])),
]


def mock_controller(rsps, prefix="/api/v2/"):
    """Route every GET under the API prefix; unknown endpoints return an empty page."""

    def route(request):
        path = request.path_url.split("?")[0][len(prefix):]
        for pattern, body in ROUTES:
            if re.fullmatch(pattern, path):
                if isinstance(body, str):
                    return 200, {"Content-Type": "text/plain"}, body
                return 200, {"Content-Type": "application/json"}, json.dumps(body)
        return 200, {"Content-Type": "application/json"}, json.dumps(page([]))

    rsps.add_callback(responses.GET, re.compile(re.escape(BASE + prefix) + ".*"), callback=route)


def run(level=Level.READONLY, prefix="/api/v2/", ptype="awx", **opts):
    target = target_from_dict("mock", {"type": ptype, "url": BASE, "kubernetes": False,
                                       "auth": {"username": "admin", "password": "pw"}})
    options = merge_options({"api_latency": {"iterations": 2, "warmup": 0}, "api_concurrency": {"duration_s": 0.3,
                                                                                               "workers": 2}}, opts)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        if prefix != "/api/v2/":
            rsps.get(BASE + "/api/controller/v2/ping/", status=404)
            rsps.get(BASE + "/api/gateway/v1/ping/", json={"version": "2.5"})
        mock_controller(rsps, prefix)
        return Runner(target, level, select(level), options).run()


def test_readonly_run_against_mock():
    report = run()
    statuses = {r.id: r.status for r in report.results}
    assert statuses["platform.ping"] is Status.PASS
    assert statuses["platform.instances"] is Status.PASS
    assert statuses["platform.metrics"] is Status.PASS
    assert statuses["perf.api_latency"] is Status.PASS
    assert statuses["cluster.nodes"] is Status.SKIP  # Kubernetes disabled
    assert report.environment["platform"]["settings"]["MAX_FORKS"] == 200
    assert report.run["unavailable"]["kube"]
    metrics = report.result("platform.metrics").attributes["gauges"]
    assert metrics["awx_running_jobs_total"] == 2
    assert report.result("platform.job_history").attributes["counts"]["successful"] == 0


def test_aap24_falls_back_to_api_v2():
    report = run(ptype="aap", prefix="/api/v2/")
    assert report.environment["platform"]["api_prefix"] == "/api/v2/"
    assert report.result("platform.ping").status is Status.PASS


def test_unreachable_api_skips_everything():
    target = target_from_dict("down", {"type": "awx", "url": "https://down.invalid", "kubernetes": False})
    with responses.RequestsMock(assert_all_requests_are_fired=False):
        report = Runner(target, Level.READONLY, select(Level.READONLY), merge_options()).run()
    assert {r.status for r in report.results} == {Status.SKIP}
    assert "api" in report.run["unavailable"]
