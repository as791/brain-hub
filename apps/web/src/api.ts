import type {
  BrainEdge,
  BrainNode,
  GraphSnapshot,
  NodeResponse,
  PathResponse,
  SearchRequest,
  SearchResponse,
  StreamEvent,
} from './types'

const trimSlash = (value: string) => value.replace(/\/$/, '')

export const API_BASE_URL = trimSlash(import.meta.env.VITE_BRAINHUB_API_URL ?? 'http://127.0.0.1:8420')
export const WS_BASE_URL = trimSlash(
  import.meta.env.VITE_BRAINHUB_WS_URL ?? API_BASE_URL.replace(/^http/, 'ws'),
)
export const TOKEN_SESSION_KEY = 'brainhub.apiToken'

export function getApiToken(): string | undefined {
  try {
    const runtimeToken = typeof window !== 'undefined' ? window.sessionStorage.getItem(TOKEN_SESSION_KEY)?.trim() : undefined
    if (runtimeToken) return runtimeToken
  } catch {
    // Sandboxed browsers may deny storage access; unauthenticated local mode still works.
  }
  const developmentToken = import.meta.env.DEV ? import.meta.env.VITE_BRAINHUB_API_TOKEN?.trim() : undefined
  return developmentToken || undefined
}

export function setRuntimeApiToken(token?: string): void {
  if (typeof window === 'undefined') return
  if (token?.trim()) window.sessionStorage.setItem(TOKEN_SESSION_KEY, token.trim())
  else window.sessionStorage.removeItem(TOKEN_SESSION_KEY)
}

/**
 * The single compatibility seam for daemon route changes. The client assumes JSON
 * uses the camelCase shapes in types.ts; normalizeSnapshot also accepts `links`.
 */
export const API_PATHS = {
  health: '/healthz',
  graph: '/v1/graph',
  search: '/v1/search',
  node: (nodeId: string) => `/v1/nodes/${encodeURIComponent(nodeId)}`,
  expand: (nodeId: string) => `/v1/nodes/${encodeURIComponent(nodeId)}/expand`,
  path: '/v1/path',
  events: '/ws',
} as const

export class BrainHubApiError extends Error {
  constructor(
    message: string,
    readonly status?: number,
    readonly body?: unknown,
  ) {
    super(message)
    this.name = 'BrainHubApiError'
  }
}

type JsonObject = Record<string, unknown>
const UNKNOWN_EVENT_TIME = '1970-01-01T00:00:00Z'

const NODE_KIND_BY_WIRE: Record<string, BrainNode['kind']> = {
  WORKSTREAM: 'Workstream',
  RUN: 'Run',
  TOPIC: 'Topic',
  TASK: 'Task',
  DECISION: 'Decision',
  ARTIFACT: 'Artifact',
  CLAIM: 'Claim',
  ACTOR: 'Actor',
  WORKSPACE: 'Workspace',
}

function camelKey(value: string): string {
  return value.replace(/_([a-z])/g, (_, letter: string) => letter.toUpperCase())
}

/** Accept canonical Python snake_case plus camelCase aliases at every depth. */
export function camelize(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(camelize)
  if (!value || typeof value !== 'object') return value
  return Object.fromEntries(
    Object.entries(value as JsonObject).map(([key, child]) => [camelKey(key), camelize(child)]),
  )
}

function objectValue(value: unknown): JsonObject {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as JsonObject : {}
}

function normalizeKind(value: unknown): BrainNode['kind'] {
  const raw = String(value ?? 'TOPIC')
  return NODE_KIND_BY_WIRE[raw.toUpperCase()] ?? NODE_KIND_BY_WIRE[raw.replaceAll('-', '_').toUpperCase()] ?? 'Topic'
}

function normalizeReviewState(value: unknown): BrainNode['reviewState'] {
  const normalized = String(value ?? 'UNREVIEWED').toLowerCase().replaceAll('_', '-')
  if (normalized === 'accepted' || normalized === 'rejected' || normalized === 'needs-review') return normalized
  return 'unreviewed'
}

function normalizeEvidence(value: unknown, index: number): BrainNode['evidence'][number] {
  const data = objectValue(camelize(value))
  const sourceEventId = String(data.sourceEventId ?? data.eventId ?? '')
  const uri = data.uri ?? data.locator ?? data.opaqueUri
  const anchor = data.anchor ? String(data.anchor) : undefined
  return {
    id: String(data.id ?? data.evidenceId ?? `${sourceEventId || 'evidence'}:${index}`),
    label: String(data.label ?? anchor ?? (sourceEventId ? `Source event ${sourceEventId}` : `Evidence ${index + 1}`)),
    uri: uri ? String(uri) : undefined,
    excerpt: data.excerpt ? String(data.excerpt) : undefined,
    contentHash: data.contentHash ? String(data.contentHash) : undefined,
    recordedAt: data.recordedAt ? String(data.recordedAt) : undefined,
  }
}

