"""LLM factory and a robust structured-output helper for a small local model.

``qwen3:4b`` on Ollama is Qwen3-4B-2507, which always reasons before answering (its
template opens a ``<think>`` block; thinking cannot be switched off). Two settings follow:

- Structured roles run with ``reasoning=False`` and a JSON-schema ``format``: Ollama's
  grammar forces valid JSON from the first token, so the call is fast and parseable.
- The tool-calling role runs with ``reasoning=True`` so the model's reasoning arrives in a
  separate field and tool calls stay clean.
"""

import asyncio
import json
import re
from collections.abc import Callable, Sequence
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from pydantic import BaseModel, ValidationError

from opspilot.config import Settings, get_settings
from opspilot.models.incident import RunMetrics

Role = Literal["triage", "investigate", "summarize", "diagnose", "propose", "report"]
TOOL_ROLES: frozenset[str] = frozenset({"investigate"})
MAX_OUTPUT_TOKENS: dict[str, int] = {
    "triage": 400,
    "investigate": 1536,
    "summarize": 200,
    "diagnose": 700,
    "propose": 500,
    "report": 400,
}

LLMFactory = Callable[[Role], BaseChatModel]


class StructuredOutputError(Exception):
    """The model did not produce a valid object after repairs and fallback extraction."""

    def __init__(self, schema: str, attempts: int, last_error: str) -> None:
        super().__init__(f"{schema}: no valid output after {attempts} attempts ({last_error})")
        self.schema = schema
        self.attempts = attempts
        self.last_error = last_error


def get_llm(role: Role, settings: Settings | None = None) -> BaseChatModel:
    """A chat model configured for one agent role (temperature 0, fixed seed)."""
    settings = settings or get_settings()
    if settings.llm_provider != "ollama":
        raise ValueError(f"unsupported LLM provider {settings.llm_provider!r}")
    from langchain_ollama import ChatOllama

    return ChatOllama(
        model=settings.llm_model,
        base_url=settings.ollama_base_url,
        temperature=0,
        seed=settings.llm_seed,
        num_ctx=settings.llm_num_ctx,
        num_predict=MAX_OUTPUT_TOKENS[role],
        keep_alive=settings.llm_keep_alive,
        reasoning=role in TOOL_ROLES,
    )


_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> dict[str, Any] | None:
    """Best-effort: the first balanced JSON object in text (thinking and fences removed)."""
    text = _THINK.sub("", text)
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1)
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(text[start : index + 1])
                    except json.JSONDecodeError:
                        break
                    return value if isinstance(value, dict) else None
        start = text.find("{", start + 1)
    return None


def _error_text(error: Any) -> str:
    if isinstance(error, ValidationError):
        return "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'value'}: {e['msg']}" for e in error.errors()
        )[:800]
    return str(error)[:800]


REPAIR_PROMPT = (
    "Your previous answer did not match the required schema.\n"
    "Problems: {error}\n"
    "Reply again with ONE JSON object that fixes exactly these problems. No other text."
)


async def astructured[T: BaseModel](
    llm: BaseChatModel,
    schema: type[T],
    messages: Sequence[BaseMessage],
    metrics: RunMetrics,
    *,
    max_repairs: int = 2,
    timeout_s: float = 120.0,
) -> T:
    """Call ``llm`` for a ``schema`` object: schema-constrained call, repair, then fallback.

    1. ``with_structured_output(schema, include_raw=True)``.
    2. On a validation error, up to ``max_repairs`` repair turns that quote the error.
    3. Fallback: extract a JSON object from the last raw text and validate it.
    4. Otherwise raise :class:`StructuredOutputError`.
    """
    chain = llm.with_structured_output(schema, include_raw=True)
    history = list(messages)
    last_raw = ""
    last_error = "no output"
    for attempt in range(max_repairs + 1):
        response = await asyncio.wait_for(chain.ainvoke(history), timeout_s)
        result: dict[str, Any] = response if isinstance(response, dict) else {"parsed": response}
        raw = result.get("raw")
        if isinstance(raw, AIMessage):
            metrics.add_usage(raw.usage_metadata)  # type: ignore[arg-type]
            last_raw = raw.content if isinstance(raw.content, str) else json.dumps(raw.content)
        parsed = result.get("parsed")
        if isinstance(parsed, schema):
            return parsed
        if isinstance(parsed, dict):
            try:
                return schema.model_validate(parsed)
            except ValidationError as exc:
                result["parsing_error"] = exc
        last_error = _error_text(result.get("parsing_error") or "empty response")
        if attempt < max_repairs:
            history += [
                AIMessage(content=last_raw or "(empty)"),
                HumanMessage(content=REPAIR_PROMPT.format(error=last_error)),
            ]
    candidate = extract_json(last_raw)
    if candidate is not None:
        try:
            return schema.model_validate(candidate)
        except ValidationError as exc:
            last_error = _error_text(exc)
    raise StructuredOutputError(schema.__name__, max_repairs + 1, last_error)
