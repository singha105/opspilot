"""Shared layer for OpsPilot MCP servers: clients, allowlist, shaping, redaction, audit."""

from opspilot.mcp_servers.common.audit import AuditLog, new_run_id
from opspilot.mcp_servers.common.errors import ToolError, to_tool_error
from opspilot.mcp_servers.common.redact import redact, redact_text
from opspilot.mcp_servers.common.runner import REQUEST_TIMEOUT_S, ToolRunner, check_namespace
from opspilot.mcp_servers.common.shaping import MAX_CHARS, cap, dumps, to_data

__all__ = [
    "MAX_CHARS",
    "REQUEST_TIMEOUT_S",
    "AuditLog",
    "ToolError",
    "ToolRunner",
    "cap",
    "check_namespace",
    "dumps",
    "new_run_id",
    "redact",
    "redact_text",
    "to_data",
    "to_tool_error",
]
