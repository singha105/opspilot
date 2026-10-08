from pathlib import Path
from typing import Any

import pytest

from opspilot.evals import agent_report as ar
from opspilot.evals.live_eval import LiveRow
from opspilot.evals.scoring import EvalRow


def row(scenario: str, split: str = "test", correct: bool = True, **kw: Any) -> EvalRow:
    base: dict[str, Any] = {
        "scenario_id": scenario,
        "split": split,
        "config": "C3",
        "category_pred": "OOM_KILLED" if correct else "UNKNOWN",
        "category_true": "OOM_KILLED",
        "component_pred": "web",
        "component_true": "web",
        "confidence": 0.9,
        "category_correct": correct,
        "component_correct": True,
        "runbook_retrieved": True,
        "runbook_cited": correct,
        "citation_validity": 1.0,
        "remediation_acceptable": correct,
        "proposal": "action",
        "action_type": "rollback_deployment",
        "escalated": False,
        "escalated_correctly": None,
        "injection_flagged": None,
        "injection_followed": None,
        "unapproved_action_attempts": 0,
        "tool_calls": 6,
        "latency_s": 100.0,
        "tokens_in": 30000,
        "tokens_out": 1000,
    }
    base.update(kw)
    return EvalRow(**base)


ROWS = [
    row("a"),
    row("b"),
    row("c", correct=False),
    row("d", split="dev"),
    row("inj", injection_flagged=True, injection_followed=False),
    row(
        "healthy",
        split="control",
        category_true="UNKNOWN",
        category_correct=None,
        remediation_acceptable=None,
        escalated_correctly=True,
    ),
]


def test_headline_counts_the_test_split_only() -> None:
    text = ar.headline(ROWS, "C3")
    assert "| Metric (test split, n=4, config C3) | Value | 95% CI |" in text
    assert "| Root-cause category accuracy | 3/4 (75%) | 30%-95% |" in text
    assert "| Prompt injections followed (all splits) | 0/1 | — |" in text
    assert "| Unapproved action attempts (all 6 runs) | 0 | — |" in text
    assert (
        "| Median latency / tokens in / tokens out per incident | 100 s / 30,000 / 1,000 | — |"
        in text
    )


def test_splits_table_and_control() -> None:
    text = ar.splits_table(ROWS, "C3")
    assert "| dev | 1 |" in text
    assert "| all | 5 |" in text
    assert "| healthy control (escalate, no action) | 1 | 1/1 (100%) |" in text


def test_ablation_per_category_safety_efficiency() -> None:
    by_config = {"C0": [row("a", config="C0", correct=False)], "C3": ROWS}
    ablation = ar.ablation(by_config)
    assert "| C0 | no RAG (tools only) | 0/1 (0%)" in ablation
    assert "| C3 | hybrid + rerank, Weaviate | 3/4 (75%)" in ablation
    categories = ar.per_category(ROWS)
    assert "| OOM_KILLED | 5 | 4/5 (80%) | 4/5 (80%) | UNKNOWN |" in categories
    safety = ar.safety(by_config)
    assert "| C3 | inj | yes | no | rollback_deployment | 0 |" in safety
    assert "C0: 0 in 1 runs; C3: 0 in 6 runs." in safety
    assert "| C3 | 6 | 100 s | 100 s | 6 | 30,000 | 1,000 |" in ar.efficiency(by_config)


def test_failures_use_the_classification_notes(tmp_path: Path) -> None:
    notes_file = tmp_path / "notes.yaml"
    notes_file.write_text(
        "- {scenario: c, config: C3, type: reasoning error, note: ignored the event}\n"
    )
    notes = ar.load_failure_notes(notes_file)
    table = ar.failures({"C3": ROWS}, notes)
    assert (
        "| c | C3 | test | UNKNOWN / web | OOM_KILLED / web | reasoning error | ignored the event |"
        in table
    )
    assert "| a |" not in table
    assert ar.failure_type_counts({"C3": ROWS}, notes) == {"reasoning error": 1}
    assert ar.load_failure_notes(tmp_path / "missing.yaml") == {}


def test_live_table() -> None:
    live = LiveRow(
        scenario_id="x",
        config="C3",
        category_pred="OOM_KILLED",
        category_true="OOM_KILLED",
        category_correct=True,
        component_correct=True,
        proposal="action",
        action={"type": "patch_container_resources"},
        decision="approve",
        decision_reason="ok",
        executed=True,
        verification="resolved",
        recovered=True,
        symptom_s=3,
        agent_s=150,
        time_to_recovery_s=150,
        tokens_in=1,
        tokens_out=1,
        tool_calls=8,
        run_id="r",
    )
    text = ar.live_table([live])
    assert (
        "| x | OOM_KILLED | ok | patch_container_resources | approve | resolved | 150 s |" in text
    )
    assert "Recovered after the harness-approved action: 1/1." in text
    assert ar.live_table([]) == "No live runs recorded."


def test_blocks_are_replaced_and_read() -> None:
    doc = "intro\n<!-- BEGIN headline -->\nold\n<!-- END headline -->\nend\n"
    new = ar.replace_block(doc, "headline", "| new |")
    assert new == "intro\n<!-- BEGIN headline -->\n| new |\n<!-- END headline -->\nend\n"
    assert ar.read_block(new, "headline") == "| new |"
    with pytest.raises(KeyError, match="nope"):
        ar.replace_block(doc, "nope", "x")
    empty = "<!-- BEGIN live -->\n<!-- END live -->\n"
    assert ar.replace_block(empty, "live", "x") == "<!-- BEGIN live -->\nx\n<!-- END live -->\n"


def test_latest_rows_picks_the_newest_file_per_config(tmp_path: Path) -> None:
    older = tmp_path / "agent-2026-10-07-C3.jsonl"
    newer = tmp_path / "agent-2026-10-08-C3.jsonl"
    older.write_text(row("old").model_dump_json() + "\n")
    newer.write_text(row("new").model_dump_json() + "\n")
    (tmp_path / "agent-2026-10-08-C0.jsonl").write_text(
        row("z", config="C0").model_dump_json() + "\n"
    )
    rows = ar.latest_rows(tmp_path)
    assert list(rows) == ["C0", "C3"]
    assert [r.scenario_id for r in rows["C3"]] == ["new"]


def test_charts_are_written(tmp_path: Path) -> None:
    ar.chart_ablation({"C3": ROWS}, tmp_path / "a.png")
    ar.chart_categories(ROWS, tmp_path / "b.png")
    assert (tmp_path / "a.png").stat().st_size > 1000
    assert (tmp_path / "b.png").stat().st_size > 1000


def test_readme_quotes_the_report_headline_exactly() -> None:
    root = Path(__file__).parents[2]
    report = ar.read_block((root / "evals" / "REPORT.md").read_text(), "headline")
    readme = ar.read_block((root / "README.md").read_text(), "results")
    assert report.startswith("| Metric (test split")
    assert readme == report
