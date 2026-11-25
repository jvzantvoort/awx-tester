"""Run context shared by all checks, and the workspace that mutating checks build in."""

from __future__ import annotations

import copy
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .api import ApiClient, ApiError
from .config import Target
from .model import Level
from .platforms import Platform

# Defaults for every tunable. Targets override them under ``options:`` and the
# command line with ``--set section.key=value``. The effective options are
# stored in each report, so a comparison can tell when two runs used different
# settings.
DEFAULT_OPTIONS: dict[str, Any] = {
    "api_latency": {
        "iterations": 10,
        "warmup": 2,
        "endpoints": [
            "ping/", "me/", "config/", "instances/", "instance_groups/",
            "organizations/?page_size=25", "inventories/?page_size=25", "hosts/?page_size=25",
            "projects/?page_size=25", "job_templates/?page_size=25",
            "jobs/?page_size=25&order_by=-id", "unified_jobs/?page_size=25&order_by=-id",
            "activity_stream/?page_size=25&order_by=-id",
        ],
        "warn_p95_ms": 1000,
        "fail_p95_ms": 5000,
    },
    "api_concurrency": {
        "workers": 8,
        "duration_s": 15,
        "endpoints": ["ping/", "job_templates/?page_size=25", "unified_jobs/?page_size=25&order_by=-id"],
        "warn_error_rate_pct": 0.1,
        "fail_error_rate_pct": 5,
    },
    "job_history": {"days": 7, "sample": 200, "warn_failure_rate_pct": 25, "warn_queue_p95_s": 60},
    "cluster": {"warn_requested_pct": 85, "warn_ephemeral_gib": 10},
    "workspace": {
        "organization": None,  # use an existing organization by name instead of creating one
        "module": "ping",
    },
    "crud": {"hosts": 25},
    "project": {
        "scm_url": "https://github.com/ansible/ansible-tower-samples",
        "scm_branch": "",
        "playbook": "hello_world.yml",
        "timeout_s": 600,
    },
    "jobs": {"timeout_s": 600, "warn_queue_s": 30, "fail_queue_s": 300},
    "schedule": {"lead_s": 75, "timeout_s": 300},
    "job_throughput": {"jobs": 10, "timeout_s": 900},
    "event_throughput": {"hosts": 50, "forks": 10, "timeout_s": 900},
    "chaos": {"recovery_timeout_s": 900, "poll_s": 1.0, "repeat": 1, "settle_s": 10},
}


def merge_options(*layers: dict[str, Any] | None) -> dict[str, Any]:
    result = copy.deepcopy(DEFAULT_OPTIONS)
    for layer in layers:
        for section, values in (layer or {}).items():
            if isinstance(values, dict) and isinstance(result.get(section), dict):
                result[section].update(values)
            else:
                result[section] = values
    return result


def parse_set(assignments: list[str]) -> dict[str, Any]:
    """Turn ``['api_latency.iterations=50']`` into ``{'api_latency': {'iterations': 50}}``."""
    import yaml

    out: dict[str, Any] = {}
    for item in assignments:
        if "=" not in item or "." not in item.split("=", 1)[0]:
            raise ValueError(f"--set expects section.key=value, got {item!r}")
        key, raw = item.split("=", 1)
        section, name = key.split(".", 1)
        out.setdefault(section, {})[name] = yaml.safe_load(raw)
    return out


@dataclass
class RunContext:
    target: Target
    platform: Platform
    level: Level
    run_id: str
    options: dict[str, Any]
    api: ApiClient | None = None
    kube: Any = None
    env: dict[str, Any] = field(default_factory=dict)
    shared: dict[str, Any] = field(default_factory=dict)
    keep_objects: bool = False
    log: Callable[[str], None] = lambda msg: None
    _cleanup: list[tuple[str, str]] = field(default_factory=list)
    _workspace: Workspace | None = None

    def opt(self, section: str, key: str) -> Any:
        return self.options.get(section, {}).get(key)

    @property
    def name_prefix(self) -> str:
        return f"awx-tester-{self.run_id}"

    # -- object lifecycle --------------------------------------------------------------

    def track(self, url: str, label: str = "") -> None:
        """Remember an object this run created so it is deleted afterwards."""
        self._cleanup.append((url, label or url))

    def create(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert self.api is not None
        obj = self.api.post(path, payload)
        self.track(obj["url"], f"{obj.get('type', path)} {obj.get('name', obj.get('id'))}")
        return obj

    def cleanup(self) -> list[str]:
        """Delete tracked objects newest first. Returns problems, never raises."""
        problems: list[str] = []
        if self.keep_objects or self.api is None:
            self._cleanup.clear()
            return problems
        while self._cleanup:
            url, label = self._cleanup.pop()
            for attempt in range(10):
                try:
                    self.api.delete(url)
                    break
                except ApiError as exc:
                    # 409: still running or in use; give it a moment
                    if exc.status == 409 and attempt < 9:
                        time.sleep(3)
                        continue
                    problems.append(f"could not delete {label}: {exc}")
                    break
        return problems

    def workspace(self) -> Workspace:
        if self._workspace is None:
            self._workspace = Workspace.create(self)
        return self._workspace


@dataclass
class Workspace:
    """An organization, a localhost inventory and a machine credential owned by this run."""

    organization: dict[str, Any]
    inventory: dict[str, Any]
    host: dict[str, Any]
    credential: dict[str, Any]

    LOCAL_VARS = (
        "ansible_connection: local\n"
        "ansible_python_interpreter: '{{ ansible_playbook_python }}'\n"
    )

    @classmethod
    def create(cls, ctx: RunContext) -> Workspace:
        api = ctx.api
        assert api is not None
        prefix = ctx.name_prefix
        org_name = ctx.opt("workspace", "organization")
        if org_name:
            org = api.find_one("organizations/", name=org_name)
            if not org:
                raise RuntimeError(f"organization {org_name!r} (workspace.organization) not found")
        else:
            try:
                org = ctx.create("organizations/", {
                    "name": prefix, "description": "Created by awx-tester; safe to delete"})
            except ApiError as exc:
                raise RuntimeError(
                    f"could not create an organization ({exc.status}). On platforms where the gateway "
                    "owns organizations (AAP 2.5+), set workspace.organization to an existing one."
                ) from exc
        inventory = ctx.create("inventories/", {
            "name": f"{prefix}-local", "organization": org["id"],
            "description": "Created by awx-tester; safe to delete",
        })
        host = ctx.create(f"inventories/{inventory['id']}/hosts/", {
            "name": "localhost", "variables": cls.LOCAL_VARS})
        ssh = api.find_one("credential_types/", namespace="ssh")
        if not ssh:
            raise RuntimeError("the Machine credential type was not found")
        credential = ctx.create("credentials/", {
            "name": f"{prefix}-machine", "organization": org["id"],
            "credential_type": ssh["id"], "inputs": {"username": "awx-tester"},
        })
        return cls(org, inventory, host, credential)

    def launch_adhoc(self, ctx: RunContext, *, inventory_id: int | None = None, module: str | None = None,
                     args: str = "", forks: int = 0, limit: str = "") -> dict[str, Any]:
        assert ctx.api is not None
        inv = inventory_id or self.inventory["id"]
        job = ctx.api.post(f"inventories/{inv}/ad_hoc_commands/", {
            "module_name": module or ctx.opt("workspace", "module"),
            "module_args": args,
            "credential": self.credential["id"],
            "forks": forks,
            "limit": limit,
        })
        ctx.track(job["url"], f"ad hoc command {job['id']}")
        return job
