"""Clean, structured tool errors. The model never sees a stack trace."""

import json
from typing import Any

from pydantic import ValidationError


def _api_message(exc: BaseException) -> str:
    """The API server's own message from an ApiException body, if any."""
    body = getattr(exc, "body", None)
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    if isinstance(body, str):
        try:
            return str(json.loads(body).get("message", ""))[:300]
        except (ValueError, AttributeError):
            return ""
    return ""


class ToolError(Exception):
    """An expected failure with a type, a message and a hint for the model."""

    def __init__(self, type_: str, message: str, hint: str = "", **extra: Any) -> None:
        super().__init__(message)
        self.type = type_
        self.message = message
        self.hint = hint
        self.extra = extra

    def payload(self) -> dict[str, Any]:
        error: dict[str, Any] = {"type": self.type, "message": self.message}
        if self.hint:
            error["hint"] = self.hint
        error.update(self.extra)
        return {"error": error}


def to_tool_error(exc: BaseException) -> ToolError:
    """Map any exception to a ToolError without leaking internals."""
    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, ValidationError):
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'value'}: {e['msg']}" for e in exc.errors()
        )
        return ToolError("invalid_argument", problems, "Fix the arguments and call again.")
    status = getattr(exc, "status", None)
    if status is not None and hasattr(exc, "reason"):  # kubernetes ApiException
        reason = str(getattr(exc, "reason", "") or "")
        if status == 403:
            return ToolError(
                "forbidden",
                "The OpsPilot identity is not allowed to read this resource.",
                "This is enforced by RBAC; use a different tool or resource.",
                status=403,
            )
        if status == 404:
            return ToolError(
                "not_found",
                "The requested object does not exist.",
                "Check the name with list_pods, list_services or get_deployment.",
                status=404,
            )
        if status == 400:
            return ToolError(
                "bad_request",
                _api_message(exc) or reason or "The API server rejected the request.",
                "For previous=true the container must have restarted at least once.",
                status=400,
            )
        return ToolError(
            "api_error", f"Kubernetes API returned {status} {reason}".strip(), status=status
        )
    name = type(exc).__name__
    if "Timeout" in name or "MaxRetry" in name or isinstance(exc, TimeoutError):
        return ToolError(
            "timeout",
            "The Kubernetes API did not answer within 10 seconds.",
            "Retry once, or narrow the call (namespace, label_selector, smaller tail_lines).",
        )
    return ToolError("internal_error", f"{name} while running the tool.")
