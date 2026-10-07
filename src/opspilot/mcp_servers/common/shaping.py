"""Output shaping: compact JSON with a hard size cap and an explicit truncation marker."""

import json
from typing import Any

from pydantic import BaseModel

MAX_CHARS = 6000
MAX_STRING = 1000


def to_data(value: Any) -> Any:
    """Convert Pydantic models (recursively) into plain JSON-compatible data."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, dict):
        return {k: to_data(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [to_data(v) for v in value]
    return value


def dumps(data: Any) -> str:
    """Compact, deterministic-order JSON (insertion order is kept)."""
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False, default=str)


def _clip_strings(value: Any, limit: int) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "…"
    if isinstance(value, dict):
        return {k: _clip_strings(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip_strings(v, limit) for v in value]
    return value


def _largest_list(value: Any) -> list[Any] | None:
    """The list (anywhere in the tree) whose serialized size is largest."""
    best: list[Any] | None = None
    best_size = -1
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            if node:
                size = len(dumps(node))
                if size > best_size:
                    best, best_size = node, size
            stack.extend(node)
    return best


def cap(data: dict[str, Any], hint: str, limit: int = MAX_CHARS) -> str:
    """Serialize ``data`` to at most ``limit`` characters.

    Long strings are clipped first, then items are dropped from the end of the
    largest list until the result fits. A truncated result carries
    ``"truncated": true`` and a hint on how to narrow the call.
    """
    data = dict(data)
    text = dumps({**data, "truncated": False})
    if len(text) <= limit:
        return text
    data = _clip_strings(data, MAX_STRING)
    marker = {"truncated": True, "hint": hint}
    while True:
        text = dumps({**data, **marker})
        if len(text) <= limit:
            return text
        target = _largest_list(data)
        if target:
            target.pop()
            continue
        # No list left to shorten: clip strings harder until it fits.
        data = _clip_strings(data, max(len(text) - limit, 50) // 4)
