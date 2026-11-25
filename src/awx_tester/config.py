"""Target configuration.

A targets file names each installation that can be tested, says what kind of
platform it is, how to reach its API and its Kubernetes cluster, and how far
awx-tester may go on it (``max_level``). See ``examples/targets.example.yml``.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .model import Level

PLATFORM_TYPES = ("awx", "aap", "ascender")

DEFAULT_CONFIG_PATHS = (
    Path("awx-tester.yml"),
    Path("targets.yml"),
    Path("~/.config/awx-tester/targets.yml").expanduser(),
)


class ConfigError(Exception):
    pass


@dataclass
class Auth:
    username: str | None = None
    password: str | None = None
    token: str | None = None
    password_secret: dict[str, str] | None = None  # {name, key[, namespace]}


@dataclass
class KubeTarget:
    context: str | None = None
    namespace: str | None = None
    instance: str | None = None
    kubeconfig: str | None = None
    enabled: bool = True


@dataclass
class Target:
    name: str
    type: str
    url: str
    auth: Auth = field(default_factory=Auth)
    verify_ssl: bool | str = True
    kubernetes: KubeTarget = field(default_factory=KubeTarget)
    labels: dict[str, str] = field(default_factory=dict)
    max_level: Level = Level.READONLY
    protected: bool = False
    options: dict[str, Any] = field(default_factory=dict)
    timeout: float = 30.0

    @property
    def effective_max_level(self) -> Level:
        return Level.READONLY if self.protected else self.max_level

    def describe(self) -> dict[str, Any]:
        """The part of the target that goes into a report (no secrets)."""
        return {
            "name": self.name,
            "type": self.type,
            "url": self.url,
            "namespace": self.kubernetes.namespace,
            "kube_context": self.kubernetes.context,
            "instance": self.kubernetes.instance,
            "labels": dict(self.labels),
            "protected": self.protected,
            "max_level": self.effective_max_level.value,
        }


def _resolve_secret_value(raw: dict[str, Any], key: str) -> str | None:
    """Read ``key`` directly, or ``key_env`` from the environment."""
    if raw.get(key) is not None:
        return str(raw[key])
    env = raw.get(f"{key}_env")
    if env:
        value = os.environ.get(env)
        if value is None:
            raise ConfigError(f"environment variable {env} (auth.{key}_env) is not set")
        return value
    return None


def target_from_dict(name: str, raw: dict[str, Any], defaults: dict[str, Any] | None = None) -> Target:
    defaults = defaults or {}
    merged = copy.deepcopy(defaults)
    for k, v in raw.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = {**merged[k], **v}
        else:
            merged[k] = v
    raw = merged

    ptype = str(raw.get("type", "")).lower()
    if ptype not in PLATFORM_TYPES:
        raise ConfigError(f"target {name}: type must be one of {', '.join(PLATFORM_TYPES)}, got {ptype!r}")
    url = raw.get("url")
    if not url:
        raise ConfigError(f"target {name}: url is required")

    auth_raw = raw.get("auth") or {}
    auth = Auth(
        username=auth_raw.get("username"),
        password=_resolve_secret_value(auth_raw, "password"),
        token=_resolve_secret_value(auth_raw, "token"),
        password_secret=auth_raw.get("password_secret"),
    )

    kube_raw = raw.get("kubernetes")
    if kube_raw is False:
        kube = KubeTarget(enabled=False)
    else:
        kube_raw = kube_raw or {}
        kube = KubeTarget(
            context=kube_raw.get("context"),
            namespace=kube_raw.get("namespace"),
            instance=kube_raw.get("instance"),
            kubeconfig=kube_raw.get("kubeconfig"),
            enabled=bool(kube_raw.get("enabled", True)),
        )

    try:
        max_level = Level(str(raw.get("max_level", "readonly")).lower())
    except ValueError as exc:
        raise ConfigError(f"target {name}: max_level must be readonly, mutating or disruptive") from exc

    return Target(
        name=name,
        type=ptype,
        url=str(url).rstrip("/"),
        auth=auth,
        verify_ssl=raw.get("verify_ssl", True),
        kubernetes=kube,
        labels={str(k): str(v) for k, v in (raw.get("labels") or {}).items()},
        max_level=max_level,
        protected=bool(raw.get("protected", False)),
        options=dict(raw.get("options") or {}),
        timeout=float(raw.get("timeout", 30.0)),
    )


def find_config(explicit: str | None) -> Path | None:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise ConfigError(f"config file {path} does not exist")
        return path
    env = os.environ.get("AWX_TESTER_CONFIG")
    if env:
        return Path(env).expanduser()
    return next((p for p in DEFAULT_CONFIG_PATHS if p.exists()), None)


def load_raw(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict) or not isinstance(data.get("targets"), dict):
        raise ConfigError(f"{path}: expected a mapping with a 'targets' key")
    return data


def list_targets(path: Path) -> dict[str, dict[str, Any]]:
    return load_raw(path)["targets"]


def load_target(path: Path, name: str) -> Target:
    data = load_raw(path)
    if name not in data["targets"]:
        known = ", ".join(sorted(data["targets"])) or "none"
        raise ConfigError(f"target {name!r} not found in {path} (known: {known})")
    return target_from_dict(name, data["targets"][name] or {}, data.get("defaults"))
