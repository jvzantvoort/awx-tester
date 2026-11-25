"""Console output, live while checks run and as a summary afterwards."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from ..compare import IMPROVED, REGRESSED, SAME, Comparison
from ..fingerprint import sizing_summary
from ..model import CheckResult, Report, Status
from ..registry import CheckSpec
from . import fmt_delta, fmt_value

STYLE = {
    "pass": "green", "warn": "yellow", "fail": "bold red", "error": "bold magenta", "skip": "dim",
    IMPROVED: "green", REGRESSED: "bold red", "changed": "yellow", "new": "cyan", "gone": "dim", SAME: "",
}


def badge(status: str) -> str:
    return f"[{STYLE.get(status, '')}]{status.upper():5}[/]"


def key_metrics(result: CheckResult, limit: int = 3) -> str:
    return "  ".join(f"{m.name}={fmt_value(m.value, m.unit)}" for m in result.metrics[:limit])


class ConsoleProgress:
    """Callback for Runner(progress=...): prints one line per check as it finishes."""

    def __init__(self, console: Console, verbose: bool = False):
        self.console = console
        self.verbose = verbose

    def __call__(self, message: str, spec: CheckSpec | None, result: CheckResult | None) -> None:
        if message:
            self.console.print(f"[dim]· {message}[/]")
        elif spec is not None and result is None and self.verbose:
            self.console.print(f"[dim]  running {spec.id} …[/]")
        elif result is not None:
            line = f"{badge(result.status.value)} {result.id:<34} {result.duration_s:7.2f}s  {key_metrics(result)}"
            self.console.print(line, highlight=False)
            if result.message and result.status is not Status.PASS:
                self.console.print(f"       [dim]{result.message}[/]", highlight=False)


def print_report(report: Report, console: Console, details: bool = False) -> None:
    t = report.target
    console.rule(f"[bold]awx-tester[/] · {t.get('name')} · {report.run.get('level')}")
    sizing = Table(title="Environment", show_header=False, box=None, padding=(0, 2))
    sizing.add_column(style="dim")
    sizing.add_column()
    for k, v in sizing_summary(report).items():
        sizing.add_row(k, fmt_value(v))
    console.print(sizing)
    unavailable = report.run.get("unavailable") or {}
    for what, why in unavailable.items():
        console.print(f"[yellow]! {what}:[/] {why}")

    if details:
        table = Table(title="Results", show_lines=False)
        table.add_column("Status")
        table.add_column("Check")
        table.add_column("Time", justify="right")
        table.add_column("Metrics")
        table.add_column("Message", overflow="fold")
        for r in report.results:
            table.add_row(badge(r.status.value), r.id, f"{r.duration_s:.2f}s", key_metrics(r, 4), r.message)
        console.print(table)

    s = report.summary
    parts = [f"[{STYLE[k]}]{s[k]} {k}[/]" for k in ("pass", "warn", "fail", "error", "skip") if s[k]]
    console.print(f"\n[bold]Overall:[/] {badge(report.overall.value)}  " + ", ".join(parts)
                  + f"  in {report.run.get('duration_s')}s")
    for problem in report.run.get("cleanup_problems") or []:
        console.print(f"[yellow]cleanup:[/] {problem}")


def print_comparison(cmp: Comparison, console: Console, show_all: bool = False) -> None:
    console.rule(f"[bold]Comparison[/] · baseline {cmp.labels[0]}")
    for w in cmp.warnings:
        console.print(f"[yellow]! {w}[/]")

    def table(title: str, rows, numeric: bool) -> None:
        shown = [r for r in rows if show_all or r.interesting]
        if not shown:
            console.print(f"[dim]{title}: no differences[/]")
            return
        tbl = Table(title=title)
        tbl.add_column("Item", overflow="fold")
        for i, label in enumerate(cmp.labels):
            tbl.add_column(("baseline\n" if i == 0 else "") + label, overflow="fold")
        for r in shown:
            cells = []
            for i, v in enumerate(r.values):
                text = fmt_value(v, r.unit) if numeric else fmt_value(v)
                if i and numeric and r.deltas[i] is not None:
                    text += f" ({fmt_delta(r.deltas[i])})"
                style = STYLE.get(r.verdicts[i], "")
                cells.append(f"[{style}]{text}[/]" if style else text)
            tbl.add_row(r.key, *cells)
        console.print(tbl)

    table("Environment", cmp.environment, numeric=False)
    table("Check outcomes", cmp.statuses, numeric=False)
    table(f"Metrics (threshold {cmp.threshold_pct:g}%)", cmp.metrics, numeric=True)
    for i, label in enumerate(cmp.labels[1:], start=1):
        c = cmp.counts(i)
        console.print(f"[bold]{label}[/]: [red]{c[REGRESSED]} regressed[/], [green]{c[IMPROVED]} improved[/], "
                      f"{c['changed']} changed, {c['environment_changes']} environment differences")
