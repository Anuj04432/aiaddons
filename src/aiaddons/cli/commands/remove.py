"""CLI command for removing installed add-ons with transactional safety and drift detection."""

import json
import sys
from pathlib import Path
from typing import Any, NoReturn

import typer
from rich.console import Console

from aiaddons.agents.manager import AgentDetectionManager
from aiaddons.cli.exit_codes import ExitCode
from aiaddons.core.execution.engine import ExecutionEngine
from aiaddons.core.execution.external.security import mask_secrets_in_text
from aiaddons.core.execution.models import ExecutionStatus
from aiaddons.core.installer.engine import InstallationEngine
from aiaddons.core.installer.models import TransactionPhase
from aiaddons.core.models.agent import AgentCapability, AgentDetectionResult, Scope
from aiaddons.core.models.manifest import (
    HandlerSpecContainer,
    IntegrationManifest,
    IntegrationType,
    MCPHandlerSpec,
    MCPRuntime,
    PluginHandlerSpec,
    PublisherClaimSpec,
    SkillHandlerSpec,
    SourceSpec,
    SourceType,
    TrustMetadata,
)
from aiaddons.core.models.manifest import (
    VerificationStatus as TrustVerificationStatus,
)
from aiaddons.core.verification.engine import VerificationEngine
from aiaddons.integrations.mcp import make_relative_config_path
from aiaddons.integrations.skill import get_skill_target_directory
from aiaddons.registry.registry import Registry
from aiaddons.state.lockfile import LockfileManager
from aiaddons.state.store import InstalledAddonRecord, InstalledStateStore
from aiaddons.state.transaction import TransactionWALManager

console = Console()


def _get_symbols() -> tuple[str, str]:
    """Return platform-safe symbols for checkmark and cross mark."""
    try:
        "✓".encode(sys.stdout.encoding or "utf-8")
        return "✓", "✗"
    except Exception:
        return "+", "-"


def _format_json_response(
    addon_id: str,
    addon_name: str,
    agent: str,
    scope: str,
    planned_operations: list[dict[str, Any]],
    warnings: list[str],
    verification_status: str,
    transaction_status: str,
    success: bool,
    error_message: str | None = None,
) -> str:
    """Deterministically format JSON response for scripting interface."""
    data = {
        "addon_id": addon_id,
        "addon_name": addon_name,
        "agent": agent,
        "scope": scope,
        "planned_operations": planned_operations,
        "warnings": warnings,
        "verification_status": verification_status,
        "transaction_status": transaction_status,
        "success": success,
        "error_message": error_message,
    }
    return json.dumps(data, indent=2)


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
) -> NoReturn:
    """Safely report error without leaking secrets or Python tracebacks."""
    safe_msg = mask_secrets_in_text(msg, [])
    if json_output:
        formatted = _format_json_response(
            addon_id=addon_id,
            addon_name=addon_name,
            agent=agent,
            scope=scope,
            planned_operations=[],
            warnings=[],
            verification_status="failed",
            transaction_status=transaction_status,
            success=False,
            error_message=safe_msg,
        )
        _print_json(formatted)
    else:
        console.print(f"[bold red]Error:[/bold red] {safe_msg}")
    raise typer.Exit(code=exit_code)


def synthesize_manifest_from_record(record: InstalledAddonRecord) -> IntegrationManifest:
    """Synthesize a fallback manifest from an installed state record."""
    handler_spec = HandlerSpecContainer()
    if record.integration_type == IntegrationType.MCP:
        handler_spec.mcp = MCPHandlerSpec(
            runtime=MCPRuntime.NPX,
            package_name=record.addon_id,
        )
    elif record.integration_type == IntegrationType.SKILL:
        handler_spec.skill = SkillHandlerSpec(
            skill_file="SKILL.md",
        )
    elif record.integration_type == IntegrationType.PLUGIN:
        handler_spec.plugin = PluginHandlerSpec(
            components=[],
        )

    return IntegrationManifest(
        id=record.addon_id,
        name=record.name or record.addon_id,
        version=record.version or "1.0.0",
        description=f"Installed {record.integration_type.value} add-on",
        license="MIT",
        category="general",
        integration_type=record.integration_type,
        target_agents=[record.target_agent],
        supported_scopes=[record.scope],
        source=SourceSpec(source_type=SourceType.LOCAL, path="."),
        trust=TrustMetadata(
            verification_status=TrustVerificationStatus.UNVERIFIED,
            publisher=PublisherClaimSpec(name="Installed State"),
        ),
        handler_spec=handler_spec,
    )