function normalizeProvenance(value: unknown): BrainNode['provenance'][number] {
  const data = objectValue(camelize(value))
  return {
    actor: String(data.actor ?? data.actorId ?? 'unknown'),
    agent: data.agent ? String(data.agent) : undefined,
    extractor: data.extractor ? String(data.extractor) : undefined,
    extractorVersion: data.extractorVersion ? String(data.extractorVersion) : undefined,
    workspace: data.workspace ? String(data.workspace) : undefined,
    runId: data.runId ? String(data.runId) : undefined,
  }
}

function normalizeNode(value: unknown): BrainNode {
  const data = (camelize(value) ?? {}) as JsonObject
  const validTime = objectValue(data.validTime)
  const recordedTime = objectValue(data.recordedTime)
  const rawProvenance = data.provenance ?? []
  const provenanceValues = Array.isArray(rawProvenance) ? rawProvenance : [rawProvenance]
  const canonicalProvenance = objectValue(rawProvenance)
  const rawEvidence = data.evidence ?? data.evidenceRefs ?? canonicalProvenance.evidence ?? []
  const properties = objectValue(data.properties)
  const validFrom = String(data.validFrom ?? validTime.start ?? data.recordedAt ?? recordedTime.start ?? new Date(0).toISOString())
  const recordedAt = String(data.recordedAt ?? recordedTime.start ?? data.validFrom ?? validTime.start ?? new Date(0).toISOString())
  return {
    ...(data as unknown as BrainNode),
    id: String(data.id ?? data.nodeId ?? ''),
    label: String(data.label ?? data.title ?? data.name ?? data.id ?? 'Untitled node'),
    kind: normalizeKind(data.kind ?? data.nodeType ?? data.type),
    summary: String(data.summary ?? data.description ?? ''),
    validFrom,
    validTo: data.validTo || validTime.end ? String(data.validTo ?? validTime.end) : null,
    validTimeKnown: data.validTimeKnown === false ? false : validFrom !== UNKNOWN_EVENT_TIME,
    recordedAt,
    confidence: Number(data.confidence ?? data.confidenceScore ?? 1),
    confidenceClass: (data.confidenceClass ?? 'EXTRACTED') as BrainNode['confidenceClass'],
    provenance: provenanceValues.filter((entry) => entry && typeof entry === 'object').map(normalizeProvenance),
    evidence: (Array.isArray(rawEvidence) ? rawEvidence : []).map(normalizeEvidence),
    tags: (Array.isArray(data.tags) ? data.tags : Array.isArray(properties.tags) ? properties.tags : []).map(String),
    sensitivity: data.sensitivity as BrainNode['sensitivity'],
    reviewState: normalizeReviewState(data.reviewState),
    metadata: properties,
  }
}

function normalizeEdge(value: unknown): BrainEdge {
  const data = (camelize(value) ?? {}) as JsonObject
  const validTime = objectValue(data.validTime)
  const recordedTime = objectValue(data.recordedTime)
  const validFrom = String(data.validFrom ?? validTime.start ?? data.recordedAt ?? recordedTime.start ?? new Date(0).toISOString())
  const recordedAt = String(data.recordedAt ?? recordedTime.start ?? data.validFrom ?? validTime.start ?? new Date(0).toISOString())
  const rawProvenance = data.provenance ?? {
    actor: data.actor ?? data.actorId ?? 'unknown',
    extractor: data.extractor,
    extractorVersion: data.extractorVersion,
  }
  const provenance = normalizeProvenance(Array.isArray(rawProvenance) ? rawProvenance[0] : rawProvenance)
  const rawEvidence = data.evidence ?? data.evidenceRefs ?? []
  return {
    ...(data as unknown as BrainEdge),
    id: String(data.id ?? data.edgeId ?? ''),
    source: String(data.source ?? data.sourceId ?? ''),
    target: String(data.target ?? data.targetId ?? ''),
    relation: String(data.relation ?? data.kind ?? data.edgeType ?? 'REFERENCES'),
    explanation: String(data.explanation ?? data.description ?? ''),
    validFrom,
    validTo: data.validTo || validTime.end ? String(data.validTo ?? validTime.end) : null,
    validTimeKnown: data.validTimeKnown === false ? false : validFrom !== UNKNOWN_EVENT_TIME,
    recordedAt,
    confidence: Number(data.confidence ?? data.confidenceScore ?? 1),
    confidenceClass: (data.confidenceClass ?? 'EXTRACTED') as BrainEdge['confidenceClass'],
    evidence: (Array.isArray(rawEvidence) ? rawEvidence : []).map(normalizeEvidence),
    provenance,
    reviewState: normalizeReviewState(data.reviewState),
    metadata: objectValue(data.properties),
  }
}

