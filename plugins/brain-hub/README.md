# Brain Hub Codex plugin

This plugin exposes the local `brainhub mcp` stdio server and teaches Codex how to record
and retrieve evidence-backed workstream memory safely.

## Local install contract

From the Brain Hub repository, run `make install-plugin-runtime` before adding the
marketplace. This creates a stable runtime at `~/.local/share/brainhub/venv`; the launcher
also falls back to a `brainhub` executable on `PATH` for development. The stdio MCP process
opens the shared SQLite authority directly; start the daemon at `127.0.0.1:8420` as well
when using the web console or passive adapter delivery. The plugin itself has no remote
credentials; the local process owns encryption keys and data access.

Run the read-only prerequisite check with:

```sh
python scripts/preflight.py
```

The manifest intentionally has no `hooks` field: it is not accepted by current Codex
plugin validation. Agent capture is supplied by the separately versioned adapters, so a
missing Brain Hub service cannot interrupt Codex.

## Publication boundary

The repository marketplace entry is ready for local and team testing. A public Codex
Marketplace submission additionally requires a deployed HTTPS MCP endpoint, publisher
identity/domain verification, production privacy and terms URLs, and review credentials.
None are fabricated in this package. Add them only after the production service and
publisher identity exist.

The release review suite is defined in `MARKETPLACE_TESTS.md`.
