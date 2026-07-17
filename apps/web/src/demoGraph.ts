import type { BrainEdge, BrainNode, ConfidenceClass, GraphSnapshot, NodeKind } from './types'

const recordedAt = '2026-07-17T09:40:00.000Z'

function node(
  id: string,
  label: string,
  kind: NodeKind,
  summary: string,
  validFrom: string,
  agent: string,
  tags: string[],
  confidence = 0.94,
  confidenceClass: ConfidenceClass = 'EXTRACTED',
): BrainNode {
  return {
    id,
    label,
    kind,
    summary,
    validFrom,
    recordedAt,
    confidence,
    confidenceClass,
    tags,
    sensitivity: 'private',
    reviewState: confidenceClass === 'AMBIGUOUS' ? 'needs-review' : 'accepted',
    provenance: [{ actor: 'Aryaman', agent, extractor: 'brainhub-demo', extractorVersion: '0.1.0', workspace: 'brain-hub' }],
    evidence: [{
      id: `ev-${id}`,
      label: `${agent} structured run event`,
      uri: `brainhub://demo/${id}`,
      excerpt: summary,
      recordedAt,
    }],
  }
}

function edge(
  id: string,
  source: string,
  target: string,
  relation: string,
  explanation: string,
  validFrom: string,
  confidence = 0.92,
  confidenceClass: ConfidenceClass = 'EXTRACTED',
): BrainEdge {
  return {
    id,
    source,
    target,
    relation,
    explanation,
    validFrom,
    recordedAt,
    confidence,
    confidenceClass,
    reviewState: confidenceClass === 'AMBIGUOUS' ? 'needs-review' : 'accepted',
    evidence: [{ id: `ev-${id}`, label: 'Captured relationship evidence', uri: `brainhub://demo/${id}`, recordedAt }],
    provenance: { actor: 'Aryaman', agent: 'brain-hub', extractor: 'relation-extractor', extractorVersion: '0.1.0' },
  }
}

const nodes: BrainNode[] = [
  node('ws-brain', 'Brain Hub product', 'Workstream', 'Build a local-first, evidence-backed memory graph shared by every AI agent.', '2026-06-12T08:00:00.000Z', 'Codex', ['product', 'knowledge-graph']),
  node('topic-capture', 'Cross-agent capture', 'Topic', 'Non-blocking structured capture from Codex, Claude, Cursor, and Antigravity.', '2026-06-12T09:10:00.000Z', 'Claude', ['agents', 'ingestion']),
  node('topic-graph', 'Temporal knowledge graph', 'Topic', 'A typed directed multigraph with evidence and valid-time on every fact.', '2026-06-13T06:20:00.000Z', 'Codex', ['graph', 'time']),
  node('decision-local', 'Local-first privacy', 'Decision', 'Raw transcripts and artifacts remain local unless the user explicitly opts in.', '2026-06-14T10:00:00.000Z', 'Claude', ['privacy', 'architecture']),
  node('decision-sqlite', 'SQLite event store', 'Decision', 'Use encrypted SQLite WAL as the canonical local event log and projection store.', '2026-06-15T12:00:00.000Z', 'Codex', ['sqlite', 'events']),
  node('decision-networkx', 'NetworkX analysis layer', 'Decision', 'Use NetworkX for bounded server-side traversal and analysis, never browser rendering.', '2026-06-16T07:30:00.000Z', 'Cursor', ['networkx', 'architecture']),
  node('topic-search', 'Semble hybrid search', 'Topic', 'Combine semantic and lexical ranking, then constrain results to an anchored graph neighborhood.', '2026-06-18T06:00:00.000Z', 'Codex', ['search', 'semble']),
  node('claim-anchor', 'Anchored search prevents drift', 'Claim', 'A strict hop boundary keeps follow-up exploration grounded in the selected work context.', '2026-06-19T06:00:00.000Z', 'Claude', ['search', 'trust'], 0.79, 'INFERRED'),
  node('artifact-schema', 'Typed graph schema', 'Artifact', 'Versioned definitions for nine node kinds, core edge relations, provenance, and corrections.', '2026-06-20T11:30:00.000Z', 'Cursor', ['schema', 'json']),
  node('task-adapters', 'Agent adapters', 'Task', 'Ship thin, resilient adapters for four agent environments and one generic SDK.', '2026-06-22T13:00:00.000Z', 'Antigravity', ['adapters', 'sdk']),
  node('run-codex', 'Codex implementation run', 'Run', 'Implemented the daemon, plugin contract, and local projection pipeline.', '2026-07-14T05:30:00.000Z', 'Codex', ['run', 'backend']),
  node('run-claude', 'Claude integration review', 'Run', 'Reviewed hook lifecycle guarantees and secret-redaction boundaries.', '2026-07-14T09:20:00.000Z', 'Claude', ['run', 'security']),
  node('run-cursor', 'Cursor adapter run', 'Run', 'Mapped supported hooks and structured event streams without parsing private databases.', '2026-07-15T08:05:00.000Z', 'Cursor', ['run', 'adapter']),
  node('artifact-plugin', 'Brain Hub Codex plugin', 'Artifact', 'Marketplace-ready MCP and skill bundle for recording, searching, and traversing memory.', '2026-07-16T04:10:00.000Z', 'Codex', ['plugin', 'marketplace']),
  node('artifact-ui', 'Temporal graph console', 'Artifact', 'A WebGL 3D graph with explicit time controls, a 2D fallback, and an accessible list.', '2026-07-17T07:15:00.000Z', 'Codex', ['ui', 'webgl']),
  node('actor-user', 'Aryaman', 'Actor', 'Owner of the private Brain Hub and its captured workstreams.', '2026-06-12T08:00:00.000Z', 'brain-hub', ['owner']),
  node('workspace-local', 'brain-hub workspace', 'Workspace', 'The local source workspace containing core, adapters, plugin, and web console.', '2026-07-14T05:20:00.000Z', 'brain-hub', ['workspace']),
  node('claim-raw', 'Transcript schema is stable', 'Claim', 'Directly parse every agent transcript for complete capture.', '2026-06-21T05:00:00.000Z', 'Antigravity', ['transcript', 'capture'], 0.35, 'AMBIGUOUS'),
  node('decision-hooks', 'Prefer supported hooks', 'Decision', 'Use supported hooks, telemetry, and structured streams; do not depend on private transcript schemas.', '2026-06-23T05:00:00.000Z', 'Codex', ['hooks', 'reliability']),
]