from aiaddons.core.drift import detect_installation_drift


REMOVE_HELP_EPILOG = """
Examples:
  # Remove an installed add-on
  aiaddons remove github-mcp

  # Preview removal without making changes
  aiaddons remove github-mcp --dry-run

  # Remove from a specific agent and scope
  aiaddons remove github-mcp --agent claude-code --scope workspace

  # Force removal even if drift is detected
  aiaddons remove github-mcp --force
"""


def remove_command(
    addon_id: str = typer.Argument(..., help="ID of the add-on to remove"),
    scope: str | None = typer.Option(
        None,
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
        help="Simulate removal and output plan without applying changes",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Automatically confirm removal prompt without interactivity",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Force removal even if drift or shared dependencies are detected",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        "-j",
        help="Output removal results in machine-readable JSON format",
    ),
) -> None:
    """Remove an installed add-on with safety verification and transactional rollback."""
    sym_ok, _ = _get_symbols()
    workspace_dir = Path.cwd().resolve()

    # 1. State lookup
    state_store = InstalledStateStore()
    lockfile_mgr = LockfileManager()
    all_records = state_store.get_installed()
    lock_entries = lockfile_mgr.get_entries(workspace_dir)

    parsed_scope: Scope | None = None
    if scope:
        try:
            parsed_scope = Scope.from_str(scope)
        except ValueError as exc:
            _handle_error(
                str(exc),
                exit_code=ExitCode.INVALID_INPUT,
                json_output=json_output,
                addon_id=addon_id,
            )

    # Filter matching installed records
    matching_records = [
        r for r in all_records if r.addon_id.strip().lower() == addon_id.strip().lower()
    ]
    if parsed_scope:
        matching_records = [r for r in matching_records if r.scope == parsed_scope]
    if agent_id:
        target_aid = agent_id.strip().lower()
        matching_records = [
            r for r in matching_records if r.target_agent.lower() == target_aid
        ]

    matching_lock = [
        e for e in lock_entries if e.addon_id.strip().lower() == addon_id.strip().lower()
    ]
    if agent_id:
        target_aid = agent_id.strip().lower()
        matching_lock = [
            e for e in matching_lock if e.target_agent.lower() == target_aid
        ]

    if not matching_records and not matching_lock:
        _handle_error(
            f"Add-on '{addon_id}' is not installed on this system.",
            exit_code=ExitCode.INVALID_INPUT,
            json_output=json_output,
            addon_id=addon_id,
            scope=scope or "",
            agent=agent_id or "",
        )

    # Pick the target record / scope / agent
    target_record: InstalledAddonRecord | None = (
        matching_records[0] if matching_records else None
    )
    effective_scope = (
        parsed_scope
        or (target_record.scope if target_record else Scope.WORKSPACE)
    )
    effective_agent_id = (
        agent_id
        or (target_record.target_agent if target_record else matching_lock[0].target_agent)
    )

    # 2. Load manifest (or synthesize from record)
    registry, _source = Registry.load_auto(custom_dir=registry_path)
    manifest = registry.get(addon_id)
    if not manifest and target_record:
        manifest = synthesize_manifest_from_record(target_record)
    elif not manifest and matching_lock:
        lock_e = matching_lock[0]
        manifest = IntegrationManifest(
            id=lock_e.addon_id,
            name=lock_e.name,
            version=lock_e.version,
            description="Installed add-on",
            license="MIT",
            category="general",
            integration_type=lock_e.integration_type,
            target_agents=[lock_e.target_agent],
            supported_scopes=[effective_scope],
            source=SourceSpec(source_type=SourceType.LOCAL, path="."),
            trust=TrustMetadata(
                verification_status=TrustVerificationStatus.UNVERIFIED,
                publisher=PublisherClaimSpec(name="Installed State"),
            ),
            handler_spec=HandlerSpecContainer(),
        )
    elif not manifest:
        _handle_error(
            f"Add-on '{addon_id}' manifest could not be resolved.",
            exit_code=ExitCode.INVALID_INPUT,
            json_output=json_output,
            addon_id=addon_id,
        )

    # 3. Agent detection
    agent_manager = AgentDetectionManager()
    detected_agents = agent_manager.detect_agents()
    target_agent: AgentDetectionResult | None = None
    for ag in detected_agents.values():
        if ag.agent_id.lower() == effective_agent_id.lower():
            target_agent = ag
            break
    if not target_agent:
        target_agent = AgentDetectionResult(
            agent_id=effective_agent_id,
            name=effective_agent_id.replace("-", " ").title(),
            installed=True,
            capabilities=[AgentCapability.MCP, AgentCapability.SKILL],
        )

    # 4. Shared component check: if addon_id is referenced by another installed plugin
    active_installed_plugins = [
        r for r in all_records if r.integration_type == IntegrationType.PLUGIN
    ]
    for inst_plugin in active_installed_plugins:
        if inst_plugin.addon_id.lower() != addon_id.lower():
            p_manifest = registry.get(inst_plugin.addon_id)
            if p_manifest and p_manifest.handler_spec.plugin:
                if addon_id in p_manifest.handler_spec.plugin.components:
                    if not force:
                        _handle_error(
                            f"Add-on '{addon_id}' is a component of active installed plugin "
                            f"'{inst_plugin.name}' ({inst_plugin.addon_id}). "
                            "Use --force to remove it anyway.",
                            exit_code=ExitCode.INVALID_INPUT,
                            json_output=json_output,
                            addon_id=addon_id,
                            addon_name=manifest.name,
                            agent=target_agent.agent_id,
                            scope=effective_scope.value,
                        )
                    elif not json_output:
                        console.print(
                            f"[bold yellow]Warning: Add-on '{addon_id}' is shared by plugin "
                            f"'{inst_plugin.addon_id}'. Proceeding due to --force.[/bold yellow]"
                        )

    # 5. Drift detection
    drifts = detect_installation_drift(
        target_record, manifest, target_agent, effective_scope, workspace_dir
    )
    if drifts:
        drift_details = "\n".join(f"  - {d}" for d in drifts)
        if not force:
            _handle_error(
                f"Configuration/filesystem drift detected for '{addon_id}':\n{drift_details}\n"
                "Live state does not match installed records. Use --force to remove anyway.",
                exit_code=ExitCode.INVALID_INPUT,
                json_output=json_output,
                addon_id=addon_id,
                addon_name=manifest.name,
                agent=target_agent.agent_id,
                scope=effective_scope.value,
            )
        elif not json_output:
            console.print(
                "[bold yellow]Warning: Configuration/filesystem drift detected. "
                "Proceeding due to --force.[/bold yellow]"
            )

    # 6. Generate removal plan and transaction
    wal_mgr = TransactionWALManager()
    engine = InstallationEngine(registry=registry, wal_manager=wal_mgr)
    tx = engine.create_removal_transaction(
        manifest=manifest,
        agent=target_agent,
        scope=effective_scope,
        registry=registry,
        force=force,
        active_installed_plugins=active_installed_plugins,
    )

    if tx.phase == TransactionPhase.FAILED or tx.plan is None:
        _handle_error(
            tx.error_message or "Failed to generate removal plan.",
            exit_code=ExitCode.EXECUTION_FAILURE,
            json_output=json_output,
            addon_id=addon_id,
            addon_name=manifest.name,
            agent=target_agent.agent_id,
            scope=effective_scope.value,
        )

    plan = tx.plan

    ops_json = [
        {
            "op_type": op.op_type.value,
            "description": op.description.replace("\n", " "),
            "target_root": str(op.target_root).replace("\\", "/"),
            "target_path": str(op.target_path or "").replace("\\", "/"),
        }
        for op in plan.planned_operations
    ]

    # 7. Dry run
    if dry_run:
        tx.is_dry_run = True
        tx.phase = TransactionPhase.PLANNED
        if json_output:
            formatted = _format_json_response(
                addon_id=manifest.id,
                addon_name=manifest.name,
                agent=target_agent.agent_id,
                scope=effective_scope.value,
                planned_operations=ops_json,
                warnings=plan.warnings,
                verification_status="skipped",
                transaction_status=TransactionPhase.PLANNED.name,
                success=True,
                error_message=None,
            )
            _print_json(formatted)
        else:
            console.print()
            console.print(f"[bold cyan]{manifest.name} (Removal)[/bold cyan]")
            console.print("-" * max(len(manifest.name) + 12, 20))
            console.print()
            console.print("Target:")
            console.print(f"  [bold green]{target_agent.name}[/bold green]")
            console.print(f"  [bold yellow]{effective_scope.value.capitalize()}[/bold yellow]")
            console.print()
            console.print("Removal Plan:")
            for op in plan.planned_operations:
                console.print(f"  [green]{sym_ok}[/green] {op.description}")
            console.print()
            console.print("[dim]No changes were made.[/dim]")
            console.print()
        return

    # 8. Plan preview and user confirmation
    if not json_output:
        console.print()
        console.print(f"[bold cyan]{manifest.name} (Removal)[/bold cyan]")
        console.print("-" * max(len(manifest.name) + 12, 20))
        console.print()
        console.print("Target:")
        console.print(f"  [bold green]{target_agent.name}[/bold green]")
        console.print(f"  [bold yellow]{effective_scope.value.capitalize()}[/bold yellow]")
        console.print()
        console.print("Removal Plan:")
        for op in plan.planned_operations:
            console.print(f"  [green]{sym_ok}[/green] {op.description}")
        console.print()
        console.print(
            "[bold yellow]This operation will remove the add-on from your system.[/bold yellow]"
        )
        console.print()

    if not yes:
        confirmed = typer.confirm("Continue?", default=False)
        if not confirmed:
            if json_output:
                formatted = _format_json_response(
                    addon_id=manifest.id,
                    addon_name=manifest.name,
                    agent=target_agent.agent_id,
                    scope=effective_scope.value,
                    planned_operations=ops_json,
                    warnings=plan.warnings,
                    verification_status="skipped",
                    transaction_status=TransactionPhase.PLANNED.name,
                    success=False,
                    error_message="Removal cancelled by user.",
                )
                _print_json(formatted)
                raise typer.Exit(code=ExitCode.INVALID_INPUT)
            else:
                console.print(
                    "[bold yellow]Removal cancelled. No changes were made.[/bold yellow]"
                )
                raise typer.Exit(code=ExitCode.SUCCESS)

    tx.phase = TransactionPhase.REVIEWED
    wal_mgr.write_transaction(tx)

    # 9. Execute removal plan
    execution_engine = ExecutionEngine(
        wal_manager=wal_mgr,
        state_store=state_store,
        lockfile_manager=lockfile_mgr,
        registry=registry,
        workspace_dir=workspace_dir,
    )
    exec_result = execution_engine.execute_plan(
        plan=plan,
        transaction=tx,
        dry_run=False,
        is_removal=True,
    )

    if exec_result.status != ExecutionStatus.SUCCESS:
        _handle_error(
            f"Removal execution failed: {exec_result.error_message}",
            exit_code=ExitCode.EXECUTION_FAILURE,
            json_output=json_output,
            addon_id=manifest.id,
            addon_name=manifest.name,
            agent=target_agent.agent_id,
            scope=effective_scope.value,
        )

    # 10. Verification
    verification_engine = VerificationEngine()
    verify_result = verification_engine.verify_plan(plan, dry_run=False)
    if not verify_result.verified:
        err_str = (
            "; ".join(verify_result.errors)
            if verify_result.errors
            else "Files or configuration entries still remain."
        )
        _handle_error(
            f"Post-removal verification failed: {err_str}",
            exit_code=ExitCode.VERIFICATION_FAILURE,
            json_output=json_output,
            addon_id=manifest.id,
            addon_name=manifest.name,
            agent=target_agent.agent_id,
            scope=effective_scope.value,
        )

    # Also cleanup child components from state store / lockfile if it was a plugin
    if manifest.integration_type == IntegrationType.PLUGIN and manifest.handler_spec.plugin:
        for child_id in manifest.handler_spec.plugin.components:
            state_store.remove_installation(
                target_agent=target_agent.agent_id,
                scope=effective_scope,
                addon_id=child_id,
            )
            if effective_scope == Scope.WORKSPACE:
                lockfile_mgr.remove_from_lockfile(
                    workspace_dir,
                    target_agent=target_agent.agent_id,
                    addon_id=child_id,
                )

    # 11. Final output
    if json_output:
        formatted = _format_json_response(
            addon_id=manifest.id,
            addon_name=manifest.name,
            agent=target_agent.agent_id,
            scope=effective_scope.value,
            planned_operations=ops_json,
            warnings=plan.warnings,
            verification_status="passed",
            transaction_status=TransactionPhase.COMMITTED.name,
            success=True,
            error_message=None,
        )
        _print_json(formatted)
    else:
        console.print()
        console.print(
            f"[bold green]{sym_ok} Successfully removed {manifest.name} ({manifest.id}) "
            f"from {target_agent.name} ({effective_scope.value}).[/bold green]"
        )
        console.print()
