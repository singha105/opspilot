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
    AddEnvFrom,
    AddInitContainer,
    AddPvc,
    DeploymentInjection,
    ExpectedSymptom,
    PatchService,
    Scale,
    Scenario,
    SetAffinity,
    SetCommand,
    SetEnv,
    SetImage,
    SetProbe,
    SetResources,
    SetSecretEnv,
    UnsetEnv,
)
from opspilot.logging import get_logger

Runner = Callable[..., subprocess.CompletedProcess[str]]
JsonDict = dict[str, Any]

FAULT_LABEL = "opspilot.dev/fault"

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


def apply_injection(
    deployment: JsonDict, injection: DeploymentInjection, container_name: str
) -> None:
    """Mutate a Deployment manifest (camelCase JSON) in place to inject one fault."""
    pod_spec: JsonDict = deployment["spec"]["template"]["spec"]
    if isinstance(injection, Scale):
        deployment["spec"]["replicas"] = injection.replicas
        return
    if isinstance(injection, SetAffinity):
        if injection.node_selector:
            pod_spec["nodeSelector"] = dict(injection.node_selector)
        if injection.required_node_labels:
            terms = [
                {"key": k, "operator": "In", "values": v}
                for k, v in injection.required_node_labels.items()
            ]
            pod_spec["affinity"] = {
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [{"matchExpressions": terms}]
                    }
                }
            }
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
        _set_probe(container, injection, container_name)
    elif isinstance(injection, SetResources):
        resources = container.setdefault("resources", {})
        for key, values in (("requests", injection.requests), ("limits", injection.limits)):
            if values:
                resources.setdefault(key, {}).update(values)
    elif isinstance(injection, SetCommand):
        if injection.command is not None:
            container["command"] = list(injection.command)
        if injection.args is not None:
            container["args"] = list(injection.args)
    elif isinstance(injection, AddInitContainer):
        init = {
            "name": injection.name,
            "image": injection.image or container["image"],
            "imagePullPolicy": "IfNotPresent",
            "args": list(injection.args),
            "env": [e for e in container.get("env") or [] if e["name"] == "SERVICE_NAME"],
            "resources": {
                "requests": {"cpu": "10m", "memory": "16Mi"},
                "limits": {"memory": "64Mi"},
            },
        }
        if "securityContext" in container:
            init["securityContext"] = container["securityContext"]
        pod_spec["initContainers"] = [*(pod_spec.get("initContainers") or []), init]
    elif isinstance(injection, AddEnvFrom):
        container["envFrom"] = [
            *(container.get("envFrom") or []),
            {"configMapRef": {"name": injection.configmap}},
        ]
    elif isinstance(injection, SetSecretEnv):
        env = [e for e in container.get("env") or [] if e["name"] != injection.name]
        ref = {"secretKeyRef": {"name": injection.secret, "key": injection.key}}
        container["env"] = [*env, {"name": injection.name, "valueFrom": ref}]
    elif isinstance(injection, AddPvc):
        volume = injection.name
        pod_spec["volumes"] = [
            *(pod_spec.get("volumes") or []),
            {"name": volume, "persistentVolumeClaim": {"claimName": injection.name}},
        ]
        container["volumeMounts"] = [
            *(container.get("volumeMounts") or []),
            {"name": volume, "mountPath": injection.mount_path},
        ]


def _set_probe(container: JsonDict, injection: SetProbe, container_name: str) -> None:
    probe = container.get(f"{injection.probe}Probe")
    if not probe or "httpGet" not in probe:
        raise ValueError(f"container {container_name!r} has no HTTP {injection.probe} probe")
    if injection.path is not None:
        probe["httpGet"]["path"] = injection.path
    if injection.port is not None:
        probe["httpGet"]["port"] = injection.port
    for field, key in (
        ("initial_delay_s", "initialDelaySeconds"),
        ("period_s", "periodSeconds"),
        ("failure_threshold", "failureThreshold"),
    ):
        value = getattr(injection, field)
        if value is not None:
            probe[key] = value


def pvc_manifest(injection: AddPvc, scenario_id: str) -> JsonDict:
    """The PersistentVolumeClaim an ``add_pvc`` injection creates (labelled for reset)."""
    spec: JsonDict = {
        "accessModes": list(injection.access_modes),
        "resources": {"requests": {"storage": injection.size}},
    }
    if injection.storage_class is not None:
        spec["storageClassName"] = injection.storage_class
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": injection.name, "labels": {FAULT_LABEL: scenario_id}},
        "spec": spec,
    }


def apply_service_patch(service: JsonDict, patch: PatchService) -> None:
    """Mutate a Service manifest in place: its selector and/or every port's targetPort."""
    if patch.selector is not None:
        service["spec"]["selector"] = dict(patch.selector)
    if patch.target_port is not None:
        for port in service["spec"].get("ports") or []:
            port["targetPort"] = patch.target_port


def pod_reasons(pod: JsonDict) -> set[str]:
    """Waiting/terminated reasons (current and last state) of a pod's containers, plus the
    reason a pod cannot be scheduled (e.g. ``Unschedulable``)."""
    reasons: set[str] = set()
    status = pod.get("status") or {}
    for condition in status.get("conditions") or []:
        if condition.get("type") == "PodScheduled" and condition.get("status") == "False":
            reasons.add(condition.get("reason") or "Unschedulable")
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


def restarts(pod: JsonDict) -> int:
    """Total restarts of a pod's containers."""
    statuses = (pod.get("status") or {}).get("containerStatuses") or []
    return sum(int(cs.get("restartCount") or 0) for cs in statuses)


def _pod_ready(pod: JsonDict) -> bool:
    conditions = (pod.get("status") or {}).get("conditions") or []
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)


