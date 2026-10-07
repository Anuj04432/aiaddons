"""Textual interactive terminal UI for AI Add-ons Manager (Phase 5B & Phase 6)."""

from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Markdown,
    Select,
    Static,
)
from textual.worker import Worker

import yaml

from aiaddons.agents.manager import AgentDetectionManager
from aiaddons.core.compatibility.engine import CompatibilityEngine
from aiaddons.core.drift import detect_installation_drift
from aiaddons.core.execution.engine import ExecutionEngine
from aiaddons.core.execution.external.security import mask_secrets_in_text
from aiaddons.core.execution.models import ExecutionStatus
from aiaddons.core.installer.engine import InstallationEngine
from aiaddons.core.installer.models import (
    InstallationPlan,
    InstallationTransaction,
    TransactionPhase,
)
from aiaddons.core.models.agent import AgentCapability, AgentDetectionResult, Scope
from aiaddons.core.models.manifest import IntegrationManifest, IntegrationType
from aiaddons.core.models.stack import AddonStack
from aiaddons.core.secrets.resolver import mask_secret_preview, validate_secret_format
from aiaddons.core.update.engine import UpdateEngine, is_newer_version
from aiaddons.core.update.models import UpdatePlan
from aiaddons.core.verification.engine import VerificationEngine
from aiaddons.core.verification.models import VerificationStatus
from aiaddons.registry.registry import Registry
from aiaddons.state.lockfile import LockfileManager
from aiaddons.state.store import InstalledAddonRecord, InstalledStateStore
from aiaddons.state.transaction import TransactionWALManager
from aiaddons.tui.screens.health import HealthScreen
from aiaddons.tui.screens.modals import (
    DriftConfirmModal,
    RemoveConfirmModal,
    SecretWarningModal,
    UpdatePlanModal,
)
from aiaddons.tui.screens.sync import SyncScreen


