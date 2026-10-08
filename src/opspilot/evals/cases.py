"""Evaluation cases: scenario ground truth plus the dev/test/control splits."""

from pathlib import Path

import yaml

from opspilot.evals.scoring import EvalCase, Split
from opspilot.faults.scenario import Scenario, SetEnv, load_scenarios
from opspilot.models import RootCauseCategory

HEALTHY = "healthy"
INJECTION_ENV = "LOG_INJECTION_TEXT"


def load_splits(path: Path) -> dict[Split, list[str]]:
    """The split lists; every id appears in exactly one split."""
    raw = yaml.safe_load(path.read_text())
    splits: dict[Split, list[str]] = {
        "dev": list(raw.get("dev") or []),
        "test": list(raw.get("test") or []),
        "control": list(raw.get("control") or []),
    }
    ids = [i for ids in splits.values() for i in ids]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ValueError(f"ids in more than one split: {duplicates}")
    return splits


def has_injection(scenario: Scenario) -> bool:
    """True when the scenario plants instructions for the agent in its logs."""
    return any(isinstance(i, SetEnv) and INJECTION_ENV in i.env for i in scenario.inject)


def case_from_scenario(scenario: Scenario, split: Split) -> EvalCase:
    truth = scenario.expected
    return EvalCase(
        id=scenario.id,
        split=split,
        category=truth.root_cause_category,
        component=truth.component,
        runbook_ids=list(truth.runbook_ids),
        acceptable_actions=list(truth.acceptable_actions),
        injection=has_injection(scenario),
    )


def control_case(case_id: str = HEALTHY) -> EvalCase:
    return EvalCase(id=case_id, split="control", category=RootCauseCategory.UNKNOWN, control=True)


def load_cases(scenarios_dir: Path, splits_path: Path) -> dict[str, EvalCase]:
    """Every case in the splits, in split order (dev, test, control)."""
    scenarios = load_scenarios(scenarios_dir)
    splits = load_splits(splits_path)
    fault_splits: tuple[Split, Split] = ("dev", "test")
    missing = sorted({i for s in fault_splits for i in splits[s]} - set(scenarios))
    if missing:
        raise ValueError(f"split ids without a scenario: {missing}")
    cases: dict[str, EvalCase] = {}
    for split in fault_splits:
        for case_id in splits[split]:
            scenario = scenarios[case_id]
            if scenario.split != split:
                raise ValueError(
                    f"{case_id}: scenario says split {scenario.split}, splits.yaml {split}"
                )
            cases[case_id] = case_from_scenario(scenario, split)
    for case_id in splits["control"]:
        cases[case_id] = control_case(case_id)
    return cases
