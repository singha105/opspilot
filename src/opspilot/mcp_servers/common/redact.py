"""Redact credentials from tool output before it reaches the model or the audit log."""

import re
from typing import Any

MASK = "[REDACTED]"

# Order matters: multi-line and structured secrets first, generic key=value last.
PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        "[REDACTED PRIVATE KEY]",
    ),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"), MASK),
    ("bearer", re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9\-._~+/]{8,}=*"), rf"\1 {MASK}"),
    (
        "url_credentials",
        re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^\s:/@]+:)[^\s@/]+@"),
        rf"\1{MASK}@",
    ),
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), MASK),
    (
        "aws_secret_access_key",
        re.compile(r"(?i)\b(aws_secret_access_key\s*[=:]\s*)[A-Za-z0-9/+=]{20,}"),
        rf"\1{MASK}",
    ),
    (
        "key_value_secret",
        re.compile(
            r"(?i)\b([\w-]*(?:secret|token|password|passwd|pwd|api[_-]?key|access[_-]?key)"
            r"[\w-]*[\"']?\s*[=:]\s*[\"']?)[^\s\"'&,;}]+"
        ),
        rf"\1{MASK}",
    ),
]


def redact_text(text: str) -> str:
    """Mask every known credential pattern in ``text``."""
    for _, pattern, replacement in PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact(value: Any) -> Any:
    """Recursively redact every string inside dicts, lists and tuples."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    return value
