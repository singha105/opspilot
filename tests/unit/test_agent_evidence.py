import json
from pathlib import Path

import pytest

from opspilot.agent.evidence import EXCERPT_MAX, SUMMARY_MAX, notable_log_lines, summarize

FIXTURES = Path(__file__).parents[2] / "evals" / "fixtures"


@pytest.mark.parametrize("fixture", sorted(p.stem for p in FIXTURES.glob("*.json")))
def test_every_recorded_output_summarizes_within_limits(fixture: str) -> None:
    calls = json.loads((FIXTURES / f"{fixture}.json").read_text())["calls"]
    for key, output in calls.items():
        spec = json.loads(key)
        summary, excerpt, is_error = summarize(spec["tool"], spec["args"], json.dumps(output))
        assert 0 < len(summary) <= SUMMARY_MAX, key
        assert len(excerpt) <= EXCERPT_MAX, key
        assert is_error == ("error" in output), key


def _call(fixture: str, tool: str, **args: object) -> str:
    calls = json.loads((FIXTURES / f"{fixture}.json").read_text())["calls"]
    for key, output in calls.items():
        spec = json.loads(key)
        if spec["tool"] == tool and spec["args"] == args:
            return json.dumps(output)
    raise KeyError(tool)


def test_pod_and_describe_summaries_name_the_failure() -> None:
    pods, _, _ = summarize(
        "list_pods", {"namespace": "shop"}, _call("oom-payments", "list_pods", namespace="shop")
    )
    assert "unhealthy" in pods
    assert "OOMKilled" in pods
    healthy, _, _ = summarize("list_pods", {}, _call("healthy", "list_pods", namespace="shop"))
    assert healthy == "4 pods, all ready, no failure reasons."


def test_events_and_history_summaries() -> None:
    events, _, _ = summarize(
        "get_events",
        {},
        _call("imagepull-orders-tag", "get_events", namespace="shop", since_minutes=120),
    )
    assert "warnings" in events
    assert "Failed" in events
    history, _, _ = summarize(
        "get_rollout_history",
        {},
        _call("imagepull-orders-tag", "get_rollout_history", namespace="shop", name="orders-api"),
    )
    assert "does-not-exist" in history


def test_error_outputs_are_marked() -> None:
    summary, excerpt, is_error = summarize(
        "describe_pod",
        {"name": "x"},
        '{"error":{"type":"not_recorded","message":"no","hint":"broader"}}',
    )
    assert is_error
    assert "not_recorded" in summary
    assert excerpt == "broader"
    assert summarize("list_pods", {}, "not json")[2] is True
    assert summarize("list_pods", {}, '{"unexpected": 1}')[0] == "No pods match."


def test_notable_log_lines_dedupes_and_counts() -> None:
    lines = ["ok", "ERROR db down", "ok", "ERROR db down", "fatal: boom"]
    assert notable_log_lines(lines) == ["ERROR db down (x2)", "fatal: boom"]
    assert notable_log_lines(["a", "b"]) == ["a", "b"]  # nothing notable: keep the tail
