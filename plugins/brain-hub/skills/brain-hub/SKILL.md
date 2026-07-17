---
name: brain-hub
description: Register AI-agent work as evidence-backed graph nodes and edges, search prior work semantically, traverse from a selected node, or continue a workstream across sessions and agents.
disable-model-invocation: false
---

# Brain Hub

Use Brain Hub when the user wants work remembered, connected to previous work, searched,
explained through evidence paths, or continued from a graph node.

## Safety and privacy

- Default to metadata, concise semantic summaries, decisions, and opaque artifact
  references. Do not send raw prompts, transcripts, credentials, secrets, environment
  values, or file contents unless the user explicitly opts in.
- Treat every write as idempotent. Reuse stable workstream, run, and artifact identifiers
  when continuing known work.
- Never use Brain Hub as authority for a claim without showing the stored evidence and
  confidence class. Mark agent-derived relationships `INFERRED`; reserve `EXTRACTED` for
  relationships directly supported by cited input.
- Do not call a destructive operation. Deletion and irreversible maintenance are
  intentionally absent from v0.1; any future implementation belongs behind a local
  authenticated admin workflow, never an agent-facing MCP tool.

## Register work

Call `brainhub.record` after a meaningful logical unit is complete or when the user asks
to save progress. Supply:

1. The workstream goal and stable workstream identifier when known.
2. The current run: agent, workspace opaque ID, status, and recorded/valid times.
3. One node per semantic topic, decision, task, claim, or artifact.
4. Typed, directed edges with a plain-language explanation no longer than 320 characters.
5. Confidence class and score, plus evidence references for every inferred connection.

Use `SUPERSEDES` or `CONTRADICTS` to correct prior facts. Never silently overwrite graph
history. Exact external IDs or content hashes may identify the same entity; semantic
similarity must never identify the same entity automatically. Keep similar nodes distinct
unless the user confirms a duplicate through feedback.

## Search and continue

Call `brainhub.search` for hybrid semantic and lexical retrieval. When the user selected
or supplied a node, pass it as the anchor and keep the default two-hop boundary. Do not
silently fall back to a global search. If the bounded search is empty, state that and ask
before broadening scope.

Use `brainhub.get_node` for the selected node, `brainhub.expand` for its neighborhood, and
`brainhub.path` to explain how two results are connected. Present edge explanations,
confidence, and evidence rather than only node labels.

## Feedback

Use `brainhub.feedback` when the user accepts, rejects, corrects, or marks a node duplicate.
Preserve the user's wording in a short review note without copying unrelated conversation
content.

If the MCP server is unavailable, report that the local Brain Hub integration is not
available and suggest the plugin preflight; do not assume the HTTP daemon is the cause.
Do not let memory capture block or fail the user's primary work.
