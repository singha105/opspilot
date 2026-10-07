"""The gated actions server against the live demo cluster.

Needs: make cluster-up demo-deploy rbac-apply, and OPSPILOT_APPROVAL_SECRET in .secrets/.env.
"""

import json
from pathlib import Path

import pytest

from opspilot.agent.approval import NonceStore, action_hash, mint_approval_token
from opspilot.config import get_settings
from opspilot.faults.injector import Injector, poll
from opspilot.kube import admin_client
from opspilot.mcp_servers.actions.executor import ActionExecutor
from opspilot.mcp_servers.actions.server import ActionTools
from opspilot.mcp_servers.common import AuditLog, ToolRunner
from opspilot.mcp_servers.common.kube import apis_from_kubeconfig
from opspilot.mcp_servers.k8s_readonly.backend import K8sReadBackend

pytestmark = pytest.mark.integration
SETTINGS = get_settings()


@pytest.fixture
def tools(tmp_path: Path) -> ActionTools:
    executor = ActionExecutor(
        apis_from_kubeconfig(SETTINGS.operator_kubeconfig),
        K8sReadBackend(apis_from_kubeconfig(SETTINGS.reader_kubeconfig)),
    )
    runner = ToolRunner(AuditLog(tmp_path / "audit.jsonl", "opspilot-actions", "live"))
    return ActionTools(executor, runner, NonceStore(tmp_path / "n.sqlite"), ["shop"], SETTINGS)


def _wait_healthy() -> None:
    injector = Injector(
        admin_client(SETTINGS.admin_context),
        context=SETTINGS.admin_context,
        base_dir=SETTINGS.demo_base_dir,
    )
    poll(lambda: injector.all_ready("shop"), timeout_s=180, interval_s=3)


@pytest.mark.parametrize(
    "action",
    [
        {"type": "restart_deployment", "namespace": "shop", "deployment": "inventory-api"},
        {"type": "scale_deployment", "namespace": "shop", "deployment": "redis", "replicas": 2},
        {
            "type": "patch_container_resources",
            "namespace": "shop",
            "deployment": "payments-api",
            "container": "app",
            "memory_limit": "256Mi",
        },
        {
            "type": "set_container_image",
            "namespace": "shop",
            "deployment": "orders-api",
            "container": "app",
            "image": "opspilot-demo-svc:dev",
        },
        {"type": "rollback_deployment", "namespace": "shop", "deployment": "orders-api"},
    ],
)
def test_plan_is_a_dry_run(tools: ActionTools, action: dict[str, object]) -> None:
    out = json.loads(tools.plan_action(action))
    assert "error" not in out, out
    assert out["dry_run"] == "passed"
    assert out["action_hash"] == action_hash(action)
    assert out["risk_notes"]


def test_plan_changes_nothing(tools: ActionTools) -> None:
    reader = K8sReadBackend(apis_from_kubeconfig(SETTINGS.reader_kubeconfig))
    before = reader.get_deployment("shop", "redis").replicas["desired"]
    tools.plan_action(
        {"type": "scale_deployment", "namespace": "shop", "deployment": "redis", "replicas": 0}
    )
    assert reader.get_deployment("shop", "redis").replicas["desired"] == before


def test_image_outside_history_is_rejected(tools: ActionTools) -> None:
    action = {
        "type": "set_container_image",
        "namespace": "shop",
        "deployment": "payments-api",
        "container": "app",
        "image": "evil/miner:latest",
    }
    out = json.loads(tools.plan_action(action))
    assert out["error"]["type"] == "out_of_bounds"


def test_valid_token_executes_once(tools: ActionTools) -> None:
    action = {"type": "restart_deployment", "namespace": "shop", "deployment": "inventory-api"}
    token = mint_approval_token(action_hash(action), "integration-test", 120, settings=SETTINGS)
    try:
        done = json.loads(tools.execute_action(action, token))
        assert done["executed"] is True, done
        assert any(d["path"].endswith("restartedAt") for d in done["diff"])
        reused = json.loads(tools.execute_action(action, token))
        assert reused["error"]["type"] == "approval_invalid"
        no_token = json.loads(tools.execute_action(action, None))
        assert no_token["error"]["type"] == "approval_required"
    finally:
        _wait_healthy()
