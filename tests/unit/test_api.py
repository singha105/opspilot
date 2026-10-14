from typing import Any

import pytest
from fastapi.testclient import TestClient

from opspilot.agent.observability import RunSummary
from opspilot.api.app import create_app, split_alerts
from opspilot.config import Settings
from opspilot.models.incident import ApprovalDecision
from opspilot.service import IncidentView, RunMeta, ServiceError

TOKEN = "t0k3n"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
WEBHOOK = {
    "version": "4",
    "status": "firing",
    "alerts": [
        {
            "status": "firing",
            "labels": {
                "alertname": "KubePodCrashLooping",
                "severity": "critical",
                "namespace": "shop",
                "opspilot_scenario": "oom-payments",
            },
            "annotations": {"summary": "payments-api pods are restarting"},
        },
        {"status": "resolved", "labels": {"alertname": "Old"}, "annotations": {}},
    ],
}
SIMPLE = {"name": "KubePodNotReady", "summary": "orders-api not ready", "scenario": "redis-down"}


class FakeService:
    """Records calls; stands in for the agent."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.runs: list[tuple[str, Any]] = []
        self.decisions: list[tuple[str, ApprovalDecision]] = []
        self.waiting = {"inc-1"}

    def create(self, **kw: Any) -> str:
        if kw.get("scenario") == "nope":
            raise ServiceError("unknown scenario 'nope'")
        self.created.append(kw)
        return f"inc-{len(self.created)}"

    def scenario_alert(self, scenario: str) -> tuple[dict[str, Any], Any]:
        return {"name": "FromScenario", "summary": scenario, "severity": "warning"}, None

    async def run(self, incident_id: str, alert: Any) -> None:
        self.runs.append((incident_id, alert))

    async def decide(self, incident_id: str, decision: ApprovalDecision) -> None:
        self.decisions.append((incident_id, decision))

    def meta(self, incident_id: str) -> RunMeta:
        if incident_id == "missing":
            raise ServiceError("no incident missing")
        return RunMeta(mode="replay")

    def pending(self, incident_id: str) -> dict[str, Any] | None:
        return {"proposal": {}} if incident_id in self.waiting else None

    def get(self, incident_id: str) -> IncidentView:
        self.meta(incident_id)
        return IncidentView(
            incident_id=incident_id, status="awaiting_approval", meta=RunMeta(mode="replay")
        )

    def list(self) -> list[RunSummary]:
        return [
            RunSummary(
                incident_id="inc-1",
                status="awaiting_approval",
                mode="replay",
                alert="payments-api pods are restarting",
                category=None,
                confidence=None,
                action=None,
                updated_at="2026-10-14T10:00:00+00:00",
            )
        ]


def client(mode: str = "replay", token: str | None = TOKEN) -> tuple[TestClient, FakeService]:
    service = FakeService()
    settings = Settings(_env_file=None, api_token=token, api_mode=mode)  # type: ignore[call-arg]
    return TestClient(create_app(service, settings)), service  # type: ignore[arg-type]


def test_healthz_is_open() -> None:
    c, _ = client()
    assert c.get("/healthz").json()["status"] == "ok"


def test_webhook_creates_one_incident_per_firing_alert() -> None:
    c, svc = client()
    r = c.post("/alerts", json=WEBHOOK, headers=AUTH)
    assert r.status_code == 202
    assert r.json() == {"incidents": ["inc-1"], "mode": "replay"}
    assert svc.created == [{"mode": "replay", "scenario": "oom-payments", "source": "webhook"}]
    incident, alert = svc.runs[0]  # the agent ran in the background
    assert incident == "inc-1"
    assert alert["alerts"][0]["labels"]["alertname"] == "KubePodCrashLooping"
    assert "opspilot_scenario" not in alert["alerts"][0]["labels"]  # never shown to the agent


def test_simple_form_and_empty_body_with_scenario() -> None:
    c, svc = client()
    assert c.post("/alerts", json=SIMPLE, headers=AUTH).status_code == 202
    assert svc.created[0]["scenario"] == "redis-down"
    assert svc.runs[0][1]["name"] == "KubePodNotReady"
    assert c.post("/alerts?scenario=oom-payments", json={}, headers=AUTH).status_code == 202
    assert svc.runs[1][1]["name"] == "FromScenario"


def test_replay_needs_a_scenario_and_live_does_not() -> None:
    c, _ = client()
    no_scenario = {k: v for k, v in SIMPLE.items() if k != "scenario"}
    r = c.post("/alerts", json=no_scenario, headers=AUTH)
    assert r.status_code == 422
    assert "scenario" in r.json()["detail"]
    live, svc = client(mode="live")
    assert live.post("/alerts", json=no_scenario, headers=AUTH).status_code == 202
    assert svc.created == [{"mode": "live", "scenario": None, "source": "webhook"}]


def test_bad_alerts_are_422() -> None:
    c, _ = client()
    assert c.post("/alerts", json={"summary": "no name"}, headers=AUTH).status_code == 422
    bad_scenario = {**SIMPLE, "scenario": "nope"}
    assert c.post("/alerts", json=bad_scenario, headers=AUTH).status_code == 422


@pytest.mark.parametrize(
    ("token", "headers", "code"),
    [
        (TOKEN, {}, 401),
        (TOKEN, {"Authorization": "Bearer wrong"}, 401),
        (None, AUTH, 503),  # no token configured: writes are refused
    ],
)
def test_write_endpoints_need_the_token(
    token: str | None, headers: dict[str, str], code: int
) -> None:
    c, svc = client(token=token)
    assert c.post("/alerts", json=SIMPLE, headers=headers).status_code == code
    body = {"decision": "approve", "approver": "sam"}
    assert c.post("/incidents/inc-1/approval", json=body, headers=headers).status_code == code
    assert svc.created == []
    assert svc.decisions == []


def test_read_endpoints() -> None:
    c, _ = client()
    assert c.get("/incidents").json()[0]["incident_id"] == "inc-1"
    assert c.get("/incidents/inc-1").json()["status"] == "awaiting_approval"
    assert c.get("/incidents/missing").status_code == 404


def test_approval_resumes_only_a_waiting_incident() -> None:
    c, svc = client()
    body = {"decision": "reject", "approver": "sam", "reason": "too risky"}
    r = c.post("/incidents/inc-1/approval", json=body, headers=AUTH)
    assert r.status_code == 202
    assert svc.decisions[0][0] == "inc-1"
    assert svc.decisions[0][1].reason == "too risky"
    assert c.post("/incidents/inc-2/approval", json=body, headers=AUTH).status_code == 409
    assert c.post("/incidents/missing/approval", json=body, headers=AUTH).status_code == 404
    invalid = {"decision": "approve", "approver": ""}  # approver is required
    assert c.post("/incidents/inc-1/approval", json=invalid, headers=AUTH).status_code == 422


def test_split_alerts_skips_resolved() -> None:
    assert len(split_alerts(WEBHOOK)) == 1
    assert split_alerts(SIMPLE)[0][1] == "redis-down"