export function normalizeSnapshot(value: unknown): GraphSnapshot {
  if (!value || typeof value !== 'object') throw new BrainHubApiError('The daemon returned an invalid graph.')
  const data = camelize(value) as JsonObject
  const graph = (data.graph && typeof data.graph === 'object' ? data.graph : data) as Record<string, unknown>
  const nodes = graph.nodes
  const edges = graph.edges ?? graph.links
  if (!Array.isArray(nodes) || !Array.isArray(edges)) {
    throw new BrainHubApiError('The graph response must include nodes and edges arrays.')
  }
  return {
    ...(graph as unknown as GraphSnapshot),
    nodes: nodes.map(normalizeNode),
    edges: edges.map(normalizeEdge),
    cursor: graph.cursor ? String(graph.cursor) : graph.projectionVersion !== undefined ? `projection:${String(graph.projectionVersion)}` : undefined,
    anchorId: graph.anchorId ? String(graph.anchorId) : undefined,
    generatedAt: graph.generatedAt ? String(graph.generatedAt) : undefined,
  }
}

export function searchRequestToWire(input: SearchRequest): JsonObject {
  const kinds = input.filters?.kinds
  return {
    query: input.query,
    anchor_id: input.anchorId,
    hops: input.hops,
    valid_at: input.validAt,
    limit: input.limit,
    scope: 'anchored',
    filters: kinds?.length ? { kinds } : undefined,
  }
}

function normalizeHit(value: unknown, graph: GraphSnapshot): SearchResponse['hits'][number] {
  const data = camelize(value) as JsonObject
  const nodeValue = data.node ?? data.item ?? graph.nodes.find((node) => node.id === String(data.nodeId ?? data.id))
  const node = nodeValue ? normalizeNode(nodeValue) : normalizeNode(data)
  const graphScore = Number(data.graphScore ?? 0)
  const inferredDistance = graphScore > 0 ? Math.max(0, Math.round((1 / graphScore) - 1)) : 0
  const reasons = []
  if (Number(data.lexicalScore ?? 0) > 0) reasons.push('lexical match')
  if (Number(data.semanticScore ?? 0) > 0) reasons.push('semantic match')
  if (graphScore > 0) reasons.push(`${inferredDistance} hop${inferredDistance === 1 ? '' : 's'} from anchor`)
  return {
    node,
    score: Number(data.score ?? data.similarity ?? 0),
    reasons: Array.isArray(data.reasons) ? data.reasons.map(String) : reasons,
    distanceFromAnchor: Number(data.distanceFromAnchor ?? data.distance ?? inferredDistance),
  }
}

function normalizePath(value: unknown, sourceId: string, targetId: string): PathResponse {
  const data = camelize(value) as JsonObject
  let steps = Array.isArray(data.steps) ? data.steps.map((rawStep) => {
    const step = camelize(rawStep) as JsonObject
    return {
      from: normalizeNode(step.from ?? step.source),
      edge: normalizeEdge(step.edge ?? step.relation),
      to: normalizeNode(step.to ?? step.target),
    }
  }) : []
  if (!steps.length && Array.isArray(data.nodes) && Array.isArray(data.edges)) {
    const nodes = data.nodes.map(normalizeNode)
    const nodeById = new Map(nodes.map((node) => [node.id, node]))
    const edges = data.edges.map(normalizeEdge)
    steps = edges.slice(0, Math.max(0, nodes.length - 1)).map((edge, index) => ({
      // The server may traverse an undirected path, but every assertion remains
      // directed. Display its stored source/target rather than reversing meaning
      // to match traversal order.
      from: nodeById.get(String(edge.source)) ?? nodes[index],
      edge,
      to: nodeById.get(String(edge.target)) ?? nodes[index + 1],
    }))
  }
  const explanation = steps
    .map((step) => `${step.from.label} ${step.edge.relation.replaceAll('_', ' ')} ${step.to.label}`)
    .join(' → ')
  return {
    sourceId: String(data.sourceId ?? sourceId),
    targetId: String(data.targetId ?? targetId),
    steps,
    explanation: String(data.explanation ?? explanation),
    confidence: Number(data.confidence ?? steps.reduce((floor, step) => Math.min(floor, step.edge.confidence), 1)),
  }
}

async function request<T>(path: string, init: RequestInit = {}, signal?: AbortSignal): Promise<T> {
  const token = getApiToken()
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    signal,
    headers: {
      Accept: 'application/json',
      ...(init.body ? { 'Content-Type': 'application/json' } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...init.headers,
    },
  })
  const contentType = response.headers.get('content-type') ?? ''
  const body: unknown = contentType.includes('json') ? await response.json() : await response.text()
  if (!response.ok) {
    const detail = typeof body === 'object' && body && 'detail' in body ? String((body as { detail: unknown }).detail) : response.statusText
    throw new BrainHubApiError(detail || `Brain Hub request failed (${response.status}).`, response.status, body)
  }
  return body as T
}

