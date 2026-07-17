# Brain Hub Codex plugin

This plugin exposes the local `brainhub mcp` stdio server and teaches Codex how to record
and retrieve evidence-backed workstream memory safely.

When Codex activates the plugin, its MCP launcher opens the shared encrypted SQLite
authority directly and drains passive hook events without claiming HTTP ports. Users
do not need Node/npm or a separate watcher. Use `brainhub ui` to start and open the
optional console, `brainhub status` to inspect it, and `brainhub stop` to stop it.

## Local install contract

Use the macOS/Linux or Windows one-command installer in the repository README. It
creates an isolated runtime keyed by the Brain Hub source fingerprint and Python
compatibility, generates an absolute MCP executable path in the installed plugin copy,
registers the marketplace and plugin when the Codex CLI is available, and installs
every adapter command. Third-party packages resolve from declared version ranges at
installation time, so the preview runtime is source-addressed rather than a
bit-for-bit reproducible artifact. No separate `make`, `pip`, Node, or npm step is
required. Restart Codex after installation.

The installed manifest gets a deterministic source cachebuster so an upgrade refreshes
Codex's plugin cache and absolute MCP path. The plugin itself has no remote credentials;
the local process owns encryption keys and data access.

Run the read-only prerequisite check with:

```sh
python scripts/preflight.py
```

The plugin bundles Codex lifecycle hooks at the standard auto-discovered
`hooks/hooks.json` path for session start/stop, prompt submission,
tool pre/post events, compaction, and subagent start/stop. The launcher accepts only
bounded scalar metadata; prompts, transcripts, messages, tool inputs/outputs, and
credentials are discarded, and the adapter pseudonymizes workspace paths before
writing the bounded local spool. The stdio MCP process or the optional supervised Brain
Hub service drains that spool in the background. Hook failures are fail-open and cannot
interrupt Codex.

On the first activation, review and trust the Brain Hub hooks in Codex with `/hooks`.
This is Codex's one-time safety boundary for plugin-provided commands; no separate
watcher, API, web, Node, or npm command is required for MCP and capture.

## Publication boundary

The repository marketplace entry is ready for local and team testing. A public Codex
Marketplace submission additionally requires a deployed HTTPS MCP endpoint, publisher
identity/domain verification, production privacy and terms URLs, and review credentials.
None are fabricated in this package. Add them only after the production service and
publisher identity exist.

The release review suite is defined in `MARKETPLACE_TESTS.md`.
