from typing import Any

import pytest
from typer.testing import CliRunner

from opspilot.cli import app
from opspilot.faults import cli as faults_cli
from opspilot.faults.injector import DeploymentStatus

runner = CliRunner()


class FakeInjector:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def inject(self, scenario: Any) -> None:
        self.calls.append(("inject", scenario.id))

    def wait_for_symptom(self, scenario: Any) -> float:
        self.calls.append(("wait", scenario.id))
        return 4.0

    def reset(self, scenario: Any) -> float:
        self.calls.append(("reset", scenario.id))
        return 9.0

    def status(self, namespace: str) -> list[DeploymentStatus]:
        return [
            DeploymentStatus("payments-api", 0, 1, ("OOMKilled",)),
            DeploymentStatus("redis", 1, 1, ()),
        ]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeInjector:
    injector = FakeInjector()
    monkeypatch.setattr(faults_cli, "_injector", lambda: injector)
    return injector


def test_list() -> None:
    result = runner.invoke(app, ["faults", "list"])
    assert result.exit_code == 0
    assert "oom-payments" in result.output
    assert "redis-down" in result.output


def test_inject_waits_for_symptom(fake: FakeInjector) -> None:
    result = runner.invoke(app, ["faults", "inject", "oom-payments"])
    assert result.exit_code == 0, result.output
    assert fake.calls == [("inject", "oom-payments"), ("wait", "oom-payments")]
    assert "symptom observed after 4s" in result.output


def test_inject_no_wait(fake: FakeInjector) -> None:
    result = runner.invoke(app, ["faults", "inject", "redis-down", "--no-wait"])
    assert result.exit_code == 0
    assert fake.calls == [("inject", "redis-down")]


def test_reset(fake: FakeInjector) -> None:
    result = runner.invoke(app, ["faults", "reset", "redis-down"])
    assert result.exit_code == 0
    assert "healthy after 9s" in result.output


def test_unknown_scenario(fake: FakeInjector) -> None:
    result = runner.invoke(app, ["faults", "inject", "no-such-fault"])
    assert result.exit_code == 2
    assert fake.calls == []


def test_status(fake: FakeInjector) -> None:
    result = runner.invoke(app, ["faults", "status"])
    assert result.exit_code == 0
    assert "OOMKilled" in result.output
    assert "0/1" in result.output


def test_verify_runs_each_scenario_and_reports_failures(
    fake: FakeInjector, monkeypatch: pytest.MonkeyPatch
) -> None:
    def flaky_wait(scenario: Any) -> float:
        fake.calls.append(("wait", scenario.id))
        if scenario.id == "redis-down":
            raise TimeoutError("condition not met within 120s")
        return 4.0

    monkeypatch.setattr(fake, "wait_for_symptom", flaky_wait)
    result = runner.invoke(
        app, ["faults", "verify", "oom-payments", "redis-down", "--batch-size", "1"]
    )
    assert result.exit_code == 1
    assert fake.calls == [
        ("inject", "oom-payments"),
        ("wait", "oom-payments"),
        ("reset", "oom-payments"),
        ("inject", "redis-down"),
        ("wait", "redis-down"),
        ("reset", "redis-down"),  # reset even after a failure
    ]
    assert "batch 2: redis-down" in result.output
    assert "1/2 scenarios verified" in result.output
