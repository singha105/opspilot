"""Inject and reset fault scenarios.

The injector always talks to the cluster through the explicitly named ADMIN
context. It never uses the agent's reader/operator kubeconfigs, so the agent's
permissions stay exactly what RBAC says they are.
"""

import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from kubernetes import client

from opspilot.faults.scenario import (
    ExpectedSymptom,
    Injection,
    Scale,
    Scenario,
    SetEnv,
    SetImage,
    SetProbe,
    UnsetEnv,
)
from opspilot.logging import get_logger

Runner = Callable[..., subprocess.CompletedProcess[str]]
JsonDict = dict[str, Any]

log = get_logger(__name__)


class FaultTimeoutError(TimeoutError):
    """Raised when a symptom or recovery does not appear within its timeout."""


# ---- pure helpers (unit-tested without a cluster) ----------------------------------


def find_container(deployment: JsonDict, name: str) -> JsonDict:
    """Return the named container of a Deployment manifest."""
    containers: list[JsonDict] = deployment["spec"]["template"]["spec"]["containers"]
    for container in containers:
        if container["name"] == name:
            return container
    raise KeyError(f"container {name!r} not found in {deployment['metadata']['name']}")


def apply_injection(deployment: JsonDict, injection: Injection, container_name: str) -> None:
    """Mutate a Deployment manifest (camelCase JSON) in place to inject one fault."""
    if isinstance(injection, Scale):
        deployment["spec"]["replicas"] = injection.replicas
        return
    container = find_container(deployment, container_name)
    if isinstance(injection, SetEnv):
        env = [e for e in container.get("env") or [] if e["name"] not in injection.env]
        env += [{"name": k, "value": v} for k, v in injection.env.items()]
        container["env"] = env
    elif isinstance(injection, UnsetEnv):
        names = set(injection.names)
        container["env"] = [e for e in container.get("env") or [] if e["name"] not in names]
    elif isinstance(injection, SetImage):
        container["image"] = injection.image
    elif isinstance(injection, SetProbe):
        key = f"{injection.probe}Probe"
        probe = container.get(key)
        if not probe or "httpGet" not in probe:
            raise ValueError(f"container {container_name!r} has no HTTP {injection.probe} probe")
        probe["httpGet"]["path"] = injection.path


def pod_reasons(pod: JsonDict) -> set[str]:
    """Collect waiting/terminated reasons (current and last state) of a pod's containers."""
    reasons: set[str] = set()
    status = pod.get("status") or {}
    for cs in (status.get("containerStatuses") or []) + (status.get("initContainerStatuses") or []):
        for state_key in ("state", "lastState"):
            for phase in ("waiting", "terminated"):
                reason = ((cs.get(state_key) or {}).get(phase) or {}).get("reason")
                if reason:
                    reasons.add(reason)
    return reasons


def is_ready(deployment: JsonDict) -> bool:
    """True when every desired replica is updated and ready (0 desired counts as ready)."""
    desired = int(deployment["spec"].get("replicas", 1))
    status = deployment.get("status") or {}
    ready = int(status.get("readyReplicas") or 0)
    updated = int(status.get("updatedReplicas") or 0)
    observed = int(status.get("observedGeneration") or 0)
    generation_seen = observed >= int(deployment["metadata"].get("generation", 0))
    return generation_seen and ready >= desired and updated >= desired


def symptom_met(
    symptom: ExpectedSymptom, target_pods: Iterable[JsonDict], deployments: dict[str, JsonDict]
) -> bool:
    """Check every condition of ``symptom`` against observed pods and Deployments."""
    if symptom.pod_reason_any_of:
        seen = set().union(*(pod_reasons(p) for p in target_pods))
        if not seen & set(symptom.pod_reason_any_of):
            return False
    for name in symptom.not_ready_deployments:
        deployment = deployments.get(name)
        if deployment is None:
            return False
        status = deployment.get("status") or {}
        if (status.get("readyReplicas") or 0) >= deployment["spec"].get("replicas", 1):
            return False
    return True


def select_deployments(rendered: str, names: set[str]) -> str:
    """Pick the named Deployment documents out of rendered kustomize output."""
    docs = [
        doc
        for doc in yaml.safe_load_all(rendered)
        if doc and doc.get("kind") == "Deployment" and doc["metadata"]["name"] in names
    ]
    missing = names - {doc["metadata"]["name"] for doc in docs}
    if missing:
        raise KeyError(f"deployments not in base manifests: {sorted(missing)}")
    return yaml.safe_dump_all(docs, sort_keys=False)


