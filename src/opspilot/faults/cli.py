"""``opspilot faults`` commands: list, inject, reset and status."""

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from opspilot.config import get_settings
from opspilot.faults.injector import Injector
from opspilot.faults.scenario import Scenario, load_scenarios
from opspilot.kube import admin_client

app = typer.Typer(help="Inject and reset fault scenarios (admin context only).")
console = Console()


def _scenario(scenario_id: str) -> Scenario:
    scenarios = load_scenarios(get_settings().scenarios_dir)
    if scenario_id not in scenarios:
        console.print(f"[red]unknown scenario {scenario_id!r}[/red]; try 'opspilot faults list'")
        raise typer.Exit(code=2)
    return scenarios[scenario_id]


def _injector() -> Injector:
    settings = get_settings()
    return Injector(
        admin_client(settings.admin_context),
        context=settings.admin_context,
        base_dir=settings.demo_base_dir,
    )


@app.command("list")
def list_scenarios() -> None:
    """List every scenario with its category and target."""
    table = Table()
    for column in ("id", "category", "split", "target"):
        table.add_column(column, no_wrap=True)
    table.add_column("title")
    for s in load_scenarios(get_settings().scenarios_dir).values():
        table.add_row(s.id, s.category.value, s.split, s.target.name, s.title)
    console.print(table)


@app.command()
def inject(
    scenario_id: Annotated[str, typer.Argument(help="Scenario id.")],
    wait: Annotated[bool, typer.Option(help="Wait for the expected symptom.")] = True,
) -> None:
    """Inject a scenario's fault into the cluster."""
    scenario = _scenario(scenario_id)
    injector = _injector()
    injector.inject(scenario)
    console.print(f"injected [bold]{scenario.id}[/bold] into {scenario.target.name}")
    if wait:
        seconds = injector.wait_for_symptom(scenario)
        console.print(f"symptom observed after {seconds:.0f}s")


@app.command()
def reset(scenario_id: Annotated[str, typer.Argument(help="Scenario id.")]) -> None:
    """Re-apply the base manifests and wait until the namespace is healthy."""
    scenario = _scenario(scenario_id)
    seconds = _injector().reset(scenario)
    console.print(f"reset [bold]{scenario.id}[/bold]; healthy after {seconds:.0f}s")


@app.command()
def status() -> None:
    """Show readiness and abnormal pod reasons for every Deployment."""
    namespace = get_settings().demo_namespace
    table = Table("deployment", "ready", "reasons", "healthy")
    for row in _injector().status(namespace):
        mark = "[green]yes[/green]" if row.healthy else "[red]no[/red]"
        table.add_row(row.name, f"{row.ready}/{row.desired}", ", ".join(row.reasons), mark)
    console.print(table)
