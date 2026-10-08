"""CLI command for installing add-ons and generating dry-run installation plans (Phase 5B.10)."""

import json
import sys
from pathlib import Path
from typing import Any, NoReturn

import typer
from rich.console import Console

from aiaddons.agents.manager import AgentDetectionManager
from aiaddons.cli.exit_codes import ExitCode
from aiaddons.core.compatibility.engine import CompatibilityEngine
from aiaddons.core.exceptions import (
    AIAddonsError,
    ExecutableNotFoundError,
    ExternalExecutionError,
    IncompatibleAgentError,
    InstallationPlanningError,
    ManifestValidationError,
    ProcessExecutionError,
    ProcessTimeoutError,
    SecretResolutionError,
    SecurityValidationError,
    UnsupportedIntegrationTypeError,
    UnsupportedScopeError,
    VerificationError,
    VerificationPathSecurityError,
)
from aiaddons.core.execution.engine import ExecutionEngine
from aiaddons.core.execution.external.security import mask_secrets_in_text
from aiaddons.core.execution.models import ExecutionStatus
from aiaddons.core.installer.engine import InstallationEngine
from aiaddons.core.installer.models import TransactionPhase
from aiaddons.core.models.agent import Scope
from aiaddons.core.models.manifest import IntegrationManifest
from aiaddons.core.models.stack import parse_stack_file
from aiaddons.core.secrets.resolver import SecretResolver
from aiaddons.core.verification.engine import VerificationEngine
from aiaddons.core.verification.models import VerificationStatus
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


def _format_batch_json_response(
    agent: str,
    scope: str,
    compatible: bool,
    transaction_status: str,
    success: bool,
    addons_data: list[dict[str, Any]],
    error_message: str | None = None,
) -> str:
    """Format JSON response for batch or single installation."""
    data: dict[str, Any] = {
        "agent": agent,
        "scope": scope,
        "compatible": compatible,
        "transaction_status": transaction_status,
        "success": success,
        "error_message": error_message,
        "addons": addons_data,
    }
    # For single-addon requests, populate top-level fields for backwards compatibility
    if len(addons_data) == 1:
        first = addons_data[0]
        data["addon_id"] = first.get("addon_id", "")
        data["addon_name"] = first.get("addon_name", "")
        data["planned_operations"] = first.get("planned_operations", [])
        data["warnings"] = first.get("warnings", [])
        data["verification_status"] = first.get("verification_status", "skipped")
    return json.dumps(data, indent=2)


def _format_json_response(
    addon_id: str,
    addon_name: str,
    agent: str,
    scope: str,
    compatible: bool,
    planned_operations: list[dict[str, Any]],
    warnings: list[str],
    verification_status: str,
    transaction_status: str,
    success: bool,
    error_message: str | None = None,
) -> str:
    """Deterministically format JSON response for single-addon interface."""
    addon_entry = {
        "addon_id": addon_id,
        "addon_name": addon_name,
        "status": "installed" if success else "failed",
        "compatible": compatible,
        "planned_operations": planned_operations,
        "warnings": warnings,
        "verification_status": verification_status,
        "transaction_status": transaction_status,
        "success": success,
        "error_message": error_message,
    }
    return _format_batch_json_response(
        agent=agent,
        scope=scope,
        compatible=compatible,
        transaction_status=transaction_status,
        success=success,
        addons_data=[addon_entry],
        error_message=error_message,
    )


def _print_json(data_str: str) -> None:
    """Print raw JSON string directly to stdout without Rich soft-wrapping."""
    sys.stdout.write(data_str + "\n")
    sys.stdout.flush()