def poll(check: Callable[[], bool], timeout_s: float, interval_s: float = 2.0) -> float:
    """Call ``check`` until it returns True; return the elapsed seconds."""
    start = time.monotonic()
    while True:
        if check():
            return time.monotonic() - start
        if time.monotonic() - start > timeout_s:
            raise FaultTimeoutError(f"condition not met within {timeout_s:.0f}s")
        time.sleep(interval_s)


# ---- cluster-facing injector -------------------------------------------------------


@dataclass(frozen=True)
class DeploymentStatus:
    name: str
    ready: int
    desired: int
    reasons: tuple[str, ...]

    @property
    def healthy(self) -> bool:
        return self.ready >= self.desired and not self.reasons


class Injector:
    """Applies scenarios to the cluster through the admin context."""

    def __init__(
        self,
        api: client.ApiClient,
        *,
        context: str,
        base_dir: Path,
        run: Runner = subprocess.run,
    ) -> None:
        self._api = api
        self._apps = client.AppsV1Api(api)
        self._core = client.CoreV1Api(api)
        self._context = context
        self._base_dir = base_dir
        self._run = run

    def _to_dict(self, obj: object) -> JsonDict:
        data: JsonDict = self._api.sanitize_for_serialization(obj)
        return data

    def _deployments(self, namespace: str) -> dict[str, JsonDict]:
        items = self._apps.list_namespaced_deployment(namespace).items
        return {d.metadata.name: self._to_dict(d) for d in items}

    def _pods(self, namespace: str, app: str) -> list[JsonDict]:
        selector = f"app.kubernetes.io/name={app}"
        pods = self._core.list_namespaced_pod(namespace, label_selector=selector).items
        return [self._to_dict(p) for p in pods]

    def _kubectl(self, *args: str, stdin: str | None = None) -> str:
        result = self._run(
            ["kubectl", "--context", self._context, *args],
            input=stdin,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout

    def inject(self, scenario: Scenario) -> None:
        """Apply every injection of ``scenario`` to its target Deployment."""
        name, namespace = scenario.target.name, scenario.namespace
        deployment = self._to_dict(self._apps.read_namespaced_deployment(name, namespace))
        for injection in scenario.inject:
            apply_injection(deployment, injection, scenario.target.container)
        deployment.pop("status", None)
        self._apps.replace_namespaced_deployment(name, namespace, deployment)
        log.info("fault injected", scenario=scenario.id, target=name)

    def symptom_present(self, scenario: Scenario) -> bool:
        """True when the cluster currently shows the scenario's expected symptom."""
        pods = self._pods(scenario.namespace, scenario.target.name)
        return symptom_met(scenario.expected_symptom, pods, self._deployments(scenario.namespace))

    def wait_for_symptom(self, scenario: Scenario, interval_s: float = 3.0) -> float:
        """Block until the expected symptom appears; return seconds taken."""
        timeout = scenario.expected_symptom.timeout_s
        elapsed = poll(lambda: self.symptom_present(scenario), timeout, interval_s)
        log.info("symptom observed", scenario=scenario.id, seconds=round(elapsed, 1))
        return elapsed

    def reset(self, scenario: Scenario, timeout_s: float = 180.0) -> float:
        """Re-apply the base manifests of the affected Deployment and wait until healthy."""
        rendered = self._kubectl("kustomize", str(self._base_dir))
        target_docs = select_deployments(rendered, {scenario.target.name})
        self._kubectl("replace", "--save-config", "-f", "-", stdin=target_docs)
        self._kubectl("apply", "-k", str(self._base_dir))
        elapsed = poll(lambda: self.all_ready(scenario.namespace), timeout_s, 3.0)
        log.info("fault reset", scenario=scenario.id, seconds=round(elapsed, 1))
        return elapsed

    def all_ready(self, namespace: str) -> bool:
        """True when every Deployment in ``namespace`` is fully rolled out and ready."""
        deployments = self._deployments(namespace)
        return bool(deployments) and all(is_ready(d) for d in deployments.values())

    def status(self, namespace: str) -> list[DeploymentStatus]:
        """Per-Deployment readiness plus any abnormal pod reasons."""
        rows = []
        for name, deployment in sorted(self._deployments(namespace).items()):
            reasons: set[str] = set()
            for pod in self._pods(namespace, name):
                reasons |= {r for r in pod_reasons(pod) if r != "Completed"}
            rows.append(
                DeploymentStatus(
                    name=name,
                    ready=(deployment.get("status") or {}).get("readyReplicas") or 0,
                    desired=deployment["spec"].get("replicas", 1),
                    reasons=tuple(sorted(reasons)),
                )
            )
        return rows
