"""HTTP API: an Alertmanager webhook that starts incidents, and the approval endpoint.

Every route is a thin wrapper over ``IncidentService``; the rules (one run at a time,
replay needs a fixture, no decision before a proposal exists) live there. Write endpoints
need ``Authorization: Bearer $OPSPILOT_API_TOKEN``; reads are open on the local machine.
"""

import secrets
from typing import Annotated, Any, Literal

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from opspilot import __version__
from opspilot.agent.observability import RunSummary
from opspilot.config import Settings, get_settings
from opspilot.logging import get_logger
from opspilot.models.incident import ApprovalDecision
from opspilot.service import IncidentService, IncidentView, ServiceError

log = get_logger(__name__)
_bearer = HTTPBearer(auto_error=False)
SCENARIO_LABEL = "opspilot_scenario"


class SimpleAlert(BaseModel):
    """The simple JSON form of an alert (the webhook format is also accepted)."""

    name: str = Field(min_length=1)
    severity: Literal["info", "warning", "critical"] = "warning"
    summary: str = Field(min_length=1)
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    scenario: str | None = Field(default=None, description="Replay mode: the recorded scenario.")


class Accepted(BaseModel):
    incidents: list[str]
    mode: str


class ApprovalAccepted(BaseModel):
    incident_id: str
    status: str = "resuming"


def split_alerts(body: dict[str, Any]) -> list[tuple[dict[str, Any], str | None]]:
    """One (alert, scenario) per firing alert: an Alertmanager webhook or a simple alert.

    In replay mode the scenario comes from a ``scenario`` field or an ``opspilot_scenario``
    label, because a recorded fixture has to be chosen.
    """
    if isinstance(body.get("alerts"), list):
        out = []
        for alert in body["alerts"]:
            if alert.get("status", "firing") != "firing":
                continue
            labels = dict(alert.get("labels") or {})
            # The scenario only selects the replay fixture; the agent must never see it.
            scenario = labels.pop(SCENARIO_LABEL, None)
            out.append(({**body, "alerts": [{**alert, "labels": labels}]}, scenario))
        return out
    simple = SimpleAlert.model_validate(body)
    return [(simple.model_dump(exclude={"scenario"}), simple.scenario)]


def create_app(service: IncidentService | None = None, settings: Settings | None = None) -> FastAPI:
    """The FastAPI application (pass a service to inject fakes in tests)."""
    settings = settings or get_settings()
    svc = service or IncidentService(settings)
    token = settings.api_token.get_secret_value() if settings.api_token else ""
    app = FastAPI(
        title="OpsPilot API",
        version=__version__,
        description="Alert intake and human approval for the OpsPilot incident agent.",
    )

    def require_token(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    ) -> None:
        if not token:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "OPSPILOT_API_TOKEN is not set"
            )
        if credentials is None or not secrets.compare_digest(credentials.credentials, token):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing bearer token")

    async def run_in_background(incident_id: str, alert: dict[str, Any]) -> None:
        try:
            await svc.run(incident_id, alert)
        except Exception:  # the incident's state shows how far it got
            log.exception("incident run failed", incident=incident_id)

    async def decide_in_background(incident_id: str, decision: ApprovalDecision) -> None:
        try:
            await svc.decide(incident_id, decision)
        except Exception:
            log.exception("approval failed", incident=incident_id)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "mode": settings.api_mode, "version": __version__}

    @app.post(
        "/alerts",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_token)],
    )
    def post_alerts(
        body: dict[str, Any],
        background: BackgroundTasks,
        scenario: Annotated[str | None, Query(description="Replay: scenario id.")] = None,
    ) -> Accepted:
        """Create one incident per firing alert and investigate it up to the approval step."""
        replay = settings.api_mode == "replay"
        try:
            # Replay convenience: an empty body with ?scenario= replays that scenario's alert.
            alerts: list[tuple[dict[str, Any], str | None]] = (
                [(svc.scenario_alert(scenario)[0], scenario)]
                if not body and scenario
                else split_alerts(body)
            )
            created = []
            for alert, alert_scenario in alerts:
                chosen = scenario or alert_scenario
                if replay and not chosen:
                    raise ServiceError("replay mode: name a recorded scenario (scenario field)")
                incident_id = svc.create(
                    mode=settings.api_mode, scenario=chosen if replay else None, source="webhook"
                )
                background.add_task(run_in_background, incident_id, alert)
                created.append(incident_id)
        except (ServiceError, ValueError) as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        return Accepted(incidents=created, mode=settings.api_mode)

    @app.get("/incidents")
    def list_incidents() -> list[RunSummary]:
        return svc.list()

    @app.get("/incidents/{incident_id}")
    def get_incident(incident_id: str) -> IncidentView:
        try:
            return svc.get(incident_id)
        except ServiceError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    @app.post(
        "/incidents/{incident_id}/approval",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_token)],
    )
    def post_approval(
        incident_id: str, decision: ApprovalDecision, background: BackgroundTasks
    ) -> ApprovalAccepted:
        """Record a human decision and resume the incident in the background."""
        try:
            svc.meta(incident_id)
        except ServiceError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        if svc.pending(incident_id) is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "incident is not waiting for approval")
        background.add_task(decide_in_background, incident_id, decision)
        return ApprovalAccepted(incident_id=incident_id)

    return app


def main() -> None:
    """``opspilot-api``: serve the API with uvicorn."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(create_app(settings=settings), host=settings.api_host, port=settings.api_port)
