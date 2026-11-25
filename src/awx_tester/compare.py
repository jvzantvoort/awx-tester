"""Compare two or more reports: environment, check outcomes and metrics.

The first report is the baseline. Every other report is compared against it.
A metric only counts as improved or regressed when it moves by more than the
relative threshold *and* more than a small absolute noise floor for its unit,
so a 2 ms -> 3 ms change is not reported as a 50 % regression.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .fingerprint import fingerprint
from .model import Better, Report, Status

NOISE_FLOOR = {"ms": 5.0, "s": 0.5, "%": 1.0, "req/s": 1.0, "jobs/min": 1.0, "events/s": 1.0}

IMPROVED, REGRESSED, CHANGED, SAME, NEW, GONE = "improved", "regressed", "changed", "same", "new", "gone"


@dataclass
class Row:
    key: str
    values: list[Any]
    verdicts: list[str]  # one per report; the baseline's is always "same"
    title: str = ""
    unit: str = ""
    better: str = "none"
    deltas: list[float | None] = field(default_factory=list)  # relative change vs baseline, %

    @property
    def interesting(self) -> bool:
        return any(v != SAME for v in self.verdicts)


@dataclass
class Comparison:
    reports: list[Report]
    labels: list[str]
    threshold_pct: float
    environment: list[Row]
    statuses: list[Row]
    metrics: list[Row]
    warnings: list[str]

    def counts(self, idx: int) -> dict[str, int]:
        result = {IMPROVED: 0, REGRESSED: 0, CHANGED: 0, NEW: 0, GONE: 0}
        for row in self.statuses + self.metrics:
            v = row.verdicts[idx]
            if v in result:
                result[v] += 1
        result["environment_changes"] = sum(1 for r in self.environment if r.verdicts[idx] != SAME)
        return result

    @property
    def has_regressions(self) -> bool:
        return any(self.counts(i)[REGRESSED] for i in range(1, len(self.reports)))


def label_for(report: Report) -> str:
    started = str(report.run.get("started", ""))[:16].replace("T", " ")
    version = (report.environment.get("platform") or {}).get("version") or "?"
    return f"{report.target.get('name', '?')} {version} ({started})"


def metric_verdict(base: float | None, other: float | None, better: str, unit: str,
                   threshold_pct: float) -> tuple[str, float | None]:
    if base is None and other is None:
        return SAME, None
    if base is None:
        return NEW, None
    if other is None:
        return GONE, None
    diff = other - base
    rel = (100.0 * diff / abs(base)) if base else (0.0 if diff == 0 else float("inf"))
    floor = NOISE_FLOOR.get(unit, 0.0)
    if abs(diff) <= floor or abs(rel) <= threshold_pct:
        return SAME, rel
    if better == Better.NONE.value:
        return CHANGED, rel
    worse = diff > 0 if better == Better.LOWER.value else diff < 0
    return (REGRESSED if worse else IMPROVED), rel


def status_verdict(base: str | None, other: str | None) -> str:
    if base is None:
        return NEW
    if other is None:
        return GONE
    if base == other:
        return SAME
    skip = Status.SKIP.value
    if skip in (base, other):
        return CHANGED
    return REGRESSED if Status(other).severity > Status(base).severity else IMPROVED


def compare(reports: list[Report], threshold_pct: float = 10.0) -> Comparison:
    if len(reports) < 2:
        raise ValueError("comparing needs at least two reports")
    labels = [label_for(r) for r in reports]
    warnings: list[str] = []
    base = reports[0]
    for i, r in enumerate(reports[1:], start=1):
        if r.run.get("options") != base.run.get("options"):
            warnings.append(f"{labels[i]} used different options than the baseline; "
                            "performance numbers may not be comparable")
        if r.run.get("level") != base.run.get("level"):
            warnings.append(f"{labels[i]} ran at level {r.run.get('level')}, the baseline at {base.run.get('level')}")
        if r.tool.get("version") != base.tool.get("version"):
            warnings.append(f"{labels[i]} was made by awx-tester {r.tool.get('version')}, "
                            f"the baseline by {base.tool.get('version')}")

    # environment
    fps = [fingerprint(r) for r in reports]
    keys: list[str] = []
    for fp in fps:
        keys.extend(k for k in fp if k not in keys)
    env_rows = []
    for k in keys:
        vals = [fp.get(k) for fp in fps]
        env_rows.append(Row(k, vals, [SAME] + [SAME if v == vals[0] else CHANGED for v in vals[1:]]))

    # check outcomes
    ids: list[str] = []
    titles: dict[str, str] = {}
    for r in reports:
        for res in r.results:
            if res.id not in ids:
                ids.append(res.id)
                titles[res.id] = res.title
    status_rows = []
    for cid in ids:
        vals = [(r.result(cid).status.value if r.result(cid) else None) for r in reports]
        status_rows.append(Row(cid, vals, [SAME] + [status_verdict(vals[0], v) for v in vals[1:]],
                               title=titles[cid]))

    # metrics
    metric_rows = []
    for cid in ids:
        names: list[tuple[str, str, str, str]] = []
        for r in reports:
            res = r.result(cid)
            for m in res.metrics if res else []:
                if m.name not in [n[0] for n in names]:
                    names.append((m.name, m.unit, m.better.value, m.description))
        for name, unit, better, _desc in names:
            vals: list[float | None] = []
            for r in reports:
                res = r.result(cid)
                m = next((m for m in res.metrics if m.name == name), None) if res else None
                vals.append(m.value if m else None)
            verdicts, deltas = [SAME], [None]
            for v in vals[1:]:
                verdict, delta = metric_verdict(vals[0], v, better, unit, threshold_pct)
                verdicts.append(verdict)
                deltas.append(delta)
            metric_rows.append(Row(f"{cid}:{name}", vals, verdicts, title=titles[cid], unit=unit,
                                   better=better, deltas=deltas))

    return Comparison(reports, labels, threshold_pct, env_rows, status_rows, metric_rows, warnings)


def to_dict(cmp: Comparison) -> dict[str, Any]:
    def rows(rs: list[Row]) -> list[dict[str, Any]]:
        return [{"key": r.key, "title": r.title, "unit": r.unit, "better": r.better, "values": r.values,
                 "verdicts": r.verdicts, "deltas_pct": r.deltas} for r in rs]

    return {
        "baseline": cmp.labels[0],
        "labels": cmp.labels,
        "run_ids": [r.run.get("id") for r in cmp.reports],
        "threshold_pct": cmp.threshold_pct,
        "warnings": cmp.warnings,
        "counts": [cmp.counts(i) for i in range(len(cmp.reports))],
        "environment": rows(cmp.environment),
        "statuses": rows(cmp.statuses),
        "metrics": rows(cmp.metrics),
    }
