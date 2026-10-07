import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from opspilot.agent.approval import (
    MAX_TTL_S,
    ApprovalError,
    NonceStore,
    action_hash,
    canonical_json,
    mint_approval_token,
    peek_claims,
    verify_approval_token,
)
from opspilot.config import Settings
from opspilot.mcp_servers.actions.executor import flatten, spec_diff
from opspilot.mcp_servers.actions.models import parse_cpu, parse_memory
from opspilot.mcp_servers.actions.server import ActionTools, build_server
from opspilot.mcp_servers.common import AuditLog, ToolError, ToolRunner

SECRET = "s" * 64
SETTINGS = Settings(_env_file=None, approval_secret=SECRET)  # type: ignore[call-arg]
NOW = 1_800_000_000.0
RESTART = {"type": "restart_deployment", "namespace": "shop", "deployment": "payments-api"}


@pytest.fixture
def nonces(tmp_path: Path) -> NonceStore:
    return NonceStore(tmp_path / "nonces.sqlite")


def mint(action: dict[str, Any] = RESTART, ttl: int = 600, now: float = NOW) -> str:
    return mint_approval_token(action_hash(action), "alice", ttl, settings=SETTINGS, now=now)


# ---- approval tokens ---------------------------------------------------------------------


def test_action_hash_is_canonical() -> None:
    reordered = {"deployment": "payments-api", "type": "restart_deployment", "namespace": "shop"}
    assert action_hash(RESTART) == action_hash(reordered)
    assert (
        canonical_json(RESTART)
        == '{"deployment":"payments-api","namespace":"shop","type":"restart_deployment"}'
    )
    assert action_hash(RESTART) != action_hash({**RESTART, "deployment": "orders-api"})


def test_valid_token_verifies_once(nonces: NonceStore) -> None:
    token = mint()
    claims = verify_approval_token(
        token, action_hash(RESTART), nonces, settings=SETTINGS, now=NOW + 1
    )
    assert claims.approver == "alice"
    with pytest.raises(ApprovalError, match="already used"):
        verify_approval_token(token, action_hash(RESTART), nonces, settings=SETTINGS, now=NOW + 2)


def test_peek_does_not_consume(nonces: NonceStore) -> None:
    token = mint()
    verify_approval_token(
        token, action_hash(RESTART), nonces, consume=False, settings=SETTINGS, now=NOW
    )
    verify_approval_token(token, action_hash(RESTART), nonces, settings=SETTINGS, now=NOW)
    with pytest.raises(ApprovalError, match="already used"):
        verify_approval_token(
            token, action_hash(RESTART), nonces, consume=False, settings=SETTINGS, now=NOW
        )
    assert peek_claims(token)["approver"] == "alice"
    assert peek_claims("garbage") == {}


@pytest.mark.parametrize(
    ("token_fn", "reason"),
    [
        (lambda: None, "missing"),
        (lambda: "", "missing"),
        (lambda: "not-a-token", "malformed"),
        (lambda: "v2." + mint().split(".", 1)[1], "version"),
        (lambda: mint().rsplit(".", 1)[0] + ".AAAA", "signature"),  # tampered signature
        (lambda: _tamper_claims(mint()), "signature"),  # tampered claims
        (lambda: mint(now=NOW - 700), "expired"),  # expired
        (lambda: mint({**RESTART, "deployment": "orders-api"}), "different action"),  # wrong hash
        (lambda: mint(now=NOW + 300), "exceeds 10 minutes"),  # expiry too far ahead
    ],
)
def test_bad_tokens_are_rejected(nonces: NonceStore, token_fn: Any, reason: str) -> None:
    with pytest.raises(ApprovalError, match=reason):
        verify_approval_token(token_fn(), action_hash(RESTART), nonces, settings=SETTINGS, now=NOW)


def _tamper_claims(token: str) -> str:
    import base64

    version, body, sig = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    claims["by"] = "mallory"
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"{version}.{forged}.{sig}"


def test_mint_refuses_long_ttl_missing_secret_and_approver() -> None:
    with pytest.raises(ApprovalError, match="ttl_s"):
        mint(ttl=MAX_TTL_S + 1)
    with pytest.raises(ApprovalError, match="approver"):
        mint_approval_token("h", "  ", settings=SETTINGS)
    with pytest.raises(ApprovalError, match="not configured"):
        mint_approval_token("h", "alice", settings=Settings(_env_file=None, approval_secret=None))  # type: ignore[call-arg]


# ---- the actions server ----------------------------------------------------------------------


class FakeExecutor:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.planned: list[str] = []

    def plan(self, action: Any, digest: str) -> dict[str, Any]:
        self.planned.append(digest)
        return {"action_hash": digest, "diff": [], "dry_run": "passed"}

    def execute(self, action: Any, digest: str) -> dict[str, Any]:
        self.executed.append(digest)
        return {"executed": True, "action_hash": digest}