const edges: BrainEdge[] = [
  edge('e1', 'ws-brain', 'topic-capture', 'ABOUT', 'The product workstream includes reliable capture across agent environments.', '2026-06-12T09:10:00.000Z'),
  edge('e2', 'ws-brain', 'topic-graph', 'ABOUT', 'The product represents semantic work as an evidence-backed temporal graph.', '2026-06-13T06:20:00.000Z'),
  edge('e3', 'ws-brain', 'topic-search', 'ABOUT', 'Fast hybrid retrieval is a core product capability.', '2026-06-18T06:00:00.000Z'),
  edge('e4', 'ws-brain', 'decision-local', 'DECIDED_IN', 'The workstream adopted a privacy-preserving local-first boundary.', '2026-06-14T10:00:00.000Z'),
  edge('e5', 'topic-graph', 'decision-sqlite', 'DEPENDS_ON', 'Local temporal projections are built from the SQLite event log.', '2026-06-15T12:00:00.000Z'),
  edge('e6', 'topic-graph', 'decision-networkx', 'DEPENDS_ON', 'Bounded graph queries are evaluated through NetworkX projections.', '2026-06-16T07:30:00.000Z'),
  edge('e7', 'topic-graph', 'artifact-schema', 'PRODUCED', 'The temporal graph design produced a typed, versioned interchange schema.', '2026-06-20T11:30:00.000Z'),
  edge('e8', 'topic-search', 'claim-anchor', 'VERIFIES', 'Anchored retrieval is the mechanism used to constrain semantic results.', '2026-06-19T06:00:00.000Z', 0.78, 'INFERRED'),
  edge('e9', 'task-adapters', 'topic-capture', 'DEPENDS_ON', 'The adapters implement cross-agent capture at supported lifecycle boundaries.', '2026-06-22T13:00:00.000Z'),
  edge('e10', 'run-codex', 'artifact-plugin', 'PRODUCED', 'The Codex implementation run created the marketplace plugin bundle.', '2026-07-16T04:10:00.000Z'),
  edge('e11', 'run-codex', 'artifact-ui', 'PRODUCED', 'The implementation run produced the temporal graph console.', '2026-07-17T07:15:00.000Z'),
  edge('e12', 'run-claude', 'decision-local', 'VERIFIES', 'The integration review confirmed local-only defaults for sensitive content.', '2026-07-14T09:20:00.000Z'),
  edge('e13', 'run-cursor', 'task-adapters', 'MODIFIES', 'The Cursor run refined the supported adapter capture surfaces.', '2026-07-15T08:05:00.000Z'),
  edge('e14', 'actor-user', 'ws-brain', 'PARTICIPATES_IN', 'Aryaman owns and directs the Brain Hub workstream.', '2026-06-12T08:00:00.000Z'),
  edge('e15', 'workspace-local', 'run-codex', 'HAS_RUN', 'The local workspace contains the Codex implementation run.', '2026-07-14T05:30:00.000Z'),
  edge('e16', 'workspace-local', 'run-claude', 'HAS_RUN', 'The local workspace contains the Claude integration review.', '2026-07-14T09:20:00.000Z'),
  edge('e17', 'workspace-local', 'run-cursor', 'HAS_RUN', 'The local workspace contains the Cursor adapter run.', '2026-07-15T08:05:00.000Z'),
  edge('e18', 'artifact-plugin', 'artifact-schema', 'USED', 'The plugin speaks the versioned graph event schema.', '2026-07-16T04:10:00.000Z'),
  edge('e19', 'artifact-ui', 'topic-search', 'USED', 'The console exposes anchored semantic search and neighborhood expansion.', '2026-07-17T07:15:00.000Z'),
  edge('e20', 'artifact-ui', 'topic-graph', 'USED', 'The console renders time-filtered graph projections.', '2026-07-17T07:15:00.000Z'),
  edge('e21', 'claim-raw', 'decision-hooks', 'CONTRADICTS', 'Private transcript formats are not stable enough to form a durable integration contract.', '2026-06-23T05:00:00.000Z', 0.96, 'EXTRACTED'),
  edge('e22', 'decision-hooks', 'claim-raw', 'SUPERSEDES', 'Supported hooks and streams replace the earlier transcript-parsing assumption.', '2026-06-23T05:00:00.000Z'),
  edge('e23', 'decision-hooks', 'task-adapters', 'DECIDED_IN', 'Adapter implementation is constrained to supported agent extension points.', '2026-06-23T05:00:00.000Z'),
  edge('e24', 'decision-local', 'artifact-plugin', 'DEPENDS_ON', 'Plugin capture preserves the product privacy boundary.', '2026-07-16T04:10:00.000Z'),
  edge('e25', 'artifact-schema', 'artifact-ui', 'USED', 'The web client consumes the canonical node, edge, evidence, and time fields.', '2026-07-17T07:15:00.000Z'),
]

export const demoGraph: GraphSnapshot = {
  nodes,
  edges,
  anchorId: 'ws-brain',
  cursor: 'demo:25',
  generatedAt: recordedAt,
}
