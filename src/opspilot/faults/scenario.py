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


class _OnDeployment(_Strict):
    """An injection that edits a Deployment: the scenario target unless ``deployment`` is set.

    ``container`` defaults to the target's container, or to ``app`` on another Deployment.
    """

    deployment: str | None = None
    container: str | None = None


class SetEnv(_OnDeployment):
    type: Literal["set_env"]
    env: dict[str, str] = Field(min_length=1)


class UnsetEnv(_OnDeployment):
    type: Literal["unset_env"]
    names: list[str] = Field(min_length=1)


class SetImage(_OnDeployment):
    type: Literal["set_image"]
    image: str


class SetProbe(_OnDeployment):
    """Change an HTTP probe: its path, port and/or timing."""

    type: Literal["set_probe"]
    probe: Literal["readiness", "liveness"]
    path: str | None = Field(default=None, pattern=r"^/")
    port: int | str | None = None
    initial_delay_s: int | None = Field(default=None, ge=0)
    period_s: int | None = Field(default=None, ge=1)
    failure_threshold: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _changes_something(self) -> "SetProbe":
        fields = (self.path, self.port, self.initial_delay_s, self.period_s, self.failure_threshold)
        if all(v is None for v in fields):
            raise ValueError("set_probe needs at least one change")
        return self


class Scale(_OnDeployment):
    type: Literal["scale"]
    replicas: int = Field(ge=0)


class SetResources(_OnDeployment):
    """Merge resource requests/limits (keys ``cpu`` and ``memory``)."""

    type: Literal["set_resources"]
    requests: dict[Literal["cpu", "memory"], str] = Field(default_factory=dict)
    limits: dict[Literal["cpu", "memory"], str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _changes_something(self) -> "SetResources":
        if not self.requests and not self.limits:
            raise ValueError("set_resources needs requests or limits")
        return self


class SetCommand(_OnDeployment):
    """Override the container's command and/or args."""

    type: Literal["set_command"]
    command: list[str] | None = None
    args: list[str] | None = None

    @model_validator(mode="after")
    def _changes_something(self) -> "SetCommand":
        if self.command is None and self.args is None:
            raise ValueError("set_command needs command or args")
        return self


class AddInitContainer(_OnDeployment):
    """Add an init container running the target's image (unless ``image`` is set)."""

    type: Literal["add_init_container"]
    name: str
    args: list[str] = Field(min_length=1)
    image: str | None = None


class AddEnvFrom(_OnDeployment):
    """Load every key of a ConfigMap as environment variables (``envFrom``)."""

    type: Literal["add_env_from"]
    configmap: str


class SetSecretEnv(_OnDeployment):
    """Set an environment variable from a Secret key (``secretKeyRef``)."""

    type: Literal["set_secret_env"]
    name: str
    secret: str
    key: str


class SetAffinity(_OnDeployment):
    """Constrain scheduling with a nodeSelector and/or required node affinity."""

    type: Literal["set_affinity"]
    node_selector: dict[str, str] = Field(default_factory=dict)
    required_node_labels: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _constrains_something(self) -> "SetAffinity":
        if not self.node_selector and not self.required_node_labels:
            raise ValueError("set_affinity needs node_selector or required_node_labels")
        return self


AccessMode = Literal["ReadWriteOnce", "ReadOnlyMany", "ReadWriteMany"]
_RWO: AccessMode = "ReadWriteOnce"


class AddPvc(_OnDeployment):
    """Create a PersistentVolumeClaim and mount it into the container."""

    type: Literal["add_pvc"]
    name: str
    mount_path: str = Field(pattern=r"^/")
    storage_class: str | None = None
    access_modes: list[AccessMode] = Field(default_factory=lambda: [_RWO])
    size: str = "64Mi"


class PatchService(_Strict):
    """Change a Service (the target's Service unless ``service`` is set)."""

    type: Literal["patch_service"]
    service: str | None = None
    selector: dict[str, str] | None = None
    target_port: int | str | None = None

    @model_validator(mode="after")
    def _changes_something(self) -> "PatchService":
        if self.selector is None and self.target_port is None:
            raise ValueError("patch_service needs selector or target_port")
        return self


DeploymentInjection = (
    SetEnv
    | UnsetEnv
    | SetImage
    | SetProbe
    | Scale
    | SetResources
    | SetCommand
    | AddInitContainer
    | AddEnvFrom
    | SetSecretEnv
    | SetAffinity
    | AddPvc
)
Injection = Annotated[DeploymentInjection | PatchService, Field(discriminator="type")]


class ExpectedSymptom(_Strict):
    """What the cluster must show once the fault has taken effect.

    Every listed condition must hold: a waiting/terminated/scheduling reason on the
    target's pods, Deployments with fewer ready replicas than desired, a target pod
    restarted at least ``min_restarts`` times, and Services that route to no ready
    pod port.
    """

    pod_reason_any_of: list[str] = Field(default_factory=list)
    not_ready_deployments: list[str] = Field(default_factory=list)
    min_restarts: int = Field(default=0, ge=0)
    broken_services: list[str] = Field(default_factory=list)
    timeout_s: int = Field(default=150, gt=0)

    @model_validator(mode="after")
    def _needs_a_condition(self) -> "ExpectedSymptom":
        conditions = (
            self.pod_reason_any_of,
            self.not_ready_deployments,
            self.min_restarts,
            self.broken_services,
        )
        if not any(conditions):
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

    @property
    def deployments(self) -> list[str]:
        """Every Deployment the injections change (target first)."""
        names = [self.target.name]
        for injection in self.inject:
            name = getattr(injection, "deployment", None)
            if name and name not in names:
                names.append(name)
        return names

    @property
    def services(self) -> list[str]:
        """Every Service the injections change."""
        return [i.service or self.target.name for i in self.inject if isinstance(i, PatchService)]


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