@pytest.fixture
def actions(tmp_path: Path, nonces: NonceStore) -> tuple[ActionTools, FakeExecutor, Path]:
    executor = FakeExecutor()
    log = tmp_path / "audit.jsonl"
    tools = ActionTools(
        executor, ToolRunner(AuditLog(log, "opspilot-actions", "live")), nonces, ["shop"], SETTINGS
    )
    return tools, executor, log


def fresh_token(action: dict[str, Any] = RESTART) -> str:
    return mint_approval_token(action_hash(action), "alice", settings=SETTINGS)


def test_plan_needs_no_token(actions: tuple[ActionTools, FakeExecutor, Path]) -> None:
    tools, executor, _ = actions
    out = json.loads(tools.plan_action(RESTART))
    assert out["action_hash"] == action_hash(RESTART)
    assert executor.executed == []


def test_execute_with_valid_token_runs_once(
    actions: tuple[ActionTools, FakeExecutor, Path],
) -> None:
    tools, executor, log = actions
    token = fresh_token()
    first = json.loads(tools.execute_action(RESTART, token))
    assert first["executed"] is True
    assert first["approved_by"] == "alice"
    again = json.loads(tools.execute_action(RESTART, token))
    assert again["error"]["type"] == "approval_invalid"
    assert "already used" in again["error"]["message"]
    assert len(executor.executed) == 1
    assert token not in log.read_text()  # the token itself is never logged


@pytest.mark.parametrize(
    ("action", "token_for", "error_type"),
    [
        (RESTART, None, "approval_required"),
        (RESTART, {**RESTART, "deployment": "orders-api"}, "approval_invalid"),
        ({**RESTART, "namespace": "kube-system"}, "same", "namespace_not_allowed"),
        (
            {"type": "scale_deployment", "namespace": "shop", "deployment": "redis", "replicas": 9},
            "same",
            "out_of_bounds",
        ),
        (
            {
                "type": "patch_container_resources",
                "namespace": "shop",
                "deployment": "payments-api",
                "container": "app",
                "memory_limit": "2Gi",
            },
            "same",
            "out_of_bounds",
        ),
        (
            {
                "type": "patch_container_resources",
                "namespace": "shop",
                "deployment": "payments-api",
                "container": "app",
                "cpu_limit": "1500m",
            },
            "same",
            "out_of_bounds",
        ),
        (
            {"type": "delete_namespace", "namespace": "shop", "deployment": "x"},
            "same",
            "action_not_allowed",
        ),
        ({**RESTART, "extra": "field"}, "same", "invalid_argument"),
    ],
)
def test_rejections_never_execute_and_are_audited(
    actions: tuple[ActionTools, FakeExecutor, Path],
    action: dict[str, Any],
    token_for: Any,
    error_type: str,
) -> None:
    tools, executor, log = actions
    token = None if token_for is None else fresh_token(action if token_for == "same" else token_for)
    out = json.loads(tools.execute_action(action, token))
    assert out["error"]["type"] == error_type
    assert executor.executed == []
    record = json.loads(log.read_text().splitlines()[-1])
    assert record["tool"] == "execute_action"
    assert record["status"] == "rejected"


def test_expired_token_rejected_by_server(actions: tuple[ActionTools, FakeExecutor, Path]) -> None:
    tools, executor, _ = actions
    an_hour_ago = time.time() - 3600
    old = mint_approval_token(action_hash(RESTART), "alice", 60, settings=SETTINGS, now=an_hour_ago)
    out = json.loads(tools.execute_action(RESTART, old))
    assert out["error"]["type"] == "approval_invalid"
    assert "expired" in out["error"]["message"]
    assert executor.executed == []


def test_quantity_parsing() -> None:
    assert parse_memory("512Mi") == 512 * 1024**2
    assert parse_memory("1G") == 10**9
    assert parse_cpu("500m") == 500
    assert parse_cpu("0.5") == 500
    with pytest.raises(ToolError):
        parse_memory("lots")
    with pytest.raises(ToolError):
        parse_cpu("2 cores")


def test_spec_diff_reports_template_and_replica_changes() -> None:
    before = {
        "spec": {
            "replicas": 1,
            "template": {
                "metadata": {"labels": {"a": "b", "pod-template-hash": "x"}},
                "spec": {"containers": [{"name": "app", "image": "v1"}]},
            },
        }
    }
    after = {
        "spec": {
            "replicas": 0,
            "template": {
                "metadata": {"labels": {"a": "b", "pod-template-hash": "y"}},
                "spec": {"containers": [{"name": "app", "image": "v2"}]},
            },
        }
    }
    assert spec_diff(before, after) == [
        {"path": "replicas", "before": 1, "after": 0},
        {"path": "template.spec.containers[app].image", "before": "v1", "after": "v2"},
    ]
    assert flatten({"a": [{"name": "x", "v": 1}, 5]}) == {"a[x].name": "x", "a[x].v": 1, "a[1]": 5}


def test_actions_server_catalog(actions: tuple[ActionTools, FakeExecutor, Path]) -> None:
    listed = asyncio.run(build_server(actions[0]).list_tools())
    assert {t.name for t in listed} == {"plan_action", "execute_action"}
