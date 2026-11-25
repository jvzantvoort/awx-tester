import pytest

from awx_tester.cluster import classify_component, parse_bytes, parse_cpu
from awx_tester.config import ConfigError, load_target, target_from_dict
from awx_tester.context import merge_options, parse_set
from awx_tester.model import Level
from awx_tester.registry import all_checks, select
from awx_tester.safety import SafetyError, authorize


def target(**kw):
    return target_from_dict("lab", {"type": "awx", "url": "https://awx.example.com/", **kw})


def test_readonly_is_default_and_always_allowed():
    t = target()
    assert t.max_level is Level.READONLY
    authorize(t, Level.READONLY)
    with pytest.raises(SafetyError):
        authorize(t, Level.MUTATING)


def test_protected_pins_readonly():
    t = target(max_level="disruptive", protected=True)
    with pytest.raises(SafetyError, match="protected"):
        authorize(t, Level.MUTATING, confirm="lab")


def test_disruptive_needs_matching_confirmation():
    t = target(max_level="disruptive")
    authorize(t, Level.MUTATING)
    authorize(t, Level.DISRUPTIVE, confirm="lab")
    with pytest.raises(SafetyError):
        authorize(t, Level.DISRUPTIVE, confirm="other")
    with pytest.raises(SafetyError):
        authorize(t, Level.DISRUPTIVE, prompt=lambda _: "nope")
    authorize(t, Level.DISRUPTIVE, prompt=lambda _: "lab")


def test_config_file(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_PW", "s3cret")
    path = tmp_path / "targets.yml"
    path.write_text(
        "defaults:\n  verify_ssl: false\n  options: {api_latency: {iterations: 3}}\n"
        "targets:\n  lab:\n    type: ascender\n    url: https://a.example.com\n"
        "    auth: {username: admin, password_env: LAB_PW}\n"
        "    kubernetes: {context: c, namespace: ascender}\n    max_level: mutating\n"
    )
    t = load_target(path, "lab")
    assert t.type == "ascender" and t.auth.password == "s3cret" and t.verify_ssl is False
    assert t.options["api_latency"]["iterations"] == 3
    assert "s3cret" not in str(t.describe())
    with pytest.raises(ConfigError):
        load_target(path, "missing")


def test_bad_type_and_missing_env(monkeypatch):
    with pytest.raises(ConfigError):
        target_from_dict("x", {"type": "tower", "url": "u"})
    monkeypatch.delenv("NOPE", raising=False)
    with pytest.raises(ConfigError):
        target_from_dict("x", {"type": "awx", "url": "u", "auth": {"password_env": "NOPE"}})


def test_options_merge_and_set():
    opts = merge_options({"api_latency": {"iterations": 3}}, parse_set(["api_latency.warn_p95_ms=50"]))
    assert opts["api_latency"]["iterations"] == 3
    assert opts["api_latency"]["warn_p95_ms"] == 50
    assert opts["api_latency"]["endpoints"]  # defaults kept
    with pytest.raises(ValueError):
        parse_set(["nodot=1"])


def test_selection_respects_level_and_dependencies():
    ro = select(Level.READONLY)
    assert ro and all(c.level is Level.READONLY for c in ro)
    picked = [c.id for c in select(Level.MUTATING, only=["func.workflow"])]
    assert picked == ["func.workspace", "func.project_sync", "func.job_template", "func.workflow"]
    assert not select(Level.READONLY, only=["func.*"])
    assert all("slow" not in c.tags for c in select(Level.MUTATING, skip=["tag:slow"]))


def test_check_ids_are_unique_and_well_formed():
    ids = [c.id for c in all_checks()]
    assert len(ids) == len(set(ids))
    assert all(c.count(".") == 1 and c == c.lower() for c in ids)
    ranks = [c.level.rank for c in all_checks()]
    assert ranks == sorted(ranks)  # readonly first, disruptive last


@pytest.mark.parametrize("q,cores", [("250m", 0.25), ("4", 4.0), ("1500000n", 0.0015), (None, 0.0)])
def test_parse_cpu(q, cores):
    assert parse_cpu(q) == pytest.approx(cores)


@pytest.mark.parametrize(
    "q,b", [("1Gi", 2**30), ("16108944Ki", 16108944 * 1024), ("1G", 1e9), ("5036520240", 5036520240)]
)
def test_parse_bytes(q, b):
    assert parse_bytes(q) == b


@pytest.mark.parametrize("name,labels,component", [
    ("awx-web-5d9c-abc", {"app.kubernetes.io/name": "awx-web"}, "web"),
    ("awx-task-5d9c-abc", {"app.kubernetes.io/name": "awx-task"}, "task"),
    ("awx-postgres-15-0", {}, "postgres"),
    ("awx-operator-controller-manager-1", {}, "operator"),
    ("automation-job-12-xyz", {}, "job"),
    ("ascender-app-web-7f-x", {"app.kubernetes.io/name": "ascender-app-web"}, "web"),
])
def test_classify_component(name, labels, component):
    assert classify_component(name, labels) == component
