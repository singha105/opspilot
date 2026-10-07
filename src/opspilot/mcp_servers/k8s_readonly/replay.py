"""Replay backend: answers k8s tool calls from a recorded fixture.

Fixtures are recorded from the real cluster through the same tool code path
(``opspilot faults record``) and never edited by hand. A call is looked up by its
canonical key (tool name + arguments with defaults filled and nulls dropped). Two
safe derivations keep replay useful: smaller ``tail_lines`` / a ``contains`` filter
are served from the recorded 200-line logs, and shorter ``since_minutes`` windows
from the recorded 120-minute events. Anything else unrecorded returns a structured
error that points at broader calls.
"""

import inspect
import json
from pathlib import Path
from typing import Any

from opspilot.mcp_servers.common.errors import ToolError
from opspilot.mcp_servers.k8s_readonly.backend import K8sReadBackend

RECORDED_TAIL = 200
RECORDED_SINCE = 120


def call_key(tool: str, args: dict[str, Any]) -> str:
    """Canonical key for a tool call: sorted keys, compact, ``None`` values dropped."""
    clean = {k: v for k, v in args.items() if v is not None}
    return json.dumps({"tool": tool, "args": clean}, sort_keys=True, separators=(",", ":"))


def bind_args(tool: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Bind a call to the live backend's signature so defaults are filled consistently."""
    signature = inspect.signature(getattr(K8sReadBackend, tool))
    bound = signature.bind(None, *args, **kwargs)  # None stands in for self
    bound.apply_defaults()
    return {k: v for k, v in bound.arguments.items() if k != "self"}


def not_recorded(tool: str) -> ToolError:
    return ToolError(
        "not_recorded",
        f"This {tool} call was not recorded for this incident.",
        "Try broader calls: list_pods(namespace), get_events(namespace), "
        "describe_pod with a name from list_pods, or get_pod_logs with tail_lines<=200.",
    )


class ReplayBackend:
    """Serves recorded tool outputs. Methods mirror K8sReadBackend."""

    TOOLS = (
        "list_pods",
        "describe_pod",
        "get_pod_logs",
        "get_events",
        "get_deployment",
        "get_rollout_history",
        "list_services",
        "get_service_endpoints",
        "get_configmap",
        "list_nodes",
        "list_pvcs",
    )

    def __init__(self, fixture: Path) -> None:
        data = json.loads(fixture.read_text())
        self.metadata: dict[str, Any] = data["metadata"]
        self.calls: dict[str, Any] = data["calls"]

    def __getattr__(self, tool: str) -> Any:
        if tool not in self.TOOLS:
            raise AttributeError(tool)

        def call(*args: Any, **kwargs: Any) -> Any:
            return self.lookup(tool, bind_args(tool, *args, **kwargs))

        return call

    def lookup(self, tool: str, args: dict[str, Any]) -> Any:
        recorded = self.calls.get(call_key(tool, args))
        if recorded is not None:
            return json.dumps(recorded, separators=(",", ":"), ensure_ascii=False)
        if tool == "get_pod_logs":
            return self._derived_logs(args)
        if tool == "get_events":
            return self._derived_events(args)
        raise not_recorded(tool)

    def _derived_logs(self, args: dict[str, Any]) -> dict[str, Any]:
        for container in (args.get("container"), None):
            base = {**args, "container": container, "tail_lines": RECORDED_TAIL, "contains": None}
            recorded = self.calls.get(call_key("get_pod_logs", base))
            if recorded is None or "lines" not in recorded:
                continue
            lines = recorded["lines"][-args["tail_lines"] :]
            if args.get("contains"):
                needle = args["contains"].lower()
                lines = [line for line in lines if needle in line.lower()]
            out = {k: v for k, v in recorded.items() if k not in ("truncated", "hint")}
            out.update(
                container=args.get("container"),
                tail_lines=args["tail_lines"],
                contains=args.get("contains"),
                line_count=len(lines),
                lines=lines,
            )
            return out
        raise not_recorded("get_pod_logs")

    def _derived_events(self, args: dict[str, Any]) -> dict[str, Any]:
        name = args.get("involved_object_name")
        base = {**args, "since_minutes": RECORDED_SINCE}
        recorded = self.calls.get(call_key("get_events", base))
        if recorded is None and name:
            # Fall back to the namespace-wide recording, filtered to this object.
            whole = self.calls.get(call_key("get_events", {**base, "involved_object_name": None}))
            if whole is not None and "events" in whole:
                recorded = {
                    **whole,
                    "involved_object_name": name,
                    "events": [e for e in whole["events"] if e["object"].endswith(f"/{name}")],
                }
        if recorded is None or "events" not in recorded:
            raise not_recorded("get_events")
        window = args["since_minutes"] * 60
        return {
            "namespace": recorded["namespace"],
            "involved_object_name": name,
            "since_minutes": args["since_minutes"],
            "events": [e for e in recorded["events"] if e["age_s"] <= window],
        }
