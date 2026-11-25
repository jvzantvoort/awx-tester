"""The repetition logic of the disruptive checks, without a cluster."""

from types import SimpleNamespace

import pytest

from awx_tester.checks.resilience import _repeat
from awx_tester.cli import build_parser
from awx_tester.context import merge_options
from awx_tester.model import Status
from awx_tester.registry import Outcome, Skip


def ctx(repeat):
    options = merge_options({"chaos": {"repeat": repeat, "settle_s": 0}})
    return SimpleNamespace(opt=lambda sec, key: options[sec][key], log=lambda msg: None)


def metrics(out):
    return {m.name: m.value for m in out.metrics}


def test_single_run_keeps_plain_metrics():
    out = Outcome()
    _repeat(ctx(1), out, lambda s, i: s.add("recovery_s", 12.0))
    assert metrics(out) == {"recovery_s": 12.0}
    assert out.attributes["repetitions"] == 1


def test_repeat_reports_median_and_worst():
    values = [10.0, 30.0, 12.0]
    out = Outcome()
    _repeat(ctx(3), out, lambda s, i: s.add("recovery_s", values[i]))
    assert metrics(out) == {"recovery_s": 12.0, "recovery_s_max": 30.0}
    assert out.attributes["samples"] == {"recovery_s": values}
    assert out.attributes["repetitions"] == 3
    assert "median of 3" in out.metrics[0].description


def test_failed_repetition_stops_the_series():
    calls = []

    def once(s, i):
        calls.append(i)
        s.add("recovery_s", 5.0)
        if i == 1:
            Outcome.fail(out, "no full recovery")

    out = Outcome()
    _repeat(ctx(4), out, once)
    assert calls == [0, 1]
    assert out.status is Status.FAIL
    assert out.attributes["repetitions"] == 2
    assert metrics(out)["recovery_s"] == 5.0  # measurements so far are still reported


def test_skip_on_first_repetition_skips_the_check():
    out = Outcome()

    def once(s, i):
        raise Skip("not healthy")

    with pytest.raises(Skip):
        _repeat(ctx(3), out, once)


def test_unhealthy_before_a_later_repetition_fails():
    def once(s, i):
        if i == 1:
            raise Skip("the web workload is not healthy")
        s.add("recovery_s", 9.0)

    out = Outcome()
    _repeat(ctx(3), out, once)
    assert out.status is Status.FAIL
    assert "repetition 2" in out.messages[0]
    assert out.attributes["repetitions"] == 1


def test_cli_flag_parses():
    args = build_parser().parse_args(["run", "-t", "x", "--repeat", "3"])
    assert args.repeat == 3
