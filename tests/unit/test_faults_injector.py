import copy
import subprocess
from pathlib import Path
from typing import Any

import pytest
from kubernetes import client

from opspilot.faults import injector as inj
from opspilot.faults.scenario import (
    ExpectedSymptom,
    Scale,
    SetEnv,
    SetImage,
    SetProbe,
    UnsetEnv,
    load_scenarios,
)

SCENARIOS = load_scenarios(Path(__file__).parents[2] / "faults" / "scenarios")


def deployment(name: str = "payments-api", ready: int = 1, replicas: int = 1) -> dict[str, Any]:
    return {
        "metadata": {"name": name, "generation": 2},
        "spec": {
            "replicas": replicas,
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "app",
                            "image": "opspilot-demo-svc:dev",
                            "env": [
                                {"name": "SERVICE_NAME", "value": name},
                                {"name": "REDIS_URL", "valueFrom": {"configMapKeyRef": {}}},
                            ],
                            "readinessProbe": {"httpGet": {"path": "/readyz", "port": "http"}},
                        }
                    ]
                }
            },
        },
        "status": {
            "observedGeneration": 2,
            "readyReplicas": ready,
            "updatedReplicas": ready,
        },
    }


def pod(**container_status: Any) -> dict[str, Any]:
    return {"status": {"containerStatuses": [container_status]}}


# ---- apply_injection ----------------------------------------------------------------


def test_set_env_adds_and_overrides() -> None:
    d = deployment()
    inj.apply_injection(d, SetEnv(type="set_env", env={"MEMORY_BALLAST_MB": "300"}), "app")
    inj.apply_injection(d, SetEnv(type="set_env", env={"REDIS_URL": "redis://x"}), "app")
    env = {e["name"]: e for e in inj.find_container(d, "app")["env"]}
    assert env["MEMORY_BALLAST_MB"] == {"name": "MEMORY_BALLAST_MB", "value": "300"}
    assert env["REDIS_URL"] == {"name": "REDIS_URL", "value": "redis://x"}


def test_unset_env_removes() -> None:
    d = deployment()
    inj.apply_injection(d, UnsetEnv(type="unset_env", names=["REDIS_URL"]), "app")
    assert [e["name"] for e in inj.find_container(d, "app")["env"]] == ["SERVICE_NAME"]


def test_set_image() -> None:
    d = deployment()
    inj.apply_injection(d, SetImage(type="set_image", image="x:missing"), "app")
    assert inj.find_container(d, "app")["image"] == "x:missing"


def test_set_probe_path() -> None:
    d = deployment()
    inj.apply_injection(d, SetProbe(type="set_probe", probe="readiness", path="/v2"), "app")
    assert inj.find_container(d, "app")["readinessProbe"]["httpGet"]["path"] == "/v2"


def test_set_probe_without_http_probe_fails() -> None:
    with pytest.raises(ValueError, match="no HTTP liveness probe"):
        inj.apply_injection(
            deployment(), SetProbe(type="set_probe", probe="liveness", path="/x"), "app"
        )


def test_scale() -> None:
    d = deployment()
    inj.apply_injection(d, Scale(type="scale", replicas=0), "ignored")
    assert d["spec"]["replicas"] == 0


def test_unknown_container() -> None:
    with pytest.raises(KeyError, match="sidecar"):
        inj.find_container(deployment(), "sidecar")


# ---- observation helpers --------------------------------------------------------------


def test_pod_reasons_reads_state_and_last_state() -> None:
    p = pod(
        state={"waiting": {"reason": "CrashLoopBackOff"}},
        lastState={"terminated": {"reason": "OOMKilled", "exitCode": 137}},
    )
    assert inj.pod_reasons(p) == {"CrashLoopBackOff", "OOMKilled"}
    assert inj.pod_reasons({"status": {}}) == set()


def test_is_ready() -> None:
    assert inj.is_ready(deployment())
    assert not inj.is_ready(deployment(ready=0))
    assert inj.is_ready(deployment(ready=0, replicas=0))
    stale = deployment()
    stale["status"]["observedGeneration"] = 1
    assert not inj.is_ready(stale)


def test_symptom_pod_reason() -> None:
    symptom = ExpectedSymptom(pod_reason_any_of=["OOMKilled"])
    crashing = pod(lastState={"terminated": {"reason": "OOMKilled"}})
    assert inj.symptom_met(symptom, [crashing], {})
    assert not inj.symptom_met(symptom, [pod(state={"running": {}})], {})


