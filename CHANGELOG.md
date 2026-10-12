# Changelog

All notable changes to `aiaddons` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-10-12

### Added
- Added Cursor and Hermes Agent support, bringing the total to 5 supported AI coding agents (Claude Code, OpenAI Codex, Antigravity CLI, Cursor, and Hermes Agent).
- Added an interactive category filter in the TUI to filter by MCP servers, skills, plugins, and stacks.
- Added comprehensive usage examples to `--help` for `install`, `remove`, and `update` commands.
- Bundled preconfigured stacks (`dev-starter-stack.yaml`, `agent-behavior-stack.yaml`) in wheel and sdist packages, with automated bundled fallback for `aiaddons install --file`.
- Promoted 4 verified Model Context Protocol (MCP) servers:
  - `chrome-devtools-mcp` (v1.10.1): Official Chrome DevTools server for browser control, debugging, and tracing.
  - `heroku-mcp` (v1.2.9): Official Heroku Platform MCP server for apps, dynos, and database management.
  - `browserstack-mcp` (v1.5.1): Official BrowserStack MCP server for cross-browser testing and device debugging.
  - `qase-mcp` (v2.7.4): Official Qase TMS MCP server for test management and test run reporting.

### Fixed
- All package-based manifests now use real, verified npm versions and cryptographic SHA-256 tarball checksums (previous versions shipped fabricated values).
- All git-sourced skill manifests now use real, verified commit SHAs and validated repository paths.
- Secrets resolution now properly resolves to the secret's actual environment variable value instead of the variable name.
- MCP servers are no longer launched as live daemons during installation.
- The CLI now prints the specific underlying error cause instead of a generic "Verification failed" message.
- The TUI now updates secret input requirements immediately when changing add-on selection.

### Removed
- `linear-mcp`: Removed from the registry because the package no longer resolves on npm.