def _port_names_and_numbers(pod: JsonDict) -> set[int | str]:
    ports: set[int | str] = set()
    for container in (pod.get("spec") or {}).get("containers") or []:
        for port in container.get("ports") or []:
            ports.add(int(port["containerPort"]))
            if port.get("name"):
                ports.add(port["name"])
    return ports


def service_broken(service: JsonDict, pods: Iterable[JsonDict]) -> bool:
    """True when no ready pod matches the selector, or no matching pod exposes a targetPort."""
    selector = (service.get("spec") or {}).get("selector") or {}
    matching = [
        p
        for p in pods
        if selector
        and all(
            ((p.get("metadata") or {}).get("labels") or {}).get(k) == v for k, v in selector.items()
        )
        and _pod_ready(p)
    ]
    if not matching:
        return True
    exposed = set().union(*(_port_names_and_numbers(p) for p in matching))
    for port in (service.get("spec") or {}).get("ports") or []:
        target = port.get("targetPort", port.get("port"))
        if target not in exposed:
            return True
    return False


def symptom_met(
    symptom: ExpectedSymptom,
    target_pods: Iterable[JsonDict],
    deployments: dict[str, JsonDict],
    services: dict[str, JsonDict] | None = None,
    namespace_pods: Iterable[JsonDict] = (),
) -> bool:
    """Check every condition of ``symptom`` against observed pods, Deployments and Services."""
    target_pods = list(target_pods)
    if symptom.pod_reason_any_of:
        seen = set().union(*(pod_reasons(p) for p in target_pods))
        if not seen & set(symptom.pod_reason_any_of):
            return False
    if symptom.min_restarts and not any(restarts(p) >= symptom.min_restarts for p in target_pods):
        return False
    if symptom.broken_services:
        pods = list(namespace_pods)
        for name in symptom.broken_services:
            service = (services or {}).get(name)
            if service is None or not service_broken(service, pods):
                return False
    for name in symptom.not_ready_deployments:
        deployment = deployments.get(name)
        if deployment is None:
            return False
        status = deployment.get("status") or {}
        if (status.get("readyReplicas") or 0) >= deployment["spec"].get("replicas", 1):
            return False
    return True


def select_docs(rendered: str, kind: str, names: set[str]) -> str:
    """Pick the named documents of one kind out of rendered kustomize output."""
    docs = [
        doc
        for doc in yaml.safe_load_all(rendered)
        if doc and doc.get("kind") == kind and doc["metadata"]["name"] in names
    ]
    missing = names - {doc["metadata"]["name"] for doc in docs}
    if missing:
        raise KeyError(f"{kind} objects not in base manifests: {sorted(missing)}")
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
        """Apply every injection of ``scenario`` (Deployments, Services and new PVCs)."""
        namespace = scenario.namespace
        for injection in scenario.inject:
            if isinstance(injection, AddPvc):
                body = pvc_manifest(injection, scenario.id)
                self._core.create_namespaced_persistent_volume_claim(namespace, body)
        for name in scenario.deployments:
            deployment = self._to_dict(self._apps.read_namespaced_deployment(name, namespace))
            for injection in scenario.inject:
                if isinstance(injection, PatchService):
                    continue
                if (injection.deployment or scenario.target.name) == name:
                    default = scenario.target.container if name == scenario.target.name else "app"
                    apply_injection(deployment, injection, injection.container or default)
            deployment.pop("status", None)
            self._apps.replace_namespaced_deployment(name, namespace, deployment)
        for injection in scenario.inject:
            if isinstance(injection, PatchService):
                name = injection.service or scenario.target.name
                service = self._to_dict(self._core.read_namespaced_service(name, namespace))
                apply_service_patch(service, injection)
                service.pop("status", None)
                self._core.replace_namespaced_service(name, namespace, service)
        log.info("fault injected", scenario=scenario.id, target=scenario.target.name)

    def symptom_present(self, scenario: Scenario) -> bool:
        """True when the cluster currently shows the scenario's expected symptom."""
        namespace, symptom = scenario.namespace, scenario.expected_symptom
        services: dict[str, JsonDict] = {}
        namespace_pods: list[JsonDict] = []
        if symptom.broken_services:
            for name in symptom.broken_services:
                services[name] = self._to_dict(self._core.read_namespaced_service(name, namespace))
            namespace_pods = [
                self._to_dict(p) for p in self._core.list_namespaced_pod(namespace).items
            ]
        return symptom_met(
            symptom,
            self._pods(namespace, scenario.target.name),
            self._deployments(namespace),
            services,
            namespace_pods,
        )

    def wait_for_symptom(self, scenario: Scenario, interval_s: float = 3.0) -> float:
        """Block until the expected symptom appears; return seconds taken."""
        timeout = scenario.expected_symptom.timeout_s
        elapsed = poll(lambda: self.symptom_present(scenario), timeout, interval_s)
        log.info("symptom observed", scenario=scenario.id, seconds=round(elapsed, 1))
        return elapsed

    def reset(self, scenario: Scenario, timeout_s: float = 180.0) -> float:
        """Restore every touched object from the base manifests and wait until healthy."""
        rendered = self._kubectl("kustomize", str(self._base_dir))
        docs = select_docs(rendered, "Deployment", set(scenario.deployments))
        if scenario.services:
            docs += "---\n" + select_docs(rendered, "Service", set(scenario.services))
        self._kubectl("replace", "--save-config", "-f", "-", stdin=docs)
        if any(isinstance(i, AddPvc) for i in scenario.inject):
            self._kubectl(
                "delete",
                "pvc",
                "-n",
                scenario.namespace,
                "-l",
                f"{FAULT_LABEL}={scenario.id}",
                "--wait=false",
            )
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
