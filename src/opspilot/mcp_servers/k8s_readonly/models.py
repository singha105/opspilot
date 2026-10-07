"""Pydantic output models of the read-only Kubernetes tools."""

from typing import Any

from pydantic import BaseModel, Field


class PodSummary(BaseModel):
    name: str
    phase: str
    ready: str  # "1/1"
    restarts: int
    reason: str | None = None  # current waiting/terminated reason, if any
    last_reason: str | None = None  # last terminated reason (e.g. OOMKilled)
    age: str
    node: str | None = None


class PodList(BaseModel):
    namespace: str
    pods: list[PodSummary]


class ContainerDetail(BaseModel):
    name: str
    image: str
    ready: bool
    restartCount: int
    state: dict[str, Any] = Field(default_factory=dict)
    lastState: dict[str, Any] = Field(default_factory=dict)
    resources: dict[str, Any] = Field(default_factory=dict)
    envNames: list[str] = Field(default_factory=list)
    envFrom: list[str] = Field(default_factory=list)
    command: list[str] | None = None
    args: list[str] | None = None
    probes: dict[str, str] = Field(default_factory=dict)
    volumeMounts: list[str] = Field(default_factory=list)


class EventItem(BaseModel):
    type: str
    reason: str
    object: str
    count: int
    message: str
    age: str
    age_s: int


class PodDetail(BaseModel):
    pod: str
    namespace: str
    phase: str
    ready: bool
    node: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    initContainers: list[ContainerDetail] = Field(default_factory=list)
    containers: list[ContainerDetail]
    volumes: list[str] = Field(default_factory=list)
    conditions: list[dict[str, Any]] = Field(default_factory=list)
    events: list[EventItem] = Field(default_factory=list)


class PodLogs(BaseModel):
    pod: str
    container: str | None
    previous: bool
    tail_lines: int
    contains: str | None = None
    line_count: int
    lines: list[str]


class EventList(BaseModel):
    namespace: str
    involved_object_name: str | None = None
    since_minutes: int
    events: list[EventItem]


class DeploymentDetail(BaseModel):
    name: str
    namespace: str
    revision: str | None = None
    replicas: dict[str, int]
    strategy: str
    selector: dict[str, str]
    conditions: list[dict[str, Any]]
    template: dict[str, Any]


class RolloutRevision(BaseModel):
    revision: int
    replicaset: str
    images: list[str]
    change_cause: str | None = None
    replicas: int
    ready: int
    created: str
    template_hash: str | None = None


class RolloutHistory(BaseModel):
    deployment: str
    namespace: str
    current_revision: str | None = None
    revisions: list[RolloutRevision]


class ServiceInfo(BaseModel):
    name: str
    type: str
    cluster_ip: str | None = None
    selector: dict[str, str] = Field(default_factory=dict)
    ports: list[str] = Field(default_factory=list)


class ServiceList(BaseModel):
    namespace: str
    services: list[ServiceInfo]


class EndpointAddress(BaseModel):
    ip: str
    ready: bool
    pod: str | None = None


class ServiceEndpoints(BaseModel):
    service: str
    namespace: str
    ready: int
    not_ready: int
    ports: list[str]
    addresses: list[EndpointAddress]


class ConfigMapView(BaseModel):
    name: str
    namespace: str
    data: dict[str, str]
    binary_keys: list[str] = Field(default_factory=list)


class NodeInfo(BaseModel):
    name: str
    kubelet: str | None = None
    capacity: dict[str, str]
    allocatable: dict[str, str]
    taints: list[str] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)
    conditions: list[dict[str, Any]] = Field(default_factory=list)


class NodeList(BaseModel):
    nodes: list[NodeInfo]


class PvcInfo(BaseModel):
    name: str
    status: str
    storage_class: str | None = None
    access_modes: list[str] = Field(default_factory=list)
    requested: str | None = None
    capacity: str | None = None
    volume: str | None = None
    events: list[EventItem] = Field(default_factory=list)


class PvcList(BaseModel):
    namespace: str
    pvcs: list[PvcInfo]
