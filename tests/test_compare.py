import pytest

from awx_tester.compare import CHANGED, IMPROVED, REGRESSED, SAME, compare, metric_verdict, to_dict
from awx_tester.model import Status
from awx_tester.output.html import comparison_to_html

from .conftest import make_report


@pytest.mark.parametrize(
    "base,other,better,unit,expected",
    [
        (100, 150, "lower", "ms", REGRESSED),
        (100, 50, "lower", "ms", IMPROVED),
        (100, 105, "lower", "ms", SAME),  # within threshold
        (2, 3, "lower", "ms", SAME),  # within the noise floor despite +50 %
        (10, 20, "higher", "req/s", IMPROVED),
        (10, 20, "none", "", CHANGED),
    ],
)
def test_metric_verdict(base, other, better, unit, expected):
    assert metric_verdict(base, other, better, unit, 10.0)[0] == expected


def test_compare_finds_regressions_and_environment_changes():
    base = make_report(version="24.6.1", p95=100, nodes=3)
    other = make_report(version="24.6.2", p95=300, nodes=2, status=Status.WARN)
    cmp = compare([base, other])
    metrics = {r.key: r for r in cmp.metrics}
    assert metrics["perf.api_latency:p95_ms"].verdicts == [SAME, REGRESSED]
    assert metrics["perf.api_latency:p95_ms"].deltas[1] == pytest.approx(200.0)
    statuses = {r.key: r for r in cmp.statuses}
    assert statuses["perf.api_latency"].verdicts[1] == REGRESSED
    env = {r.key: r for r in cmp.environment}
    assert env["platform.version"].verdicts[1] == CHANGED
    assert env["cluster.nodes"].values == [3, 2]
    assert cmp.has_regressions
    assert cmp.counts(1)[REGRESSED] == 2
    assert to_dict(cmp)["labels"][0].startswith("lab 24.6.1")


def test_compare_warns_on_different_options():
    cmp = compare([make_report(), make_report(options={"api_latency": {"iterations": 99}})])
    assert any("different options" in w for w in cmp.warnings)


def test_compare_html_renders():
    html = comparison_to_html(compare([make_report(), make_report(p95=500), make_report(p95=50)]))
    assert "v-regressed" in html and "v-improved" in html


def test_compare_needs_two():
    with pytest.raises(ValueError):
        compare([make_report()])
