export const NODE_KINDS = [
  'Workstream',
  'Run',
  'Topic',
  'Task',
  'Decision',
  'Artifact',
  'Claim',
  'Actor',
  'Workspace',
] as const

export type NodeKind = (typeof NODE_KINDS)[number]
export type ConfidenceClass = 'EXTRACTED' | 'INFERRED' | 'AMBIGUOUS'

export interface EvidenceRef {
  id: string
  label: string
  uri?: string
  excerpt?: string
  contentHash?: string
  recordedAt?: string
}

export interface Provenance {
  actor: string
  agent?: string
  extractor?: string
  extractorVersion?: string
  workspace?: string
  runId?: string
}

/** Canonical graph node returned by the Brain Hub REST API. */
export interface BrainNode {
  id: string
  label: string
  kind: NodeKind
  summary: string
  validFrom: string
  validTo?: string | null
  validTimeKnown?: boolean
  recordedAt: string
  confidence: number
  confidenceClass: ConfidenceClass
  provenance: Provenance[]
  evidence: EvidenceRef[]
  tags: string[]
  sensitivity?: 'PUBLIC' | 'INTERNAL' | 'CONFIDENTIAL' | 'RESTRICTED' | 'public' | 'internal' | 'private' | 'secret'
  reviewState?: 'accepted' | 'needs-review' | 'rejected' | 'unreviewed'
  metadata?: Record<string, unknown>
  x?: number
  y?: number
  z?: number
  vx?: number
  vy?: number
  vz?: number
}

/** The renderer may replace endpoint ids with node objects at runtime. */
export interface BrainEdge {
  id: string
  source: string | BrainNode
  target: string | BrainNode
  relation: string
  explanation: string
  validFrom: string
  validTo?: string | null
  validTimeKnown?: boolean
  recordedAt: string
  confidence: number
  confidenceClass: ConfidenceClass
  evidence: EvidenceRef[]
  provenance: Provenance
  reviewState?: 'accepted' | 'needs-review' | 'rejected' | 'unreviewed'
  metadata?: Record<string, unknown>
}

export interface GraphSnapshot {
  nodes: BrainNode[]
  edges: BrainEdge[]
  cursor?: string
  anchorId?: string
  generatedAt?: string
  truncated?: boolean
}

export interface SearchFilters {
  kinds?: NodeKind[]
  confidenceClasses?: ConfidenceClass[]
  agents?: string[]
  tags?: string[]
}

export interface SearchRequest {
  query: string
  anchorId: string
  hops: number
  validAt: string
  limit?: number
  filters?: SearchFilters
}

export interface SearchHit {
  node: BrainNode
  score: number
  reasons: string[]
  distanceFromAnchor: number
}

export interface SearchResponse {
  hits: SearchHit[]
  graph: GraphSnapshot
  query: SearchRequest
  tookMs?: number
}

export interface PathStep {
  from: BrainNode
  edge: BrainEdge
  to: BrainNode
}

export interface PathResponse {
  sourceId: string
  targetId: string
  steps: PathStep[]
  explanation: string
  confidence: number
}

export interface NodeResponse {
  node: BrainNode
  neighborhood?: GraphSnapshot
}

export interface StreamEvent {
  type: string
  projectionVersion?: number
  externalProcess?: boolean
  cursor?: string
}

export type ConnectionMode = 'live' | 'demo' | 'offline'
export type SceneMode = '3d' | '2d' | 'list'

export interface SceneBudget {
  maxNodes: number
  maxEdges: number
}
