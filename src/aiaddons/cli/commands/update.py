"""CLI command for updating installed add-ons with transactional rollback safety."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, NoReturn

import typer
from rich.console import Console

from aiaddons.agents.manager import AgentDetectionManager
from aiaddons.cli.exit_codes import ExitCode
from aiaddons.core.exceptions import (
    AIAddonsError,
    IncompatibleAgentError,
    InstallationError,
    InstallationPlanningError,
    ManifestValidationError,
    SecretResolutionError,
    SecurityValidationError,
    UnsupportedIntegrationTypeError,
    UnsupportedScopeError,
    VerificationError,
)
from aiaddons.core.execution.external.security import mask_secrets_in_text
from aiaddons.core.installer.models import TransactionPhase
from aiaddons.core.models.agent import Scope
from aiaddons.core.secrets.resolver import SecretResolver
from aiaddons.core.update.engine import UpdateEngine
from aiaddons.core.update.models import UpdatePlan
from aiaddons.registry.registry import Registry
from aiaddons.state.lockfile import LockfileManager
from aiaddons.state.store import InstalledStateStore
from aiaddons.state.transaction import TransactionWALManager

console = Console()


def _get_symbols() -> tuple[str, str]:
    """Return platform-safe symbols for checkmark and cross mark."""
    try:
        "✓".encode(sys.stdout.encoding or "utf-8")
        return "✓", "✗"
    except Exception:
        return "+", "-"


def _format_update_json_response(
    agent: str,
    scope: str,
    items_data: list[dict[str, Any]],
    dry_run: bool,
    success: bool,
    already_up_to_date: bool = False,
    error_message: str | None = None,
) -> str:
    """Format structured machine-readable JSON response for update command."""
    data: dict[str, Any] = {
        "agent": agent,
        "scope": scope,
        "already_up_to_date": already_up_to_date,
        "dry_run": dry_run,
        "success": success,
        "error_message": error_message,
        "addons": items_data,
    }
    if len(items_data) == 1:
        first = items_data[0]
        data["addon_id"] = first.get("addon_id", "")
        data["addon_name"] = first.get("addon_name", "")
        data["old_version"] = first.get("old_version", "")
        data["new_version"] = first.get("new_version", "")
        data["planned_operations"] = first.get("planned_operations", [])
        data["warnings"] = first.get("warnings", [])
    return json.dumps(data, indent=2)


def _print_json(data_str: str) -> None:
    """Print raw JSON string directly to stdout without Rich soft-wrapping."""
    sys.stdout.write(data_str + "\n")
    sys.stdout.flush()


def _handle_update_error(
    msg: str,
    exit_code: int = ExitCode.INVALID_INPUT,
    json_output: bool = False,
    addon_id: str = "",
    addon_name: str = "",
    agent: str = "",
    scope: str = "",
    secret_values: dict[str, str] | None = None,
    items_data: list[dict[str, Any]] | None = None,
) -> NoReturn:
    """Safely report error without leaking secrets or Python tracebacks."""
    secrets_list = list(secret_values.values()) if secret_values else []
    safe_msg = mask_secrets_in_text(msg, secrets_list)
    if json_output:
        items = items_data or []
        if not items and addon_id:
            items = [
                {
                    "addon_id": addon_id,
                    "addon_name": addon_name or addon_id,
                    "status": "failed",
                    "old_version": "",
                    "new_version": "",
                    "planned_operations": [],
                    "warnings": [],
                    "error_message": safe_msg,
                }
            ]
        formatted = _format_update_json_response(
            agent=agent,
            scope=scope,
            items_data=items,
            dry_run=False,
            success=False,
            already_up_to_date=False,
            error_message=safe_msg,
        )
        _print_json(formatted)
    else:
        console.print(f"[bold red]Error:[/bold red] {safe_msg}")
    raise typer.Exit(code=exit_code)


UPDATE_HELP_EPILOG = """
Examples:
  # Update a single add-on to the latest version
  aiaddons update github-mcp

  # Update an add-on to a specific pinned version
  aiaddons update github-mcp --version 2025.4.8

  # Preview updating all installed add-ons
  aiaddons update --all --dry-run

  # Update all installed add-ons non-interactively
  aiaddons update --all --yes
