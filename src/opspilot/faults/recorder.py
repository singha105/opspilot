"""Record every read tool's view of the cluster during a fault, for deterministic evals.

The recorder injects a scenario, waits for its symptom plus a settle period, then
calls every read tool exhaustively for the namespace through the same ``K8sTools``
code path the agent uses (so recorded outputs are redacted and capped exactly like
live ones). It saves ``evals/fixtures/<id>.json`` and resets the fault.
"""

import datetime as dt
import hashlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from opspilot import __version__
from opspilot.faults.injector import Injector, poll
from opspilot.faults.scenario import Scenario
from opspilot.logging import get_logger
from opspilot.mcp_servers.k8s_readonly.backend import K8sReadBackend
from opspilot.mcp_servers.k8s_readonly.replay import (
    RECORDED_SINCE,
    RECORDED_TAIL,
    bind_args,
    call_key,
)
from opspilot.mcp_servers.k8s_readonly.server import K8sTools

HEALTHY = "healthy"
SETTLE_S = 20.0
NAME_LABEL = "app.kubernetes.io/name"

log = get_logger(__name__)


class Recorder:
    """Calls every read tool for one namespace and collects canonical outputs."""

    def __init__(self, tools: K8sTools, backend: K8sReadBackend, namespace: str) -> None:
        self.tools = tools
        self.backend = backend
        self.namespace = namespace
        self.calls: dict[str, Any] = {}

    def _record(self, tool: str, **kwargs: Any) -> dict[str, Any]:
        text = getattr(self.tools, tool)(**kwargs)
        output: dict[str, Any] = json.loads(text)
        self.calls[call_key(tool, bind_args(tool, **kwargs))] = output
        return output

    def record_all(self) -> dict[str, Any]:
        """Call every read tool exhaustively; return the canonical call map."""
        ns = self.namespace
        pods = self._record("list_pods", namespace=ns).get("pods", [])
        deployments = sorted(
            d.metadata.name for d in self.backend.apis.apps.list_namespaced_deployment(ns).items
        )
        for name in deployments:
            self._record("list_pods", namespace=ns, label_selector=f"{NAME_LABEL}={name}")
            self._record("get_deployment", namespace=ns, name=name)
            self._record("get_rollout_history", namespace=ns, name=name)

        objects = list(deployments)
        for pod in pods:
            detail = self._record("describe_pod", namespace=ns, name=pod["name"])
            containers = [
                c["name"] for c in detail.get("initContainers", []) + detail.get("containers", [])
            ]
            for container in containers:
                # A lone container is recorded without a name, matching the default call.
                arg = None if len(containers) == 1 else container
                for previous in (False, True):
                    self._record(
                        "get_pod_logs",
                        namespace=ns,
                        name=pod["name"],
                        container=arg,
                        previous=previous,
                        tail_lines=RECORDED_TAIL,
                    )
            objects.append(pod["name"])

        services = self._record("list_services", namespace=ns).get("services", [])
        for service in services:
            self._record("get_service_endpoints", namespace=ns, name=service["name"])
            objects.append(service["name"])
        for name in self.backend.configmap_names(ns):
            self._record("get_configmap", namespace=ns, name=name)
        pvcs = self._record("list_pvcs", namespace=ns).get("pvcs", [])
        objects.extend(p["name"] for p in pvcs)
        self._record("list_nodes")

        self._record("get_events", namespace=ns, since_minutes=RECORDED_SINCE)
        for name in sorted(set(objects)):
            self._record(
                "get_events", namespace=ns, involved_object_name=name, since_minutes=RECORDED_SINCE
            )
        return self.calls


def scenario_hash(path: Path | None) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path else None


def fixture_problems(scenarios_dir: Path, fixtures_dir: Path) -> list[str]:
    """Why the fixtures do not match the scenarios (empty list = every fixture is current).

    Every scenario needs a fixture recorded from the current version of its file (same
    SHA-256), a healthy baseline must exist, and no fixture may be left without a scenario.
    """
    problems = []
    scenario_files = {p.stem: p for p in sorted(scenarios_dir.glob("*.yaml"))}
    fixtures = {p.stem: p for p in sorted(fixtures_dir.glob("*.json"))}
    if HEALTHY not in fixtures:
        problems.append("missing the healthy baseline fixture")
    for fixture_id, path in scenario_files.items():
        if fixture_id not in fixtures:
            problems.append(f"{fixture_id}: no fixture; run opspilot faults record {fixture_id}")
            continue
        meta = json.loads(fixtures[fixture_id].read_text())["metadata"]
        if meta.get("scenario_sha256") != scenario_hash(path):
            problems.append(
                f"{fixture_id}: fixture was recorded from another version of the scenario file"
            )
    for fixture_id in sorted(set(fixtures) - set(scenario_files) - {HEALTHY}):
        problems.append(f"{fixture_id}: fixture without a scenario")
    return problems


def record_fixture(
    scenario: Scenario | None,
    scenario_path: Path | None,
    injector: Injector,
    tools: K8sTools,
    backend: K8sReadBackend,
    out_dir: Path,
    namespace: str,
    settle_s: float = SETTLE_S,
    sleep: Callable[[float], None] = time.sleep,
) -> Path:
    """Inject (unless healthy), wait, record every read call, save, then reset."""
    fixture_id = scenario.id if scenario else HEALTHY
    poll(lambda: injector.all_ready(namespace), timeout_s=180, interval_s=3)
    symptom_s: float | None = None
    try:
        if scenario:
            injector.inject(scenario)
            symptom_s = injector.wait_for_symptom(scenario)
        sleep(settle_s)
        calls = Recorder(tools, backend, namespace).record_all()
        recorded_at = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    finally:
        if scenario:
            injector.reset(scenario)
    fixture = {
        "metadata": {
            "fixture_id": fixture_id,
            "scenario_file": str(scenario_path) if scenario_path else None,
            "scenario_sha256": scenario_hash(scenario_path),
            "namespace": namespace,
            "recorded_at": recorded_at,
            "kubernetes_version": backend.server_version(),
            "opspilot_version": __version__,
            "identity": "opspilot-reader",
            "symptom_seconds": round(symptom_s, 1) if symptom_s is not None else None,
            "settle_seconds": settle_s,
            "tool_calls": len(calls),
        },
        "calls": dict(sorted(calls.items())),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{fixture_id}.json"
    path.write_text(json.dumps(fixture, indent=1, ensure_ascii=False) + "\n")
    log.info("fixture recorded", fixture=fixture_id, calls=len(calls), path=str(path))
    return path
