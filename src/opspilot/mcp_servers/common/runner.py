"""Run one tool call: validate namespace, time it, redact, cap, audit, never raise."""

import time
from collections.abc import Callable, Sequence
from typing import Any

from opspilot.mcp_servers.common.audit import AuditLog
from opspilot.mcp_servers.common.errors import ToolError, to_tool_error
from opspilot.mcp_servers.common.redact import redact
from opspilot.mcp_servers.common.shaping import MAX_CHARS, cap, dumps, to_data

REQUEST_TIMEOUT_S = 10


def check_namespace(namespace: str, allowed: Sequence[str]) -> None:
    """Reject namespaces outside the configured allowlist."""
    if namespace not in allowed:
        raise ToolError(
            "namespace_not_allowed",
            f"Namespace {namespace!r} is not in the allowlist.",
            f"Allowed namespaces: {', '.join(allowed)}.",
        )


class ToolRunner:
    """Shared execution path for every tool of a server (live or replay)."""

    def __init__(self, audit: AuditLog, limit: int = MAX_CHARS) -> None:
        self.audit = audit
        self.limit = limit

    def run(
        self,
        tool: str,
        args: dict[str, Any],
        fn: Callable[[], Any],
        hint: str = "Narrow the call to get a smaller result.",
        **audit_extra: Any,
    ) -> str:
        """Execute ``fn`` and return the shaped JSON text the model will see."""
        start = time.perf_counter()
        status = "ok"
        try:
            result = to_data(fn())
            if isinstance(result, str):  # replay hands back already-shaped text
                text = result
                if text.startswith('{"error"'):
                    status = "error"
            else:
                data = result if isinstance(result, dict) else {"items": result}
                text = cap(redact(data), hint, self.limit)
        except Exception as exc:
            error = to_tool_error(exc)
            status = "rejected" if error.type in REJECTED_TYPES else "error"
            text = dumps(redact(error.payload()))
            audit_extra = {**audit_extra, "error_type": error.type}
        duration = (time.perf_counter() - start) * 1000
        self.audit.write(
            tool, args, duration_ms=duration, size=len(text), status=status, **audit_extra
        )
        return text


REJECTED_TYPES = frozenset(
    {
        "namespace_not_allowed",
        "invalid_argument",
        "approval_required",
        "approval_invalid",
        "out_of_bounds",
        "action_not_allowed",
    }
)