class AIAddonsTUIApp(App[None]):
    """Textual TUI interface for add-on discovery, compatibility check, installer, doctor, and sync."""

    BINDINGS = [
        Binding("h", "doctor", "Health Check"),
        Binding("s", "sync", "Sync Workspace"),
        Binding("t", "cycle_category", "Category"),
        Binding("a", "cycle_agent", "Switch Agent"),
        Binding("o", "toggle_scope", "Toggle Scope"),
        Binding("i", "install", "Install"),
        Binding("r", "remove", "Remove"),
        Binding("u", "update", "Update"),
        Binding("c", "check_compat", "Check Compat"),
        Binding("p", "preview_plan", "Preview Plan"),
        Binding("q", "quit", "Quit"),
    ]

    CSS = """
    Screen {
        layout: vertical;
        background: $surface;
    }

    #main-container {
        layout: horizontal;
        height: 1fr;
    }

    #sidebar {
        width: 35%;
        height: 100%;
        border-right: heavy $primary;
        padding: 1;
    }

    #content-panel {
        width: 65%;
        height: 100%;
        padding: 1;
    }

    .section-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }

    .config-label {
        color: $accent;
        text-style: bold;
        margin-top: 1;
    }

    #target-config-box {
        margin-top: 1;
        margin-bottom: 1;
        padding: 1;
        border: solid $secondary;
        height: auto;
    }

    #select-agent, #select-scope {
        width: 100%;
        margin-bottom: 1;
    }

    #target-config-buttons {
        height: auto;
        margin-top: 1;
        width: 100%;
    }

    #target-config-buttons Button {
        width: 1fr;
        margin: 0 1;
        min-width: 6;
    }

    .status-pass {
        color: green;
        text-style: bold;
    }

    .status-fail {
        color: red;
        text-style: bold;
    }

    #select-category {
        width: 100%;
        margin-bottom: 1;
    }

    #addon-search {
        width: 100%;
        margin-bottom: 1;
    }

    #addon-list {
        height: 8;
        min-height: 4;
        border: solid $primary;
    }

    #secrets-container {
        margin-top: 1;
        margin-bottom: 1;
    }

    #action-bar {
        height: 3;
        width: 100%;
        align: right middle;
        padding: 0 1;
        border-top: solid $primary;
        background: $surface;
    }

    #action-bar Button {
        min-width: 8;
        padding: 0 1;
        margin-left: 1;
    }

    Button {
        margin-left: 1;
    }
    """

    TITLE = "AI Add-ons Manager"
    SUB_TITLE = "Interactive Discovery, Management, Diagnostics & Synchronization"

    def __init__(
        self,
        registry_dir: Path | None = None,
        workspace_dir: Path | None = None,
        store_dir: Path | None = None,
    ) -> None:
        super().__init__()
        self.workspace_dir = (workspace_dir or Path.cwd()).resolve()
        self.store_dir = store_dir or (Path.home() / ".aiaddons")
        self.registry_dir = registry_dir
        self.registry: Registry | None = None
        self.manifests: list[IntegrationManifest] = []
        self.selected_manifest: IntegrationManifest | None = None
        self.detected_agents: dict[str, AgentDetectionResult] = {}
        self.selected_agent: AgentDetectionResult | None = None
        self.selected_scope: Scope = Scope.WORKSPACE
        self.current_plan: InstallationPlan | None = None
        self.current_tx: InstallationTransaction | None = None
        self.secret_inputs: dict[str, Input] = {}
        self.resolved_secrets: dict[str, str] = {}
        self._preview_token: int = 0
        self._secret_counter: int = 0
        self.stacks: dict[str, AddonStack] = {}
        self.selected_stack: AddonStack | None = None
        self.selected_stack_id: str | None = None
        self.selected_category: str = "all"
        self.search_query: str = ""

    @staticmethod
    def _default_registry_dir() -> Path | None:
        return Registry.find_default_registry_dir()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="main-container"):
            with VerticalScroll(id="sidebar"):
                yield Label("Available Add-ons", classes="section-title")
                yield Label("Category Filter [T]:", classes="config-label")
                yield Select(
                    [
                        ("All Categories", "all"),
                        ("MCP Servers", "mcp"),
                        ("Agent Skills", "skill"),
                        ("Composite Plugins", "plugin"),
                        ("Stacks", "stack"),
                    ],
                    value="all",
                    id="select-category",
                    allow_blank=False,
                )
                yield Input(placeholder="Search add-ons...", id="addon-search")
                yield ListView(id="addon-list")
                yield Label("Target Configuration", classes="section-title")
                yield Static(id="config-summary", content="Select an add-on to begin.")
                with Vertical(id="target-config-box"):
                    yield Label("Agent Selector [A]:", classes="config-label")
                    yield Select(
                        [
                            ("Claude Code", "claude-code"),
                            ("Codex", "codex"),
                            ("Antigravity CLI", "antigravity"),
                            ("Cursor", "cursor"),
                            ("Hermes Agent", "hermes"),
                        ],
                        value="claude-code",
                        id="select-agent",
                        allow_blank=False,
                    )
                    yield Label("Scope Selector [O]:", classes="config-label")
                    yield Select(
                        [
                            ("Workspace", "workspace"),
                            ("Global", "global"),
                        ],
                        value="workspace",
                        id="select-scope",
                        allow_blank=False,
                    )
                    with Horizontal(id="target-config-buttons"):
                        yield Button("Agent [A]", id="btn-switch-agent", variant="default")
                        yield Button("Scope [O]", id="btn-toggle-scope", variant="default")
            with VerticalScroll(id="content-panel"):
                yield Label("Add-on Details & Plan Preview", classes="section-title")
                yield Markdown(id="detail-view", markdown="Select an add-on from the list.")
                with Vertical(id="secrets-container"):
                    yield Label(id="secrets-label", content="")
        with Horizontal(id="action-bar"):
            yield Button("Check Compat [C]", id="btn-compat", variant="default")
            yield Button("Preview Plan [P]", id="btn-plan", variant="default")
            yield Button("Install [I]", id="btn-confirm", variant="primary", disabled=True)
            yield Button("Remove [R]", id="btn-remove", variant="error", disabled=True)
            yield Button("Update [U]", id="btn-update", variant="warning", disabled=True)
        yield Footer()

    def _find_stacks_dir(self) -> Path | None:
        """Locate registry stacks directory."""
        candidates: list[Path] = []
        if self.registry_dir:
            candidates.extend([
                self.registry_dir / "stacks",
                self.registry_dir.parent / "stacks",
            ])
        cwd = self.workspace_dir
        candidates.extend([
            cwd / "registry" / "stacks",
            Path.cwd() / "registry" / "stacks",
        ])
        pkg_file = Path(__file__).resolve()
        for parent in pkg_file.parents:
            candidates.extend([parent / "registry" / "stacks", parent / "stacks"])
        for cand in candidates:
            if cand.exists() and cand.is_dir():
                return cand
        return None

    def _load_stacks(self) -> None:
        """Discover and load stacks from the registry stacks directory."""
        self.stacks.clear()
        stacks_dir = self._find_stacks_dir()
        if not stacks_dir:
            return
        files = (
            sorted(stacks_dir.glob("*.yaml"))
            + sorted(stacks_dir.glob("*.yml"))
            + sorted(stacks_dir.glob("*.json"))
        )
        for file_path in files:
            try:
                raw_dict = yaml.safe_load(file_path.read_text(encoding="utf-8"))
                if isinstance(raw_dict, dict):
                    stack = AddonStack.model_validate(raw_dict)
                    self.stacks[file_path.stem] = stack
            except Exception:
                pass

    async def on_mount(self) -> None:
        """Initialize registry and agent detection on app startup."""
        if self.registry_dir is not None and self.registry_dir.exists() and self.registry_dir.is_dir():
            self.registry, _ = Registry.from_directory(self.registry_dir)
        else:
            self.registry, _ = Registry.load_auto(custom_dir=self.registry_dir)

        if self.registry:
            self.manifests = self.registry.list()

        self._load_stacks()

        manager = AgentDetectionManager()
        self.detected_agents = manager.detect_agents(project_path=self.workspace_dir)

        if self.detected_agents:
            installed = [a for a in self.detected_agents.values() if a.installed]
            if installed:
                self.selected_agent = installed[0]
            else:
                self.selected_agent = list(self.detected_agents.values())[0]
        else:
            default_agent = AgentDetectionResult(
                agent_id="claude-code",
                name="Claude Code",
                installed=True,
                capabilities=[AgentCapability.MCP, AgentCapability.SKILL],
            )
            self.detected_agents = {"claude-code": default_agent}
            self.selected_agent = default_agent

        # Populate agent Select options with detection status badges
        agent_select = self.query_one("#select-agent", Select)
        agent_options = [
            (
                f"{agent.name} (detected)" if agent.installed else f"{agent.name} (not detected)",
                agent.agent_id,
            )
            for agent in self.detected_agents.values()
        ]
        agent_select.set_options(agent_options)
        if self.selected_agent:
            agent_select.value = self.selected_agent.agent_id

        # Set scope Select value
        scope_select = self.query_one("#select-scope", Select)
        scope_select.value = self.selected_scope.value

        await self._do_refresh_addon_list()
        self._update_config_summary()

    def _get_addon_installed_record(self, addon_id: str) -> InstalledAddonRecord | None:
        """Look up the installed record for an add-on in the state store."""
        if not self.selected_agent:
            return None
        state_store = InstalledStateStore(store_dir=self.store_dir)
        return state_store.get_record(
            target_agent=self.selected_agent.agent_id,
            scope=self.selected_scope,
            addon_id=addon_id,
        )

    def _get_stack_status(self, stack: AddonStack) -> tuple[int, int]:
        """Return (installed_count, total_count) for add-ons in the stack."""
        total = len(stack.addons)
        installed = 0
        for entry in stack.addons:
            addon_id = entry if isinstance(entry, str) else entry.id
            rec = self._get_addon_installed_record(addon_id)
            if rec is not None:
                installed += 1
            elif self.selected_scope == Scope.WORKSPACE and self.selected_agent:
                lockfile_mgr = LockfileManager()
                entries = lockfile_mgr.get_entries(self.workspace_dir)
                for lent in entries:
                    if (
                        lent.addon_id.strip().lower() == addon_id.strip().lower()
                        and lent.target_agent.strip().lower() == self.selected_agent.agent_id.strip().lower()
                    ):
                        installed += 1
                        break
        return installed, total

    def _get_installation_status(self, manifest: IntegrationManifest) -> tuple[bool, str, str | None]:
        """Return (is_installed, installed_version, newer_version_or_None)."""
        rec = self._get_addon_installed_record(manifest.id)
        if rec is not None:
            installed_ver = rec.version or "1.0.0"
            has_newer = is_newer_version(installed_ver, manifest.version)
            newer_ver = manifest.version if has_newer else None
            return True, installed_ver, newer_ver

        # Check lockfile entries as fallback
        if self.selected_scope == Scope.WORKSPACE and self.selected_agent:
            lockfile_mgr = LockfileManager()
            entries = lockfile_mgr.get_entries(self.workspace_dir)
            for entry in entries:
                if (
                    entry.addon_id.strip().lower() == manifest.id.strip().lower()
                    and entry.target_agent.strip().lower() == self.selected_agent.agent_id.strip().lower()
                ):
                    installed_ver = entry.version or "1.0.0"
                    has_newer = is_newer_version(installed_ver, manifest.version)
                    newer_ver = manifest.version if has_newer else None
                    return True, installed_ver, newer_ver

        return False, "", None

    async def _do_refresh_addon_list(self) -> None:
        """Populate or refresh the Addon ListView with installation status badges."""
        list_view = self.query_one("#addon-list", ListView)
        await list_view.clear()

        prev_id: str | None = None
        if self.selected_manifest:
            prev_id = f"item-{self.selected_manifest.id}"
        elif self.selected_stack_id:
            prev_id = f"item-stack-{self.selected_stack_id}"

        items_to_add: list[ListItem] = []
        new_selected_index: int | None = None

        query = self.search_query.strip().lower()
        cat = self.selected_category

        # 1. Filter manifests
        if cat in ("all", "mcp", "skill", "plugin"):
            for m in self.manifests:
                if cat != "all":
                    if m.integration_type.value != cat:
                        continue

                if query:
                    m_tags = [t.lower() for t in (m.tags or [])]
                    match = (
                        query in m.id.lower()
                        or query in m.name.lower()
                        or query in (m.description or "").lower()
                        or any(query in t for t in m_tags)
                    )
                    if not match:
                        continue

                is_inst, curr_v, newer_v = self._get_installation_status(m)
                if is_inst and newer_v:
                    badge = f"[bold yellow](Update: v{curr_v}->v{newer_v})[/bold yellow]"
                elif is_inst:
                    badge = f"[bold green](Installed v{curr_v})[/bold green]"
                else:
                    badge = "[dim](Available)[/dim]"

                type_badge = f"[{m.integration_type.value.upper()}]"
                label_str = f"{type_badge} {m.name} ({m.id}) {badge}"
                item_widget_id = f"item-{m.id}"
                items_to_add.append(ListItem(Label(label_str), id=item_widget_id))

        # 2. Filter stacks
        if cat in ("all", "stack"):
            for sid, stack in self.stacks.items():
                if query:
                    match = (
                        query in sid.lower()
                        or query in (stack.name or "").lower()
                        or query in (stack.description or "").lower()
                    )
                    if not match:
                        continue

                inst_count, total_count = self._get_stack_status(stack)
                if inst_count == total_count and total_count > 0:
                    badge = f"[bold green](Installed: {inst_count}/{total_count})[/bold green]"
                elif inst_count > 0:
                    badge = f"[bold yellow](Partial: {inst_count}/{total_count})[/bold yellow]"
                else:
                    badge = f"[dim](Available: {total_count} items)[/dim]"

                stack_name = stack.name or sid
                label_str = f"[STACK] {stack_name} ({sid}) {badge}"
                item_widget_id = f"item-stack-{sid}"
                items_to_add.append(ListItem(Label(label_str), id=item_widget_id))

        for idx, itm in enumerate(items_to_add):
            list_view.append(itm)
            if prev_id and itm.id == prev_id:
                new_selected_index = idx

        if items_to_add:
            if new_selected_index is not None:
                list_view.index = new_selected_index
                self._update_details_view()
            else:
                list_view.index = 0
                first_item = items_to_add[0]
                if first_item.id:
                    if first_item.id.startswith("item-stack-"):
                        self._select_stack(first_item.id[len("item-stack-"):])
                    else:
                        self._select_addon(first_item.id[len("item-"):])
        else:
            self.selected_manifest = None
            self.selected_stack = None
            self.selected_stack_id = None
            self._preview_token += 1
            self._update_details_view()

    def _refresh_addon_list(self) -> Worker[None]:
        """Schedule list refresh worker with exclusive lock."""
        return self.run_worker(
            self._do_refresh_addon_list(),
            group="refresh_addon_list",
            exclusive=True,
        )

    def _update_config_summary(self) -> None:
        summary_widget = self.query_one("#config-summary", Static)
        agent_str = self.selected_agent.name if self.selected_agent else "None detected"
        scope_str = self.selected_scope.value.capitalize()
        installed_status = (
            " [green](detected)[/green]"
            if (self.selected_agent and self.selected_agent.installed)
            else " [yellow](not detected)[/yellow]"
        )
        text = f"Agent: [bold]{agent_str}[/bold]{installed_status}\nScope: [bold]{scope_str}[/bold]"
        summary_widget.update(text)

    def _on_target_config_changed(self) -> None:
        """Refresh summary, list badges, and detail view when agent or scope changes."""
        self._update_config_summary()
        self._refresh_addon_list()
        self._update_details_view()

    def _select_addon(self, addon_id: str) -> None:
        """Select an add-on, synchronize preview token, and refresh details view."""
        if not self.registry:
            return
        manifest = self.registry.get(addon_id)
        if not manifest:
            return
        if self.selected_manifest and self.selected_manifest.id == manifest.id and self.selected_stack is None:
            return
        self.selected_manifest = manifest
        self.selected_stack = None
        self.selected_stack_id = None
        self._preview_token += 1
        try:
            list_view = self.query_one("#addon-list", ListView)
            for idx, child in enumerate(list_view.children):
                if child.id == f"item-{addon_id}":
                    if list_view.index != idx:
                        list_view.index = idx
                    break
        except Exception:
            pass
        self._update_details_view()

    def _select_stack(self, stack_id: str) -> None:
        """Select a stack, synchronize preview token, and refresh details view."""
        if stack_id not in self.stacks:
            return
        stack = self.stacks[stack_id]
        if self.selected_stack and self.selected_stack_id == stack_id and self.selected_manifest is None:
            return
        self.selected_stack = stack
        self.selected_stack_id = stack_id
        self.selected_manifest = None
        self._preview_token += 1
        try:
            list_view = self.query_one("#addon-list", ListView)
            for idx, child in enumerate(list_view.children):
                if child.id == f"item-stack-{stack_id}":
                    if list_view.index != idx:
                        list_view.index = idx
                    break
        except Exception:
            pass
        self._update_details_view()

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """Handle highlight changes (keyboard navigation / cursor movement)."""
        if not event.item or not event.item.id:
            return
        if event.item.id.startswith("item-stack-"):
            stack_id = event.item.id[len("item-stack-"):]
            self._select_stack(stack_id)
        else:
            addon_id = event.item.id.replace("item-", "")
            self._select_addon(addon_id)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Handle explicit selection of an add-on item from the list."""
        if not event.item or not event.item.id:
            return
        if event.item.id.startswith("item-stack-"):
            stack_id = event.item.id[len("item-stack-"):]
            self._select_stack(stack_id)
        else:
            addon_id = event.item.id.replace("item-", "")
            self._select_addon(addon_id)

    def _update_details_view(self) -> None:
        detail_view = self.query_one("#detail-view", Markdown)
        confirm_btn = self.query_one("#btn-confirm", Button)
        remove_btn = self.query_one("#btn-remove", Button)
        update_btn = self.query_one("#btn-update", Button)

        confirm_btn.disabled = True
        remove_btn.disabled = True
        update_btn.disabled = True

        if self.selected_stack:
            stack = self.selected_stack
            sid = self.selected_stack_id or "stack"
            inst_count, total_count = self._get_stack_status(stack)

            md_text = f"## Stack: {stack.name or sid} (`{sid}`)\n\n"
            md_text += f"**Bundle Version:** {stack.version or '1.0'} | **Total Add-ons:** {total_count}\n\n"
            if stack.description:
                md_text += f"{stack.description}\n\n"

            if inst_count == total_count and total_count > 0:
                md_text += f"**Installation Status:** ✅ **Fully Installed** ({inst_count}/{total_count})\n\n"
            elif inst_count > 0:
                md_text += f"**Installation Status:** ⚠️ **Partially Installed** ({inst_count}/{total_count})\n\n"
                confirm_btn.disabled = False
            else:
                md_text += f"**Installation Status:** ⚪ **Not Installed** (0/{total_count})\n\n"
                confirm_btn.disabled = False

            md_text += "### Bundled Add-ons:\n"
            all_compat = True
            incompat_reasons: list[str] = []

            for entry in stack.addons:
                addon_id = entry if isinstance(entry, str) else entry.id
                pinned_v = None if isinstance(entry, str) else entry.version
                m = self.registry.get(addon_id) if self.registry else None
                rec = self._get_addon_installed_record(addon_id)
                status_icon = "✅ Installed" if rec else "⚪ Not Installed"
                ver_str = f"v{pinned_v}" if pinned_v else "latest"
                addon_title = m.name if m else addon_id
                md_text += f"- **{addon_title}** (`{addon_id}`) [{ver_str}] — {status_icon}\n"

                if m and self.selected_agent:
                    compat_engine = CompatibilityEngine(registry=self.registry)
                    cres = compat_engine.evaluate(m, self.selected_agent, self.selected_scope, registry=self.registry)
                    if not cres.compatible:
                        all_compat = False
                        incompat_reasons.extend([f"{addon_id}: {r}" for r in cres.reasons])
                elif not self.selected_agent:
                    all_compat = False

            if self.selected_agent:
                if all_compat:
                    md_text += f"\n### Compatibility: ✅ All add-ons compatible with {self.selected_agent.name}\n"
                else:
                    md_text += f"\n### Compatibility: ❌ Incompatible add-ons with {self.selected_agent.name}\n"
                    for r in incompat_reasons:
                        md_text += f"- {r}\n"
                    confirm_btn.disabled = True
            else:
                md_text += "\n### Compatibility: ⚠️ No AI Agent Detected\n"
                confirm_btn.disabled = True

            detail_view.update(md_text)
            self._prepare_secrets_ui()
            return

        if not self.selected_manifest:
            detail_view.update("No add-on selected.")
            self._prepare_secrets_ui()
            return

        m = self.selected_manifest
        is_inst, curr_ver, newer_ver = self._get_installation_status(m)

        md_text = f"## {m.name} (`{m.id}`)\n\n"
        md_text += (
            f"**Version:** {m.version} | **License:** {m.license} | **Category:** {m.category}\n\n"
        )
        md_text += f"{m.description}\n\n"
        md_text += f"**Integration Type:** `{m.integration_type.value}`\n\n"

        if is_inst:
            if newer_ver:
                md_text += (
                    f"**Installation Status:** ⚠️ **Installed** (v{curr_ver}) — "
                    f"**Update Available (v{newer_ver})**\n\n"
                )
                update_btn.disabled = False
            else:
                md_text += f"**Installation Status:** ✅ **Installed** (v{curr_ver})\n\n"
            remove_btn.disabled = False
        else:
            md_text += "**Installation Status:** ⚪ **Not Installed**\n\n"

        if self.selected_agent:
            compat_engine = CompatibilityEngine(registry=self.registry)
            compat = compat_engine.evaluate(
                m, self.selected_agent, self.selected_scope, registry=self.registry
            )
            if compat.compatible:
                md_text += f"### Compatibility: ✅ Compatible with {self.selected_agent.name}\n\n"
                if not is_inst:
                    confirm_btn.disabled = False
            else:
                reasons = "\n".join([f"- {r}" for r in compat.reasons])
                md_text += (
                    f"### Compatibility: ❌ Incompatible with {self.selected_agent.name}\n"
                    f"{reasons}\n\n"
                )
        else:
            md_text += "### Compatibility: ⚠️ No AI Agent Detected\n\n"

        detail_view.update(md_text)
        self._prepare_secrets_ui()

    def _prepare_secrets_ui(self) -> None:
        self.secret_inputs.clear()
        container = self.query_one("#secrets-container", Vertical)
        container.remove_children()

        manifests_to_check: list[IntegrationManifest] = []
        if self.selected_stack and self.registry:
            for entry in self.selected_stack.addons:
                aid = entry if isinstance(entry, str) else entry.id
                m = self.registry.get(aid)
                if m:
                    manifests_to_check.append(m)
        elif self.selected_manifest:
            manifests_to_check.append(self.selected_manifest)

        required_vars_all: list[tuple[str, Any, str]] = []
        for m in manifests_to_check:
            if m.handler_spec.mcp and m.handler_spec.mcp.env_vars:
                for v in m.handler_spec.mcp.env_vars:
                    if v.required:
                        required_vars_all.append((v.name, v, m.name))

        if not required_vars_all:
            return

        self._secret_counter += 1
        container.mount(Label("Required Secrets Input", classes="section-title"))
        for name, spec, addon_name in required_vars_all:
            lbl = Label(f"{name} ({addon_name} - {spec.description or 'Required secret'}):")
            inp = Input(
                placeholder=f"Enter {name}",
                password=True,
                id=f"sec-{name}-{self._secret_counter}",
            )
            container.mount(lbl)
            container.mount(inp)
            self.secret_inputs[name] = inp

    # -------------------------------------------------------------------------
    # Keybinding & Button Actions
    # -------------------------------------------------------------------------
    def action_doctor(self) -> None:
        """Open the HealthScreen diagnostics modal/screen."""
        self.push_screen(HealthScreen(workspace_dir=self.workspace_dir, store_dir=self.store_dir))

    def action_sync(self) -> None:
        """Open the SyncScreen workspace reconciliation screen."""
        self.push_screen(
            SyncScreen(
                workspace_dir=self.workspace_dir,
                target_agent=self.selected_agent,
                scope=self.selected_scope,
                registry=self.registry,
                store_dir=self.store_dir,
            )
        )

    def action_cycle_agent(self) -> None:
        """Cycle through detected/available target agents."""
        if not self.detected_agents:
            return
        agent_keys = list(self.detected_agents.keys())
        if self.selected_agent and self.selected_agent.agent_id in agent_keys:
            curr_idx = agent_keys.index(self.selected_agent.agent_id)
            next_idx = (curr_idx + 1) % len(agent_keys)
        else:
            next_idx = 0
        next_agent_id = agent_keys[next_idx]
        self.selected_agent = self.detected_agents[next_agent_id]

        agent_select = self.query_one("#select-agent", Select)
        if agent_select.value != next_agent_id:
            agent_select.value = next_agent_id
        else:
            self._on_target_config_changed()

        self.notify(f"Target agent set to {self.selected_agent.name}", severity="information")

    def action_toggle_scope(self) -> None:
        """Toggle between Workspace and Global configuration scopes."""
        if self.selected_scope == Scope.WORKSPACE:
            self.selected_scope = Scope.GLOBAL
        else:
            self.selected_scope = Scope.WORKSPACE

        scope_select = self.query_one("#select-scope", Select)
        if scope_select.value != self.selected_scope.value:
            scope_select.value = self.selected_scope.value
        else:
            self._on_target_config_changed()

        self.notify(f"Scope switched to {self.selected_scope.value.capitalize()}", severity="information")

    def action_cycle_category(self) -> None:
        """Cycle through category filters."""
        categories = ["all", "mcp", "skill", "plugin", "stack"]
        if self.selected_category in categories:
            curr_idx = categories.index(self.selected_category)
            next_idx = (curr_idx + 1) % len(categories)
        else:
            next_idx = 0
        next_cat = categories[next_idx]
        cat_select = self.query_one("#select-category", Select)
        cat_select.value = next_cat

    def on_select_changed(self, event: Select.Changed) -> None:
        """Handle user selection from target agent, scope, and category dropdowns."""
        if event.control.id == "select-agent":
            if event.value != Select.BLANK and event.value is not None:
                agent_id = str(event.value)
                if agent_id in self.detected_agents:
                    target_agent = self.detected_agents[agent_id]
                    if self.selected_agent is None or self.selected_agent.agent_id != target_agent.agent_id:
                        self.selected_agent = target_agent
                        self._on_target_config_changed()
                        self.notify(f"Target agent set to {self.selected_agent.name}", severity="information")
        elif event.control.id == "select-scope":
            if event.value != Select.BLANK and event.value is not None:
                scope_str = str(event.value)
                try:
                    new_scope = Scope.from_str(scope_str)
                    if self.selected_scope != new_scope:
                        self.selected_scope = new_scope
                        self._on_target_config_changed()
                        self.notify(f"Scope switched to {self.selected_scope.value.capitalize()}", severity="information")
                except ValueError:
                    pass
        elif event.control.id == "select-category":
            if event.value != Select.BLANK and event.value is not None:
                new_cat = str(event.value)
                if self.selected_category != new_cat:
                    self.selected_category = new_cat
                    self._refresh_addon_list()
                    cat_name = dict(
                        all="All Categories",
                        mcp="MCP Servers",
                        skill="Agent Skills",
                        plugin="Composite Plugins",
                        stack="Stacks",
                    ).get(new_cat, new_cat)
                    self.notify(f"Filtered by: {cat_name}", severity="information")

    def on_input_changed(self, event: Input.Changed) -> None:
        """Handle live search filtering in the add-on search bar."""
        if event.input.id == "addon-search":
            self.search_query = event.value.strip().lower()
            self._refresh_addon_list()

    def action_install(self) -> None:
        """Trigger add-on installation."""
        btn = self.query_one("#btn-confirm", Button)
        if not btn.disabled:
            self._execute_real_installation()
        else:
            self.notify("Installation is not available for current selection.", severity="warning")

    def action_remove(self) -> None:
        """Trigger add-on removal with drift detection."""
        if self.selected_stack:
            self.notify("Please select an individual add-on to remove it.", severity="warning")
            return
        self._initiate_removal()

    def action_update(self) -> None:
        """Trigger add-on version update with dry-run preview."""
        if self.selected_stack:
            self.notify("Please select an individual add-on to update it.", severity="warning")
            return
        self._initiate_update()

    def action_check_compat(self) -> None:
        self._run_compatibility_check()

    def action_preview_plan(self) -> None:
        self._run_plan_preview()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Dispatch action buttons to core application engine services."""
        if event.button.id == "btn-compat":
            self._run_compatibility_check()
        elif event.button.id == "btn-plan":
            self._run_plan_preview()
        elif event.button.id == "btn-confirm":
            self._execute_real_installation()
        elif event.button.id == "btn-remove":
            self._initiate_removal()
        elif event.button.id == "btn-update":
            self._initiate_update()
        elif event.button.id == "btn-health":
            self.action_doctor()
        elif event.button.id == "btn-sync":
            self.action_sync()
        elif event.button.id == "btn-switch-agent":
            self.action_cycle_agent()
        elif event.button.id == "btn-toggle-scope":
            self.action_toggle_scope()

    # -------------------------------------------------------------------------
    # Compatibility & Plan Preview
    # -------------------------------------------------------------------------
    def _run_compatibility_check(self) -> None:
        if not self.selected_agent:
            return

        detail_view = self.query_one("#detail-view", Markdown)

        if self.selected_stack and self.registry:
            target_stack = self.selected_stack
            compat_engine = CompatibilityEngine(registry=self.registry)
            manifests: list[IntegrationManifest] = []
            for entry in target_stack.addons:
                aid = entry if isinstance(entry, str) else entry.id
                m = self.registry.get(aid)
                if m:
                    manifests.append(m)

            results = compat_engine.evaluate_batch(
                manifests=manifests, agent=self.selected_agent, scope=self.selected_scope, registry=self.registry
            )
            all_compat = all(r.compatible for r in results)
            status_sym = "✅" if all_compat else "❌"
            is_comp = "Compatible" if all_compat else "Incompatible"

            md = f"## Stack Compatibility Result\n\n"
            md += f"**Stack:** {target_stack.name or self.selected_stack_id}\n"
            md += f"**Agent:** {self.selected_agent.name}\n"
            md += f"**Status:** {status_sym} {is_comp}\n\n"
            for m, r in zip(manifests, results):
                r_sym = "✅" if r.compatible else "❌"
                md += f"### {r_sym} {m.name} (`{m.id}`)\n"
                for reason in r.reasons:
                    md += f"- {reason}\n"
            detail_view.update(md)
            self.notify(f"Stack compatibility: {is_comp}", severity="information" if all_compat else "warning")
            return

        if not self.selected_manifest:
            return

        compat_engine = CompatibilityEngine(registry=self.registry)
        compat = compat_engine.evaluate(
            self.selected_manifest,
            self.selected_agent,
            self.selected_scope,
            registry=self.registry,
        )

        status_sym = "✅" if compat.compatible else "❌"
        is_comp = "Compatible" if compat.compatible else "Incompatible"
        md = "## Compatibility Result\n\n"
        md += f"**Add-on:** {self.selected_manifest.name}\n"
        md += f"**Agent:** {self.selected_agent.name}\n"
        md += f"**Status:** {status_sym} {is_comp}\n\n"
        for r in compat.reasons:
            md += f"- {r}\n"
        detail_view.update(md)
        self.notify(f"Compatibility check: {is_comp}", severity="information" if compat.compatible else "warning")

    def _run_plan_preview(self) -> None:
        if not self.selected_agent or not self.registry:
            return
        token = self._preview_token
        target_agent = self.selected_agent
        target_scope = self.selected_scope

        if self.selected_stack:
            target_stack = self.selected_stack
            target_stack_id = self.selected_stack_id
            inst_engine = InstallationEngine(registry=self.registry)
            try:
                manifests: list[IntegrationManifest] = []
                for entry in target_stack.addons:
                    aid = entry if isinstance(entry, str) else entry.id
                    m = self.registry.get(aid)
                    if m:
                        manifests.append(m)
                batch_plan = inst_engine.generate_batch_plan(
                    manifests=manifests, agent=target_agent, scope=target_scope, registry=self.registry
                )
                if token != self._preview_token or self.selected_stack != target_stack:
                    return
                detail_view = self.query_one("#detail-view", Markdown)
                md = f"## Batch Plan Preview for Stack: {target_stack.name or target_stack_id}\n\n"
                md += f"**Target Agent:** {target_agent.name}\n"
                md += f"**Scope:** {target_scope.value}\n\n"
                md += f"### Add-ons in Batch ({len(batch_plan.plans)}):\n"
                for p in batch_plan.plans:
                    md += f"#### {p.addon_name} (`{p.addon_id}`)\n"
                    for op in p.planned_operations:
                        md += f"- ✓ {op.description}\n"
                md += "\n*No changes were made to host system state.*\n"
                detail_view.update(md)
                self.notify(f"Generated dry-run batch plan for stack '{target_stack.name}'.", severity="information")
            except Exception as exc:
                if token != self._preview_token or self.selected_stack != target_stack:
                    return
                detail_view = self.query_one("#detail-view", Markdown)
                detail_view.update(f"## Stack Plan Preview Error\n\n❌ {exc}")
                self.notify(f"Plan error: {exc}", severity="error")
            return

        if not self.selected_manifest:
            return

        target_manifest = self.selected_manifest
        inst_engine = InstallationEngine(registry=self.registry)
        try:
            plan = inst_engine.generate_plan(
                target_manifest, target_agent, target_scope
            )
            if token != self._preview_token or self.selected_manifest != target_manifest:
                return
            self.current_plan = plan
            detail_view = self.query_one("#detail-view", Markdown)

            md = "## Installation Plan Preview (Dry-Run)\n\n"
            md += f"**Target Agent:** {target_agent.name}\n"
            md += f"**Scope:** {target_scope.value}\n\n"
            md += "### Operations to be executed:\n"
            for op in plan.planned_operations:
                md += f"- ✓ {op.description}\n"
            md += "\n*No changes were made to host system state.*\n"
            detail_view.update(md)
            self.notify(f"Generated dry-run plan for {target_manifest.name}.", severity="information")
        except Exception as exc:
            if token != self._preview_token or self.selected_manifest != target_manifest:
                return
            detail_view = self.query_one("#detail-view", Markdown)
            detail_view.update(f"## Plan Preview Error\n\n❌ {exc}")
            self.notify(f"Plan error: {exc}", severity="error")

    # -------------------------------------------------------------------------
    # Installation Execution
    # -------------------------------------------------------------------------
    def _execute_real_installation(self, force_secrets: bool = False) -> None:
        """Delegate installation execution directly to core ExecutionEngine & VerificationEngine."""
        if not self.selected_agent or not self.registry:
            return

        detail_view = self.query_one("#detail-view", Markdown)

        # 1. Stack installation flow
        if self.selected_stack:
            target_stack = self.selected_stack
            manifests: list[IntegrationManifest] = []
            for entry in target_stack.addons:
                aid = entry if isinstance(entry, str) else entry.id
                m = self.registry.get(aid)
                if m:
                    manifests.append(m)

            secret_map: dict[str, str] = {}
            for m in manifests:
                if m.handler_spec.mcp and m.handler_spec.mcp.env_vars:
                    for req_spec in m.handler_spec.mcp.env_vars:
                        if req_spec.required:
                            inp = self.secret_inputs.get(req_spec.name)
                            val = inp.value.strip() if inp else ""
                            if not val:
                                detail_view.update(
                                    f"## Installation Error\n\n❌ Secret '{req_spec.name}' is required for {m.name}.\n\n"
                                    "No value was entered — please fill in the field and try again."
                                )
                                self.notify(f"Secret '{req_spec.name}' is required. No value was entered.", severity="error")
                                return
                            secret_map[req_spec.name] = val

            wal_mgr = TransactionWALManager(transactions_dir=self.store_dir / "transactions")
            state_store = InstalledStateStore(store_dir=self.store_dir)
            lockfile_mgr = LockfileManager()
            inst_engine = InstallationEngine(registry=self.registry, wal_manager=wal_mgr)
            execution_engine = ExecutionEngine(
                wal_manager=wal_mgr,
                state_store=state_store,
                lockfile_manager=lockfile_mgr,
                registry=self.registry,
                workspace_dir=self.workspace_dir,
            )

            try:
                tx = inst_engine.create_batch_transaction(
                    manifests=manifests, agent=self.selected_agent, scope=self.selected_scope, registry=self.registry
                )
                if not tx.batch_plan or tx.phase == TransactionPhase.FAILED:
                    detail_view.update(f"## Planning Error\n\n❌ {tx.error_message}")
                    self.notify(f"Planning error: {tx.error_message}", severity="error")
                    return

                batch_plan = tx.batch_plan
                tx.phase = TransactionPhase.REVIEWED
                tx.phase = TransactionPhase.EXECUTING

                res = execution_engine.execute_batch_plan(
                    batch_plan=batch_plan,
                    transaction=tx,
                    dry_run=False,
                    secret_values=secret_map,
                    registry=self.registry,
                    workspace_dir=self.workspace_dir,
                )

                if res.status == ExecutionStatus.SUCCESS:
                    tx.phase = TransactionPhase.COMMITTED
                    md = "## Stack Installation Successful! 🎉\n\n"
                    md += f"**Stack:** {target_stack.name or self.selected_stack_id}\n"
                    md += f"**Agent:** {self.selected_agent.name}\n"
                    md += f"**Scope:** {self.selected_scope.value}\n\n"
                    md += f"### Installed Add-ons ({len(manifests)}):\n"
                    for m in manifests:
                        md += f"- ✓ {m.name} (`{m.id}`)\n"
                    detail_view.update(md)
                    self.notify(f"Successfully installed stack '{target_stack.name}'!", severity="information")
                    self._refresh_addon_list()
                    self._update_details_view()
                else:
                    tx.phase = TransactionPhase.ROLLED_BACK
                    raw_err = res.error_message or "Batch execution failed"
                    err_msg = mask_secrets_in_text(raw_err, list(secret_map.values()))
                    md = f"## Batch Installation Failed ❌\n\n**Error:** {err_msg}"
                    detail_view.update(md)
                    self.notify(f"Batch installation failed: {err_msg}", severity="error")
            except Exception as exc:
                err_msg = mask_secrets_in_text(str(exc), list(secret_map.values()))
                detail_view.update(f"## Installation Error\n\n❌ {err_msg}")
                self.notify(f"Installation error: {err_msg}", severity="error")
            return

        # 2. Single add-on installation flow
        if not self.selected_manifest:
            return

        secret_map = {}
        all_warnings: list[str] = []

        mcp_spec = self.selected_manifest.handler_spec.mcp
        env_vars_map = {v.name: v for v in mcp_spec.env_vars} if mcp_spec else {}
        required_vars = [v for v in mcp_spec.env_vars if v.required] if mcp_spec else []

        for req_spec in required_vars:
            inp = self.secret_inputs.get(req_spec.name)
            val = inp.value.strip() if inp else ""
            if not val:
                detail_view.update(
                    f"## Installation Error\n\n❌ Secret '{req_spec.name}' is required.\n\n"
                    "No value was entered — please fill in the field and try again."
                )
                self.notify(f"Secret '{req_spec.name}' is required. No value was entered.", severity="error")
                return

            if not force_secrets:
                warnings = validate_secret_format(val, req_spec)
                if warnings:
                    all_warnings.append(f"{req_spec.name}: " + ", ".join(warnings))

            secret_map[req_spec.name] = val

        for name, inp in self.secret_inputs.items():
            if name not in secret_map and name in env_vars_map:
                val = inp.value.strip()
                if val:
                    spec = env_vars_map[name]
                    if not force_secrets:
                        warnings = validate_secret_format(val, spec)
                        if warnings:
                            all_warnings.append(f"{name}: " + ", ".join(warnings))
                    secret_map[name] = val

        if all_warnings and not force_secrets:
            def handle_warning_result(confirmed: bool | None) -> None:
                if confirmed:
                    self._execute_real_installation(force_secrets=True)
            self.push_screen(SecretWarningModal(all_warnings), callback=handle_warning_result)
            return

        for name, val in secret_map.items():
            preview = mask_secret_preview(val)
            self.notify(f"Received {name}: {preview}", severity="information")

        wal_mgr = TransactionWALManager(transactions_dir=self.store_dir / "transactions")
        state_store = InstalledStateStore(store_dir=self.store_dir)
        lockfile_mgr = LockfileManager()
        inst_engine = InstallationEngine(registry=self.registry, wal_manager=wal_mgr)
        execution_engine = ExecutionEngine(
            wal_manager=wal_mgr,
            state_store=state_store,
            lockfile_manager=lockfile_mgr,
            registry=self.registry,
            workspace_dir=self.workspace_dir,
        )
        verification_engine = VerificationEngine()

        try:
            tx = inst_engine.create_transaction(
                self.selected_manifest, self.selected_agent, self.selected_scope
            )
            if not tx.plan:
                detail_view.update(f"## Planning Error\n\n❌ {tx.error_message}")
                self.notify(f"Planning error: {tx.error_message}", severity="error")
                return

            plan = tx.plan
            tx.phase = TransactionPhase.REVIEWED
            tx.phase = TransactionPhase.EXECUTING

            # Delegate execution to core engine
            res = execution_engine.execute_plan(
                plan, transaction=tx, dry_run=False, secret_values=secret_map
            )

            if res.status == ExecutionStatus.SUCCESS:
                tx.phase = TransactionPhase.COMMITTED
                ver_res = verification_engine.verify_plan(
                    plan, dry_run=False, secret_values=secret_map
                )

                md = "## Installation Successful! 🎉\n\n"
                md += f"**Add-on:** {self.selected_manifest.name}\n"
                md += f"**Agent:** {self.selected_agent.name}\n"
                md += f"**Transaction Phase:** `{TransactionPhase.COMMITTED.value}`\n\n"
                md += "### Verification Results:\n"
                for chk in ver_res.checks:
                    chk_desc = mask_secrets_in_text(chk.description, list(secret_map.values()))
                    sym = "✓" if chk.status == VerificationStatus.PASSED else "✗"
                    md += f"- {sym} {chk_desc}\n"
                detail_view.update(md)
                self.notify(f"Successfully installed {self.selected_manifest.name}!", severity="information")
                self._refresh_addon_list()
                self._update_details_view()
            else:
                tx.phase = TransactionPhase.ROLLED_BACK
                raw_err = res.error_message or "Execution failed"
                err_msg = mask_secrets_in_text(raw_err, list(secret_map.values()))
                md = "## Verification / Execution Failed ❌\n\n"
                md += f"**Error:** {err_msg}\n\n"
                md += f"**Transaction Phase:** `{TransactionPhase.ROLLED_BACK.value}`\n\n"
                md += "### Rollback Actions Executed:\n"
                for rb in res.rolled_back_operations:
                    rb_desc = mask_secrets_in_text(rb.description, list(secret_map.values()))
                    md += f"- ↩ {rb_desc}\n"
                detail_view.update(md)
                self.notify(f"Installation failed: {err_msg}", severity="error")

        except Exception as exc:
            err_msg = mask_secrets_in_text(str(exc), list(secret_map.values()))
            detail_view.update(f"## Installation Error\n\n❌ {err_msg}")
            self.notify(f"Installation error: {err_msg}", severity="error")

    # -------------------------------------------------------------------------
    # Removal Flow & Drift Detection
    # -------------------------------------------------------------------------
    def _initiate_removal(self) -> None:
        """Check installed state, run drift detection, and trigger removal confirmation modal."""
        if not self.selected_manifest or not self.selected_agent:
            return

        is_inst, _, _ = self._get_installation_status(self.selected_manifest)
        if not is_inst:
            self.notify(f"'{self.selected_manifest.name}' is not installed.", severity="warning")
            return

        target_record = self._get_addon_installed_record(self.selected_manifest.id)

        # Run drift detection
        drifts = detect_installation_drift(
            record=target_record,
            manifest=self.selected_manifest,
            agent=self.selected_agent,
            scope=self.selected_scope,
            workspace_dir=self.workspace_dir,
        )

        if drifts:
            # Drift detected -> show Drift Confirmation Modal
            modal = DriftConfirmModal(
                addon_id=self.selected_manifest.id,
                addon_name=self.selected_manifest.name,
                drifts=drifts,
            )
            self.push_screen(modal, callback=self._handle_drift_confirm_result)
        else:
            # Generate removal plan for preview in standard confirm modal
            inst_engine = InstallationEngine(registry=self.registry)
            rem_plan = inst_engine.generate_removal_plan(
                manifest=self.selected_manifest,
                agent=self.selected_agent,
                scope=self.selected_scope,
                registry=self.registry,
                force=False,
            )
            ops_desc = [op.description for op in rem_plan.planned_operations]
            remove_modal = RemoveConfirmModal(
                manifest=self.selected_manifest,
                agent_name=self.selected_agent.name,
                scope=self.selected_scope.value,
                planned_ops=ops_desc,
            )
            self.push_screen(remove_modal, callback=self._handle_remove_confirm_result)

    def _handle_drift_confirm_result(self, confirmed: bool | None) -> None:
        if confirmed:
            self._execute_removal(force=True)
        else:
            self.notify("Removal cancelled by user.", severity="information")

    def _handle_remove_confirm_result(self, confirmed: bool | None) -> None:
        if confirmed:
            self._execute_removal(force=False)
        else:
            self.notify("Removal cancelled by user.", severity="information")

    def _execute_removal(self, force: bool = False) -> None:
        """Execute transactional removal with WAL, file locking, and verification."""
        if not self.selected_manifest or not self.selected_agent:
            return

        wal_mgr = TransactionWALManager(transactions_dir=self.store_dir / "transactions")
        state_store = InstalledStateStore(store_dir=self.store_dir)
        lockfile_mgr = LockfileManager()
        inst_engine = InstallationEngine(registry=self.registry, wal_manager=wal_mgr)
        execution_engine = ExecutionEngine(
            wal_manager=wal_mgr,
            state_store=state_store,
            lockfile_manager=lockfile_mgr,
            registry=self.registry,
            workspace_dir=self.workspace_dir,
        )
        verification_engine = VerificationEngine()
        detail_view = self.query_one("#detail-view", Markdown)

        try:
            tx = inst_engine.create_removal_transaction(
                manifest=self.selected_manifest,
                agent=self.selected_agent,
                scope=self.selected_scope,
                registry=self.registry,
                force=force,
            )
            if tx.phase == TransactionPhase.FAILED or tx.plan is None:
                detail_view.update(f"## Removal Planning Error\n\n❌ {tx.error_message}")
                self.notify(f"Removal planning error: {tx.error_message}", severity="error")
                return

            plan = tx.plan
            tx.phase = TransactionPhase.REVIEWED
            wal_mgr.write_transaction(tx)

            exec_res = execution_engine.execute_plan(
                plan=plan,
                transaction=tx,
                dry_run=False,
                is_removal=True,
            )

            if exec_res.status != ExecutionStatus.SUCCESS:
                detail_view.update(f"## Removal Execution Failed\n\n❌ {exec_res.error_message}")
                self.notify(f"Removal failed: {exec_res.error_message}", severity="error")
                return

            # Verify removal
            ver_res = verification_engine.verify_plan(plan, dry_run=False)
            if not ver_res.verified:
                err_str = "; ".join(ver_res.errors) if ver_res.errors else "Remnants remain."
                detail_view.update(f"## Post-Removal Verification Failed\n\n❌ {err_str}")
                self.notify(f"Verification warning: {err_str}", severity="warning")

            # If plugin, clean up child components
            if (
                self.selected_manifest.integration_type == IntegrationType.PLUGIN
                and self.selected_manifest.handler_spec.plugin
            ):
                for child_id in self.selected_manifest.handler_spec.plugin.components:
                    state_store.remove_installation(
                        target_agent=self.selected_agent.agent_id,
                        scope=self.selected_scope,
                        addon_id=child_id,
                    )
                    if self.selected_scope == Scope.WORKSPACE:
                        lockfile_mgr.remove_from_lockfile(
                            self.workspace_dir,
                            target_agent=self.selected_agent.agent_id,
                            addon_id=child_id,
                        )

            md = "## Removal Successful! 🗑️\n\n"
            md += f"**Add-on:** {self.selected_manifest.name} (`{self.selected_manifest.id}`)\n"
            md += f"**Agent:** {self.selected_agent.name}\n"
            md += f"**Scope:** {self.selected_scope.value}\n\n"
            md += "All configurations and assets have been cleaned up.\n"
            detail_view.update(md)
            self.notify(
                f"Successfully removed {self.selected_manifest.name} ({self.selected_manifest.id})!",
                severity="information",
            )
            self._refresh_addon_list()
            self._update_details_view()

        except Exception as exc:
            detail_view.update(f"## Removal Error\n\n❌ {exc}")
            self.notify(f"Removal error: {exc}", severity="error")

    # -------------------------------------------------------------------------
    # Update Flow
    # -------------------------------------------------------------------------
    def _initiate_update(self) -> None:
        """Plan update for selected add-on and present preview modal."""
        if not self.selected_manifest or not self.selected_agent:
            return

        state_store = InstalledStateStore(store_dir=self.store_dir)
        lockfile_mgr = LockfileManager()
        wal_mgr = TransactionWALManager(transactions_dir=self.store_dir / "transactions")
        update_engine = UpdateEngine(
            state_store=state_store,
            lockfile_manager=lockfile_mgr,
            registry=self.registry,
            wal_manager=wal_mgr,
            workspace_dir=self.workspace_dir,
        )

        try:
            update_plan = update_engine.plan_update(
                addon_id=self.selected_manifest.id,
                target_agent=self.selected_agent,
                scope=self.selected_scope,
                registry=self.registry,
                workspace_dir=self.workspace_dir,
            )
        except Exception as exc:
            self.notify(f"Update planning error: {exc}", severity="error")
            detail_view = self.query_one("#detail-view", Markdown)
            detail_view.update(f"## Update Planning Error\n\n❌ {exc}")
            return

        if update_plan.is_empty:
            self.notify(f"'{self.selected_manifest.name}' is already up to date.", severity="information")
            return

        modal = UpdatePlanModal(plan=update_plan)
        self.push_screen(modal, callback=lambda confirmed: self._handle_update_confirm_result(confirmed, update_plan))

    def _handle_update_confirm_result(self, confirmed: bool | None, update_plan: UpdatePlan) -> None:
        if not confirmed:
            self.notify("Update cancelled by user.", severity="information")
            return
        self._execute_update(update_plan)

    def _execute_update(self, update_plan: UpdatePlan) -> None:
        """Execute update within single WAL transaction and file locking."""
        if not self.selected_agent:
            return

        state_store = InstalledStateStore(store_dir=self.store_dir)
        lockfile_mgr = LockfileManager()
        wal_mgr = TransactionWALManager(transactions_dir=self.store_dir / "transactions")
        update_engine = UpdateEngine(
            state_store=state_store,
            lockfile_manager=lockfile_mgr,
            registry=self.registry,
            wal_manager=wal_mgr,
            workspace_dir=self.workspace_dir,
        )
        detail_view = self.query_one("#detail-view", Markdown)

        try:
            res = update_engine.execute_update(
                plan=update_plan,
                target_agent=self.selected_agent,
                workspace_dir=self.workspace_dir,
                registry=self.registry,
                dry_run=False,
            )

            if res.success:
                item = update_plan.items[0] if update_plan.items else None
                new_ver = item.target_version if item else "latest"
                old_ver = item.current_version if item else ""
                addon_name = self.selected_manifest.name if self.selected_manifest else (item.addon_id if item else "Unknown")
                md = "## Update Successful! 🔄\n\n"
                md += f"**Add-on:** {addon_name}\n"
                md += f"**Updated:** `v{old_ver}` → `v{new_ver}`\n"
                md += f"**Target Agent:** {self.selected_agent.name}\n\n"
                md += "All removal and installation operations were committed atomically.\n"
                detail_view.update(md)
                self.notify(
                    f"Successfully updated to v{new_ver}!",
                    severity="information",
                )
                self._refresh_addon_list()
                self._update_details_view()
            else:
                detail_view.update(f"## Update Failed ❌\n\n**Error:** {res.error_message}")
                self.notify(f"Update failed: {res.error_message}", severity="error")

        except Exception as exc:
            detail_view.update(f"## Update Error\n\n❌ {exc}")
            self.notify(f"Update error: {exc}", severity="error")
