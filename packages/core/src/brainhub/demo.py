"""Deterministic demonstration graph shared with the web console's offline fixture."""

from __future__ import annotations

from datetime import datetime

from .models import BrainEvent
from .service import BrainHubService


DEMO_TIME = datetime.fromisoformat("2026-07-17T09:40:00+00:00")


DEMO_NODES = [
    ("ws-brain", "WORKSTREAM", "Brain Hub product", "Build a local-first, evidence-backed memory graph shared by every AI agent."),
    ("topic-capture", "TOPIC", "Cross-agent capture", "Non-blocking structured capture from Codex, Claude, Cursor, and Antigravity."),
    ("topic-graph", "TOPIC", "Temporal knowledge graph", "A typed directed multigraph with evidence and valid-time on every fact."),
    ("decision-local", "DECISION", "Local-first privacy", "Raw transcripts and artifacts remain local unless the user explicitly opts in."),
    ("decision-sqlite", "DECISION", "SQLite event store", "Use encrypted SQLite WAL as the canonical local event log and projection store."),
    ("decision-networkx", "DECISION", "NetworkX analysis layer", "Use NetworkX for bounded server-side traversal and analysis, never browser rendering."),
    ("topic-search", "TOPIC", "Semble hybrid search", "Combine semantic and lexical ranking, then constrain results to an anchored graph neighborhood."),
    ("claim-anchor", "CLAIM", "Anchored search prevents drift", "A strict hop boundary keeps follow-up exploration grounded in the selected work context."),
    ("artifact-schema", "ARTIFACT", "Typed graph schema", "Versioned definitions for nine node kinds, core edge relations, provenance, and corrections."),
    ("task-adapters", "TASK", "Agent adapters", "Ship thin resilient adapters for four agent environments and a generic SDK."),
    ("run-codex", "RUN", "Codex implementation run", "Implemented the daemon, plugin contract, and local projection pipeline."),
    ("run-claude", "RUN", "Claude integration review", "Reviewed hook lifecycle guarantees and secret-redaction boundaries."),
    ("run-cursor", "RUN", "Cursor adapter run", "Mapped supported hooks and structured streams without parsing private databases."),
    ("artifact-plugin", "ARTIFACT", "Brain Hub Codex plugin", "Marketplace-ready MCP and skill bundle for graph memory operations."),
    ("artifact-ui", "ARTIFACT", "Temporal graph console", "A WebGL 3D graph with time controls, 2D fallback, and accessible list."),
    ("actor-user", "ACTOR", "Brain Hub owner", "Owner of the private Brain Hub and captured workstreams."),
    ("workspace-local", "WORKSPACE", "brain-hub workspace", "Local source workspace for the core, adapters, plugin, and web console."),
    ("claim-raw", "CLAIM", "Transcript schema is stable", "Directly parse every agent transcript for complete capture."),
    ("decision-hooks", "DECISION", "Prefer supported hooks", "Use supported hooks, telemetry, and streams rather than private transcript schemas."),
]


