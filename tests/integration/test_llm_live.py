"""The structured-output helper against the real local model (needs Ollama with qwen3:4b)."""

import asyncio
from typing import Literal

import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from opspilot.llm import astructured, get_llm
from opspilot.models.incident import RunMetrics

pytestmark = pytest.mark.integration


class Label(BaseModel):
    category: Literal["OOM_KILLED", "IMAGE_PULL_ERROR", "DEPENDENCY_UNAVAILABLE"]
    service: str


def test_structured_call_on_qwen() -> None:
    metrics = RunMetrics()
    messages = [
        SystemMessage("Classify the Kubernetes symptom. Answer with JSON only."),
        HumanMessage(
            "Container 'app' of deployment checkout-api: Last State Terminated, "
            "Reason OOMKilled, Exit Code 137."
        ),
    ]
    out = asyncio.run(astructured(get_llm("triage"), Label, messages, metrics, timeout_s=180))
    assert out.category == "OOM_KILLED"
    assert "checkout" in out.service
    assert metrics.tokens_in > 0
