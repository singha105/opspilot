"""The fixed allowlist of remediation actions and their bounds."""

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from opspilot.mcp_servers.common.errors import ToolError

MAX_REPLICAS = 5
MAX_MEMORY_BYTES = 512 * 1024**2  # 512Mi
MAX_CPU_MILLICORES = 1000  # 1 core


class _Action(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    namespace: str
    deployment: str = Field(min_length=1)


class RestartDeployment(_Action):
    type: Literal["restart_deployment"]


class RollbackDeployment(_Action):
    type: Literal["rollback_deployment"]
    to_revision: int | None = Field(default=None, ge=1)


class ScaleDeployment(_Action):
    type: Literal["scale_deployment"]
    replicas: int


class PatchContainerResources(_Action):
    type: Literal["patch_container_resources"]
    container: str = Field(min_length=1)
    memory_limit: str | None = None
    cpu_limit: str | None = None


class SetContainerImage(_Action):
    type: Literal["set_container_image"]
    container: str = Field(min_length=1)
    image: str = Field(min_length=1)


Action = Annotated[
    RestartDeployment
    | RollbackDeployment
    | ScaleDeployment
    | PatchContainerResources
    | SetContainerImage,
    Field(discriminator="type"),
]
ACTION_TYPES = (
    "restart_deployment",
    "rollback_deployment",
    "scale_deployment",
    "patch_container_resources",
    "set_container_image",
)
_ADAPTER: TypeAdapter[Action] = TypeAdapter(Action)

_MEMORY = re.compile(r"^(\d+(?:\.\d+)?)(Ki|Mi|Gi|K|M|G)?$")
_MEMORY_UNITS = {
    None: 1,
    "K": 10**3,
    "M": 10**6,
    "G": 10**9,
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
}
_CPU = re.compile(r"^(\d+(?:\.\d+)?)(m)?$")


def parse_memory(value: str) -> int:
    """Kubernetes memory quantity to bytes (Ki/Mi/Gi/K/M/G or plain bytes)."""
    match = _MEMORY.match(value.strip())
    if not match:
        raise ToolError(
            "invalid_argument", f"Invalid memory quantity {value!r}.", "Use e.g. 256Mi."
        )
    return int(float(match.group(1)) * _MEMORY_UNITS[match.group(2)])


def parse_cpu(value: str) -> int:
    """Kubernetes CPU quantity to millicores (e.g. '500m', '1', '0.5')."""
    match = _CPU.match(value.strip())
    if not match:
        raise ToolError("invalid_argument", f"Invalid CPU quantity {value!r}.", "Use e.g. 500m.")
    number = float(match.group(1))
    return int(number if match.group(2) else number * 1000)


def parse_action(raw: object) -> Action:
    """Validate raw tool input into one of the allowlisted actions."""
    if isinstance(raw, dict) and raw.get("type") not in ACTION_TYPES:
        raise ToolError(
            "action_not_allowed",
            f"Action type {raw.get('type')!r} is not allowed.",
            f"Allowed: {', '.join(ACTION_TYPES)}.",
        )
    return _ADAPTER.validate_python(raw)


def check_bounds(action: Action, allowed_namespaces: list[str]) -> None:
    """Server-side allowlist and bounds. Raises ToolError for anything outside them."""
    if action.namespace not in allowed_namespaces:
        raise ToolError(
            "namespace_not_allowed",
            f"Namespace {action.namespace!r} is not in the allowlist.",
            f"Allowed namespaces: {', '.join(allowed_namespaces)}.",
        )
    if isinstance(action, ScaleDeployment) and not 0 <= action.replicas <= MAX_REPLICAS:
        raise ToolError("out_of_bounds", f"replicas must be between 0 and {MAX_REPLICAS}.")
    if isinstance(action, PatchContainerResources):
        if action.memory_limit is None and action.cpu_limit is None:
            raise ToolError("invalid_argument", "Set memory_limit, cpu_limit or both.")
        if (
            action.memory_limit is not None
            and not 0 < parse_memory(action.memory_limit) <= MAX_MEMORY_BYTES
        ):
            raise ToolError("out_of_bounds", "memory_limit must be above 0 and at most 512Mi.")
        if (
            action.cpu_limit is not None
            and not 0 < parse_cpu(action.cpu_limit) <= MAX_CPU_MILLICORES
        ):
            raise ToolError("out_of_bounds", "cpu_limit must be above 0 and at most 1 core.")
