import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opspilot.faults.recorder import HEALTHY, Recorder, record_fixture
from opspilot.faults.scenario import load_scenarios
from opspilot.mcp_servers.common import AuditLog, ToolRunner
from opspilot.mcp_servers.k8s_readonly.replay import ReplayBackend
from opspilot.mcp_servers.k8s_readonly.server import K8sTools

ROOT = Path(__file__).parents[2]
FIXTURES = ROOT / "evals" / "fixtures"


class ReplayAsLive:
    """A replay backend that also answers the two recorder-only enumeration calls."""

    def __init__(self, fixture: Path) -> None:
        self.replay = ReplayBackend(fixture)
        calls = [json.loads(k) for k in self.replay.calls]
        deployments = sorted({c["args"]["name"] for c in calls if c["tool"] == "get_deployment"})
        self._configmaps = sorted(
            {c["args"]["name"] for c in calls if c["tool"] == "get_configmap"}
        )
        items = [SimpleNamespace(metadata=SimpleNamespace(name=n)) for n in deployments]
        self.apis = SimpleNamespace(
            apps=SimpleNamespace(list_namespaced_deployment=lambda ns: SimpleNamespace(items=items))
        )

    def configmap_names(self, namespace: str) -> list[str]:
        return self._configmaps

    def server_version(self) -> str:
        return str(self.replay.metadata["kubernetes_version"])

    def __getattr__(self, name: str) -> Any:
        return getattr(self.replay, name)


def tools_for(backend: Any, tmp_path: Path) -> K8sTools:
    return K8sTools(
        backend, ToolRunner(AuditLog(tmp_path / "a.jsonl", "opspilot-k8s", "replay")), ["shop"]
    )


@pytest.mark.parametrize("fixture", ["healthy", "oom-payments", "redis-down"])
def test_rerecording_a_fixture_reproduces_it(fixture: str, tmp_path: Path) -> None:
    path = FIXTURES / f"{fixture}.json"
    backend = ReplayAsLive(path)
    calls = Recorder(tools_for(backend, tmp_path), backend, "shop").record_all()  # type: ignore[arg-type]
    assert calls == json.loads(path.read_text())["calls"]


class FakeInjector:
    def __init__(self, fail_on_wait: bool = False) -> None:
        self.events: list[str] = []
        self.fail_on_wait = fail_on_wait

    def all_ready(self, namespace: str) -> bool:
        return True

    def inject(self, scenario: Any) -> None:
        self.events.append("inject")

    def wait_for_symptom(self, scenario: Any) -> float:
        if self.fail_on_wait:
            raise TimeoutError("no symptom")
        self.events.append("symptom")
        return 4.2

    def reset(self, scenario: Any, timeout_s: float = 180.0) -> float:
        self.events.append("reset")
        return 1.0


def test_record_fixture_writes_metadata_and_resets(tmp_path: Path) -> None:
    scenario = load_scenarios(ROOT / "faults" / "scenarios")["oom-payments"]
    backend = ReplayAsLive(FIXTURES / "oom-payments.json")
    injector = FakeInjector()
    out = record_fixture(
        scenario,
        ROOT / "faults" / "scenarios" / "oom-payments.yaml",
        injector,  # type: ignore[arg-type]
        tools_for(backend, tmp_path),
        backend,
        tmp_path,
        "shop",
        sleep=lambda s: None,  # type: ignore[arg-type]
    )
    meta = json.loads(out.read_text())["metadata"]
    assert injector.events == ["inject", "symptom", "reset"]
    assert meta["fixture_id"] == "oom-payments"
    assert meta["symptom_seconds"] == 4.2
    assert len(meta["scenario_sha256"]) == 64


def test_record_fixture_resets_even_when_the_symptom_never_appears(tmp_path: Path) -> None:
    scenario = load_scenarios(ROOT / "faults" / "scenarios")["oom-payments"]
    backend = ReplayAsLive(FIXTURES / "oom-payments.json")
    injector = FakeInjector(fail_on_wait=True)
    with pytest.raises(TimeoutError):
        record_fixture(
            scenario,
            None,
            injector,
            tools_for(backend, tmp_path),
            backend,
            tmp_path,
            "shop",  # type: ignore[arg-type]
            sleep=lambda s: None,
        )
    assert injector.events == ["inject", "reset"]
    assert not (tmp_path / "oom-payments.json").exists()


def test_healthy_baseline_does_not_inject(tmp_path: Path) -> None:
    backend = ReplayAsLive(FIXTURES / "healthy.json")
    injector = FakeInjector()
    out = record_fixture(
        None,
        None,
        injector,
        tools_for(backend, tmp_path),
        backend,
        tmp_path,
        "shop",  # type: ignore[arg-type]
        sleep=lambda s: None,
    )
    assert out.name == f"{HEALTHY}.json"
    assert injector.events == []