DEMO_EDGES = [
    ("demo-e1", "ws-brain", "topic-capture", "ABOUT", "The product workstream includes reliable capture across agent environments."),
    ("demo-e2", "ws-brain", "topic-graph", "ABOUT", "The product represents semantic work as an evidence-backed temporal graph."),
    ("demo-e3", "ws-brain", "topic-search", "ABOUT", "Fast hybrid retrieval is a core product capability."),
    ("demo-e4", "ws-brain", "decision-local", "DECIDED_IN", "The workstream adopted a privacy-preserving local-first boundary."),
    ("demo-e5", "topic-graph", "decision-sqlite", "DEPENDS_ON", "Local projections are built from the SQLite event log."),
    ("demo-e6", "topic-graph", "decision-networkx", "DEPENDS_ON", "Bounded graph queries use NetworkX projections."),
    ("demo-e7", "topic-graph", "artifact-schema", "PRODUCED", "The graph design produced a typed interchange schema."),
    ("demo-e8", "topic-search", "claim-anchor", "VERIFIES", "Anchored retrieval constrains semantic results."),
    ("demo-e9", "task-adapters", "topic-capture", "DEPENDS_ON", "The adapters implement supported cross-agent capture."),
    ("demo-e10", "run-codex", "artifact-plugin", "PRODUCED", "The implementation run created the plugin bundle."),
    ("demo-e11", "run-codex", "artifact-ui", "PRODUCED", "The implementation run produced the temporal graph console."),
    ("demo-e12", "run-claude", "decision-local", "VERIFIES", "The review confirmed local-only defaults for sensitive content."),
    ("demo-e13", "run-cursor", "task-adapters", "MODIFIES", "The Cursor run refined supported capture surfaces."),
    ("demo-e14", "actor-user", "ws-brain", "PARTICIPATES_IN", "The owner directs the Brain Hub workstream."),
    ("demo-e15", "workspace-local", "run-codex", "HAS_RUN", "The workspace contains the Codex implementation run."),
    ("demo-e16", "workspace-local", "run-claude", "HAS_RUN", "The workspace contains the Claude integration review."),
    ("demo-e17", "workspace-local", "run-cursor", "HAS_RUN", "The workspace contains the Cursor adapter run."),
    ("demo-e18", "artifact-plugin", "artifact-schema", "USED", "The plugin speaks the versioned graph event schema."),
    ("demo-e19", "artifact-ui", "topic-search", "USED", "The console exposes anchored hybrid search."),
    ("demo-e20", "artifact-ui", "topic-graph", "USED", "The console renders time-filtered projections."),
    ("demo-e21", "claim-raw", "decision-hooks", "CONTRADICTS", "Private transcript formats are not a durable integration contract."),
    ("demo-e22", "decision-hooks", "claim-raw", "SUPERSEDES", "Supported hooks replace the transcript-parsing assumption."),
    ("demo-e23", "decision-hooks", "task-adapters", "DECIDED_IN", "Adapters are constrained to supported extension points."),
    ("demo-e24", "decision-local", "artifact-plugin", "DEPENDS_ON", "Plugin capture preserves the privacy boundary."),
    ("demo-e25", "artifact-schema", "artifact-ui", "USED", "The web client consumes canonical graph fields."),
]


def demo_event() -> BrainEvent:
    nodes = [
        {
            "id": node_id,
            "type": node_type,
            "title": title,
            "summary": summary,
            "review_state": "ACCEPTED" if node_id != "claim-raw" else "NEEDS_REVIEW",
            "extractor": "brainhub-demo",
            "extractor_version": "0.1.0",
        }
        for node_id, node_type, title, summary in DEMO_NODES
    ]
    edges = [
        {
            "id": edge_id,
            "source_id": source,
            "target_id": target,
            "relation": relation,
            "explanation": explanation,
            "confidence_class": (
                "AMBIGUOUS"
                if edge_id == "demo-e21"
                else "INFERRED" if edge_id == "demo-e8" else "EXTRACTED"
            ),
            "confidence_score": (
                0.35 if edge_id == "demo-e21" else 0.82 if edge_id == "demo-e8" else 0.95
            ),
            "review_state": "NEEDS_REVIEW" if edge_id == "demo-e21" else "ACCEPTED",
        }
        for edge_id, source, target, relation, explanation in DEMO_EDGES
    ]
    return BrainEvent.create(
        source="urn:brainhub:demo:0.1.0",
        type="com.brainhub.graph.imported.v1",
        subject="demo/brain-hub",
        time=DEMO_TIME,
        data={
            "agent": {"product": "brainhub", "surface": "demo", "version": "0.1.0"},
            "workspace_id": "demo-workspace",
            "session_id": "demo-session",
            "status": "completed",
            "capture": {"mode": "import", "content_level": "summary"},
            "nodes": nodes,
            "edges": edges,
        },
    )


def seed_demo(service: BrainHubService):
    return service.record(demo_event())
