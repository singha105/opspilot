import json
from pathlib import Path

import pytest
from kubernetes.client.exceptions import ApiException
from pydantic import BaseModel, ValidationError

from opspilot.mcp_servers.common import (
    AuditLog,
    ToolError,
    ToolRunner,
    cap,
    check_namespace,
    redact,
    redact_text,
    to_tool_error,
)

# ---- redaction: one test per pattern ------------------------------------------------

# Fixtures are assembled at runtime so no literal credential-shaped string sits in the repo
# (the detect-private-key hook would rightly reject one).
JWT = ".".join(["eyJhbGciOiJIUzI1NiJ9", "eyJzdWIiOiIxMjM0NTY3ODkwIn0", "dozjgNryP4J3jVmNHl0w5N"])
PEM = "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEowIBAAKCAQEA7\n-----END RSA " + "PRIVATE KEY-----"


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        ("Authorization: Bearer abcdef0123456789.zyx", "abcdef0123456789"),
        ("connecting to postgres://shop:hunter2pass@db:5432/orders", "hunter2pass"),
        ("redis url rediss://default:s3cr3tvalue@redis:6380/0", "s3cr3tvalue"),
        ("key AKIAIOSFODNN7EXAMPLE used", "AKIAIOSFODNN7EXAMPLE"),
        ("aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "wJalrXUtnFEMI"),
        (f"id {JWT}", "eyJzdWIiOiIxMjM0NTY3ODkwIn0"),
        (PEM, "MIIEowIBAAKCAQEA7"),
        ("password=correct-horse-battery", "correct-horse-battery"),
        ("secret: tops3cret", "tops3cret"),
        ("api_key=ak_live_12345", "ak_live_12345"),
        ('{"token": "tok-abcdef"}', "tok-abcdef"),
        ("callback?state=1&access_token=zzz999&x=1", "zzz999"),
    ],
)
def test_redaction_patterns(raw: str, secret: str) -> None:
    redacted = redact_text(raw)
    assert secret not in redacted
    assert "REDACTED" in redacted


def test_redaction_leaves_normal_text_alone() -> None:
    text = "call to redis failed: ConnectionRefusedError [Errno 111] latency_ms=3.2 status=503"
    assert redact_text(text) == text
    assert redact_text("MEMORY_BALLAST_MB=300") == "MEMORY_BALLAST_MB=300"


def test_redact_walks_nested_data() -> None:
    data = {"a": ["password=x1y2z3", {"b": "Bearer abcdefghijkl"}], "n": 5}
    out = redact(data)
    assert out["n"] == 5
    assert "x1y2z3" not in json.dumps(out)
    assert "abcdefghijkl" not in json.dumps(out)


# ---- output cap ------------------------------------------------------------------------


def test_cap_small_result_not_truncated() -> None:
    text = cap({"pods": [1, 2, 3]}, hint="narrow it")
    assert json.loads(text) == {"pods": [1, 2, 3], "truncated": False}


def test_cap_truncates_largest_list_and_marks_it() -> None:
    data = {"lines": [f"line {i} " + "x" * 80 for i in range(500)], "pod": "p"}
    text = cap(data, hint="use contains= or smaller tail_lines", limit=6000)
    assert len(text) <= 6000
    parsed = json.loads(text)
    assert parsed["truncated"] is True
    assert parsed["hint"] == "use contains= or smaller tail_lines"
    assert parsed["pod"] == "p"
    assert parsed["lines"][0].startswith("line 0 ")  # keeps the head of the list
    assert 0 < len(parsed["lines"]) < 500


def test_cap_clips_huge_strings() -> None:
    text = cap({"blob": "y" * 50_000}, hint="h", limit=2000)
    assert len(text) <= 2000
    assert json.loads(text)["truncated"] is True


# ---- errors ---------------------------------------------------------------------------


def test_api_exceptions_map_to_clean_errors() -> None:
    forbidden = to_tool_error(ApiException(status=403, reason="Forbidden")).payload()
    assert forbidden["error"]["type"] == "forbidden"
    assert "Traceback" not in json.dumps(forbidden)
    assert to_tool_error(ApiException(status=404, reason="Not Found")).type == "not_found"
    assert to_tool_error(ApiException(status=500, reason="Boom")).type == "api_error"
    assert to_tool_error(ApiException(status=400, reason="Bad")).type == "bad_request"


def test_timeout_validation_and_unknown_errors() -> None:
    assert to_tool_error(TimeoutError()).type == "timeout"

    class M(BaseModel):
        x: int

    with pytest.raises(ValidationError) as info:
        M(x="nope")  # type: ignore[arg-type]
    assert to_tool_error(info.value).type == "invalid_argument"
    unknown = to_tool_error(RuntimeError("secret internals"))
    assert unknown.type == "internal_error"
    assert "secret internals" not in unknown.message


def test_check_namespace() -> None:
    check_namespace("shop", ["shop"])
    with pytest.raises(ToolError) as info:
        check_namespace("kube-system", ["shop"])
    assert info.value.type == "namespace_not_allowed"


# ---- runner and audit ------------------------------------------------------------------


def test_runner_audits_success_and_failure(tmp_path: Path) -> None:
    log = tmp_path / "audit.jsonl"
    runner = ToolRunner(AuditLog(log, "opspilot-k8s", "live", run_id="run-test"))
    ok = runner.run("list_pods", {"namespace": "shop"}, lambda: {"pods": ["a"]})
    assert json.loads(ok)["pods"] == ["a"]

    def boom() -> None:
        raise ApiException(status=403, reason="Forbidden")

    bad = runner.run("get_configmap", {"namespace": "shop", "token": "password=abc123"}, boom)
    assert json.loads(bad)["error"]["type"] == "forbidden"

    def not_allowed() -> None:
        check_namespace("kube-system", ["shop"])

    rejected = runner.run("list_pods", {"namespace": "kube-system"}, not_allowed)
    assert json.loads(rejected)["error"]["type"] == "namespace_not_allowed"

    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert [r["status"] for r in records] == ["ok", "error", "rejected"]
    assert {r["run_id"] for r in records} == {"run-test"}
    assert {r["mode"] for r in records} == {"live"}
    assert all(r["bytes"] > 0 and r["duration_ms"] >= 0 for r in records)
    assert "abc123" not in log.read_text()
    assert set(records[0]) >= {"ts", "server", "tool", "args", "duration_ms", "bytes", "status"}


def test_runner_redacts_output(tmp_path: Path) -> None:
    runner = ToolRunner(AuditLog(tmp_path / "a.jsonl", "s", "live"))
    text = runner.run("get_pod_logs", {}, lambda: {"lines": ["DSN postgres://u:pw12345@db/x"]})
    assert "pw12345" not in text


def test_run_id_from_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPSPILOT_RUN_ID", "inc-20261007-1403")
    assert AuditLog(tmp_path / "a.jsonl", "s", "replay").run_id == "inc-20261007-1403"
