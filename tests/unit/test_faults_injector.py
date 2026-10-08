import copy
import subprocess
from pathlib import Path
from typing import Any

import pytest
from kubernetes import client

from opspilot.faults import injector as inj
from opspilot.faults.scenario import (
    AddEnvFrom,
    AddInitContainer,
    AddPvc,
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


SERVICE: dict[str, Any] = {
    "metadata": {"name": "payments-api"},
    "spec": {
        "selector": {"app.kubernetes.io/name": "payments-api"},
        "ports": [{"name": "http", "port": 8080, "targetPort": "http"}],
    },
}


KUSTOMIZED = (
    "kind: Deployment\nmetadata: {name: payments-api}\n---\n"
    "kind: Deployment\nmetadata: {name: orders-api}\n---\n"
    "kind: Service\nmetadata: {name: payments-api}\n"
)


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


def test_select_docs() -> None:
    rendered = (
        "kind: Namespace\nmetadata: {name: shop}\n---\n"
        "kind: Deployment\nmetadata: {name: redis}\n---\n"
        "kind: Deployment\nmetadata: {name: payments-api}\n"
    )
    out = inj.select_docs(rendered, "Deployment", {"redis"})
    assert "redis" in out
    assert "payments-api" not in out
    with pytest.raises(KeyError, match="nope"):
        inj.select_docs(rendered, "Deployment", {"nope"})


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
        self.services = {"payments-api": SERVICE}
        self.replaced: list[tuple[str, dict[str, Any]]] = []
        self.created: list[dict[str, Any]] = []

    def read_namespaced_service(self, name: str, namespace: str) -> _Obj:
        return _Obj(copy.deepcopy(self.services[name]))

    def replace_namespaced_service(self, name: str, namespace: str, body: Any) -> None:
        self.replaced.append((name, body))

    def create_namespaced_persistent_volume_claim(self, namespace: str, body: Any) -> None:
        self.created.append(body)

    def list_namespaced_pod(self, namespace: str, label_selector: str = "") -> Any:
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
        stdout = KUSTOMIZED if "kustomize" in args else ""
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


# ---- Day 5 injection types ------------------------------------------------------------


def test_resources_command_and_affinity() -> None:
    d = deployment()
    inj.apply_injection(
        d,
        SetResources(type="set_resources", requests={"cpu": "50"}, limits={"memory": "48Mi"}),
        "app",
    )
    inj.apply_injection(d, SetCommand(type="set_command", args=["--enable-turbo"]), "app")
    inj.apply_injection(
        d,
        SetAffinity(
            type="set_affinity",
            node_selector={"disktype": "ssd"},
            required_node_labels={"zone": ["z9"]},
        ),
        "app",
    )
    c = inj.find_container(d, "app")
    assert c["resources"] == {"requests": {"cpu": "50"}, "limits": {"memory": "48Mi"}}
    assert c["args"] == ["--enable-turbo"]
    assert "command" not in c
    spec = d["spec"]["template"]["spec"]
    assert spec["nodeSelector"] == {"disktype": "ssd"}
    terms = spec["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
    assert terms["nodeSelectorTerms"][0]["matchExpressions"] == [
        {"key": "zone", "operator": "In", "values": ["z9"]}
    ]


def test_init_container_env_refs_and_pvc() -> None:
    d = deployment()
    inj.apply_injection(
        d,
        AddInitContainer(type="add_init_container", name="wait-db", args=["wait-for", "db:5432"]),
        "app",
    )
    inj.apply_injection(d, AddEnvFrom(type="add_env_from", configmap="orders-flags"), "app")
    inj.apply_injection(
        d,
        SetSecretEnv(type="set_secret_env", name="API_KEY", secret="pay-keys", key="api-key"),
        "app",
    )
    pvc = AddPvc(type="add_pvc", name="cache", mount_path="/cache", storage_class="fast-ssd")
    inj.apply_injection(d, pvc, "app")
    spec = d["spec"]["template"]["spec"]
    init = spec["initContainers"][0]
    assert (init["name"], init["image"], init["args"]) == (
        "wait-db",
        "opspilot-demo-svc:dev",
        ["wait-for", "db:5432"],
    )
    c = inj.find_container(d, "app")
    assert c["envFrom"] == [{"configMapRef": {"name": "orders-flags"}}]
    assert c["env"][-1] == {
        "name": "API_KEY",
        "valueFrom": {"secretKeyRef": {"name": "pay-keys", "key": "api-key"}},
    }
    assert spec["volumes"] == [{"name": "cache", "persistentVolumeClaim": {"claimName": "cache"}}]
    assert c["volumeMounts"] == [{"name": "cache", "mountPath": "/cache"}]
    manifest = inj.pvc_manifest(pvc, "pvc-x")
    assert manifest["metadata"]["labels"] == {inj.FAULT_LABEL: "pvc-x"}
    assert manifest["spec"]["storageClassName"] == "fast-ssd"
    assert manifest["spec"]["accessModes"] == ["ReadWriteOnce"]


def test_probe_port_and_timing() -> None:
    d = deployment()
    inj.apply_injection(
        d,
        SetProbe(
            type="set_probe", probe="readiness", port=9090, initial_delay_s=0, failure_threshold=1
        ),
        "app",
    )
    probe = inj.find_container(d, "app")["readinessProbe"]
    assert probe["httpGet"] == {"path": "/readyz", "port": 9090}
    assert (probe["initialDelaySeconds"], probe["failureThreshold"]) == (0, 1)


def test_service_patch_and_broken_service_detection() -> None:
    ready_pod = {
        "metadata": {"labels": {"app.kubernetes.io/name": "payments-api"}},
        "spec": {"containers": [{"ports": [{"name": "http", "containerPort": 8080}]}]},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }
    service = copy.deepcopy(SERVICE)
    assert not inj.service_broken(service, [ready_pod])
    inj.apply_service_patch(service, PatchService(type="patch_service", target_port=9090))
    assert inj.service_broken(service, [ready_pod])  # no pod exposes 9090
    service = copy.deepcopy(SERVICE)
    inj.apply_service_patch(
        service,
        PatchService(type="patch_service", selector={"app.kubernetes.io/name": "payment-api"}),
    )
    assert inj.service_broken(service, [ready_pod])  # selector matches nothing
    symptom = ExpectedSymptom(broken_services=["payments-api"])
    assert inj.symptom_met(symptom, [], {}, {"payments-api": service}, [ready_pod])
    assert not inj.symptom_met(symptom, [], {}, {"payments-api": SERVICE}, [ready_pod])


def test_symptom_restarts_and_unschedulable() -> None:
    restarted = pod(restartCount=2)
    assert inj.symptom_met(ExpectedSymptom(min_restarts=1), [restarted], {})
    assert not inj.symptom_met(ExpectedSymptom(min_restarts=3), [restarted], {})
    pending = {
        "status": {
            "conditions": [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable"}]
        }
    }
    assert inj.pod_reasons(pending) == {"Unschedulable"}


def test_scenario_validation_of_new_types() -> None:
    with pytest.raises(ValueError, match="at least one change"):
        SetProbe(type="set_probe", probe="liveness")
    with pytest.raises(ValueError, match="selector or target_port"):
        PatchService(type="patch_service")
    with pytest.raises(ValueError, match="command or args"):
        SetCommand(type="set_command")


def _scenario(**overrides: Any) -> Scenario:
    data = SCENARIOS["oom-payments"].model_dump(mode="json")
    return Scenario.model_validate({**data, **overrides})


def test_injector_patches_services_and_creates_pvcs(
    fake_injector: tuple[inj.Injector, FakeApps, list[Any]],
) -> None:
    injector, apps, commands = fake_injector
    core: FakeCore = injector._core  # type: ignore[assignment]
    scenario = _scenario(
        id="pvc-x",
        inject=[
            {"type": "add_pvc", "name": "cache", "mount_path": "/cache"},
            {"type": "patch_service", "target_port": 9090},
            {"type": "set_env", "deployment": "orders-api", "env": {"LOG_INJECTION_TEXT": "hi"}},
        ],
    )
    assert scenario.deployments == ["payments-api", "orders-api"]
    apps.deployments["orders-api"]["spec"]["template"]["spec"]["containers"][0]["name"] = "app"
    scenario = scenario.model_copy(
        update={"target": scenario.target.model_copy(update={"container": "main"})}
    )
    apps.deployments["payments-api"]["spec"]["template"]["spec"]["containers"][0]["name"] = "main"
    assert scenario.services == ["payments-api"]
    injector.inject(scenario)
    assert core.created[0]["metadata"]["name"] == "cache"
    assert [name for name, _ in apps.replaced] == ["payments-api", "orders-api"]
    assert core.replaced[0][1]["spec"]["ports"][0]["targetPort"] == 9090
    orders_env = inj.find_container(apps.replaced[1][1], "app")["env"]
    assert {"name": "LOG_INJECTION_TEXT", "value": "hi"} in orders_env
    injector.reset(scenario, timeout_s=1)
    verbs = [args[3] for args, _ in commands]
    assert verbs == ["kustomize", "replace", "delete", "apply"]
    assert commands[2][0][-3:] == ["-l", f"{inj.FAULT_LABEL}=pvc-x", "--wait=false"]
