# Brain Hub Command Center Roadmap

## Outcome

Make Brain Hub a local-first command center for personal AI work: a user can
see what is active, what needs attention, what happened, which tools were
used, and whether the result was verified. The default experience is a
workstream timeline, not a graph. The existing graph remains an optional
relationship-exploration view.

## Guardrails

- Keep UUIDs as internal identifiers; display explicit titles or neutral,
  metadata-only fallback labels.
- Store and export only allowlisted telemetry. Never collect prompts,
  responses, commands, paths, tool arguments/results, headers, raw errors, or
  secrets for this command-center feature.
- Use documented client telemetry and controls only. Do not scrape client
  databases, automate their UI, or proxy third-party MCP traffic.
- Treat token savings as an experiment result, not an assumption. Until a
  paired benchmark exists, present usage and association only.
- Tools and MCP changes are recommendations requiring user approval, never
  automatic install, removal, or permission expansion.

## Product Shape

The default Command Center has:

1. workstream lanes: Active, Waiting, Needs attention, Done;
2. a readable timeline of sessions, tool activity, handoffs, and verification;
3. one clear next action and progress summary;
4. a quiet usage strip for tokens, cache reuse, tools, and connected MCP
   servers;
5. compact recent-run and Tool & MCP Health cards; and
6. an optional Relationships view for the existing graph.

Advanced telemetry and raw operational detail stay behind progressive
disclosure so a new user can operate the product without learning internal
agent terminology.

## API Surface

| Group | Initial endpoints | Purpose |
| --- | --- | --- |
| Work | `GET/POST /workstreams`, `GET/PATCH /workstreams/{id}` | Title, state, next action, and privacy class. |
| Runs | `GET /workstreams/{id}/runs`, `GET /runs/{id}` | Client runs and readable timeline projection. |
| Evidence | `GET/POST /verifications` | Acceptance checks and evidence references. |
| Handoffs | `POST /handoffs`, `POST /handoffs/{id}/acknowledge` | Minimal, content-safe cross-client handoff bundles. |
| Capabilities | `GET /capabilities`, `GET /capabilities/health` | MCP/tool/skill state, permissions, reliability, and latency. |
| Telemetry | `GET /telemetry/today`, `GET /telemetry/coverage` | Tokens, cache, latency, outcomes, source grade, and coverage. |
| Recommendations | `GET /recommendations` | User-reviewable keep/disable/remove/add candidates. |

All responses include source provenance and a grade: authoritative, observed,
derived, or unavailable. Unknown data is never converted to zero.

## Milestones

### M0 — Integrity and privacy contract

- Repair collision-free hook event identity and separate occurred/observed
  timestamps.
- Maintain loopback-only allowlist redaction with canary-secret tests.
- Inventory MCP transport, owner, permission/data scope, and last review.

Exit: retries are idempotent, repeated tool calls stay distinct, and sensitive
canaries never reach the projection or telemetry stores.

### M1 — Command Center foundation

- Add workstream, run, operation, capability-use, and verification projections.
- Add readable display titles with explicit-title and neutral fallback rules.
- Ship the light Command Center layout and optional Relationships tab.

Exit: a user can find active work, understand what happened, and identify the
next action without viewing a UUID or graph.

### M2 — Tools and handoffs

- Add capability health, permissions, and last-use projection.
- Add content-safe handoff bundle creation, acknowledgement, and evidence.
- Show run-to-tool-to-verification links in the timeline.

Exit: a handoff can be created and accepted with traceable evidence, and tool
health is visible without tool payload retention.

### M3 — Telemetry adapters

- Normalize supported Codex and Claude telemetry through the local collector.
- Publish tokens, cache reuse, latency, failures, and coverage in `/today`.
- Add only supported Cursor/Cowork adapters when the relevant entitlement and
  privacy canary checks are available.

Exit: every total displays source and coverage; a telemetry outage does not
block normal work or bypass redaction.

### M4 — Outcome and recommendations

- Add verified completion association for skills, tools, and MCP servers.
- Publish observational efficiency views with sample sizes and confidence.
- Add user-approved MCP scorecards and paired experiments for causal savings.

Exit: every recommendation has an evidence window, risk, confidence, and
explicit user disposition. Every savings claim links to a valid experiment.

### M5 — Hardening

- Add schema-drift and collector lag/drop alerts, backup/restore drills,
  retention/deletion controls, and redaction regression tests.

Exit: restore, redaction, and adapter contract drills pass on a scheduled
cadence.

## End-to-End Test Plan

For each milestone, run this local scenario before release:

1. Start a clean Brain Hub runtime plus collector/dashboard stack.
2. Create a workstream with an explicit safe title and record a session.
3. Emit three same-turn tool calls, one replay, and one failed call.
4. Confirm three operations remain distinct, the replay is idempotent, and the
   timeline is chronologically correct.
5. Verify prompts, commands, paths, headers, tool payloads, and a canary
   secret are absent from the event store, Loki, and metrics labels.
6. Create a handoff, acknowledge it from a second run, and attach verification
   evidence.
7. Compare the API projections with the Command Center and ensure unsupported
   fields render as unavailable rather than zero.
8. Stop telemetry delivery and confirm work recording remains available while
   exports fail closed.

## Delivery Model

Each milestone is a separate workstream with an investigator pass for existing
boundaries, small focused implementation tasks, and an independent diff review.
Only one milestone is deployed at a time. Merge, reinstall the managed runtime,
restart the local service, and run the end-to-end scenario before starting the
next milestone.