"""


def update_command(
    addon_id: str | None = typer.Argument(
        None,
        help="ID of the add-on to update (or omit when using --all)",
    ),
    version: str | None = typer.Option(
        None,
        "--version",
        "-v",
        help="Specific target version to update or downgrade to (e.g. '1.2.0')",
    ),
    all_addons: bool = typer.Option(
        False,
        "--all",
        "-A",
        help="Update all installed add-ons with available newer versions",
    ),
    scope: str = typer.Option(
        "workspace",
        "--scope",
        "-s",
        help="Target configuration scope ('workspace' or 'global')",
    ),
    agent_id: str | None = typer.Option(
        None,
        "--agent",
        "-a",
        help="Target agent ID (e.g. 'claude-code', 'codex', 'antigravity', 'cursor', or 'hermes')",
    ),
    registry_path: Path | None = typer.Option(
        None,
        "--registry",
        "-r",
        help="Path to local registry directory",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Simulate update and output plan without applying changes",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Automatically confirm update prompt without interactivity",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        "-j",
        help="Output update results in machine-readable JSON format",
    ),
) -> None:
    """Update an installed add-on or all add-ons to the latest or pinned version with transactional safety."""
    sym_ok, _ = _get_symbols()
    workspace_dir = Path.cwd().resolve()

    # 1. Argument validation
    if not addon_id and not all_addons:
        _handle_update_error(
            "No add-on ID specified. Specify an add-on ID to update or use --all to update all installed add-ons.",
            exit_code=ExitCode.INVALID_INPUT,
            json_output=json_output,
            scope=scope,
            agent=agent_id or "",
        )

    if all_addons and version is not None:
        _handle_update_error(
            "Cannot specify --version with --all. Pinned versions can only be applied to a single add-on.",
            exit_code=ExitCode.INVALID_INPUT,
            json_output=json_output,
            scope=scope,
            agent=agent_id or "",
        )

    # 2. Scope parsing
    try:
        parsed_scope = Scope.from_str(scope)
    except ValueError as exc:
        _handle_update_error(
            str(exc),
            exit_code=ExitCode.INVALID_INPUT,
            json_output=json_output,
            addon_id=addon_id or "",
            scope=scope,
            agent=agent_id or "",
        )

    # 3. Detect and filter target agent
    manager = AgentDetectionManager()
    detected_agents = manager.detect_agents()

    if agent_id:
        target_aid = agent_id.strip().lower()
        matched_agent = None
        if target_aid in detected_agents:
            matched_agent = detected_agents[target_aid]
        else:
            normalized_target = target_aid.replace(" ", "-").replace("_", "-")
            if normalized_target in detected_agents:
                matched_agent = detected_agents[normalized_target]
            else:
                for ag in detected_agents.values():
                    if ag.agent_id.lower() == target_aid or ag.name.lower() == target_aid:
                        matched_agent = ag
                        break

        if matched_agent is None:
            _handle_update_error(
                f"Specified agent '{agent_id}' is not registered.",
                exit_code=ExitCode.INVALID_INPUT,
                json_output=json_output,
                addon_id=addon_id or "",
                scope=parsed_scope.value,
                agent=agent_id,
            )
        target_agent = matched_agent
        if not target_agent.installed:
            _handle_update_error(
                f"Specified agent '{agent_id}' is not installed on this system.",
                exit_code=ExitCode.INVALID_INPUT,
                json_output=json_output,
                addon_id=addon_id or "",
                scope=parsed_scope.value,
                agent=target_agent.agent_id,
            )
    else:
        installed_agents = [ag for ag in detected_agents.values() if ag.installed]
        if not installed_agents:
            _handle_update_error(
                "No installed agent found on this system.",
                exit_code=ExitCode.INVALID_INPUT,
                json_output=json_output,
                addon_id=addon_id or "",
                scope=parsed_scope.value,
            )
        target_agent = installed_agents[0]

    # 4. Load Registry
    registry, _source = Registry.load_auto(custom_dir=registry_path)

    # 5. Initialize UpdateEngine
    state_store = InstalledStateStore()
    lockfile_mgr = LockfileManager()
    wal_mgr = TransactionWALManager()

    update_engine = UpdateEngine(
        state_store=state_store,
        lockfile_manager=lockfile_mgr,
        registry=registry,
        wal_manager=wal_mgr,
        workspace_dir=workspace_dir,
    )

    # 6. Generate Update Plan
    try:
        if all_addons:
            plan = update_engine.plan_all_updates(
                target_agent=target_agent,
                scope=parsed_scope,
                registry=registry,
                workspace_dir=workspace_dir,
            )
        else:
            assert addon_id is not None
            plan = update_engine.plan_update(
                addon_id=addon_id,
                target_agent=target_agent,
                scope=parsed_scope,
                target_version=version,
                registry=registry,
                workspace_dir=workspace_dir,
            )
    except IncompatibleAgentError as exc:
        _handle_update_error(
            str(exc),
            exit_code=ExitCode.COMPATIBILITY_FAILURE,
            json_output=json_output,
            addon_id=addon_id or "",
            agent=target_agent.agent_id,
            scope=parsed_scope.value,
        )
    except UnsupportedScopeError as exc:
        _handle_update_error(
            str(exc),
            exit_code=ExitCode.COMPATIBILITY_FAILURE,
            json_output=json_output,
            addon_id=addon_id or "",
            agent=target_agent.agent_id,
            scope=parsed_scope.value,
        )
    except UnsupportedIntegrationTypeError as exc:
        _handle_update_error(
            str(exc),
            exit_code=ExitCode.COMPATIBILITY_FAILURE,
            json_output=json_output,
            addon_id=addon_id or "",
            agent=target_agent.agent_id,
            scope=parsed_scope.value,
        )
    except SecurityValidationError as exc:
        _handle_update_error(
            str(exc),
            exit_code=ExitCode.SECURITY_FAILURE,
            json_output=json_output,
            addon_id=addon_id or "",
            agent=target_agent.agent_id,
            scope=parsed_scope.value,
        )
    except (InstallationPlanningError, ManifestValidationError, AIAddonsError) as exc:
        _handle_update_error(
            str(exc),
            exit_code=ExitCode.INVALID_INPUT,
            json_output=json_output,
            addon_id=addon_id or "",
            agent=target_agent.agent_id,
            scope=parsed_scope.value,
        )

    # 7. Check if already up to date (no-op)
    if plan.is_empty:
        if all_addons:
            if json_output:
                items_data = [
                    {
                        "addon_id": s.get("addon_id", ""),
                        "addon_name": s.get("name", ""),
                        "status": "up-to-date",
                        "old_version": s.get("old_version", ""),
                        "new_version": s.get("new_version", ""),
                        "planned_operations": [],
                        "warnings": [],
                    }
                    for s in plan.skipped_items
                ]
                formatted = _format_update_json_response(
                    agent=target_agent.agent_id,
                    scope=parsed_scope.value,
                    items_data=items_data,
                    dry_run=dry_run,
                    success=True,
                    already_up_to_date=True,
                    error_message=None,
                )
                _print_json(formatted)
            else:
                console.print()
                console.print(
                    f"[bold green]{sym_ok} All installed add-on(s) are already up to date for "
                    f"{target_agent.name} ({parsed_scope.value}).[/bold green]"
                )
                console.print("[dim]No changes required.[/dim]")
                console.print()
            return
        else:
            assert addon_id is not None
            skipped_info = plan.skipped_items[0] if plan.skipped_items else {}
            curr_ver = skipped_info.get("old_version", "unknown")
            item_name = skipped_info.get("name", addon_id)
            if json_output:
                items_data = [
                    {
                        "addon_id": addon_id,
                        "addon_name": item_name,
                        "status": "up-to-date",
                        "old_version": curr_ver,
                        "new_version": curr_ver,
                        "planned_operations": [],
                        "warnings": [],
                    }
                ]
                formatted = _format_update_json_response(
                    agent=target_agent.agent_id,
                    scope=parsed_scope.value,
                    items_data=items_data,
                    dry_run=dry_run,
                    success=True,
                    already_up_to_date=True,
                    error_message=None,
                )
                _print_json(formatted)
            else:
                console.print()
                console.print(
                    f"[bold green]{sym_ok} Add-on '{item_name}' ({addon_id}) is already up to date "
                    f"(v{curr_ver}) for {target_agent.name} ({parsed_scope.value}).[/bold green]"
                )
                console.print("[dim]No changes required.[/dim]")
                console.print()
            return

    # Build per-item JSON data structure
    addons_json_data: list[dict[str, Any]] = []
    for item in plan.items:
        ops_json = [
            {
                "op_type": op.op_type.value,
                "description": op.description.replace("\n", " "),
                "target_root": str(op.target_root).replace("\\", "/"),
                "target_path": str(op.target_path or "").replace("\\", "/"),
            }
            for op in item.planned_operations
        ]
        addons_json_data.append(
            {
                "addon_id": item.addon_id,
                "addon_name": item.addon_name,
                "status": "planned",
                "old_version": item.current_version,
                "new_version": item.target_version,
                "planned_operations": ops_json,
                "warnings": item.warnings,
            }
        )

    # 8. Dry-Run Handling
    if dry_run:
        plan.is_dry_run = True
        if json_output:
            formatted = _format_update_json_response(
                agent=target_agent.agent_id,
                scope=parsed_scope.value,
                items_data=addons_json_data,
                dry_run=True,
                success=True,
                already_up_to_date=False,
                error_message=None,
            )
            _print_json(formatted)
        else:
            console.print()
            if len(plan.items) == 1:
                item = plan.items[0]
                console.print(
                    f"[bold cyan]{item.addon_name} (Update v{item.current_version} -> v{item.target_version})[/bold cyan]"
                )
                console.print("-" * max(len(item.addon_name) + 30, 20))
                console.print()
                console.print("Target:")
                console.print(f"  [bold green]{target_agent.name}[/bold green]")
                console.print(f"  [bold yellow]{parsed_scope.value.capitalize()}[/bold yellow]")
                console.print()
                console.print("Update Plan (Single WAL Transaction):")
                console.print("  [bold yellow]Phase 1: Remove Old Version[/bold yellow]")
                for op in item.removal_plan.planned_operations:
                    console.print(f"    [green]{sym_ok}[/green] {op.description}")
                console.print("  [bold cyan]Phase 2: Install New Version[/bold cyan]")
                for op in item.install_plan.planned_operations:
                    console.print(f"    [green]{sym_ok}[/green] {op.description}")
            else:
                console.print(
                    f"[bold cyan]Update Plan ({len(plan.items)} add-on{'s' if len(plan.items) > 1 else ''})[/bold cyan]"
                )
                console.print("-" * 35)
                console.print()
                console.print("Target:")
                console.print(f"  [bold green]{target_agent.name}[/bold green]")
                console.print(f"  [bold yellow]{parsed_scope.value.capitalize()}[/bold yellow]")
                console.print()
                for item in plan.items:
                    console.print(
                        f"Add-on: [bold cyan]{item.addon_name}[/bold cyan] ({item.addon_id}) "
                        f"[yellow]v{item.current_version}[/yellow] -> [green]v{item.target_version}[/green]"
                    )
                    console.print("  [bold yellow]Removal Operations:[/bold yellow]")
                    for op in item.removal_plan.planned_operations:
                        console.print(f"    [green]{sym_ok}[/green] {op.description}")
                    console.print("  [bold cyan]Installation Operations:[/bold cyan]")
                    for op in item.install_plan.planned_operations:
                        console.print(f"    [green]{sym_ok}[/green] {op.description}")
                    console.print()

            if plan.warnings:
                console.print()
                for w in plan.warnings:
                    console.print(f"[bold yellow]Warning:[/bold yellow] {w}")

            console.print()
            console.print("[dim]Dry-run mode: No changes were made.[/dim]")
            console.print()
        return

    # 9. Plan preview and user confirmation
    if not json_output:
        console.print()
        if len(plan.items) == 1:
            item = plan.items[0]
            console.print(
                f"[bold cyan]{item.addon_name} (Update v{item.current_version} -> v{item.target_version})[/bold cyan]"
            )
            console.print("-" * max(len(item.addon_name) + 30, 20))
            console.print()
            console.print("Target:")
            console.print(f"  [bold green]{target_agent.name}[/bold green]")
            console.print(f"  [bold yellow]{parsed_scope.value.capitalize()}[/bold yellow]")
            console.print()
            console.print("Update Plan (Single WAL Transaction):")
            console.print("  [bold yellow]Phase 1: Remove Old Version[/bold yellow]")
            for op in item.removal_plan.planned_operations:
                console.print(f"    [green]{sym_ok}[/green] {op.description}")
            console.print("  [bold cyan]Phase 2: Install New Version[/bold cyan]")
            for op in item.install_plan.planned_operations:
                console.print(f"    [green]{sym_ok}[/green] {op.description}")
        else:
            console.print(
                f"[bold cyan]Update Plan ({len(plan.items)} add-on{'s' if len(plan.items) > 1 else ''})[/bold cyan]"
            )
            console.print("-" * 35)
            console.print()
            console.print("Target:")
            console.print(f"  [bold green]{target_agent.name}[/bold green]")
            console.print(f"  [bold yellow]{parsed_scope.value.capitalize()}[/bold yellow]")
            console.print()
            for item in plan.items:
                console.print(
                    f"Add-on: [bold cyan]{item.addon_name}[/bold cyan] ({item.addon_id}) "
                    f"[yellow]v{item.current_version}[/yellow] -> [green]v{item.target_version}[/green]"
                )
                console.print("  [bold yellow]Removal Operations:[/bold yellow]")
                for op in item.removal_plan.planned_operations:
                    console.print(f"    [green]{sym_ok}[/green] {op.description}")
                console.print("  [bold cyan]Installation Operations:[/bold cyan]")
                for op in item.install_plan.planned_operations:
                    console.print(f"    [green]{sym_ok}[/green] {op.description}")
                console.print()

        console.print("[bold yellow]This update will modify your system configurations.[/bold yellow]")
        console.print()

    # User confirmation prompt
    if not yes:
        confirmed = typer.confirm("Continue with update?", default=False)
        if not confirmed:
            if json_output:
                formatted = _format_update_json_response(
                    agent=target_agent.agent_id,
                    scope=parsed_scope.value,
                    items_data=addons_json_data,
                    dry_run=False,
                    success=False,
                    already_up_to_date=False,
                    error_message="Update cancelled by user.",
                )
                _print_json(formatted)
                raise typer.Exit(code=ExitCode.INVALID_INPUT)
            else:
                console.print("[bold yellow]Update cancelled. No changes were made.[/bold yellow]")
                raise typer.Exit(code=ExitCode.SUCCESS)

    # 10. Resolve secrets for all new manifests
    secret_map: dict[str, str] = {}
    secret_resolver = SecretResolver()
    for item in plan.items:
        try:
            resolved_secrets = secret_resolver.resolve_manifest(
                item.new_manifest, allow_interactive=sys.stdin.isatty() and not yes
            )
            for sec in resolved_secrets:
                if sec.value is not None:
                    secret_map[sec.name] = sec.value
        except SecretResolutionError as exc:
            _handle_update_error(
                str(exc),
                exit_code=ExitCode.EXECUTION_FAILURE,
                json_output=json_output,
                addon_id=item.addon_id,
                addon_name=item.addon_name,
                agent=target_agent.agent_id,
                scope=parsed_scope.value,
                secret_values=secret_map,
                items_data=addons_json_data,
            )

    # 11. Execute Update Plan
    if not json_output:
        if len(plan.items) == 1:
            console.print(
                f"[bold cyan]Updating {plan.items[0].addon_name} ({plan.items[0].addon_id}) "
                f"to v{plan.items[0].target_version}...[/bold cyan]"
            )
        else:
            console.print(f"[bold cyan]Updating {len(plan.items)} add-on(s)...[/bold cyan]")

    update_result = update_engine.execute_update(
        plan=plan,
        target_agent=target_agent,
        workspace_dir=workspace_dir,
        registry=registry,
        dry_run=False,
        secret_values=secret_map,
    )

    if not update_result.success:
        _handle_update_error(
            update_result.error_message or "Update execution failed.",
            exit_code=ExitCode.EXECUTION_FAILURE,
            json_output=json_output,
            addon_id=plan.items[0].addon_id if len(plan.items) == 1 else "",
            addon_name=plan.items[0].addon_name if len(plan.items) == 1 else "",
            agent=target_agent.agent_id,
            scope=parsed_scope.value,
            secret_values=secret_map,
            items_data=addons_json_data,
        )

    # 12. Final Result Output
    if json_output:
        for d in addons_json_data:
            d["status"] = "updated"
        formatted = _format_update_json_response(
            agent=target_agent.agent_id,
            scope=parsed_scope.value,
            items_data=addons_json_data,
            dry_run=False,
            success=True,
            already_up_to_date=False,
            error_message=None,
        )
        _print_json(formatted)
    else:
        console.print()
        if len(plan.items) == 1:
            item = plan.items[0]
            console.print(
                f"[bold green]{sym_ok} Successfully updated {item.addon_name} ({item.addon_id}) "
                f"from v{item.current_version} to v{item.target_version} for {target_agent.name} "
                f"({parsed_scope.value}).[/bold green]"
            )
        else:
            updated_names = ", ".join(
                f"{it.addon_name} (v{it.target_version})" for it in plan.items
            )
            console.print(
                f"[bold green]{sym_ok} Successfully updated {len(plan.items)} add-on(s): "
                f"{updated_names} for {target_agent.name} ({parsed_scope.value}).[/bold green]"
            )
        console.print()