def test_symptom_not_ready_deployments() -> None:
    symptom = ExpectedSymptom(not_ready_deployments=["orders-api"])
    assert inj.symptom_met(symptom, [], {"orders-api": deployment("orders-api", ready=0)})
    assert not inj.symptom_met(symptom, [], {"orders-api": deployment("orders-api")})
    assert not inj.symptom_met(symptom, [], {})


def test_select_deployments() -> None:
    rendered = (
        "kind: Namespace\nmetadata: {name: shop}\n---\n"
        "kind: Deployment\nmetadata: {name: redis}\n---\n"
        "kind: Deployment\nmetadata: {name: payments-api}\n"
    )
    out = inj.select_deployments(rendered, {"redis"})
    assert "redis" in out
    assert "payments-api" not in out
    with pytest.raises(KeyError, match="nope"):
        inj.select_deployments(rendered, {"nope"})


def test_poll_returns_and_times_out() -> None:
    calls = iter([False, True])
    assert inj.poll(lambda: next(calls), timeout_s=5, interval_s=0) >= 0
    with pytest.raises(inj.FaultTimeoutError):
        inj.poll(lambda: False, timeout_s=0, interval_s=0)


# ---- Injector against a fake API -------------------------------------------------------


class _Obj:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.metadata = type("Meta", (), {"name": data["metadata"]["name"]})()


class FakeApps:
    def __init__(self, deployments: dict[str, dict[str, Any]]) -> None:
        self.deployments = deployments
        self.replaced: list[tuple[str, dict[str, Any]]] = []

    def read_namespaced_deployment(self, name: str, namespace: str) -> _Obj:
        return _Obj(copy.deepcopy(self.deployments[name]))

    def replace_namespaced_deployment(self, name: str, namespace: str, body: Any) -> None:
        self.replaced.append((name, body))

    def list_namespaced_deployment(self, namespace: str) -> Any:
        return type("L", (), {"items": [_Obj(d) for d in self.deployments.values()]})()


class FakeCore:
    def __init__(self, pods: list[dict[str, Any]]) -> None:
        self.pods = pods

    def list_namespaced_pod(self, namespace: str, label_selector: str) -> Any:
        return type(
            "L", (), {"items": [_Obj({"metadata": {"name": "p"}, **p}) for p in self.pods]}
        )()


class FakeApiClient:
    def sanitize_for_serialization(self, obj: _Obj) -> dict[str, Any]:
        return copy.deepcopy(obj.data)


@pytest.fixture
def fake_injector(monkeypatch: pytest.MonkeyPatch) -> tuple[inj.Injector, FakeApps, list[Any]]:
    apps = FakeApps({"payments-api": deployment(), "orders-api": deployment("orders-api")})
    core = FakeCore([pod(lastState={"terminated": {"reason": "OOMKilled"}})])
    monkeypatch.setattr(client, "AppsV1Api", lambda api: apps)
    monkeypatch.setattr(client, "CoreV1Api", lambda api: core)
    commands: list[Any] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append((args, kwargs.get("input")))
        stdout = "kind: Deployment\nmetadata: {name: payments-api}\n" if "kustomize" in args else ""
        return subprocess.CompletedProcess(args, 0, stdout, "")

    injector = inj.Injector(
        FakeApiClient(),  # type: ignore[arg-type]
        context="k3d-test",
        base_dir=Path("demo/k8s/base"),
        run=run,
    )
    return injector, apps, commands


def test_injector_inject_replaces_without_status(
    fake_injector: tuple[inj.Injector, FakeApps, list[Any]],
) -> None:
    injector, apps, _ = fake_injector
    injector.inject(SCENARIOS["oom-payments"])
    name, body = apps.replaced[0]
    assert name == "payments-api"
    assert "status" not in body
    env = {e["name"] for e in inj.find_container(body, "app")["env"]}
    assert "MEMORY_BALLAST_MB" in env


def test_injector_symptom_and_status(
    fake_injector: tuple[inj.Injector, FakeApps, list[Any]],
) -> None:
    injector, _, _ = fake_injector
    assert injector.wait_for_symptom(SCENARIOS["oom-payments"], interval_s=0) >= 0
    rows = {row.name: row for row in injector.status("shop")}
    assert rows["payments-api"].reasons == ("OOMKilled",)
    assert not rows["payments-api"].healthy


def test_injector_reset_uses_admin_context(
    fake_injector: tuple[inj.Injector, FakeApps, list[Any]],
) -> None:
    injector, _, commands = fake_injector
    assert injector.reset(SCENARIOS["oom-payments"], timeout_s=1) >= 0
    for args, _ in commands:
        assert args[:3] == ["kubectl", "--context", "k3d-test"]
    verbs = [args[3] for args, _ in commands]
    assert verbs == ["kustomize", "replace", "apply"]
    assert "payments-api" in commands[1][1]
