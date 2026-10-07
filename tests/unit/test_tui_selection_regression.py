"""Regression tests for TUI selection synchronization and plan preview race conditions."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

from textual.widgets import Button, Input, ListView, Markdown, Select

from aiaddons.core.installer.engine import InstallationEngine
from aiaddons.core.models.agent import AgentCapability, AgentDetectionResult, Scope
from aiaddons.tui.app import AIAddonsTUIApp


def _create_regression_registry(tmp_path: Path) -> tuple[Path, Path, Path, AgentDetectionResult]:
    reg_dir = tmp_path / "registry"
    reg_dir.mkdir(parents=True, exist_ok=True)
    store_dir = tmp_path / ".aiaddons"
    store_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    dummy_checksum = "sha256:" + "a" * 64

    # 1. brave-search-mcp with required BRAVE_API_KEY
    (reg_dir / "brave-search-mcp.json").write_text(
        json.dumps(
            {
                "id": "brave-search-mcp",
                "name": "Brave Search MCP Server",
                "version": "0.6.2",
                "description": "Brave Search integration",
                "license": "MIT",
                "category": "search",
                "integration_type": "mcp",
                "target_agents": ["*"],
                "supported_scopes": ["global", "workspace"],
                "source": {
                    "source_type": "package",
                    "package_name": "@modelcontextprotocol/server-brave-search",
                    "checksum": dummy_checksum,
                },
                "trust": {
                    "verification_status": "verified",
                    "publisher": {"name": "MCP Team"},
                },
                "handler_spec": {
                    "mcp": {
                        "transport": "stdio",
                        "runtime": "npx",
                        "package_name": "@modelcontextprotocol/server-brave-search",
                        "env_vars": [
                            {
                                "name": "BRAVE_API_KEY",
                                "required": True,
                                "secret": True,
                                "description": "Brave Search API Key",
                            }
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    # 2. context7-mcp with optional CONTEXT7_API_KEY
    (reg_dir / "context7-mcp.json").write_text(
        json.dumps(
            {
                "id": "context7-mcp",
                "name": "Context7 Documentation MCP Server",
                "version": "1.0.0",
                "description": "Documentation MCP integration",
                "license": "MIT",
                "category": "documentation",
                "integration_type": "mcp",
                "target_agents": ["*"],
                "supported_scopes": ["global", "workspace"],
                "source": {
                    "source_type": "package",
                    "package_name": "@upstash/context7-mcp",
                    "checksum": dummy_checksum,
                },
                "trust": {
                    "verification_status": "verified",
                    "publisher": {"name": "Upstash"},
                },
                "handler_spec": {
                    "mcp": {
                        "transport": "stdio",
                        "runtime": "npx",
                        "package_name": "@upstash/context7-mcp",
                        "env_vars": [
                            {
                                "name": "CONTEXT7_API_KEY",
                                "required": False,
                                "secret": True,
                                "description": "Optional API Key",
                            }
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    # 3. caveman skill (no secrets)
    (reg_dir / "caveman.json").write_text(
        json.dumps(
            {
                "id": "caveman",
                "name": "Caveman Skill",
                "version": "1.0.0",
                "description": "Output compression skill",
                "license": "MIT",
                "category": "productivity",
                "integration_type": "skill",
                "target_agents": ["*"],
                "supported_scopes": ["global", "workspace"],
                "source": {
                    "source_type": "local",
                    "package_name": "caveman",
                    "checksum": dummy_checksum,
                },
                "trust": {
                    "verification_status": "verified",
                    "publisher": {"name": "Community"},
                },
                "handler_spec": {
                    "skill": {
                        "skill_file": "SKILL.md",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    agent = AgentDetectionResult(
        agent_id="claude-code",
        name="Claude Code",
        installed=True,
        supported_scopes=[Scope.GLOBAL, Scope.WORKSPACE],
        capabilities=[AgentCapability.MCP, AgentCapability.SKILL],
        workspace_config_path=str(workspace_dir / ".claude.json"),
    )

    return reg_dir, store_dir, workspace_dir, agent


def test_tui_exact_repro_brave_to_context7_secret_isolation(tmp_path: Path) -> None:
    """Verify repro: selecting brave-search-mcp, attempting install, then navigating to

    context7-mcp completely clears BRAVE_API_KEY and only requires context7-mcp specs.
    """
    async def _run() -> None:
        reg_dir, store_dir, workspace_dir, agent = _create_regression_registry(tmp_path)

        with patch("aiaddons.tui.app.AgentDetectionManager.detect_agents", return_value={"claude-code": agent}):
            app = AIAddonsTUIApp(registry_dir=reg_dir, workspace_dir=workspace_dir, store_dir=store_dir)
            async with app.run_test() as pilot:
                await pilot.pause()
                lv = app.query_one("#addon-list", ListView)
                dv = app.query_one("#detail-view", Markdown)

                # 1. Start on brave-search-mcp (first row)
                assert app.selected_manifest is not None
                assert app.selected_manifest.id == "brave-search-mcp"
                assert "BRAVE_API_KEY" in app.secret_inputs

                # 2. Attempt install without secrets -> displays error demanding BRAVE_API_KEY
                app.action_install()
                await pilot.pause()
                assert "BRAVE_API_KEY" in dv._markdown
                assert "Installation Error" in dv._markdown

                # 3. Arrow down to context7-mcp
                lv.focus()
                await pilot.pause()
                # Down once to caveman
                await pilot.press("down")
                await pilot.pause()
                assert app.selected_manifest.id == "caveman"
                assert len(app.secret_inputs) == 0

                # Down again to context7-mcp
                await pilot.press("down")
                await pilot.pause()
                assert app.selected_manifest.id == "context7-mcp"
                assert lv.highlighted_child is not None
                assert lv.highlighted_child.id == "item-context7-mcp"
                # secret_inputs MUST NOT have BRAVE_API_KEY
                assert "BRAVE_API_KEY" not in app.secret_inputs
                assert len(app.secret_inputs) == 0

                # 4. Preview plan for context7-mcp -> preview should NOT demand BRAVE_API_KEY
                app.action_preview_plan()
                await pilot.pause()
                assert app.current_plan is not None
                assert app.current_plan.addon_id == "context7-mcp"
                assert "BRAVE_API_KEY" not in dv._markdown
                assert "Installation Plan Preview" in dv._markdown

    asyncio.run(_run())


def test_tui_rapid_sequential_selection_synchronization(tmp_path: Path) -> None:
    """Verify rapid sequential arrow navigation keeps ListView highlight,

    selected_manifest, and detail preview strictly synchronized.
    """
    async def _run() -> None:
        reg_dir, store_dir, workspace_dir, agent = _create_regression_registry(tmp_path)

        with patch("aiaddons.tui.app.AgentDetectionManager.detect_agents", return_value={"claude-code": agent}):
            app = AIAddonsTUIApp(registry_dir=reg_dir, workspace_dir=workspace_dir, store_dir=store_dir)
            async with app.run_test() as pilot:
                await pilot.pause()
                lv = app.query_one("#addon-list", ListView)
                dv = app.query_one("#detail-view", Markdown)

                lv.focus()
                await pilot.pause()

                # Sequential rapid down keys
                for expected_id in ["caveman", "context7-mcp"]:
                    await pilot.press("down")
                    await pilot.pause()
                    assert lv.highlighted_child is not None
                    assert lv.highlighted_child.id == f"item-{expected_id}"
                    assert app.selected_manifest is not None
                    assert app.selected_manifest.id == expected_id
                    assert expected_id in dv._markdown or app.selected_manifest.name in dv._markdown

                # Sequential rapid up keys
                for expected_id in ["caveman", "brave-search-mcp"]:
                    await pilot.press("up")
                    await pilot.pause()
                    assert lv.highlighted_child is not None
                    assert lv.highlighted_child.id == f"item-{expected_id}"
                    assert app.selected_manifest is not None
                    assert app.selected_manifest.id == expected_id

    asyncio.run(_run())


def test_tui_artificially_delayed_preview_race_condition(tmp_path: Path) -> None:
    """Verify race condition handling: an in-flight slow preview request for an earlier

    selection is discarded when a newer selection occurs before it completes.
    """
    async def _run() -> None:
        reg_dir, store_dir, workspace_dir, agent = _create_regression_registry(tmp_path)

        with patch("aiaddons.tui.app.AgentDetectionManager.detect_agents", return_value={"claude-code": agent}):
            app = AIAddonsTUIApp(registry_dir=reg_dir, workspace_dir=workspace_dir, store_dir=store_dir)
            async with app.run_test() as pilot:
                await pilot.pause()
                assert app.selected_manifest.id == "brave-search-mcp"

                orig_generate_plan = InstallationEngine.generate_plan

                # Artificially intercept generate_plan to simulate user changing selection mid-flight
                def slow_generate_plan(engine_self, manifest, target_agent, scope=None, **kwargs):
                    if manifest.id == "brave-search-mcp":
                        # User switches to context7-mcp while brave's preview was computing!
                        app._select_addon("context7-mcp")
                    return orig_generate_plan(engine_self, manifest, target_agent, scope=scope, **kwargs)

                with patch.object(InstallationEngine, "generate_plan", new=slow_generate_plan):
                    # Trigger preview for brave-search-mcp
                    app.action_preview_plan()
                    await pilot.pause()

                    # The stale brave-search-mcp preview MUST have been dropped
                    assert app.selected_manifest.id == "context7-mcp"
                    # current_plan must NOT be brave-search-mcp
                    assert app.current_plan is None or app.current_plan.addon_id != "brave-search-mcp"

                # Now trigger preview for the active addon (context7-mcp)
                app.action_preview_plan()
                await pilot.pause()

                assert app.current_plan is not None
                assert app.current_plan.addon_id == "context7-mcp"

    asyncio.run(_run())


def test_tui_category_separation_and_agent_scope_persistence(tmp_path: Path) -> None:
    """Verify switching categories preserves agent and scope selections,

    and filters the list view appropriately.
    """
    async def _run() -> None:
        reg_dir, store_dir, workspace_dir, agent = _create_regression_registry(tmp_path)
        # Create a sample stack
        stacks_dir = reg_dir / "stacks"
        stacks_dir.mkdir(parents=True, exist_ok=True)
        (stacks_dir / "test-stack.yaml").write_text(
            "name: Test Stack\ndescription: Test stack bundle\naddons:\n  - brave-search-mcp\n  - caveman\n",
            encoding="utf-8",
        )

        with patch("aiaddons.tui.app.AgentDetectionManager.detect_agents", return_value={"claude-code": agent}):
            app = AIAddonsTUIApp(registry_dir=reg_dir, workspace_dir=workspace_dir, store_dir=store_dir)
            async with app.run_test() as pilot:
                await pilot.pause()
                lv = app.query_one("#addon-list", ListView)
                cat_select = app.query_one("#select-category", Select)
                agent_select = app.query_one("#select-agent", Select)
                scope_select = app.query_one("#select-scope", Select)

                # Set custom scope
                app.action_toggle_scope()
                await pilot.pause(0.05)
                assert app.selected_scope == Scope.GLOBAL
                assert scope_select.value == "global"
                assert agent_select.value == "claude-code"

                # 1. Switch category to 'skill'
                cat_select.value = "skill"
                await pilot.pause(0.05)
                assert app.selected_category == "skill"
                # Agent and scope MUST be preserved
                assert app.selected_scope == Scope.GLOBAL
                assert scope_select.value == "global"
                assert agent_select.value == "claude-code"
                # ListView should contain only skill items
                item_ids = [c.id for c in lv.children]
                assert item_ids == ["item-caveman"]
                assert app.selected_manifest.id == "caveman"

                # 2. Switch category to 'mcp'
                cat_select.value = "mcp"
                await pilot.pause(0.05)
                assert app.selected_category == "mcp"
                assert app.selected_scope == Scope.GLOBAL
                assert scope_select.value == "global"
                assert agent_select.value == "claude-code"
                mcp_ids = [c.id for c in lv.children]
                assert "item-brave-search-mcp" in mcp_ids
                assert "item-context7-mcp" in mcp_ids
                assert "item-caveman" not in mcp_ids

                # 3. Switch category to 'stack'
                cat_select.value = "stack"
                await pilot.pause(0.05)
                assert app.selected_category == "stack"
                assert app.selected_scope == Scope.GLOBAL
                assert scope_select.value == "global"
                stack_ids = [c.id for c in lv.children]
                assert stack_ids == ["item-stack-test-stack"]
                assert app.selected_stack is not None
                assert app.selected_stack_id == "test-stack"

                # 4. Cycle category with keybinding action_cycle_category
                app.action_cycle_category()  # from stack -> all
                await pilot.pause(0.05)
                assert app.selected_category == "all"
                assert app.selected_scope == Scope.GLOBAL
                assert scope_select.value == "global"
                assert len(lv.children) == 4  # 3 manifests + 1 stack

    asyncio.run(_run())


def test_tui_category_search_and_plan_preview_isolation(tmp_path: Path) -> None:
    """Verify search within category filters correctly and selecting an add-on

    in a filtered category generates the correct plan preview without cross-addon contamination.
    """
    async def _run() -> None:
        reg_dir, store_dir, workspace_dir, agent = _create_regression_registry(tmp_path)
        with patch("aiaddons.tui.app.AgentDetectionManager.detect_agents", return_value={"claude-code": agent}):
            app = AIAddonsTUIApp(registry_dir=reg_dir, workspace_dir=workspace_dir, store_dir=store_dir)
            async with app.run_test() as pilot:
                await pilot.pause()
                lv = app.query_one("#addon-list", ListView)
                search_inp = app.query_one("#addon-search", Input)
                cat_select = app.query_one("#select-category", Select)
                dv = app.query_one("#detail-view", Markdown)

                # Filter by MCP category first
                cat_select.value = "mcp"
                await pilot.pause(0.05)

                # Search for "context"
                search_inp.value = "context"
                await pilot.pause(0.05)

                # Only context7-mcp should remain
                item_ids = [c.id for c in lv.children]
                assert item_ids == ["item-context7-mcp"]
                assert app.selected_manifest.id == "context7-mcp"
                assert len(app.secret_inputs) == 0

                # Preview plan for context7-mcp
                app.action_preview_plan()
                await pilot.pause(0.05)
                assert app.current_plan is not None
                assert app.current_plan.addon_id == "context7-mcp"
                assert "BRAVE_API_KEY" not in dv._markdown

                # Search for "brave"
                search_inp.value = "brave"
                await pilot.pause(0.05)

                item_ids = [c.id for c in lv.children]
                assert item_ids == ["item-brave-search-mcp"]
                assert app.selected_manifest.id == "brave-search-mcp"
                assert "BRAVE_API_KEY" in app.secret_inputs

                # Clear search
                search_inp.value = ""
                await pilot.pause(0.05)
                assert len(lv.children) == 2

    asyncio.run(_run())


def test_tui_stack_category_batch_plan_preview(tmp_path: Path) -> None:
    """Verify selecting a stack renders stack details and previews batch plan operations."""
    async def _run() -> None:
        reg_dir, store_dir, workspace_dir, agent = _create_regression_registry(tmp_path)
        stacks_dir = reg_dir / "stacks"
        stacks_dir.mkdir(parents=True, exist_ok=True)
        (stacks_dir / "dev-stack.yaml").write_text(
            "name: Dev Stack\ndescription: Starter bundle\naddons:\n  - context7-mcp\n  - caveman\n",
            encoding="utf-8",
        )

        with patch("aiaddons.tui.app.AgentDetectionManager.detect_agents", return_value={"claude-code": agent}):
            app = AIAddonsTUIApp(registry_dir=reg_dir, workspace_dir=workspace_dir, store_dir=store_dir)
            async with app.run_test() as pilot:
                await pilot.pause()
                cat_select = app.query_one("#select-category", Select)
                dv = app.query_one("#detail-view", Markdown)

                cat_select.value = "stack"
                await pilot.pause()

                assert app.selected_stack is not None
                assert app.selected_stack.name == "Dev Stack"
                assert "Dev Stack" in dv._markdown

                # Preview batch plan for stack
                app.action_preview_plan()
                await pilot.pause()
                assert "Batch Plan Preview for Stack: Dev Stack" in dv._markdown
                assert "context7-mcp" in dv._markdown
                assert "caveman" in dv._markdown

    asyncio.run(_run())

