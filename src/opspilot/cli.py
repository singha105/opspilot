"""The ``opspilot`` command-line interface."""

from typing import Annotated

import typer

from opspilot import __version__
from opspilot.config import get_settings
from opspilot.logging import configure_logging

app = typer.Typer(
    name="opspilot",
    help="OpsPilot: AI incident-response agent for Kubernetes.",
    no_args_is_help=True,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"opspilot {__version__}")
        raise typer.Exit


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """OpsPilot: AI incident-response agent for Kubernetes."""
    settings = get_settings()
    configure_logging(settings.log_level, json=settings.log_json)


@app.command()
def info() -> None:
    """Show the active configuration (no secrets are ever stored here)."""
    settings = get_settings()
    for key, value in settings.model_dump().items():
        typer.echo(f"{key}: {value}")
