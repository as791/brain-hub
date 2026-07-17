# Data contract

## Event envelope

All adapters emit a CloudEvents 1.0 JSON object. The required semantic fields are agent identity, workspace/session identity, event type, and status; content-bearing fields are optional.

```json
{
  "specversion": "1.0",
  "id": "sha256:deterministic-canonical-input",
  "source": "urn:brainhub-adapter:codex:cli:installation-id",
  "type": "com.brainhub.workstream.turn.completed.v1",
  "subject": "workspace/opaque-id/session/native-id",
  "time": "2026-07-17T10:00:00Z",
  "datacontenttype": "application/json",
  "data": {
    "agent": {"product": "codex", "surface": "cli", "version": "1.0.0"},
    "workspace_id": "opaque-id",
    "session_id": "native-id",
    "status": "completed",
    "summary": "Implemented an idempotent event projection.",
    "artifacts": [],
    "capture": {"mode": "hook", "content_level": "summary"}
  }
}
```

Event vocabulary starts with:

- `com.brainhub.workstream.started.v1`
- `com.brainhub.workstream.turn.completed.v1`
- `com.brainhub.artifact.produced.v1`
- `com.brainhub.workstream.completed.v1`
- `com.brainhub.workstream.failed.v1`
- `com.brainhub.relationship.asserted.v1`
- `com.brainhub.feedback.recorded.v1`

Unknown event types and unknown payload fields are retained but cannot mutate the canonical projection until a projector explicitly supports them.

## Node contract

Canonical node types are `WORKSTREAM`, `RUN`, `TOPIC`, `TASK`, `DECISION`, `ARTIFACT`, `CLAIM`, `ACTOR`, and `WORKSPACE`.

Every node includes:

- stable ID, type, title, and short summary;
- encrypted content payload when content is allowed;
- sensitivity and review state;
- valid and recorded time intervals;
- actor, extractor name/version, and evidence references;
- exact external identifiers and content hashes when available;
- creation event and latest revision IDs.

## Edge contract

Core edge types are `HAS_RUN`, `ABOUT`, `PRODUCED`, `USED`, `MODIFIES`, `DEPENDS_ON`, `BLOCKS`, `DECIDED_IN`, `DERIVED_FROM`, `REFERENCES`, `VERIFIES`, `CONTRADICTS`, `SUPERSEDES`, `CONTINUES`, `ASSERTED_BY`, and `PARTICIPATES_IN`.

An edge is a directed assertion and requires:

- source and target node IDs;
- relation type and a 1–2 sentence explanation of at most 320 characters;
- confidence class (`EXTRACTED`, `INFERRED`, or `AMBIGUOUS`) and score in `[0, 1]`;
- at least one evidence reference;
- valid and recorded time;
- actor/extractor/version, sensitivity, and review state.

Because two nodes can have multiple meaningful relations at different times, the canonical graph is a directed multigraph.

## Evidence references

Evidence is a citation, not necessarily copied content. A reference contains a source event ID, opaque artifact or URI identifier, optional line/byte/time anchor, content hash, and visibility. Absolute local paths are converted to installation-scoped opaque IDs before graph sync.

Deletion of a source marks dependent assertions unavailable and queues deterministic re-projection. It does not leave orphaned searchable text.

## Query semantics

`search` returns score components, search mode, scope, and a projection version. `expand` and `path` return only evidence-visible assertions. A result derived from an inferred edge is labeled; the API never phrases it as an extracted fact.

The formal JSON Schemas in `schemas/` are transport guards. Backend Pydantic models remain the executable reference and schema changes require compatibility tests.
