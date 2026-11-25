"""The canonical result model.

Every run produces exactly one :class:`Report`. All output formats (console,
HTML, xUnit) and all comparisons are rendered from it, so the JSON form of the
report is the contract between runs, clusters and tool versions. Bump
``SCHEMA_VERSION`` when a field changes meaning; additive changes bump the minor
number.
"""

from __future__ import annotations

import enum
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"


class Level(str, enum.Enum):
    """How much a check is allowed to change the target.

    readonly    only reads (API GETs, Kubernetes list/get). Safe for production.
    mutating    creates, runs and deletes its own objects (prefixed awx-tester-),
                which adds jobs to the history and load to the system.
    disruptive  deliberately breaks things (deletes pods) to measure recovery.
    """

    READONLY = "readonly"
    MUTATING = "mutating"
    DISRUPTIVE = "disruptive"

    @property
    def rank(self) -> int:
        return {"readonly": 0, "mutating": 1, "disruptive": 2}[self.value]

    def allows(self, other: Level) -> bool:
        return other.rank <= self.rank


class Status(str, enum.Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    ERROR = "error"
    SKIP = "skip"

    @property
    def severity(self) -> int:
        return {"skip": 0, "pass": 1, "warn": 2, "fail": 3, "error": 4}[self.value]


class Better(str, enum.Enum):
    """Which direction of change is an improvement, used by comparisons."""

    LOWER = "lower"
    HIGHER = "higher"
    NONE = "none"


@dataclass
class Metric:
    name: str
    value: float
    unit: str = ""
    better: Better = Better.NONE
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["better"] = self.better.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Metric:
        return cls(
            name=d["name"],
            value=d["value"],
            unit=d.get("unit", ""),
            better=Better(d.get("better", "none")),
            description=d.get("description", ""),
        )


@dataclass
class CheckResult:
    id: str
    title: str
    category: str
    level: Level
    tags: list[str] = field(default_factory=list)
    status: Status = Status.PASS
    duration_s: float = 0.0
    message: str = ""
    details: list[str] = field(default_factory=list)
    metrics: list[Metric] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "category": self.category,
            "level": self.level.value,
            "tags": list(self.tags),
            "status": self.status.value,
            "duration_s": round(self.duration_s, 4),
            "message": self.message,
            "details": list(self.details),
            "metrics": [m.to_dict() for m in self.metrics],
            "attributes": self.attributes,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CheckResult:
        return cls(
            id=d["id"],
            title=d.get("title", d["id"]),
            category=d.get("category", d["id"].split(".")[0]),
            level=Level(d.get("level", "readonly")),
            tags=list(d.get("tags", [])),
            status=Status(d["status"]),
            duration_s=float(d.get("duration_s", 0.0)),
            message=d.get("message", ""),
            details=list(d.get("details", [])),
            metrics=[Metric.from_dict(m) for m in d.get("metrics", [])],
            attributes=dict(d.get("attributes", {})),
        )


@dataclass
class Report:
    tool: dict[str, Any]
    run: dict[str, Any]
    target: dict[str, Any]
    environment: dict[str, Any]
    results: list[CheckResult]
    schema_version: str = SCHEMA_VERSION

    @property
    def summary(self) -> dict[str, int]:
        counts = {s.value: 0 for s in Status}
        for r in self.results:
            counts[r.status.value] += 1
        counts["total"] = len(self.results)
        return counts

    @property
    def overall(self) -> Status:
        worst = Status.PASS
        for r in self.results:
            if r.status.severity > worst.severity:
                worst = r.status
        return worst

    def result(self, check_id: str) -> CheckResult | None:
        return next((r for r in self.results if r.id == check_id), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "tool": self.tool,
            "run": self.run,
            "target": self.target,
            "environment": self.environment,
            "summary": {**self.summary, "overall": self.overall.value},
            "results": [r.to_dict() for r in self.results],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=False, default=str)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Report:
        major = str(d.get("schema_version", "0")).split(".")[0]
        if major != SCHEMA_VERSION.split(".")[0]:
            raise ValueError(
                f"report schema {d.get('schema_version')} is not compatible with {SCHEMA_VERSION}"
            )
        return cls(
            schema_version=d["schema_version"],
            tool=d.get("tool", {}),
            run=d.get("run", {}),
            target=d.get("target", {}),
            environment=d.get("environment", {}),
            results=[CheckResult.from_dict(r) for r in d.get("results", [])],
        )

    @classmethod
    def load(cls, path: str | Path) -> Report:
        """Load a report from its JSON file or from an HTML report that embeds it."""
        text = Path(path).read_text(encoding="utf-8")
        if text.lstrip().startswith("{"):
            return cls.from_dict(json.loads(text))
        marker = '<script type="application/json" id="awx-tester-report">'
        start = text.find(marker)
        if start < 0:
            raise ValueError(f"{path}: neither a JSON report nor an awx-tester HTML report")
        start += len(marker)
        end = text.index("</script>", start)
        payload = text[start:end].replace("<\\/", "</")
        return cls.from_dict(json.loads(payload))
