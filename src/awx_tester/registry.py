"""Check registration.

A check is a function decorated with :func:`check`. It receives the run context
and an :class:`Outcome` to record metrics, attributes and its verdict::

    @check("platform.ping", "API answers ping", requires=("api",))
    def ping(ctx, out):
        out.metric("latency_ms", 12.3, "ms", Better.LOWER)

Check ids are the stable keys that comparisons match on: never reuse or rename
an id for a check that measures something different.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from .model import Better, Level, Metric, Status

REQUIREMENTS = {
    "api": "the controller API is reachable",
    "auth": "API credentials are configured and valid",
    "superuser": "the API user is a superuser or system auditor",
    "kube": "the Kubernetes cluster is reachable",
    "namespace": "the installation's namespace is known",
}


class Skip(Exception):
    """Raise from a check to mark it skipped, with the reason as message."""


@dataclass
class CheckSpec:
    id: str
    title: str
    func: Callable[[Any, Outcome], None]
    level: Level = Level.READONLY
    tags: tuple[str, ...] = ()
    requires: tuple[str, ...] = ("api",)
    depends: tuple[str, ...] = ()
    order: int = 0

    @property
    def category(self) -> str:
        return self.id.split(".", 1)[0]

    @property
    def description(self) -> str:
        return (self.func.__doc__ or "").strip()

    def matches(self, pattern: str) -> bool:
        if pattern.startswith("tag:"):
            return pattern[4:] in self.tags
        if pattern.startswith("level:"):
            return pattern[6:] == self.level.value
        return fnmatch.fnmatch(self.id, pattern) or fnmatch.fnmatch(self.category, pattern)


REGISTRY: dict[str, CheckSpec] = {}


def check(
    id: str,
    title: str,
    *,
    level: Level = Level.READONLY,
    tags: Iterable[str] = (),
    requires: Iterable[str] = ("api",),
    depends: Iterable[str] = (),
):
    def decorator(func):
        if id in REGISTRY:
            raise ValueError(f"duplicate check id {id}")
        unknown = set(requires) - set(REQUIREMENTS)
        if unknown:
            raise ValueError(f"{id}: unknown requirements {unknown}")
        REGISTRY[id] = CheckSpec(
            id=id, title=title, func=func, level=level, tags=tuple(tags),
            requires=tuple(requires), depends=tuple(depends), order=len(REGISTRY),
        )
        return func

    return decorator


def all_checks() -> list[CheckSpec]:
    from . import checks  # noqa: F401  (imports register the checks)

    return sorted(REGISTRY.values(), key=lambda c: (c.level.rank, c.order))


def select(level: Level, only: list[str] | None = None, skip: list[str] | None = None) -> list[CheckSpec]:
    chosen = [c for c in all_checks() if level.allows(c.level)]
    if only:
        wanted = [c for c in chosen if any(c.matches(p) for p in only)]
        # pull in dependencies of what was asked for
        ids = {c.id for c in wanted}
        changed = True
        while changed:
            changed = False
            for c in list(wanted):
                for dep in c.depends:
                    if dep not in ids and dep in REGISTRY and level.allows(REGISTRY[dep].level):
                        wanted.append(REGISTRY[dep])
                        ids.add(dep)
                        changed = True
        chosen = [c for c in chosen if c.id in ids]
    if skip:
        chosen = [c for c in chosen if not any(c.matches(p) for p in skip)]
    return chosen


@dataclass
class Outcome:
    """What a check records while it runs."""

    status: Status = Status.PASS
    messages: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)
    metrics: list[Metric] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)

    def metric(self, name: str, value: float | int | None, unit: str = "",
               better: Better = Better.NONE, description: str = "") -> None:
        if value is None:
            return
        self.metrics.append(Metric(name, round(float(value), 4), unit, better, description))

    def attr(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def detail(self, text: str) -> None:
        self.details.append(text)

    def info(self, msg: str) -> None:
        self.messages.append(msg)

    def _raise(self, status: Status, msg: str) -> None:
        if status.severity > self.status.severity:
            self.status = status
        self.messages.append(msg)

    def warn(self, msg: str) -> None:
        self._raise(Status.WARN, msg)

    def fail(self, msg: str) -> None:
        self._raise(Status.FAIL, msg)

    def threshold(self, name: str, value: float, warn: float | None, fail: float | None,
                  unit: str = "", higher_is_worse: bool = True) -> None:
        """Warn or fail when ``value`` crosses a configured limit."""
        def crossed(limit: float | None) -> bool:
            if limit is None:
                return False
            return value > limit if higher_is_worse else value < limit

        if crossed(fail):
            self.fail(f"{name} {value:g}{unit} beyond fail limit {fail:g}{unit}")
        elif crossed(warn):
            self.warn(f"{name} {value:g}{unit} beyond warn limit {warn:g}{unit}")
