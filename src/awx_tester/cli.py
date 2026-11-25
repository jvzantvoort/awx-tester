"""Command line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from . import __version__
from .config import (
    PLATFORM_TYPES,
    ConfigError,
    Target,
    find_config,
    list_targets,
    load_target,
    target_from_dict,
)
from .context import merge_options, parse_set
from .model import Level, Report, Status
from .registry import REQUIREMENTS, all_checks, select
from .safety import SafetyError, authorize

FORMATS = ("console", "html", "junit", "json")
EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="awx-tester",
        description="Test and benchmark AWX, Ansible Automation Platform and Ascender installations.",
    )
    p.add_argument("--version", action="version", version=f"awx-tester {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run checks against a target")
    run.add_argument("-c", "--config", help="targets file (default: ./awx-tester.yml, ./targets.yml, "
                                            "~/.config/awx-tester/targets.yml, or $AWX_TESTER_CONFIG)")
    run.add_argument("-t", "--target", help="target name from the targets file")
    adhoc = run.add_argument_group("ad hoc target (no targets file; read-only runs only)")
    adhoc.add_argument("--type", choices=PLATFORM_TYPES, help="platform type")
    adhoc.add_argument("--url", help="base URL, e.g. https://awx.example.com")
    adhoc.add_argument("--username")
    adhoc.add_argument("--password-env", help="environment variable holding the password")
    adhoc.add_argument("--token-env", help="environment variable holding an OAuth token")
    adhoc.add_argument("--kube-context", help="kubeconfig context of the cluster")
    adhoc.add_argument("--namespace", help="namespace of the installation")
    adhoc.add_argument("--instance", help="name of the operator custom resource")
    adhoc.add_argument("--no-kube", action="store_true", help="do not collect Kubernetes facts")
    adhoc.add_argument("--insecure", action="store_true", help="do not verify TLS certificates")
    run.add_argument("-l", "--level", default="readonly", choices=[x.value for x in Level],
                     help="highest level of checks to run (default: readonly)")
    run.add_argument("--only", action="append", default=[], metavar="PATTERN",
                     help="run only matching checks: id or category glob, tag:NAME or level:NAME (repeatable)")
    run.add_argument("--skip", action="append", default=[], metavar="PATTERN", help="skip matching checks")
    run.add_argument("--set", action="append", default=[], metavar="SECTION.KEY=VALUE",
                     help="override an option, e.g. --set api_latency.iterations=50")
    run.add_argument("-f", "--format", default="console,html,junit,json", type=_csv,
                     help=f"outputs to produce, comma separated from {', '.join(FORMATS)} (json is always written)")
    run.add_argument("-o", "--output-dir", help="directory for report files (default: reports/<target>/<run id>)")
    run.add_argument("--confirm", metavar="TARGET", help="confirm a disruptive run by repeating the target name")
    run.add_argument("--keep-objects", action="store_true", help="do not delete objects created by mutating checks")
    run.add_argument("--redact", action="store_true", help="replace hostnames and addresses in the written reports")
    run.add_argument("--label", action="append", default=[], metavar="KEY=VALUE",
                     help="extra label stored with the report, e.g. --label change=CHG123")
    run.add_argument("--notes", default="", help="free text stored with the report")
    run.add_argument("-v", "--verbose", action="store_true", help="show checks as they start and a results table")

    lc = sub.add_parser("list-checks", help="list the available checks")
    lc.add_argument("-l", "--level", default="disruptive", choices=[x.value for x in Level])
    lc.add_argument("--markdown", action="store_true", help="print a Markdown table")

    tg = sub.add_parser("targets", help="list the targets in the targets file")
    tg.add_argument("-c", "--config")

    rd = sub.add_parser("render", help="render a saved report again")
    rd.add_argument("report", help="report.json or report.html")
    rd.add_argument("--html", metavar="FILE")
    rd.add_argument("--junit", metavar="FILE")
    rd.add_argument("--json", metavar="FILE", help="write the (optionally redacted) JSON")
    rd.add_argument("--redact", action="store_true")
    rd.add_argument("-v", "--verbose", action="store_true")

    cp = sub.add_parser("compare", help="compare two or more reports; the first is the baseline")
    cp.add_argument("reports", nargs="+", help="report.json or report.html files")
    cp.add_argument("--threshold", type=float, default=10.0, help="relative change in %% that counts (default 10)")
    cp.add_argument("--html", metavar="FILE", help="write an HTML comparison")
    cp.add_argument("--json", metavar="FILE", help="write the comparison as JSON")
    cp.add_argument("--all", action="store_true", help="show unchanged rows on the console too")
    cp.add_argument("--fail-on-regression", action="store_true", help="exit 1 when anything regressed")

    sub.add_parser("schema", help="print the JSON schema of the report format")
    return p


def _adhoc_target(args) -> Target:
    if not (args.type and args.url):
        raise ConfigError("give --target (with a targets file) or both --type and --url")
    raw = {
        "type": args.type, "url": args.url, "verify_ssl": not args.insecure,
        "auth": {"username": args.username, "password_env": args.password_env, "token_env": args.token_env},
        "kubernetes": False if args.no_kube else {
            "context": args.kube_context, "namespace": args.namespace, "instance": args.instance},
        "max_level": "readonly",
    }
    return target_from_dict("adhoc", raw)


def _labels(items: list[str]) -> dict[str, str]:
    out = {}
    for item in items:
        if "=" not in item:
            raise ConfigError(f"--label expects KEY=VALUE, got {item!r}")
        k, v = item.split("=", 1)
        out[k] = v
    return out


def cmd_run(args, console: Console) -> int:
    from .output.console import ConsoleProgress, print_report
    from .runner import Runner

    unknown = set(args.format) - set(FORMATS)
    if unknown:
        raise ConfigError(f"unknown format(s) {', '.join(sorted(unknown))}")
    if args.target:
        path = find_config(args.config)
        if path is None:
            raise ConfigError("no targets file found; pass --config or create awx-tester.yml")
        target = load_target(path, args.target)
    else:
        target = _adhoc_target(args)

    level = Level(args.level)
    authorize(target, level, confirm=args.confirm)
    options = merge_options(target.options, parse_set(args.set))
    checks = select(level, args.only, args.skip)
    if not checks:
        raise ConfigError("no checks selected")

    show = "console" in args.format
    out_console = console if show else Console(file=open(os.devnull, "w"))  # noqa: SIM115
    runner = Runner(target, level, checks, options, keep_objects=args.keep_objects,
                    progress=ConsoleProgress(out_console, verbose=args.verbose),
                    notes=args.notes, extra_labels=_labels(args.label))
    out_console.print(f"[bold]{len(checks)} checks[/] at level [bold]{level.value}[/] against "
                      f"[bold]{target.name}[/] ({target.type}, {target.url})")
    report = runner.run()

    written = write_outputs(report, args.format, args.output_dir, args.redact)
    if show:
        print_report(report, console, details=args.verbose)
        for p in written:
            console.print(f"[dim]wrote {p}[/]")
    else:
        print(json.dumps({"overall": report.overall.value, "files": [str(p) for p in written]}))
    return EXIT_FAILED if report.overall in (Status.FAIL, Status.ERROR) else EXIT_OK


def write_outputs(report: Report, formats: list[str], output_dir: str | None, redact: bool) -> list[Path]:
    from .output.html import to_html
    from .output.junit import to_junit
    from .redact import redact as do_redact

    if redact:
        report = do_redact(report)
    out = Path(output_dir) if output_dir else Path("reports") / report.target.get("name", "run") / report.run["id"]
    out.mkdir(parents=True, exist_ok=True)
    written = [out / "report.json"]
    written[0].write_text(report.to_json() + "\n", encoding="utf-8")
    if "html" in formats:
        written.append(out / "report.html")
        written[-1].write_text(to_html(report), encoding="utf-8")
    if "junit" in formats:
        written.append(out / "junit.xml")
        written[-1].write_text(to_junit(report), encoding="utf-8")
    return written


def cmd_list_checks(args, console: Console) -> int:
    checks = [c for c in all_checks() if Level(args.level).allows(c.level)]
    if args.markdown:
        print("| Check | Level | Needs | Tags | What it does |")
        print("|---|---|---|---|---|")
        for c in checks:
            print(f"| `{c.id}` | {c.level.value} | {', '.join(c.requires)} | {', '.join(c.tags)} | "
                  f"{c.description} |")
        return EXIT_OK
    table = Table()
    for col in ("Check", "Level", "Needs", "Tags", "Description"):
        table.add_column(col, overflow="fold")
    for c in checks:
        table.add_row(c.id, c.level.value, ", ".join(c.requires), ", ".join(c.tags), c.description)
    console.print(table)
    console.print("[dim]Needs: " + "; ".join(f"{k} = {v}" for k, v in REQUIREMENTS.items()) + "[/]")
    return EXIT_OK


def cmd_targets(args, console: Console) -> int:
    path = find_config(args.config)
    if path is None:
        raise ConfigError("no targets file found")
    table = Table(title=str(path))
    for col in ("Target", "Type", "URL", "Context / namespace", "Max level"):
        table.add_column(col)
    for name, raw in list_targets(path).items():
        raw = raw or {}
        kube = raw.get("kubernetes") or {}
        level = "readonly (protected)" if raw.get("protected") else raw.get("max_level", "readonly")
        table.add_row(name, str(raw.get("type")), str(raw.get("url")),
                      f"{kube.get('context') or '-'} / {kube.get('namespace') or '-'}", level)
    console.print(table)
    return EXIT_OK


def cmd_render(args, console: Console) -> int:
    from .output.console import print_report
    from .output.html import to_html
    from .output.junit import to_junit
    from .redact import redact

    report = Report.load(args.report)
    if args.redact:
        report = redact(report)
    for path, text in ((args.html, lambda: to_html(report)), (args.junit, lambda: to_junit(report)),
                       (args.json, lambda: report.to_json() + "\n")):
        if path:
            Path(path).write_text(text(), encoding="utf-8")
    print_report(report, console, details=True)
    return EXIT_OK


def cmd_compare(args, console: Console) -> int:
    from .compare import compare, to_dict
    from .output.console import print_comparison
    from .output.html import comparison_to_html

    reports = [Report.load(p) for p in args.reports]
    cmp = compare(reports, args.threshold)
    print_comparison(cmp, console, show_all=args.all)
    if args.html:
        Path(args.html).write_text(comparison_to_html(cmp), encoding="utf-8")
        console.print(f"[dim]wrote {args.html}[/]")
    if args.json:
        Path(args.json).write_text(json.dumps(to_dict(cmp), indent=2, default=str) + "\n", encoding="utf-8")
    return EXIT_FAILED if args.fail_on_regression and cmp.has_regressions else EXIT_OK


def cmd_schema(args, console: Console) -> int:
    from importlib.resources import files

    print(files("awx_tester").joinpath("report.schema.json").read_text(encoding="utf-8"))
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    console = Console(highlight=False)
    handlers = {"run": cmd_run, "list-checks": cmd_list_checks, "targets": cmd_targets,
                "render": cmd_render, "compare": cmd_compare, "schema": cmd_schema}
    try:
        return handlers[args.command](args, console)
    except (ConfigError, SafetyError, ValueError) as exc:
        console.print(f"[bold red]error:[/] {exc}")
        return EXIT_USAGE
    except KeyboardInterrupt:
        console.print("[yellow]interrupted[/]")
        return 130


if __name__ == "__main__":
    sys.exit(main())