def _handle_error(
    msg: str,
    exit_code: int = ExitCode.INVALID_INPUT,
    json_output: bool = False,
    addon_id: str = "",
    addon_name: str = "",
    agent: str = "",
    scope: str = "",
    transaction_status: str = TransactionPhase.FAILED.name,
    secret_values: dict[str, str] | None = None,
    addons_data: list[dict[str, Any]] | None = None,
) -> NoReturn:
    """Safely report error without leaking secrets or Python tracebacks."""
    secrets_list = list(secret_values.values()) if secret_values else []
    safe_msg = mask_secrets_in_text(msg, secrets_list)
    if json_output:
        items = addons_data or []
        if not items and addon_id:
            items = [
                {
                    "addon_id": addon_id,
                    "addon_name": addon_name or addon_id,
                    "status": "failed",
                    "compatible": False,
                    "planned_operations": [],
                    "warnings": [],
                    "verification_status": "failed",
                    "transaction_status": transaction_status,
                    "success": False,
                    "error_message": safe_msg,
                }
            ]
        formatted = _format_batch_json_response(
            agent=agent,
            scope=scope,
            compatible=False,
            transaction_status=transaction_status,
            success=False,
            addons_data=items,
            error_message=safe_msg,
        )
        _print_json(formatted)
    else:
        console.print(f"[bold red]Error:[/bold red] {safe_msg}")
    raise typer.Exit(code=exit_code)


INSTALL_HELP_EPILOG = """
Examples:
  # Install a single add-on
  aiaddons install github-mcp

  # Preview the installation plan without making changes
  aiaddons install github-mcp --dry-run

  # Install for a specific agent and global scope
  aiaddons install context7-mcp --agent claude-code --scope global

  # Install multiple add-ons in one command
  aiaddons install github-mcp caveman

  # Install from a stack file
  aiaddons install --file registry/stacks/dev-starter-stack.yaml
"""


