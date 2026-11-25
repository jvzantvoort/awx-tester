"""xUnit (JUnit XML) output, one testsuite per check category.

JUnit has no 'warning' state: warnings are reported as passed test cases with
the message in system-out, so CI does not fail on them. Metrics are attached as
properties of each test case.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import OrderedDict

from ..model import Report, Status


def to_junit(report: Report) -> str:
    suites = ET.Element("testsuites", name=f"awx-tester {report.target.get('name')}")
    by_category: OrderedDict[str, list] = OrderedDict()
    for r in report.results:
        by_category.setdefault(r.category, []).append(r)

    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0, "time": 0.0}
    for category, results in by_category.items():
        counts = {
            "tests": len(results),
            "failures": sum(r.status is Status.FAIL for r in results),
            "errors": sum(r.status is Status.ERROR for r in results),
            "skipped": sum(r.status is Status.SKIP for r in results),
            "time": sum(r.duration_s for r in results),
        }
        for k in totals:
            totals[k] += counts[k]
        suite = ET.SubElement(
            suites, "testsuite", name=category, timestamp=str(report.run.get("started", "")),
            hostname=str(report.target.get("name", "")),
            **{k: (f"{v:.3f}" if k == "time" else str(v)) for k, v in counts.items()},
        )
        props = ET.SubElement(suite, "properties")
        for key, value in (("target", report.target.get("name")), ("type", report.target.get("type")),
                           ("version", (report.environment.get("platform") or {}).get("version")),
                           ("level", report.run.get("level")), ("run_id", report.run.get("id"))):
            ET.SubElement(props, "property", name=key, value=str(value))
        for r in results:
            case = ET.SubElement(suite, "testcase", classname=f"awx_tester.{category}", name=r.id,
                                 time=f"{r.duration_s:.3f}")
            if r.metrics:
                cprops = ET.SubElement(case, "properties")
                for m in r.metrics:
                    ET.SubElement(cprops, "property", name=m.name, value=f"{m.value:g}{m.unit}")
            text = "\n".join(filter(None, [r.message, *r.details]))
            if r.status is Status.FAIL:
                ET.SubElement(case, "failure", message=r.message or "failed", type="fail").text = text
            elif r.status is Status.ERROR:
                ET.SubElement(case, "error", message=r.message or "error", type="error").text = text
            elif r.status is Status.SKIP:
                ET.SubElement(case, "skipped", message=r.message or "skipped")
            elif r.status is Status.WARN:
                ET.SubElement(case, "system-out").text = f"WARNING: {text}"
            elif text:
                ET.SubElement(case, "system-out").text = text
    for k, v in totals.items():
        suites.set(k, f"{v:.3f}" if k == "time" else str(v))
    ET.indent(suites)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(suites, encoding="unicode") + "\n"
