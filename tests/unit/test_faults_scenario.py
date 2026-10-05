from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from opspilot.faults.scenario import Scenario, SetEnv, load_scenario, load_scenarios
from opspilot.models import RootCauseCategory

SCENARIOS_DIR = Path(__file__).parents[2] / "faults" / "scenarios"
DAY1_IDS = {
    "oom-payments",
    "imagepull-orders-tag",
    "missing-env-inventory",
    "readiness-payments",
    "redis-down",
}


def _raw(**overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load((SCENARIOS_DIR / "oom-payments.yaml").read_text())
    raw.update(overrides)
    return raw


def test_all_repo_scenarios_load() -> None:
    scenarios = load_scenarios(SCENARIOS_DIR)
    assert set(scenarios) >= DAY1_IDS
    for scenario in scenarios.values():
        assert scenario.category == scenario.expected.root_cause_category


def test_example_scenario_fields() -> None:
    scenario = load_scenario(SCENARIOS_DIR / "oom-payments.yaml")
    assert scenario.category is RootCauseCategory.OOM_KILLED
    assert scenario.target.container == "app"
    assert isinstance(scenario.inject[0], SetEnv)
    assert scenario.inject[0].env == {"MEMORY_BALLAST_MB": "300"}
    assert scenario.alert.severity == "critical"
    assert scenario.reset.type == "reapply_base"


def test_unknown_injection_type_rejected() -> None:
    with pytest.raises(ValidationError):
        Scenario.model_validate(_raw(inject=[{"type": "delete_namespace"}]))


def test_extra_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        Scenario.model_validate(_raw(surprise=True))


def test_category_must_match_ground_truth() -> None:
    raw = _raw(category="IMAGE_PULL_ERROR")
    with pytest.raises(ValidationError, match="category must equal"):
        Scenario.model_validate(raw)


def test_symptom_needs_a_condition() -> None:
    with pytest.raises(ValidationError, match="at least one condition"):
        Scenario.model_validate(_raw(expected_symptom={"timeout_s": 10}))


def test_actions_must_be_snake_case() -> None:
    raw = _raw()
    raw["expected"]["acceptable_actions"] = ["Delete Everything"]
    with pytest.raises(ValidationError):
        Scenario.model_validate(raw)


def test_id_must_match_file_name(tmp_path: Path) -> None:
    path = tmp_path / "other-name.yaml"
    path.write_text(yaml.safe_dump(_raw()))
    with pytest.raises(ValueError, match="does not match the file name"):
        load_scenario(path)
