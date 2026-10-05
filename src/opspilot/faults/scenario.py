"""Fault scenario schema and loader."""

from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from opspilot.models import Alert, RootCauseCategory

ACTION_PATTERN = r"^[a-z][a-z0-9_]*$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Target(_Strict):
    kind: Literal["Deployment"] = "Deployment"
    name: str
    container: str = "app"


class SetEnv(_Strict):
    type: Literal["set_env"]
    env: dict[str, str] = Field(min_length=1)


class UnsetEnv(_Strict):
    type: Literal["unset_env"]
    names: list[str] = Field(min_length=1)


class SetImage(_Strict):
    type: Literal["set_image"]
    image: str


class SetProbe(_Strict):
    type: Literal["set_probe"]
    probe: Literal["readiness", "liveness"]
    path: str = Field(pattern=r"^/")


class Scale(_Strict):
    type: Literal["scale"]
    replicas: int = Field(ge=0)


Injection = Annotated[SetEnv | UnsetEnv | SetImage | SetProbe | Scale, Field(discriminator="type")]


class ExpectedSymptom(_Strict):
    """What the cluster must show once the fault has taken effect.

    Every listed condition must hold: a waiting/terminated reason on the
    target's pods, and/or Deployments that have fewer ready replicas than desired.
    """

    pod_reason_any_of: list[str] = Field(default_factory=list)
    not_ready_deployments: list[str] = Field(default_factory=list)
    timeout_s: int = Field(default=150, gt=0)

    @model_validator(mode="after")
    def _needs_a_condition(self) -> "ExpectedSymptom":
        if not self.pod_reason_any_of and not self.not_ready_deployments:
            raise ValueError("expected_symptom needs at least one condition")
        return self


class GroundTruth(_Strict):
    root_cause_category: RootCauseCategory
    component: str
    runbook_ids: list[str] = Field(default_factory=list)
    acceptable_actions: list[Annotated[str, Field(pattern=ACTION_PATTERN)]] = Field(min_length=1)


class Reset(_Strict):
    type: Literal["reapply_base"] = "reapply_base"


class Scenario(_Strict):
    """A reproducible fault with its alert and known ground-truth root cause."""

    id: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    title: str
    split: Literal["dev", "test"] = "dev"
    category: RootCauseCategory
    namespace: str = "shop"
    target: Target
    inject: list[Injection] = Field(min_length=1)
    expected_symptom: ExpectedSymptom
    alert: Alert
    expected: GroundTruth
    reset: Reset = Field(default_factory=Reset)

    @model_validator(mode="after")
    def _category_matches_ground_truth(self) -> "Scenario":
        if self.category != self.expected.root_cause_category:
            raise ValueError("category must equal expected.root_cause_category")
        return self


def load_scenario(path: Path) -> Scenario:
    """Load one scenario file; its id must match the file name."""
    scenario = Scenario.model_validate(yaml.safe_load(path.read_text()))
    if scenario.id != path.stem:
        raise ValueError(f"{path.name}: id {scenario.id!r} does not match the file name")
    return scenario


def load_scenarios(directory: Path) -> dict[str, Scenario]:
    """Load every ``*.yaml`` scenario in ``directory``, keyed and sorted by id."""
    scenarios = [load_scenario(path) for path in sorted(directory.glob("*.yaml"))]
    return {scenario.id: scenario for scenario in scenarios}
