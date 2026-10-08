"""Unit tests for the CLI module of aiaddons."""

import re
from typer.testing import CliRunner
import typer.main

from aiaddons import __version__
from aiaddons.cli.main import app

runner = CliRunner()


def test_package_version() -> None:
    """Test package version variable."""
    assert __version__ == "0.1.0"


def test_cli_version_flag() -> None:
    """Test `aiaddons --version` flag."""
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "aiaddons version 0.1.0" in result.stdout


def test_cli_version_short_flag() -> None:
    """Test `aiaddons -v` short flag."""
    result = runner.invoke(app, ["-v"])
    assert result.exit_code == 0
    assert "aiaddons version 0.1.0" in result.stdout


def test_cli_version_subcommand() -> None:
    """Test `aiaddons version` subcommand."""
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "aiaddons version 0.1.0" in result.stdout


def test_cli_main_no_args() -> None:
    """Test running `aiaddons` with no arguments."""
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "AI Add-ons Manager (`aiaddons`)" in result.stdout


def _verify_help_examples(cmd_name: str) -> None:
    """Assert Examples section exists in --help and all referenced flags are valid."""
    result = runner.invoke(app, [cmd_name, "--help"])
    assert result.exit_code == 0
    assert "Examples:" in result.stdout

    click_app = typer.main.get_command(app)
    assert isinstance(click_app, typer.core.TyperGroup)
    click_cmd = click_app.commands[cmd_name]
    accepted_flags: set[str] = set()
    for param in click_cmd.params:
        accepted_flags.update(param.opts)
        accepted_flags.update(param.secondary_opts)

    examples_section = result.stdout.split("Examples:")[1]
    flags_in_examples = set(re.findall(r"--[a-zA-Z0-9-]+", examples_section))
    assert len(flags_in_examples) > 0
    for flag in flags_in_examples:
        assert flag in accepted_flags, f"Flag '{flag}' in {cmd_name} examples is not accepted by the command."


def test_cli_install_help_examples() -> None:
    """Test that `aiaddons install --help` contains valid examples with accepted flags."""
    _verify_help_examples("install")


def test_cli_remove_help_examples() -> None:
    """Test that `aiaddons remove --help` contains valid examples with accepted flags."""
    _verify_help_examples("remove")


def test_cli_update_help_examples() -> None:
    """Test that `aiaddons update --help` contains valid examples with accepted flags."""
    _verify_help_examples("update")
