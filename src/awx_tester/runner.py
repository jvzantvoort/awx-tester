"""Runs a selection of checks against one target and produces a :class:`Report`."""

from __future__ import annotations

import contextlib
import platform as pyplatform
import secrets
import time
import traceback
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from . import __version__
from .api import ApiClient, ApiError
from .config import Target
from .context import RunContext
from .model import SCHEMA_VERSION, CheckResult, Level, Report, Status
from .platforms import get_platform
from .registry import CheckSpec, Outcome, Skip

# Settings that change behaviour or performance and are worth comparing between runs.
SETTINGS_OF_INTEREST = {
    "jobs": ["AWX_ISOLATION_BASE_PATH", "DEFAULT_JOB_TIMEOUT", "DEFAULT_JOB_IDLE_TIMEOUT",
             "DEFAULT_INVENTORY_UPDATE_TIMEOUT", "DEFAULT_PROJECT_UPDATE_TIMEOUT", "MAX_FORKS",
             "AWX_COLLECTIONS_ENABLED", "AWX_ROLES_ENABLED", "AWX_SHOW_PLAYBOOK_LINKS",
             "EVENT_STDOUT_MAX_BYTES_DISPLAY", "MAX_WEBSOCKET_EVENT_RATE", "SCHEDULE_MAX_JOBS",
             "AWX_RUNNER_KEEPALIVE_SECONDS", "PROJECT_UPDATE_VVV", "AD_HOC_COMMANDS"],
    "system": ["ACTIVITY_STREAM_ENABLED", "ACTIVITY_STREAM_ENABLED_FOR_INVENTORY_SYNC",
               "ORG_ADMINS_CAN_SEE_ALL_USERS", "MANAGE_ORGANIZATION_AUTH", "DEFAULT_EXECUTION_ENVIRONMENT",
               "AUTOMATION_ANALYTICS_GATHER_INTERVAL", "INSIGHTS_TRACKING_STATE", "REMOTE_HOST_HEADERS",
               "CSRF_TRUSTED_ORIGINS", "SUBSCRIPTIONS_USERNAME"],
    "logging": ["LOG_AGGREGATOR_ENABLED", "LOG_AGGREGATOR_TYPE", "LOG_AGGREGATOR_LEVEL"],
}

# Object counts describe how much data the installation holds; latencies depend on it.
SCALE_ENDPOINTS = ("organizations", "users", "teams", "credentials", "projects", "inventories", "hosts",
                   "job_templates", "workflow_job_templates", "schedules", "unified_jobs")

Progress = Callable[[str, CheckSpec | None, CheckResult | None], None]


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(2)


def resolve_password(target: Target, kube: Any) -> None:
    """Fill the password from a Kubernetes secret when the target asks for that."""
    ref = target.auth.password_secret
    if not ref or target.auth.password or target.auth.token:
        return
    if kube is None:
        raise RuntimeError("auth.password_secret needs Kubernetes access")
    import base64

    ns = ref.get("namespace") or target.kubernetes.namespace
    secret = kube.core.read_namespaced_secret(ref["name"], ns)
    target.auth.password = base64.b64decode(secret.data[ref.get("key", "password")]).decode()


def collect_platform(ctx: RunContext, ping: dict[str, Any]) -> dict[str, Any]:
    api = ctx.api
    assert api is not None
    out: dict[str, Any] = {
        "type": ctx.platform.name,
        "display": ctx.platform.display,
        "api_prefix": api.prefix,
        "version": ping.get("version"),
        "ha": ping.get("ha"),
        "instances": [
            {"node": i.get("node"), "node_type": i.get("node_type"), "capacity": i.get("capacity"),
             "version": i.get("version")}
            for i in ping.get("instances", [])
        ],
        "instance_groups": [
            {"name": g.get("name"), "capacity": g.get("capacity"), "instances": g.get("instances")}
            for g in ping.get("instance_groups", [])
        ],
        "errors": [],
    }
    for label, path in ctx.platform.extra_pings:
        with contextlib.suppress(ApiError):
            out[f"{label}_version"] = (api.get(path, auth=False) or {}).get("version")
    if not api.has_credentials:
        out["errors"].append("no credentials: platform details limited to ping")
        return out
    try:
        me = api.get("me/")["results"][0]
        out["me"] = {k: me.get(k) for k in ("username", "is_superuser", "is_system_auditor")}
    except ApiError as exc:
        out["errors"].append(f"me: {exc.status}")
        return out
    try:
        cfg = api.get("config/")
        out["config"] = {
            "version": cfg.get("version"),
            "time_zone": cfg.get("time_zone"),
            "license_type": (cfg.get("license_info") or {}).get("license_type"),
            "subscription_name": (cfg.get("license_info") or {}).get("subscription_name"),
            "analytics_status": cfg.get("analytics_status"),
            "become_methods": [b[0] if isinstance(b, list) else b for b in cfg.get("become_methods", [])],
        }
    except ApiError as exc:
        out["errors"].append(f"config: {exc.status}")
    scale = {}
    for name in SCALE_ENDPOINTS:
        with contextlib.suppress(ApiError):
            scale[name] = api.count(f"{name}/")
    out["scale"] = scale
    if out["me"].get("is_superuser") or out["me"].get("is_system_auditor"):
        settings: dict[str, Any] = {}
        for category, keys in SETTINGS_OF_INTEREST.items():
            try:
                values = api.get(f"settings/{category}/")
            except ApiError:
                continue
            settings.update({k: values[k] for k in keys if k in values})
        out["settings"] = settings
    return out


