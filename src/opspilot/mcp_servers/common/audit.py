"""Append-only JSONL audit log of every tool call."""

import datetime as dt
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Literal

from opspilot.mcp_servers.common.redact import redact

Mode = Literal["live", "replay"]


def new_run_id() -> str:
    """Run id shared by every server of one agent run (``OPSPILOT_RUN_ID``) or a fresh one."""
    return os.environ.get("OPSPILOT_RUN_ID") or (
        "run-" + dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    )


class AuditLog:
    """Writes one JSON line per tool call: ts, server, tool, redacted args, timing, status."""

    def __init__(self, path: Path, server: str, mode: Mode, run_id: str | None = None) -> None:
        self.path = path
        self.server = server
        self.mode = mode
        self.run_id = run_id or new_run_id()
        self._lock = threading.Lock()

    def write(
        self,
        tool: str,
        args: dict[str, Any],
        *,
        duration_ms: float,
        size: int,
        status: str,
        **extra: Any,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "ts": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "server": self.server,
            "tool": tool,
            "args": redact(args),
            "duration_ms": round(duration_ms, 1),
            "bytes": size,
            "status": status,
            "mode": self.mode,
            "run_id": self.run_id,
            **redact(extra),
        }
        line = json.dumps(record, separators=(",", ":"), default=str)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        return record
