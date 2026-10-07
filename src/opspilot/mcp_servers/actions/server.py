"""``opspilot-actions``: the only MCP server that can change the cluster.

Two phases. ``plan_action`` runs a server-side dry run and returns the action hash.
``execute_action`` only runs with a valid approval token for exactly that hash,
minted by ``opspilot.agent.approval`` after a human decision. There is no bypass.
"""

from collections.abc import Sequence
from typing import Annotated, Any, Protocol

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from opspilot.agent.approval import (
    ApprovalError,
    NonceStore,
    action_hash,
    peek_claims,
    verify_approval_token,
)
from opspilot.config import Settings
from opspilot.mcp_servers.actions.models import Action, check_bounds, parse_action
from opspilot.mcp_servers.common import ToolError, ToolRunner

SERVER_NAME = "opspilot-actions"

INSTRUCTIONS = (
    "Gated remediation actions for the Shopfront demo. Call plan_action first; it never "
    "changes anything. execute_action needs an approval token that only a human can grant "
    "for that exact action. Never invent or reuse a token."
)

ACTION_SHAPES = (
    "Actions (JSON object): "
    "{type:'restart_deployment', namespace, deployment}; "
    "{type:'rollback_deployment', namespace, deployment, to_revision?}; "
    "{type:'scale_deployment', namespace, deployment, replicas 0-5}; "
    "{type:'patch_container_resources', namespace, deployment, container, "
    "memory_limit<=512Mi?, cpu_limit<=1?}; "
    "{type:'set_container_image', namespace, deployment, container, image from rollout history}."
)

DESCRIPTIONS = {
    "plan_action": (
        "Dry-run one remediation action on the server (dryRun=All) without changing anything. "
        "Returns the diff, risk notes and the action_hash a human must approve. "
        "Example: plan_action(action={'type':'rollback_deployment','namespace':'shop',"
        "'deployment':'orders-api'}). " + ACTION_SHAPES
    ),
    "execute_action": (
        "Apply one action that a human approved. Requires approval_token for exactly this "
        "action; tokens expire after 10 minutes and work once. Without a valid token the call "
        "is rejected and audited. Example: execute_action(action={...same as planned...}, "
        "approval_token='v1....')"
    ),
}


class Executor(Protocol):
    def plan(self, action: Action, action_hash: str) -> dict[str, Any]: ...
    def execute(self, action: Action, action_hash: str) -> dict[str, Any]: ...


class ActionTools:
    """Validation, approval checks and auditing around the executor."""

    def __init__(
        self,
        executor: Executor,
        runner: ToolRunner,
        nonces: NonceStore,
        allowed: Sequence[str],
        settings: Settings | None = None,
    ) -> None:
        self.executor = executor
        self.runner = runner
        self.nonces = nonces
        self.allowed = list(allowed)
        self.settings = settings

    def _validated(self, raw: object) -> tuple[Action, str]:
        action = parse_action(raw)
        check_bounds(action, self.allowed)
        return action, action_hash(action)

    def plan_action(self, action: dict[str, Any]) -> str:
        def run() -> dict[str, Any]:
            parsed, digest = self._validated(action)
            return self.executor.plan(parsed, digest)

        return self.runner.run("plan_action", {"action": action}, run)

    def execute_action(self, action: dict[str, Any], approval_token: str | None) -> str:
        def run() -> dict[str, Any]:
            parsed, digest = self._validated(action)
            try:
                claims = verify_approval_token(
                    approval_token, digest, self.nonces, settings=self.settings
                )
            except ApprovalError as exc:
                kind = "approval_required" if not approval_token else "approval_invalid"
                raise ToolError(
                    kind,
                    f"Execution refused: {exc.reason}.",
                    "Run plan_action and ask a human to approve that exact action_hash.",
                ) from exc
            result = self.executor.execute(parsed, digest)
            result["approved_by"] = claims.approver
            return result

        # The token itself is never logged; only unverified claims useful for forensics.
        audit_args = {"action": action, "approval_token": "[REDACTED]" if approval_token else None}
        extra = peek_claims(approval_token) if approval_token else {}
        return self.runner.run("execute_action", audit_args, run, **extra)


def build_server(tools: ActionTools) -> FastMCP:
    mcp = FastMCP(SERVER_NAME, instructions=INSTRUCTIONS)

    @mcp.tool(description=DESCRIPTIONS["plan_action"], structured_output=False)
    def plan_action(
        action: Annotated[dict[str, Any], Field(description="One allowlisted action object.")],
    ) -> str:
        return tools.plan_action(action)

    @mcp.tool(description=DESCRIPTIONS["execute_action"], structured_output=False)
    def execute_action(
        action: Annotated[dict[str, Any], Field(description="Exactly the planned action.")],
        approval_token: Annotated[
            str | None, Field(description="Token from a human approval.")
        ] = None,
    ) -> str:
        return tools.execute_action(action, approval_token)

    return mcp
