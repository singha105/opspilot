"""``opspilot faults`` commands: list, inject, reset, status and record."""

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


@app.command()
def verify(
    ids: Annotated[list[str] | None, typer.Argument(help="Scenario ids (default: all).")] = None,
    batch_size: Annotated[int, typer.Option(help="Scenarios per batch.")] = 5,
) -> None:
    """Inject each scenario, wait for its symptom, reset; exit 1 if any fails."""
    scenarios = load_scenarios(get_settings().scenarios_dir)
    chosen = [_scenario(i) for i in ids] if ids else list(scenarios.values())
    injector = _injector()
    table = Table("scenario", "category", "symptom", "reset", "result")
    failures = 0
    for start in range(0, len(chosen), batch_size):
        batch = chosen[start : start + batch_size]
        console.print(f"batch {start // batch_size + 1}: {', '.join(s.id for s in batch)}")
        for scenario in batch:
            symptom, result = "-", "[green]ok[/green]"
            try:
                injector.inject(scenario)
                symptom = f"{injector.wait_for_symptom(scenario):.0f}s"
            except Exception as exc:  # report every failure (timeouts included), keep going
                failures += 1
                result = f"[red]{type(exc).__name__}: {str(exc)[:60]}[/red]"
            finally:
                reset_s = f"{injector.reset(scenario):.0f}s"
            table.add_row(scenario.id, scenario.category.value, symptom, reset_s, result)
    console.print(table)
    console.print(f"{len(chosen) - failures}/{len(chosen)} scenarios verified")
    if failures:
        raise typer.Exit(code=1)


@app.command("check-fixtures")
def check_fixtures() -> None:
    """Fail unless every scenario has a fixture recorded from its current file (CI check)."""
    from opspilot.faults.recorder import fixture_problems

    settings = get_settings()
    problems = fixture_problems(settings.scenarios_dir, settings.fixtures_dir)
    for problem in problems:
        console.print(f"[red]{problem}[/red]")
    if problems:
        raise typer.Exit(code=1)
    count = len(list(settings.fixtures_dir.glob("*.json")))
    console.print(f"{count} fixtures match their scenarios")


@app.command()
def record(
    scenario_id: Annotated[
        str | None, typer.Argument(help="Scenario id, or 'healthy' for the no-fault baseline.")
    ] = None,
    all_: Annotated[bool, typer.Option("--all", help="Every scenario plus the baseline.")] = False,
) -> None:
    """Record a replay fixture: inject, wait, call every read tool, save, reset."""
    from opspilot.faults.recorder import HEALTHY, record_fixture
    from opspilot.mcp_servers.common import AuditLog, ToolRunner
    from opspilot.mcp_servers.common.kube import apis_from_kubeconfig
    from opspilot.mcp_servers.k8s_readonly.backend import K8sReadBackend
    from opspilot.mcp_servers.k8s_readonly.server import SERVER_NAME, K8sTools

    if not all_ and scenario_id is None:
        console.print("[red]give a scenario id or --all[/red]")
        raise typer.Exit(code=2)
    settings = get_settings()
    scenarios = load_scenarios(settings.scenarios_dir)
    ids = [HEALTHY, *scenarios] if all_ else [scenario_id or HEALTHY]
    backend = K8sReadBackend(apis_from_kubeconfig(settings.reader_kubeconfig))
    tools = K8sTools(
        backend,
        ToolRunner(AuditLog(settings.audit_log, SERVER_NAME, "live")),
        settings.allowed_namespaces,
    )
    for fixture_id in ids:
        scenario = None if fixture_id == HEALTHY else _scenario(fixture_id)
        path = settings.scenarios_dir / f"{fixture_id}.yaml" if scenario else None
        out = record_fixture(
            scenario,
            path,
            _injector(),
            tools,
            backend,
            settings.fixtures_dir,
            settings.demo_namespace,
        )
        console.print(f"recorded [bold]{fixture_id}[/bold] -> {out}")
