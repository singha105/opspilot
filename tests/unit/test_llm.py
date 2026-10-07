import asyncio

import pytest
from agent_fakes import ScriptedChatModel
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from opspilot.config import Settings
from opspilot.llm import (
    MAX_OUTPUT_TOKENS,
    StructuredOutputError,
    astructured,
    extract_json,
    get_llm,
)
from opspilot.models.incident import RunMetrics


class Pick(BaseModel):
    category: str = Field(pattern="^[A-Z_]+$")
    confidence: float = Field(ge=0, le=1)


def run(model: ScriptedChatModel, metrics: RunMetrics, repairs: int = 2) -> Pick:
    return asyncio.run(astructured(model, Pick, [HumanMessage("q")], metrics, max_repairs=repairs))


def test_valid_first_try_counts_tokens() -> None:
    metrics = RunMetrics()
    assert (
        run(
            ScriptedChatModel(replies=[{"category": "OOM_KILLED", "confidence": 0.9}]), metrics
        ).confidence
        == 0.9
    )
    assert (metrics.llm_calls, metrics.tokens_in, metrics.tokens_out) == (1, 100, 10)


def test_repair_prompt_quotes_the_error() -> None:
    model = ScriptedChatModel(
        replies=[
            {"category": "oom", "confidence": 2},
            {"category": "OOM_KILLED", "confidence": 0.7},
        ]
    )
    metrics = RunMetrics()
    assert run(model, metrics).category == "OOM_KILLED"
    repair = model.prompts[1][-1].content
    assert "did not match" in repair
    assert "confidence" in repair
    assert "category" in repair
    assert metrics.llm_calls == 2


def test_fallback_extracts_json_from_chatty_text() -> None:
    chatty = (
        "Sure! <think>hmm</think> Here it is:\n"
        '```json\n{"category": "BAD_ROLLOUT", "confidence": 0.4}\n```'
    )
    model = ScriptedChatModel(replies=[chatty])
    assert run(model, RunMetrics(), repairs=0).category == "BAD_ROLLOUT"


def test_gives_up_with_typed_error() -> None:
    model = ScriptedChatModel(replies=["no json", "still none", "nope"])
    with pytest.raises(StructuredOutputError) as info:
        run(model, RunMetrics())
    assert info.value.attempts == 3
    assert info.value.schema == "Pick"


def test_extract_json_edge_cases() -> None:
    assert extract_json('noise {"a": "x}y", "b": {"c": 1}} tail') == {"a": "x}y", "b": {"c": 1}}
    assert extract_json('{broken {"ok": true}') == {"ok": True}
    assert extract_json("[1, 2]") is None
    assert extract_json("nothing") is None


def test_get_llm_roles() -> None:
    settings = Settings(_env_file=None)
    tool_llm = get_llm("investigate", settings)
    structured = get_llm("diagnose", settings)
    assert tool_llm.reasoning is True  # type: ignore[attr-defined]
    assert structured.reasoning is False  # type: ignore[attr-defined]
    assert structured.temperature == 0  # type: ignore[attr-defined]
    assert structured.seed == 42  # type: ignore[attr-defined]
    assert structured.num_ctx == 8192  # type: ignore[attr-defined]
    assert structured.num_predict == MAX_OUTPUT_TOKENS["diagnose"]  # type: ignore[attr-defined]


def test_unknown_provider_rejected() -> None:
    settings = Settings(_env_file=None).model_copy(update={"llm_provider": "other"})
    with pytest.raises(ValueError, match="unsupported"):
        get_llm("triage", settings)
