import json
import xml.etree.ElementTree as ET
from importlib.resources import files

import jsonschema
import pytest

from awx_tester.model import Report, Status
from awx_tester.output.html import to_html
from awx_tester.output.junit import to_junit
from awx_tester.redact import redact

from .conftest import make_report

SCHEMA = json.loads(files("awx_tester").joinpath("report.schema.json").read_text())


def test_report_roundtrip_and_schema(report, tmp_path):
    data = report.to_dict()
    jsonschema.validate(data, SCHEMA)
    path = tmp_path / "report.json"
    path.write_text(report.to_json())
    again = Report.load(path)
    assert again.to_dict() == data
    assert again.summary["pass"] == 2
    assert again.overall is Status.PASS


def test_overall_is_worst_status():
    assert make_report(status=Status.FAIL).overall is Status.FAIL


def test_incompatible_schema_rejected(report):
    data = report.to_dict()
    data["schema_version"] = "2.0"
    with pytest.raises(ValueError):
        Report.from_dict(data)


def test_html_embeds_report_and_loads_back(report, tmp_path):
    html = to_html(report)
    assert "perf.api_latency" in html and "<script" in html
    path = tmp_path / "report.html"
    path.write_text(html)
    assert Report.load(path).to_dict() == report.to_dict()


def test_html_escapes_script_end_in_data(tmp_path):
    r = make_report()
    r.results[0].message = "</script><b>x</b>"
    path = tmp_path / "r.html"
    path.write_text(to_html(r))
    assert Report.load(path).results[0].message == "</script><b>x</b>"


def test_junit_structure():
    r = make_report(status=Status.FAIL)
    root = ET.fromstring(to_junit(r).split("\n", 1)[1])
    assert root.tag == "testsuites"
    assert root.get("tests") == "2" and root.get("failures") == "1"
    suites = {s.get("name"): s for s in root.findall("testsuite")}
    assert set(suites) == {"platform", "perf"}
    case = suites["perf"].find("testcase")
    assert case.get("name") == "perf.api_latency"
    assert case.find("failure").get("message") == "slow"
    assert {p.get("name") for p in case.find("properties")} == {"p95_ms", "requests"}


def test_redact_removes_hosts_and_ips(report):
    text = redact(report).to_json()
    for secret in ("awx.lab.example.com", "node0.lab.example.com", "lab-admin", "10.1.2.3", "awx-task-abc"):
        assert secret not in text
    clean = redact(report)
    assert clean.run["redacted"] is True
    assert clean.environment["cluster"]["totals"]["nodes"] == 3  # sizing survives
    jsonschema.validate(clean.to_dict(), SCHEMA)


def test_redact_short_node_name_does_not_clobber_other_values():
    r = make_report(nodes=1)
    node = r.environment["cluster"]["nodes"][0]
    node["name"] = "k3s"
    r.environment["cluster"]["context"] = "default"
    r.environment["cluster"]["kubernetes_version"] = "v1.36.5+k3s1"
    r.environment["cluster"]["node_usage"] = {"k3s": {"cpu": 1.0, "memory_bytes": 1}}
    r.target["name"] = "k3s-awx-test"
    r.results[0].attributes["groups"] = ["default", "controlplane"]
    r.results[0].attributes["image"] = "10.20.30.40:5000/awx-ee:latest"
    clean = redact(r)
    assert clean.environment["cluster"]["nodes"][0]["name"] == "<node-1>"
    assert list(clean.environment["cluster"]["node_usage"]) == ["<node-1>"]
    assert clean.environment["cluster"]["kubernetes_version"] == "v1.36.5+k3s1"
    assert clean.environment["cluster"]["distribution"] == "k3s"
    assert clean.target["name"] == "k3s-awx-test"
    assert clean.results[0].attributes["groups"] == ["default", "controlplane"]
    assert clean.results[0].attributes["image"] == "<ip>:5000/awx-ee:latest"
