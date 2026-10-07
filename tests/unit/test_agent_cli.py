import contextlib
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from agent_fakes import ScriptedChatModel, replay_servers, tool_call
from typer.testing import CliRunner

from opspilot.agent import cli as agent_cli
from opspilot.agent.toolbox import InProcessToolBox
from opspilot.cli import app
from opspilot.config import get_settings

runner = CliRunner()
ROOT = Path(__file__).parents[2]
TRIAGE = {
    "service": "payments-api",
    "namespace": "shop",
    "symptom_summary": "restarts",
    "candidate_categories": ["OOM_KILLED"],
    "search_queries": ["exit code 137", "memory limit"],
}
DIAGNOSIS = {
    "root_cause_category": "OOM_KILLED",
    "component": "payments-api",
    "summary": "OOMKilled at startup [E1].",
    "evidence_refs": ["E1"],
    "runbook_refs": [],
    "confidence": 0.8,
    "alternatives": [],
}
CHOICE = {
    "action_type": "patch_container_resources",
    "deployment": "payments-api",
    "memory_limit": "512Mi",
}
FOLLOW = {"follow_ups": ["Alert on memory near the limit."]}


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ScriptedChatModel]:
    monkeypatch.setenv("OPSPILOT_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("OPSPILOT_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    model = ScriptedChatModel()

    @contextlib.asynccontextmanager
    async def toolbox(
        mode: str, fixture: Path | None, run_id: str
    ) -> AsyncIterator[InProcessToolBox]:
        name = fixture.stem if fixture else "oom-payments"
        async with InProcessToolBox(*replay_servers(name, tmp_path)) as box:
            yield box

    monkeypatch.setattr(agent_cli, "toolbox_factory", toolbox)
    monkeypatch.setattr(agent_cli, "llm_factory", lambda role: model)
    yield model
    get_settings.cache_clear()


def script(model: ScriptedChatModel, *extra: Any) -> None:
    model.replies = [
        TRIAGE,
        tool_call("list_pods", {"namespace": "shop"}),
        tool_call("finish_investigation", {"reason": "seen"}),
        DIAGNOSIS,
        CHOICE,
        *extra,
    ]


def test_investigate_replay_with_decision(cli_env: ScriptedChatModel) -> None:
    script(cli_env, FOLLOW)
    result = runner.invoke(
        app,
        [
            "investigate",
            "--scenario",
            "oom-payments",
            "--no-rag",
            "--decision",
            "approve",
            "--approver",
            "arnab",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "→ triage" in result.output
    assert "· list_pods(namespace=shop)" in result.output
    assert "Approval needed" in result.output
    assert "Root cause: OOM_KILLED" in result.output
    assert "Report:" in result.output
    listing = runner.invoke(app, ["runs", "list"])
    assert "reported" in listing.output
    assert "OOM_KILLED" in listing.output


def test_pause_then_resume_from_another_invocation(cli_env: ScriptedChatModel) -> None:
    script(cli_env)
    paused = runner.invoke(app, ["investigate", "--scenario", "oom-payments", "--no-rag"])
    assert paused.exit_code == 0, paused.output
    assert "Paused for approval" in paused.output
    incident = paused.output.split("opspilot resume ")[1].split()[0]
    runs_dir = get_settings().runs_dir
    assert (
        json.loads((runs_dir / incident / "state.json").read_text())["status"]
        == "awaiting_approval"
    )

    cli_env.replies = [FOLLOW]
    resumed = runner.invoke(
        app, ["resume", incident, "--decision", "reject", "--approver", "arnab"]
    )
    assert resumed.exit_code == 0, resumed.output
    state = json.loads((runs_dir / incident / "state.json").read_text())
    assert state["approval"]["decision"] == "reject"
    assert state["action_result"] is None
    shown = runner.invoke(app, ["runs", "show", incident])
    assert "## Approval: reject by arnab" in shown.output
    assert "events:" in shown.output


def test_live_runs_refuse_a_decision_given_in_advance(cli_env: ScriptedChatModel) -> None:
    result = runner.invoke(
        app,
        ["investigate", "--scenario", "oom-payments", "--mode", "live", "--decision", "approve"],
    )
    assert result.exit_code != 0
    assert "after seeing the proposal" in result.output


def test_bad_inputs(cli_env: ScriptedChatModel, tmp_path: Path) -> None:
    assert runner.invoke(app, ["investigate"]).exit_code != 0
    assert runner.invoke(app, ["investigate", "--scenario", "nope"]).exit_code != 0
    assert runner.invoke(app, ["resume", "inc-missing"]).exit_code != 0
    text_alert = tmp_path / "alert.txt"
    text_alert.write_text("checkout is slow in namespace shop")
    assert (
        runner.invoke(app, ["investigate", "--alert-file", str(text_alert)]).exit_code != 0
    )  # no fixture


def test_load_alert_from_file(tmp_path: Path) -> None:
    path = tmp_path / "a.json"
    path.write_text(json.dumps({"alerts": [{"labels": {"alertname": "X"}}]}))
    alert, fixture = agent_cli.load_alert(None, path)
    assert alert["alerts"][0]["labels"]["alertname"] == "X"
    assert fixture is None
