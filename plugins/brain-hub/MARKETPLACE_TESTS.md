# Marketplace review cases

These fixed cases test the plugin contract before a public submission. The five positive
and three negative cases are deliberately distinct.

## Positive

1. **Record a goal** — Register a new workstream with a goal, run, topic, and evidence-backed
   `ABOUT` edge; verify a repeated request is idempotent.
2. **Continue cross-agent work** — Continue a Codex run from a workstream started by Claude
   Code; verify `CONTINUES` and actor provenance are preserved.
3. **Anchored search** — Search from a selected Decision node; verify every result is within
   two hops and an empty bounded result does not trigger a global fallback.
4. **Evidence path** — Explain the path from a Task to a Claim; verify direction, plain-language
   edge explanations, confidence class, score, and evidence references are returned.
5. **Correction over time** — Correct a prior decision; verify the original remains queryable
   and the new fact uses `SUPERSEDES` with valid and recorded times.
6. **Automatic lifecycle capture** — Trust the bundled hooks, start a task, run a tool,
   compact context, and stop; verify start, pre/post tool, compact, and stop metadata is
   delivered by the plugin MCP's direct spool watcher or the optional managed service
   without a separate user-run watcher.

## Negative

1. **Transcript capture disabled** — Supply a hook payload containing `transcript_path`, raw
   prompts, and messages; verify none appears in the stored event.
2. **Secret storage rejected** — Ask the plugin to store a bearer token and password; verify it
   refuses and records neither value.
3. **Destructive delete unavailable** — Ask the MCP plugin to delete a workstream; verify no
   destructive tool exists and it directs the user to the local UI or CLI.
