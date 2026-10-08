"""Typer CLI main entry point for aiaddons."""

import typer
from rich.console import Console

from aiaddons import __version__
from aiaddons.cli.commands.agents import agents_command
from aiaddons.cli.commands.compatibility import check_command
from aiaddons.cli.commands.doctor import doctor_command
from aiaddons.cli.commands.install import INSTALL_HELP_EPILOG, install_command
from aiaddons.cli.commands.registry import (
    info_command,
    list_command,
    registry_status_command,
    registry_update_command,
    search_command,
)
from aiaddons.cli.commands.remove import REMOVE_HELP_EPILOG, remove_command
from aiaddons.cli.commands.sync import sync_command
from aiaddons.cli.commands.tui import tui_command
from aiaddons.cli.commands.update import UPDATE_HELP_EPILOG, update_command

app = typer.Typer(
    name="aiaddons",
    help="AI Add-ons Manager - Package & Integration Manager for AI Agents",
    add_completion=False,
)

console = Console()

# Top-level commands
app.command(name="agents")(agents_command)
app.command(name="list")(list_command)
app.command(name="search")(search_command)
app.command(name="info")(info_command)
app.command(name="check")(check_command)
app.command(name="install", epilog=INSTALL_HELP_EPILOG)(install_command)
app.command(name="update", epilog=UPDATE_HELP_EPILOG)(update_command)
app.command(name="sync")(sync_command)
app.command(name="remove", epilog=REMOVE_HELP_EPILOG)(remove_command)
app.command(name="doctor")(doctor_command)
app.command(name="tui")(tui_command)

# Registry subcommand group
registry_app = typer.Typer(
    name="registry",
    help="Manage, update, and inspect the add-on registry cache.",
    add_completion=False,
)
registry_app.command(name="update")(registry_update_command)
registry_app.command(name="status")(registry_status_command)
registry_app.command(name="list")(list_command)
registry_app.command(name="search")(search_command)
registry_app.command(name="info")(info_command)

app.add_typer(registry_app, name="registry")


def version_callback(value: bool) -> None:
    """Print the version and exit."""
    if value:
        console.print(f"[bold cyan]aiaddons[/bold cyan] version [green]{__version__}[/green]")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool | None = typer.Option(
        None,
        "--version",
        "-v",
        help="Show the version and exit.",
        callback=version_callback,
        is_eager=True,
    ),
) -> None:
    """AI Add-ons Manager CLI."""
    if ctx.invoked_subcommand is None and not version:
        console.print("[bold cyan]AI Add-ons Manager (`aiaddons`)[/bold cyan]")
        console.print("Run [yellow]aiaddons --help[/yellow] for available commands.")


@app.command()
def version() -> None:
    """Show the version of aiaddons."""
    console.print(f"[bold cyan]aiaddons[/bold cyan] version [green]{__version__}[/green]")
