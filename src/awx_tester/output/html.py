"""Self-contained HTML reports (no external assets, readable offline and as e-mail attachment).

The single-run report embeds its canonical JSON, so ``awx-tester compare`` can
read HTML reports as well as JSON ones.
"""

from __future__ import annotations

import json

from jinja2 import Environment, PackageLoader, select_autoescape

from ..compare import Comparison
from ..compare import to_dict as comparison_dict
from ..fingerprint import fingerprint, sizing_summary
from ..model import Report
from . import fmt_delta, fmt_value


def _env() -> Environment:
    env = Environment(
        loader=PackageLoader("awx_tester", "templates"),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["v"] = fmt_value
    env.filters["delta"] = fmt_delta
    env.filters["gib"] = lambda b: None if b is None else round(b / 2**30, 1)
    return env


def _embed(data: dict) -> str:
    return json.dumps(data, default=str).replace("</", "<\\/")


def to_html(report: Report) -> str:
    return _env().get_template("report.html.j2").render(
        r=report,
        sizing=sizing_summary(report),
        fingerprint=fingerprint(report),
        cluster=report.environment.get("cluster") or {},
        platform=report.environment.get("platform") or {},
        categories=sorted({x.category for x in report.results}, key=lambda c: [x.category for x in
                                                                              report.results].index(c)),
        embedded=_embed(report.to_dict()),
    )


def comparison_to_html(cmp: Comparison) -> str:
    return _env().get_template("compare.html.j2").render(
        c=cmp, counts=[cmp.counts(i) for i in range(len(cmp.reports))],
        embedded=_embed(comparison_dict(cmp)),
    )