def install_command(
    addon_ids: list[str] = typer.Argument(
        None,
        help="ID(s) of the add-on(s) to install",
    ),
    file: Path | None = typer.Option(
        None,
        "--file",
        "-f",
        help="Path to stack YAML/JSON file containing list of add-ons to install",
    ),
    scope: str = typer.Option(
        "workspace",
        "--scope",
        "-s",
        help="Target configuration scope ('global' or 'workspace')",
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
        help="Simulate installation and output plan without applying changes",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Automatically confirm installation prompt without interactivity",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        "-j",
        help="Output installation results in machine-readable JSON format",
    ),
) -> None:
    """Install one or more add-ons or output a dry-run installation plan with safety verification.

    Required secrets (e.g. API tokens) are prompted interactively with masked input supporting Ctrl+V clipboard pasting on Windows. Alternatively, you can pre-set secrets via environment variables before running:
      PowerShell: $env:TOKEN_NAME="your_token"
      Bash/Zsh:   export TOKEN_NAME="your_token"
    """
    sym_ok, sym_fail = _get_symbols()

    # 1. Collect requested add-on specs (from CLI arguments and/or stack file)
    requested_specs: list[tuple[str, str | None]] = []

    if file is not None:
        try:
            file_specs = parse_stack_file(file)
            requested_specs.extend(file_specs)
        except ManifestValidationError as exc:
            _handle_error(
                str(exc),
                exit_code=ExitCode.INVALID_INPUT,
                json_output=json_output,
                scope=scope,
                agent=agent_id or "",
            )
        except Exception as exc:
            _handle_error(
                f"Failed to load stack file '{file}': {exc}",
                exit_code=ExitCode.INVALID_INPUT,
                json_output=json_output,
                scope=scope,
                agent=agent_id or "",
            )

    if addon_ids:
        for aid in addon_ids:
            clean_aid = aid.strip().lower()
            if clean_aid:
                requested_specs.append((clean_aid, None))

    if not requested_specs:
        _handle_error(
            "No add-on IDs or stack file provided. Specify at least one add-on ID or use --file.",
            exit_code=ExitCode.INVALID_INPUT,
            json_output=json_output,
            scope=scope,
            agent=agent_id or "",
        )

    # Deduplicate requested IDs preserving first-seen order
    unique_specs: dict[str, str | None] = {}
    for aid, ver in requested_specs:
        if aid not in unique_specs or (unique_specs[aid] is None and ver is not None):
            unique_specs[aid] = ver

    # 2. Upfront Validation: Validate ALL add-on IDs against the registry before doing anything
    registry, _source = Registry.load_auto(custom_dir=registry_path)
    missing_ids: list[str] = [aid for aid in unique_specs if not registry.get(aid)]
    if missing_ids:
        if len(missing_ids) == 1 and len(unique_specs) == 1:
            err_msg = f"Add-on '{missing_ids[0]}' not found in registry."
        else:
            err_msg = f"The following add-on(s) were not found in registry: {', '.join(missing_ids)}"
        _handle_error(
            err_msg,
            exit_code=ExitCode.INVALID_INPUT,
            json_output=json_output,
            addon_id=missing_ids[0] if len(missing_ids) == 1 else "",
            scope=scope,
            agent=agent_id or "",
        )

    # Verify version pinning if specified in stack file
    for aid, pinned_ver in unique_specs.items():
        if pinned_ver:
            manifest = registry.get(aid)
            if manifest and manifest.version != pinned_ver:
                err_msg = (
                    f"Add-on '{aid}' version mismatch: requested version '{pinned_ver}', "
                    f"but registry provides '{manifest.version}'."
                )
                _handle_error(
                    err_msg,
                    exit_code=ExitCode.INVALID_INPUT,
                    json_output=json_output,
                    addon_id=aid,
                    addon_name=manifest.name,
                    scope=scope,
                    agent=agent_id or "",
                )

    # Resolve all manifest objects
    all_manifests: list[IntegrationManifest] = [
        registry.get(aid) for aid in unique_specs  # type: ignore[misc]
    ]

    # 3. Parse requested scope
    try:
        parsed_scope = Scope.from_str(scope)
    except ValueError as exc:
        _handle_error(
            str(exc),
            exit_code=ExitCode.INVALID_INPUT,
            json_output=json_output,
            addon_id=all_manifests[0].id if all_manifests else "",
            addon_name=all_manifests[0].name if all_manifests else "",
            scope=scope,
            agent=agent_id or "",
        )

    # 4. Detect and filter target agents
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
            _handle_error(
                f"Specified agent '{agent_id}' is not registered.",
                exit_code=ExitCode.INVALID_INPUT,
                json_output=json_output,
                scope=parsed_scope.value,
                agent=agent_id,
            )
        target_agent = matched_agent
        if not target_agent.installed:
            _handle_error(
                f"Specified agent '{agent_id}' is not installed on this system.",
                exit_code=ExitCode.INVALID_INPUT,
                json_output=json_output,
                scope=parsed_scope.value,
                agent=target_agent.agent_id,
            )
    else:
        # Auto-detect target agent supporting all requested manifests
        matching_agents = [
            ag
            for ag in detected_agents.values()
            if ag.installed
            and all("*" in m.target_agents or ag.agent_id.lower() in [t.lower() for t in m.target_agents] for m in all_manifests)
        ]
        if not matching_agents:
            # Fallback to any installed agent
            any_installed = [ag for ag in detected_agents.values() if ag.installed]
            if not any_installed:
                _handle_error(
                    "No installed agent found on this system.",
                    exit_code=ExitCode.INVALID_INPUT,
                    json_output=json_output,
                    scope=parsed_scope.value,
                )
            target_agent = any_installed[0]
        else:
            target_agent = matching_agents[0]

    # 5. Check for already installed add-ons (skip with clear notice)
    state_store = InstalledStateStore()
    lockfile_mgr = LockfileManager()
    workspace_dir = Path.cwd().resolve()
    workspace_lock_entries = {
        e.addon_id.lower()
        for e in lockfile_mgr.get_entries(workspace_dir)
        if e.target_agent.lower() == target_agent.agent_id.lower()
    }

    is_batch = len(unique_specs) > 1 or file is not None
    uninstalled_manifests: list[IntegrationManifest] = []
    skipped_manifests: list[IntegrationManifest] = []

    for manifest in all_manifests:
        if is_batch:
            if parsed_scope == Scope.WORKSPACE:
                is_installed = manifest.id.lower() in workspace_lock_entries
            else:
                is_installed = (
                    state_store.get_record(target_agent.agent_id, Scope.GLOBAL, manifest.id) is not None
                )
        else:
            is_installed = False

        if is_installed:
            skipped_manifests.append(manifest)
            if not json_output:
                console.print(
                    f"[bold yellow]Notice: Add-on '{manifest.name}' ({manifest.id}) is already installed "
                    f"for {target_agent.name} ({parsed_scope.value}). Skipping.[/bold yellow]"
                )
        else:
            uninstalled_manifests.append(manifest)

    # If all requested add-ons are already installed, exit cleanly with success
    if not uninstalled_manifests:
        if not json_output:
            console.print()
            console.print(
                f"[bold yellow]All requested add-on(s) are already installed for {target_agent.name}. "
                "No changes were made.[/bold yellow]"
            )
            console.print()
        else:
            addons_data = [
                {
                    "addon_id": m.id,
                    "addon_name": m.name,
                    "status": "skipped",
                    "compatible": True,
                    "planned_operations": [],
                    "warnings": ["Add-on is already installed."],
                    "verification_status": "skipped",
                    "transaction_status": TransactionPhase.COMMITTED.name,
                    "success": True,
                    "error_message": None,
                }
                for m in skipped_manifests
            ]
            formatted = _format_batch_json_response(
                agent=target_agent.agent_id,
                scope=parsed_scope.value,
                compatible=True,
                transaction_status=TransactionPhase.COMMITTED.name,
                success=True,
                addons_data=addons_data,
                error_message=None,
            )
            _print_json(formatted)
        return

    # 6. Evaluate batch compatibility & check inter-addon conflicts
    compat_engine = CompatibilityEngine(registry=registry)
    compat_results = compat_engine.evaluate_batch(
        uninstalled_manifests, target_agent, parsed_scope, registry=registry
    )
    incompatible_results = [cr for cr in compat_results if not cr.compatible]
    if incompatible_results:
        incompat_messages = [
            f"Add-on '{cr.addon_name}' is incompatible with agent '{target_agent.name}': {'; '.join(cr.reasons)}"
            for cr in incompatible_results
        ]
        incompat_msg = "; ".join(incompat_messages)
        _handle_error(
            incompat_msg,
            exit_code=ExitCode.COMPATIBILITY_FAILURE,
            json_output=json_output,
            addon_id=incompatible_results[0].addon_id,
            addon_name=incompatible_results[0].addon_name,
            agent=target_agent.agent_id,
            scope=parsed_scope.value,
            transaction_status="COMPATIBILITY_CHECKED",
        )

    # 7. Create combined installation plan and single WAL transaction
    wal_mgr = TransactionWALManager()
    engine = InstallationEngine(registry=registry, wal_manager=wal_mgr)
    execution_engine = ExecutionEngine(
        wal_manager=wal_mgr,
        state_store=state_store,
        lockfile_manager=lockfile_mgr,
        registry=registry,
    )

    try:
        tx = engine.create_batch_transaction(
            uninstalled_manifests, target_agent, parsed_scope, registry=registry
        )
    except IncompatibleAgentError as exc:
        _handle_error(
            str(exc),
            exit_code=ExitCode.COMPATIBILITY_FAILURE,
            json_output=json_output,
            addon_id=uninstalled_manifests[0].id,
            addon_name=uninstalled_manifests[0].name,
            agent=target_agent.agent_id,
            scope=parsed_scope.value,
        )
    except UnsupportedScopeError as exc:
        _handle_error(
            str(exc),
            exit_code=ExitCode.COMPATIBILITY_FAILURE,
            json_output=json_output,
            addon_id=uninstalled_manifests[0].id,
            addon_name=uninstalled_manifests[0].name,
            agent=target_agent.agent_id,
            scope=parsed_scope.value,
        )
    except UnsupportedIntegrationTypeError as exc:
        _handle_error(
            str(exc),
            exit_code=ExitCode.COMPATIBILITY_FAILURE,
            json_output=json_output,
            addon_id=uninstalled_manifests[0].id,
            addon_name=uninstalled_manifests[0].name,
            agent=target_agent.agent_id,
            scope=parsed_scope.value,
        )
    except SecurityValidationError as exc:
        _handle_error(
            str(exc),
            exit_code=ExitCode.SECURITY_FAILURE,
            json_output=json_output,
            addon_id=uninstalled_manifests[0].id,
            addon_name=uninstalled_manifests[0].name,
            agent=target_agent.agent_id,
            scope=parsed_scope.value,
        )
    except (InstallationPlanningError, AIAddonsError) as exc:
        _handle_error(
            str(exc),
            exit_code=ExitCode.EXECUTION_FAILURE,
            json_output=json_output,
            addon_id=uninstalled_manifests[0].id,
            addon_name=uninstalled_manifests[0].name,
            agent=target_agent.agent_id,
            scope=parsed_scope.value,
        )

    if tx.phase == TransactionPhase.FAILED or tx.batch_plan is None:
        err_msg = tx.error_message or "Failed to generate installation plan."
        _handle_error(
            err_msg,
            exit_code=ExitCode.EXECUTION_FAILURE,
            json_output=json_output,
            addon_id=uninstalled_manifests[0].id,
            addon_name=uninstalled_manifests[0].name,
            agent=target_agent.agent_id,
            scope=parsed_scope.value,
        )

    batch_plan = tx.batch_plan

    # Build per-addon operations data for JSON output
    addons_json_data: list[dict[str, Any]] = []
    for plan in batch_plan.plans:
        ops_json = [
            {
                "op_type": op.op_type.value,
                "description": op.description.replace("\n", " "),
                "target_root": str(op.target_root).replace("\\", "/"),
                "target_path": str(op.target_path or "").replace("\\", "/"),
            }
            for op in plan.planned_operations
        ]
        addons_json_data.append(
            {
                "addon_id": plan.addon_id,
                "addon_name": plan.addon_name,
                "status": "planned",
                "compatible": True,
                "planned_operations": ops_json,
                "warnings": plan.warnings,
                "verification_status": "skipped",
                "transaction_status": TransactionPhase.PLANNED.name,
                "success": True,
                "error_message": None,
            }
        )

    for sm in skipped_manifests:
        addons_json_data.append(
            {
                "addon_id": sm.id,
                "addon_name": sm.name,
                "status": "skipped",
                "compatible": True,
                "planned_operations": [],
                "warnings": ["Add-on is already installed."],
                "verification_status": "skipped",
                "transaction_status": TransactionPhase.PLANNED.name,
                "success": True,
                "error_message": None,
            }
        )

    # 8. Dry-Run Handling
    if dry_run:
        tx.is_dry_run = True
        tx.phase = TransactionPhase.PLANNED
        if json_output:
            formatted = _format_batch_json_response(
                agent=target_agent.agent_id,
                scope=parsed_scope.value,
                compatible=True,
                transaction_status=TransactionPhase.PLANNED.name,
                success=True,
                addons_data=addons_json_data,
                error_message=None,
            )
            _print_json(formatted)
        else:
            console.print()
            if len(batch_plan.plans) == 1:
                single_m = uninstalled_manifests[0]
                console.print(f"[bold cyan]{single_m.name}[/bold cyan]")
                console.print("-" * max(len(single_m.name) + 2, 20))
                console.print()
                console.print("Target:")
                console.print(f"  [bold green]{target_agent.name}[/bold green]")
                console.print(f"  [bold yellow]{parsed_scope.value.capitalize()}[/bold yellow]")
                console.print()
                console.print("Plan:")
                console.print(f"  [green]{sym_ok}[/green] Validate source")
                itype_name = single_m.integration_type.value.upper()
                console.print(f"  [green]{sym_ok}[/green] Prepare {itype_name} configuration")
                for op in batch_plan.plans[0].planned_operations:
                    console.print(f"  [green]{sym_ok}[/green] {op.description}")
            else:
                console.print(
                    f"[bold cyan]Installation Plan ({len(batch_plan.plans)} add-on"
                    f"{'s' if len(batch_plan.plans) > 1 else ''})[/bold cyan]"
                )
                console.print("-" * 35)
                console.print()
                console.print("Target:")
                console.print(f"  [bold green]{target_agent.name}[/bold green]")
                console.print(f"  [bold yellow]{parsed_scope.value.capitalize()}[/bold yellow]")
                console.print()
                for plan in batch_plan.plans:
                    console.print(f"Add-on: [bold cyan]{plan.addon_name}[/bold cyan] ({plan.addon_id}) [{plan.integration_type.value}]")
                    console.print(f"  [green]{sym_ok}[/green] Validate source")
                    itype_name = plan.integration_type.value.upper()
                    console.print(f"  [green]{sym_ok}[/green] Prepare {itype_name} configuration")
                    for op in plan.planned_operations:
                        console.print(f"  [green]{sym_ok}[/green] {op.description}")
                    console.print()

            if skipped_manifests:
                for sm in skipped_manifests:
                    console.print(f"[dim]Skipped (already installed): {sm.name} ({sm.id})[/dim]")
                console.print()

            console.print("[dim]No changes were made.[/dim]")
            console.print()
        return

    # Real installation flow
    tx.is_dry_run = False

    # 9. Plan preview and user confirmation
    if not json_output:
        console.print()
        if len(batch_plan.plans) == 1:
            single_m = uninstalled_manifests[0]
            console.print(f"[bold cyan]{single_m.name}[/bold cyan]")
            console.print("-" * max(len(single_m.name) + 2, 20))
            console.print()
            console.print("Target:")
            console.print(f"  [bold green]{target_agent.name}[/bold green]")
            console.print(f"  [bold yellow]{parsed_scope.value.capitalize()}[/bold yellow]")
            console.print()
            console.print("Plan:")
            console.print(f"  [green]{sym_ok}[/green] Validate source")
            itype_name = single_m.integration_type.value.upper()
            console.print(f"  [green]{sym_ok}[/green] Prepare {itype_name} configuration")
            for op in batch_plan.plans[0].planned_operations:
                console.print(f"  [green]{sym_ok}[/green] {op.description}")
        else:
            console.print(
                f"[bold cyan]Installation Plan ({len(batch_plan.plans)} add-on"
                f"{'s' if len(batch_plan.plans) > 1 else ''})[/bold cyan]"
            )
            console.print("-" * 35)
            console.print()
            console.print("Target:")
            console.print(f"  [bold green]{target_agent.name}[/bold green]")
            console.print(f"  [bold yellow]{parsed_scope.value.capitalize()}[/bold yellow]")
            console.print()
            for plan in batch_plan.plans:
                console.print(f"Add-on: [bold cyan]{plan.addon_name}[/bold cyan] ({plan.addon_id}) [{plan.integration_type.value}]")
                console.print(f"  [green]{sym_ok}[/green] Validate source")
                itype_name = plan.integration_type.value.upper()
                console.print(f"  [green]{sym_ok}[/green] Prepare {itype_name} configuration")
                for op in plan.planned_operations:
                    console.print(f"  [green]{sym_ok}[/green] {op.description}")
                console.print()

        console.print("[bold yellow]This installation will modify your system.[/bold yellow]")
        console.print()

    # User confirmation prompt
    if not yes:
        confirmed = typer.confirm("Continue?", default=False)
        if not confirmed:
            if json_output:
                formatted = _format_batch_json_response(
                    agent=target_agent.agent_id,
                    scope=parsed_scope.value,
                    compatible=True,
                    transaction_status=TransactionPhase.PLANNED.name,
                    success=False,
                    addons_data=addons_json_data,
                    error_message="Installation cancelled by user.",
                )
                _print_json(formatted)
                raise typer.Exit(code=ExitCode.INVALID_INPUT)
            else:
                console.print("[bold yellow]Installation cancelled. No changes were made.[/bold yellow]")
                raise typer.Exit(code=ExitCode.SUCCESS)

    tx.phase = TransactionPhase.REVIEWED
    wal_mgr.write_transaction(tx)

    # 10. Resolve secrets for all uninstalled manifests
    secret_map: dict[str, str] = {}
    secret_resolver = SecretResolver()
    for manifest in uninstalled_manifests:
        try:
            resolved_secrets = secret_resolver.resolve_manifest(
                manifest, allow_interactive=sys.stdin.isatty() and not yes
            )
            for sec in resolved_secrets:
                if sec.value is not None:
                    secret_map[sec.name] = sec.value
        except SecretResolutionError as exc:
            _handle_error(
                str(exc),
                exit_code=ExitCode.EXECUTION_FAILURE,
                json_output=json_output,
                addon_id=manifest.id,
                addon_name=manifest.name,
                agent=target_agent.agent_id,
                scope=parsed_scope.value,
                transaction_status=TransactionPhase.FAILED.name,
            )

    # 11. Execute Batch Plan with progress output
    total_addons = len(uninstalled_manifests)
    for idx, manifest in enumerate(uninstalled_manifests, start=1):
        if not json_output:
            if total_addons > 1:
                console.print(
                    f"[bold cyan][{idx}/{total_addons}] Installing {manifest.name} ({manifest.id}) "
                    f"for {target_agent.name} ({parsed_scope.value})...[/bold cyan]"
                )
            else:
                console.print(
                    f"[bold cyan]Installing {manifest.name} ({manifest.id}) "
                    f"for {target_agent.name} ({parsed_scope.value})...[/bold cyan]"
                )

    tx.phase = TransactionPhase.EXECUTING
    try:
        result = execution_engine.execute_batch_plan(
            batch_plan, transaction=tx, dry_run=False, secret_values=secret_map, workspace_dir=workspace_dir
        )
    except SecurityValidationError as exc:
        _handle_error(
            str(exc),
            exit_code=ExitCode.SECURITY_FAILURE,
            json_output=json_output,
            addon_id=uninstalled_manifests[0].id,
            addon_name=uninstalled_manifests[0].name,
            agent=target_agent.agent_id,
            scope=parsed_scope.value,
            transaction_status=TransactionPhase.ROLLED_BACK.name,
            secret_values=secret_map,
        )
    except (
        ExecutableNotFoundError,
        ProcessExecutionError,
        ProcessTimeoutError,
        ExternalExecutionError,
    ) as exc:
        _handle_error(
            str(exc),
            exit_code=ExitCode.EXECUTION_FAILURE,
            json_output=json_output,
            addon_id=uninstalled_manifests[0].id,
            addon_name=uninstalled_manifests[0].name,
            agent=target_agent.agent_id,
            scope=parsed_scope.value,
            transaction_status=TransactionPhase.ROLLED_BACK.name,
            secret_values=secret_map,
        )
    except (VerificationPathSecurityError, VerificationError) as exc:
        _handle_error(
            str(exc),
            exit_code=ExitCode.VERIFICATION_FAILURE,
            json_output=json_output,
            addon_id=uninstalled_manifests[0].id,
            addon_name=uninstalled_manifests[0].name,
            agent=target_agent.agent_id,
            scope=parsed_scope.value,
            transaction_status=TransactionPhase.ROLLED_BACK.name,
            secret_values=secret_map,
        )
    except AIAddonsError as exc:
        _handle_error(
            str(exc),
            exit_code=ExitCode.EXECUTION_FAILURE,
            json_output=json_output,
            addon_id=uninstalled_manifests[0].id,
            addon_name=uninstalled_manifests[0].name,
            agent=target_agent.agent_id,
            scope=parsed_scope.value,
            transaction_status=TransactionPhase.FAILED.name,
            secret_values=secret_map,
        )

    # 12. Verification & Final Results
    if result.status == ExecutionStatus.SUCCESS:
        tx.phase = TransactionPhase.COMMITTED
        verification_engine = VerificationEngine()
        all_ver_results: list[tuple[str, Any]] = []

        for plan in batch_plan.plans:
            ver_res = verification_engine.verify_plan(plan, dry_run=False, secret_values=secret_map)
            all_ver_results.append((plan.addon_name, ver_res))

        # Build final success JSON data
        final_addons_json: list[dict[str, Any]] = []
        for plan in batch_plan.plans:
            matching_ver = next((vr for an, vr in all_ver_results if an == plan.addon_name), None)
            ops_json = [
                {
                    "op_type": op.op_type.value,
                    "description": op.description.replace("\n", " "),
                    "target_root": str(op.target_root).replace("\\", "/"),
                    "target_path": str(op.target_path or "").replace("\\", "/"),
                }
                for op in plan.planned_operations
            ]
            final_addons_json.append(
                {
                    "addon_id": plan.addon_id,
                    "addon_name": plan.addon_name,
                    "status": "installed",
                    "compatible": True,
                    "planned_operations": ops_json,
                    "warnings": plan.warnings,
                    "verification_status": "passed" if (matching_ver and matching_ver.verified) else "failed",
                    "transaction_status": TransactionPhase.COMMITTED.name,
                    "success": True,
                    "error_message": None,
                }
            )

        for sm in skipped_manifests:
            final_addons_json.append(
                {
                    "addon_id": sm.id,
                    "addon_name": sm.name,
                    "status": "skipped",
                    "compatible": True,
                    "planned_operations": [],
                    "warnings": ["Add-on is already installed."],
                    "verification_status": "skipped",
                    "transaction_status": TransactionPhase.COMMITTED.name,
                    "success": True,
                    "error_message": None,
                }
            )

        if json_output:
            formatted = _format_batch_json_response(
                agent=target_agent.agent_id,
                scope=parsed_scope.value,
                compatible=True,
                transaction_status=TransactionPhase.COMMITTED.name,
                success=True,
                addons_data=final_addons_json,
                error_message=None,
            )
            _print_json(formatted)
        else:
            console.print()
            console.print("[bold cyan]Verification[/bold cyan]")
            for addon_name, ver_res in all_ver_results:
                for chk in ver_res.checks:
                    chk_desc = mask_secrets_in_text(chk.description, list(secret_map.values()))
                    prefix = f"{addon_name}: " if len(batch_plan.plans) > 1 else ""
                    if chk.status == VerificationStatus.PASSED:
                        console.print(f"  [green]{sym_ok}[/green] {prefix}{chk_desc}")
                    elif chk.status == VerificationStatus.FAILED:
                        console.print(f"  [red]{sym_fail}[/red] {prefix}{chk_desc}")
            console.print()

            if len(uninstalled_manifests) == 1:
                console.print(
                    f"[bold green]{sym_ok} Successfully installed {uninstalled_manifests[0].name} for "
                    f"{target_agent.name}[/bold green]"
                )
            else:
                console.print(
                    f"[bold green]{sym_ok} Successfully installed {len(uninstalled_manifests)} add-on(s) for "
                    f"{target_agent.name}[/bold green]"
                )

            if skipped_manifests:
                console.print(
                    f"[dim]Skipped {len(skipped_manifests)} already-installed add-on(s): "
                    f"{', '.join(m.name for m in skipped_manifests)}[/dim]"
                )
            console.print()
    else:
        tx.phase = TransactionPhase.ROLLED_BACK
        err_detail = result.error_message or "Installation execution failed."
        safe_err = mask_secrets_in_text(err_detail, list(secret_map.values()))

        if json_output:
            formatted = _format_batch_json_response(
                agent=target_agent.agent_id,
                scope=parsed_scope.value,
                compatible=True,
                transaction_status=TransactionPhase.ROLLED_BACK.name,
                success=False,
                addons_data=addons_json_data,
                error_message=safe_err,
            )
            _print_json(formatted)
        else:
            console.print()
            console.print(f"[bold red]Installation failed: {safe_err}[/bold red]")
            console.print()
            console.print("[bold yellow]Rolling back installation...[/bold yellow]")
            if result.rolled_back_operations:
                for rb in result.rolled_back_operations:
                    rb_desc = mask_secrets_in_text(rb.description, list(secret_map.values()))
                    console.print(f"  [green]{sym_ok}[/green] {rb_desc}")
            console.print(f"[bold green]{sym_ok} Rollback completed[/bold green]")
            console.print()

        exit_code = (
            ExitCode.VERIFICATION_FAILURE
            if "verification" in safe_err.lower()
            else ExitCode.EXECUTION_FAILURE
        )
        raise typer.Exit(code=exit_code)