export const brainHubApi = {
  async health(signal?: AbortSignal): Promise<{ status: string; version?: string }> {
    return request(API_PATHS.health, {}, signal)
  },

  async graph(validAt?: string, signal?: AbortSignal): Promise<GraphSnapshot> {
    const query = validAt ? `?valid_at=${encodeURIComponent(validAt)}` : ''
    return normalizeSnapshot(await request(`${API_PATHS.graph}${query}`, {}, signal))
  },

  async search(input: SearchRequest, signal?: AbortSignal): Promise<SearchResponse> {
    const rawResponse = await request<unknown>(
      API_PATHS.search,
      { method: 'POST', body: JSON.stringify(searchRequestToWire(input)) },
      signal,
    )
    const response = camelize(rawResponse) as JsonObject
    let graph: GraphSnapshot
    try {
      graph = normalizeSnapshot(response)
    } catch {
      const rawHits = Array.isArray(response.hits) ? response.hits : Array.isArray(response.results) ? response.results : []
      const nodes = rawHits.map((hit) => normalizeHit(hit, EMPTY_SNAPSHOT).node)
      graph = { nodes, edges: [] }
    }
    const rawHits = Array.isArray(response.hits) ? response.hits : Array.isArray(response.results) ? response.results : []
    return {
      ...(response as unknown as SearchResponse),
      graph,
      hits: rawHits.length
        ? rawHits.map((hit) => normalizeHit(hit, graph))
        : graph.nodes.map((node) => ({ node, score: 1, reasons: [], distanceFromAnchor: 0 })),
      query: input,
    }
  },

  async node(nodeId: string, signal?: AbortSignal): Promise<NodeResponse> {
    const response = camelize(await request<unknown>(API_PATHS.node(nodeId), {}, signal)) as JsonObject
    const rawNode = response.node ?? response
    return {
      node: normalizeNode(rawNode),
      neighborhood: response.neighborhood ? normalizeSnapshot(response.neighborhood) : undefined,
    }
  },

  async expand(nodeId: string, hops: number, validAt: string, signal?: AbortSignal): Promise<GraphSnapshot> {
    const query = `?hops=${hops}&valid_at=${encodeURIComponent(validAt)}`
    return normalizeSnapshot(await request(`${API_PATHS.expand(nodeId)}${query}`, {}, signal))
  },

  async path(sourceId: string, targetId: string, validAt: string, maxLength = 2, signal?: AbortSignal): Promise<PathResponse> {
    const response = await request<unknown>(
      API_PATHS.path,
      { method: 'POST', body: JSON.stringify({ source_id: sourceId, target_id: targetId, valid_at: validAt, max_length: maxLength }) },
      signal,
    )
    return normalizePath(response, sourceId, targetId)
  },
}

const EMPTY_SNAPSHOT: GraphSnapshot = { nodes: [], edges: [] }

export interface EventSubscription {
  close: () => void
}

export function subscribeToEvents(
  onEvent: (event: StreamEvent) => void,
  onStatus: (status: 'open' | 'closed' | 'error') => void,
): EventSubscription {
  let socket: WebSocket | undefined
  let stopped = false
  let reconnectTimer: number | undefined
  let attempts = 0

  const connect = () => {
    if (stopped) return
    socket = new WebSocket(`${WS_BASE_URL}${API_PATHS.events}`)
    socket.addEventListener('open', () => {
      attempts = 0
      const token = getApiToken()
      if (token) {
        // Browsers cannot set an Authorization header on WebSocket handshakes. The
        // daemon may authenticate this first frame; hosted deployments should prefer
        // a same-origin HttpOnly cookie. Local loopback can be explicitly exempted.
        socket?.send(JSON.stringify({ type: 'brainhub.auth', token }))
      }
      onStatus('open')
    })
    socket.addEventListener('message', (message) => {
      try {
        onEvent(camelize(JSON.parse(String(message.data))) as StreamEvent)
      } catch {
        onStatus('error')
      }
    })
    socket.addEventListener('error', () => onStatus('error'))
    socket.addEventListener('close', () => {
      onStatus('closed')
      if (!stopped) {
        const delay = Math.min(30_000, 1_000 * 2 ** Math.min(attempts, 5))
        attempts += 1
        reconnectTimer = window.setTimeout(connect, delay)
      }
    })
  }
  connect()

  return {
    close: () => {
      stopped = true
      if (reconnectTimer) window.clearTimeout(reconnectTimer)
      socket?.close()
    },
  }
}
