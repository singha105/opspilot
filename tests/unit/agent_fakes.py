"""Test doubles for the agent: a scripted chat model that needs no LLM."""

import json
from collections.abc import Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from pydantic import BaseModel, Field, ValidationError

USAGE = {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}


class ScriptedChatModel(BaseChatModel):
    """Replays scripted replies in order.

    Each reply is an AIMessage, a dict/BaseModel (returned as JSON text) or a raw string.
    ``with_structured_output`` parses the next reply against the schema, like the real one.
    """

    replies: list[Any] = Field(default_factory=list)
    prompts: list[list[BaseMessage]] = Field(default_factory=list)
    bound_tools: list[Any] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _next(self, messages: Sequence[BaseMessage]) -> AIMessage:
        self.prompts.append(list(messages))
        if not self.replies:
            raise AssertionError("ScriptedChatModel ran out of replies")
        reply = self.replies.pop(0)
        if isinstance(reply, AIMessage):
            if reply.usage_metadata is None:
                reply = reply.model_copy(update={"usage_metadata": USAGE})
            return reply
        if isinstance(reply, BaseModel):
            reply = reply.model_dump(mode="json")
        text = reply if isinstance(reply, str) else json.dumps(reply)
        return AIMessage(content=text, usage_metadata=USAGE)  # type: ignore[arg-type]

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._next(messages))])

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Runnable[Any, Any]:  # type: ignore[override]
        self.bound_tools = list(tools)
        return self

    def with_structured_output(  # type: ignore[override]
        self, schema: Any, *, include_raw: bool = False, **kwargs: Any
    ) -> Runnable[Any, Any]:
        def run(messages: Any) -> Any:
            raw = self._next(messages if isinstance(messages, list) else [messages])
            parsed = error = None
            try:
                parsed = schema.model_validate_json(raw.content)
            except (ValidationError, ValueError) as exc:
                error = exc
            if include_raw:
                return {"raw": raw, "parsed": parsed, "parsing_error": error}
            if error:
                raise error
            return parsed

        return RunnableLambda(run)


def tool_call(name: str, args: dict[str, Any], call_id: str = "c1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


# ---- tools ------------------------------------------------------------------------------

from pathlib import Path  # noqa: E402

ROOT = Path(__file__).parents[2]
FIXTURES = ROOT / "evals" / "fixtures"


def replay_servers(fixture: str, audit_dir: Path) -> list[Any]:
    """The real k8s MCP server in replay mode plus a kb server over a stub retriever."""
    from opspilot.mcp_servers.common import AuditLog, ToolRunner
    from opspilot.mcp_servers.k8s_readonly.replay import ReplayBackend
    from opspilot.mcp_servers.k8s_readonly.server import K8sTools
    from opspilot.mcp_servers.k8s_readonly.server import build_server as build_k8s
    from opspilot.mcp_servers.knowledge.server import KbTools
    from opspilot.mcp_servers.knowledge.server import build_server as build_kb

    runner = ToolRunner(AuditLog(audit_dir / "audit.jsonl", "test", "replay"))
    k8s = K8sTools(ReplayBackend(FIXTURES / f"{fixture}.json"), runner, ["shop"])  # type: ignore[arg-type]
    kb = KbTools(lambda: StubRetriever(), ROOT / "knowledge", runner)  # type: ignore[arg-type, return-value]
    return [build_k8s(k8s), build_kb(kb)]


class StubRetriever:
    """Returns fixed chunks from real runbooks so hints and citations resolve."""

    def __init__(self, doc_ids: tuple[str, ...] = ("rb-oom-killed", "rb-bad-rollout")) -> None:
        self.doc_ids = doc_ids
        self.queries: list[Any] = []

    def retrieve(self, query: Any, k: int = 6, **kwargs: Any) -> Any:
        from opspilot.rag.models import RetrievalResult, RetrievedChunk

        self.queries.append(query)
        chunks = [
            RetrievedChunk(
                chunk_id=f"c{i}",
                doc_id=d,
                doc_type="runbook",
                title=d,
                section="Symptoms",
                text=f"{d} symptoms text",
                score=1.0 - i / 10,
                rank=i + 1,
                citation_id=f"R{i + 1}",
            )
            for i, d in enumerate(self.doc_ids[:k])
        ]
        return RetrievalResult(chunks=chunks, timings_ms={"total": 1.0})


class FakeToolBox:
    """Tools answered by plain functions; records every call."""

    def __init__(self, outputs: dict[str, Any]) -> None:
        self.outputs = outputs
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def specs(self, names: Any) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {"name": n, "description": n, "parameters": {"type": "object"}},
            }
            for n in names
            if n in self.outputs
        ]

    async def call(self, name: str, args: dict[str, Any]) -> str:
        self.calls.append((name, args))
        out = self.outputs[name]
        return out(args) if callable(out) else out
