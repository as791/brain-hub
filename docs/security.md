# Security and privacy threat model

Brain Hub observes development work and therefore has surveillance-grade risk. The safe default is local, visible, metadata/summary-only capture.

## Protected assets

- prompts, assistant messages, source code, tool inputs/results, and artifacts;
- repository identity, absolute paths, branches, commits, and work patterns;
- graph inferences, decisions, claims, and relationships;
- master keys, API and connector tokens, cloud sync credentials;
- user consent, retention, correction, deletion, and audit history.

## Trust boundaries

Agent hosts and adapter processes are untrusted producers. Graph text is untrusted content, not an instruction to an LLM. The local daemon is the policy and encryption boundary. The browser receives only authorized projections. Cloud sync is a distinct tenant boundary and has no access to the local master key.

## Required controls

### Consent and minimization

- Capture is enabled per user and workspace with a visible host indicator.
- Metadata and semantic summaries are the default. Prompt text, assistant text, code, transcript content, and tool payloads require separate explicit opt-ins.
- Hidden chain-of-thought is never requested, extracted, or stored.
- A denylist removes secrets, `.env` values, private keys, bearer tokens, connector material, and absolute paths before indexing or sync.
- Capture level and redaction decisions are attached to every event for auditability.

### Local data protection

- SQLite uses WAL and restrictive file permissions.
- Content fields and blobs use XChaCha20-Poly1305 with a fresh nonce and authenticated record context.
- The pre-daemon adapter spool contains only redacted metadata and explicitly supplied summaries, is bounded, and uses owner-only permissions. It is not yet application-encrypted, so daily use requires an encrypted home volume; spool encryption is a public-release hardening gate.
- The master key comes from the OS keychain where available. Environment keys are an explicit deployment override; plaintext key files are not a supported production mode.
- Semble indexes only a redacted temporary projection and must not persist a second content copy.
- Backups are encrypted and exports clearly separate metadata from content.

### Access control

- Loopback is the default bind address; non-loopback startup requires explicit configuration.
- API tokens are compared in constant time and are never accepted in query strings.
- Browser WebSocket origins use the same explicit allowlist as CORS, and authenticated
  sockets require either an authorization header or a bounded first-frame token exchange.
- Local HTTP uses one bearer token and local stdio MCP relies on the operating-system user boundary. A hosted service must enforce separate `brain.read`, `brain.write.events`, `brain.write.relationships`, and `brain.admin` scopes before publication.
- Destructive deletion is absent from v0.1 and from the public MCP tool set. A future retention workflow must require authenticated local administration and verify removal from derived indexes, spools, backups, and replicas.
- Future multi-tenant cloud data uses tenant-scoped keys, row-level security, token audience validation, and derived permission inheritance.
- Upstream connector tokens are never passed through Brain Hub.

### Availability and integrity

- Hooks write to a bounded spool with short timeouts and fail open for the host agent.
- Event IDs are deterministic. Reuse with different content is rejected; durable conflict audit and metrics remain a hosted-release gate.
- Payload, batch, traversal depth, query time, and scene sizes are bounded.
- Extractor versions and source hashes make projection replay deterministic and reviewable.
- Corrections append `SUPERSEDES` or `CONTRADICTS`; they never erase the prior assertion.

### Prompt and graph injection

Titles, summaries, evidence, Graphify imports, and search results can contain hostile instructions. They are rendered as quoted data, escaped in the UI, excluded from privileged system prompts, and never allowed to select tools or change authorization. Any future LLM extractor must use structured output validation and a least-privilege, no-tools execution context.

## Abuse cases to test

1. A hook contains a token, private key, `.env` content, or traversal path.
2. An event reuses an ID with altered canonical JSON.
3. A graph import contains HTML/script, prompt injection, oversized fields, or dangling nodes.
4. An unauthenticated or cross-origin caller attempts an event write or WebSocket connection.
5. A caller requests a traversal exceeding depth or result budgets.
6. A local content deletion leaves text in search, spool, backup, or cloud queues.
7. A tenant or workspace boundary is crossed through an edge or evidence citation.

Public deployment is blocked until these cases are automated and an independent review verifies key handling, auth, redaction, and deletion.