class Runner:
    def __init__(self, target: Target, level: Level, checks: list[CheckSpec], options: dict[str, Any],
                 keep_objects: bool = False, progress: Progress | None = None,
                 notes: str = "", extra_labels: dict[str, str] | None = None):
        self.target = target
        self.level = level
        self.checks = checks
        self.options = options
        self.keep_objects = keep_objects
        self.progress = progress or (lambda *a: None)
        self.notes = notes
        self.extra_labels = extra_labels or {}

    def _say(self, msg: str) -> None:
        self.progress(msg, None, None)

    def run(self) -> Report:
        run_id = new_run_id()
        started = datetime.now(timezone.utc)
        t0 = time.monotonic()
        platform = get_platform(self.target.type)
        ctx = RunContext(target=self.target, platform=platform, level=self.level, run_id=run_id,
                         options=self.options, keep_objects=self.keep_objects, log=self._say)
        problems: dict[str, str] = {}

        # Kubernetes first: the API password may live in a secret.
        if self.target.kubernetes.enabled:
            try:
                from .cluster import Kube

                ctx.kube = Kube(self.target.kubernetes)
            except Exception as exc:  # noqa: BLE001
                problems["kube"] = f"Kubernetes not reachable: {exc}"
        else:
            problems["kube"] = "Kubernetes disabled for this target"
        if not self.target.kubernetes.namespace:
            problems["namespace"] = "no kubernetes.namespace configured"

        try:
            resolve_password(self.target, ctx.kube)
        except Exception as exc:  # noqa: BLE001
            problems["auth"] = f"could not read the password secret: {exc}"

        a = self.target.auth
        ctx.api = ApiClient(self.target.url, platform, a.username, a.password, a.token,
                            self.target.verify_ssl, self.target.timeout)
        self._say(f"contacting {self.target.url}")
        try:
            ping = ctx.api.detect()
            ctx.env["platform"] = collect_platform(ctx, ping)
        except ApiError as exc:
            problems["api"] = f"API not reachable: {exc}"
            ctx.env["platform"] = {"type": platform.name, "display": platform.display, "errors": [str(exc)]}
        me = ctx.env["platform"].get("me")
        if "api" not in problems and not me:
            problems.setdefault("auth", "no valid credentials" if not ctx.api.has_credentials
                                else "credentials were rejected")
        if me and not (me.get("is_superuser") or me.get("is_system_auditor")):
            problems["superuser"] = "user is not a superuser or system auditor"
        if "auth" in problems:
            problems.setdefault("superuser", problems["auth"])

        if ctx.kube is not None:
            self._say("collecting cluster facts")
            from .cluster import collect_cluster

            ctx.env["cluster"] = collect_cluster(ctx.kube, platform)
            if ctx.kube.instance and not self.target.kubernetes.instance:
                self.target.kubernetes.instance = ctx.kube.instance

        results: list[CheckResult] = []
        by_id: dict[str, CheckResult] = {}
        try:
            for spec in self.checks:
                self.progress("", spec, None)
                result = self._run_one(ctx, spec, problems, by_id)
                results.append(result)
                by_id[spec.id] = result
                self.progress("", spec, result)
        finally:
            if ctx._cleanup:
                self._say(f"cleaning up {len(ctx._cleanup)} objects")
            cleanup_problems = ctx.cleanup()

        finished = datetime.now(timezone.utc)
        target_info = self.target.describe()
        target_info["labels"].update(self.extra_labels)
        return Report(
            schema_version=SCHEMA_VERSION,
            tool={"name": "awx-tester", "version": __version__, "python": pyplatform.python_version()},
            run={
                "id": run_id,
                "started": started.isoformat(),
                "finished": finished.isoformat(),
                "duration_s": round(time.monotonic() - t0, 2),
                "level": self.level.value,
                "checks": [c.id for c in self.checks],
                "options": self.options,
                "notes": self.notes,
                "unavailable": problems,
                "cleanup_problems": cleanup_problems,
                "kept_objects": self.keep_objects,
            },
            target=target_info,
            environment=ctx.env,
            results=results,
        )

    def _run_one(self, ctx: RunContext, spec: CheckSpec, problems: dict[str, str],
                 done: dict[str, CheckResult]) -> CheckResult:
        result = CheckResult(id=spec.id, title=spec.title, category=spec.category, level=spec.level,
                             tags=list(spec.tags))
        missing = [problems[r] for r in spec.requires if r in problems]
        if missing:
            result.status, result.message = Status.SKIP, missing[0]
            return result
        for dep in spec.depends:
            dep_result = done.get(dep)
            if dep_result is not None and dep_result.status in (Status.FAIL, Status.ERROR, Status.SKIP):
                result.status, result.message = Status.SKIP, f"depends on {dep}, which did not pass"
                return result
        out = Outcome()
        start = time.perf_counter()
        try:
            spec.func(ctx, out)
            result.status = out.status
        except Skip as exc:
            result.status = Status.SKIP
            out.messages.insert(0, str(exc))
        except Exception as exc:  # noqa: BLE001 - one broken check must not stop the run
            result.status = Status.ERROR
            out.messages.insert(0, f"{type(exc).__name__}: {exc}")
            out.details.append(traceback.format_exc(limit=4))
        result.duration_s = time.perf_counter() - start
        result.message = "; ".join(out.messages)
        result.details = out.details
        result.metrics = out.metrics
        result.attributes = out.attributes
        return result
