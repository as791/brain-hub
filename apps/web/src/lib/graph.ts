import type {
  BrainEdge,
  BrainNode,
  ConfidenceClass,
  GraphSnapshot,
  NodeKind,
  PathResponse,
  SearchHit,
} from '../types'

export const MAX_GRAPH_HOPS = 20

export const KIND_COLORS: Record<NodeKind, string> = {
  Workstream: '#56e4ff',
  Run: '#58a6ff',
  Topic: '#b88cff',
  Task: '#46e6a1',
  Decision: '#ffc857',
  Artifact: '#ff8bd7',
  Claim: '#f9846f',
  Actor: '#8ef0d0',
  Workspace: '#a9b7c7',
}

export const CONFIDENCE_COLORS: Record<ConfidenceClass, string> = {
  EXTRACTED: '#68ead0',
  INFERRED: '#f6c767',
  AMBIGUOUS: '#ff7b91',
}

export function endpointId(endpoint: string | BrainNode): string {
  return typeof endpoint === 'string' ? endpoint : endpoint.id
}

function isValidAt(value: { validFrom: string; validTo?: string | null; validTimeKnown?: boolean }, epoch: number): boolean {
  if (value.validTimeKnown === false) return true
  const startsAt = Date.parse(value.validFrom)
  const endsAt = value.validTo ? Date.parse(value.validTo) : Number.POSITIVE_INFINITY
  return startsAt <= epoch && epoch <= endsAt
}

export function filterAtTime(snapshot: GraphSnapshot, validAt: string): GraphSnapshot {
  const epoch = Date.parse(validAt)
  const nodes = snapshot.nodes.filter((node) => isValidAt(node, epoch))
  const ids = new Set(nodes.map((node) => node.id))
  const edges = snapshot.edges.filter(
    (edge) =>
      isValidAt(edge, epoch) &&
      ids.has(endpointId(edge.source)) &&
      ids.has(endpointId(edge.target)),
  )
  return { ...snapshot, nodes, edges }
}

export function boundedSubgraph(
  snapshot: GraphSnapshot,
  anchorId: string,
  hops: number,
): GraphSnapshot {
  if (!snapshot.nodes.some((node) => node.id === anchorId)) return { ...snapshot, nodes: [], edges: [] }

  const distance = new Map<string, number>([[anchorId, 0]])
  let frontier = new Set([anchorId])
  for (let depth = 1; depth <= hops && frontier.size > 0; depth += 1) {
    const next = new Set<string>()
    for (const edge of snapshot.edges) {
      const source = endpointId(edge.source)
      const target = endpointId(edge.target)
      if (frontier.has(source) && !distance.has(target)) {
        distance.set(target, depth)
        next.add(target)
      }
      if (frontier.has(target) && !distance.has(source)) {
        distance.set(source, depth)
        next.add(source)
      }
    }
    frontier = next
  }
  const ids = new Set(distance.keys())
  return {
    ...snapshot,
    anchorId,
    nodes: snapshot.nodes.filter((node) => ids.has(node.id)),
    edges: snapshot.edges.filter(
      (edge) => ids.has(endpointId(edge.source)) && ids.has(endpointId(edge.target)),
    ),
  }
}

export function topDownSubgraph(
  snapshot: GraphSnapshot,
  rootId: string,
  depthLimit: number,
): GraphSnapshot {
  const nodeById = new Map(snapshot.nodes.map((node) => [node.id, node]))
  if (!nodeById.has(rootId)) return { ...snapshot, nodes: [], edges: [] }

  const depth = new Map<string, number>([[rootId, 0]])
  const treeEdges: BrainEdge[] = []
  let frontier = [rootId]
  for (let level = 1; level <= depthLimit && frontier.length > 0; level += 1) {
    const next: string[] = []
    for (const parentId of frontier) {
      for (const edge of snapshot.edges) {
        if (endpointId(edge.source) !== parentId) continue
        const childId = endpointId(edge.target)
        if (!nodeById.has(childId) || depth.has(childId)) continue
        depth.set(childId, level)
        treeEdges.push(edge)
        next.push(childId)
      }
    }
    frontier = next
  }

  return {
    ...snapshot,
    anchorId: rootId,
    nodes: [...depth].map(([id, hierarchyDepth]) => ({
      ...nodeById.get(id)!,
      hierarchyDepth,
    })),
    edges: treeEdges,
  }
}

export function topDownPath(
  snapshot: GraphSnapshot,
  sourceId: string,
  targetId: string,
  depthLimit: number,
): string[] | null {
  const tree = topDownSubgraph(snapshot, sourceId, depthLimit)
  if (!tree.nodes.some((node) => node.id === targetId)) return null
  const parent = new Map(tree.edges.map((edge) => [endpointId(edge.target), endpointId(edge.source)]))
  const path = [targetId]
  while (path[0] !== sourceId) {
    const parentId = parent.get(path[0])
    if (!parentId) return null
    path.unshift(parentId)
  }
  return path
}

export function graphDistance(snapshot: GraphSnapshot, anchorId: string): Map<string, number> {
  const distances = new Map<string, number>([[anchorId, 0]])
  const queue = [anchorId]
  while (queue.length) {
    const current = queue.shift()!
    const depth = distances.get(current) ?? 0
    for (const edge of snapshot.edges) {
      const source = endpointId(edge.source)
      const target = endpointId(edge.target)
      const neighbor = source === current ? target : target === current ? source : undefined
      if (neighbor && !distances.has(neighbor)) {
        distances.set(neighbor, depth + 1)
        queue.push(neighbor)
      }
    }
  }
  return distances
}

export function localSearch(
  snapshot: GraphSnapshot,
  query: string,
  anchorId: string,
  hops: number,
  kinds: NodeKind[] = [],
): { graph: GraphSnapshot; hits: SearchHit[] } {
  const graph = boundedSubgraph(snapshot, anchorId, hops)
  const distances = graphDistance(graph, anchorId)
  const terms = query
    .toLocaleLowerCase()
    .split(/[^\p{L}\p{N}_-]+/u)
    .filter(Boolean)

  const scored = graph.nodes
    .filter((node) => kinds.length === 0 || kinds.includes(node.kind))
    .map((node) => {
      const label = node.label.toLocaleLowerCase()
      const body = `${node.summary} ${node.kind} ${node.tags.join(' ')}`.toLocaleLowerCase()
      const labelMatches = terms.filter((term) => label.includes(term)).length
      const bodyMatches = terms.filter((term) => body.includes(term)).length
      const exactBoost = query.trim() && label === query.trim().toLocaleLowerCase() ? 2 : 0
      const distance = distances.get(node.id) ?? hops + 1
      const proximity = 1 / (1 + distance)
      const textScore = terms.length === 0 ? 1 : (labelMatches * 2 + bodyMatches) / (terms.length * 3)
      const score = Math.min(1, textScore * 0.8 + proximity * 0.2 + exactBoost)
      const reasons = []
      if (labelMatches) reasons.push('label match')
      if (bodyMatches) reasons.push('semantic text match')
      if (distance === 0) reasons.push('anchor node')
      else reasons.push(`${distance} hop${distance === 1 ? '' : 's'} from anchor`)
      return { node, score, reasons, distanceFromAnchor: distance }
    })
    .filter((hit) => terms.length === 0 || hit.score > 0.1)
    .sort((a, b) => b.score - a.score || a.distanceFromAnchor - b.distanceFromAnchor)

  return {
    // Preserve intermediate nodes and evidence edges so a two-hop result remains
    // explainable even when its connecting node did not match the query text.
    graph,
    hits: scored,
  }
}

export function shortestPath(
  snapshot: GraphSnapshot,
  sourceId: string,
  targetId: string,
): PathResponse | null {
  if (sourceId === targetId) {
    return { sourceId, targetId, steps: [], explanation: 'The selected node is the anchor.', confidence: 1 }
  }
  const nodeById = new Map(snapshot.nodes.map((node) => [node.id, node]))
  if (!nodeById.has(sourceId) || !nodeById.has(targetId)) return null

  const previous = new Map<string, { nodeId: string; edge: BrainEdge }>()
  const queue = [sourceId]
  const seen = new Set([sourceId])
  while (queue.length) {
    const current = queue.shift()!
    for (const edge of snapshot.edges) {
      const source = endpointId(edge.source)
      const target = endpointId(edge.target)
      const neighbor = source === current ? target : target === current ? source : undefined
      if (!neighbor || seen.has(neighbor)) continue
      seen.add(neighbor)
      previous.set(neighbor, { nodeId: current, edge })
      if (neighbor === targetId) queue.length = 0
      else queue.push(neighbor)
    }
  }
  if (!previous.has(targetId)) return null

  const reverse: { fromId: string; edge: BrainEdge; toId: string }[] = []
  let cursor = targetId
  while (cursor !== sourceId) {
    const entry = previous.get(cursor)
    if (!entry) return null
    reverse.push({ fromId: entry.nodeId, edge: entry.edge, toId: cursor })
    cursor = entry.nodeId
  }
  const steps = reverse.reverse().map(({ fromId, edge, toId }) => ({
    from: nodeById.get(fromId)!,
    edge,
    to: nodeById.get(toId)!,
  }))
  const confidence = steps.reduce((minimum, step) => Math.min(minimum, step.edge.confidence), 1)
  return {
    sourceId,
    targetId,
    steps,
    explanation: steps.map((step) => `${step.from.label} ${step.edge.relation} ${step.to.label}`).join(' → '),
    confidence,
  }
}

export function applySceneBudget(
  snapshot: GraphSnapshot,
  maxNodes: number,
  maxEdges: number,
  anchorId?: string,
): GraphSnapshot {
  if (snapshot.nodes.length <= maxNodes && snapshot.edges.length <= maxEdges) return snapshot
  const distances = anchorId ? graphDistance(snapshot, anchorId) : new Map<string, number>()
  const nodes = [...snapshot.nodes]
    .sort((a, b) => {
      const distance = (distances.get(a.id) ?? Number.MAX_SAFE_INTEGER) - (distances.get(b.id) ?? Number.MAX_SAFE_INTEGER)
      return distance || b.confidence - a.confidence
    })
    .slice(0, maxNodes)
  const ids = new Set(nodes.map((node) => node.id))
  const edges = snapshot.edges
    .filter((edge) => ids.has(endpointId(edge.source)) && ids.has(endpointId(edge.target)))
    .sort((a, b) => b.confidence - a.confidence)
    .slice(0, maxEdges)
  return { ...snapshot, nodes, edges, truncated: true }
}

export function cloneForRenderer(snapshot: GraphSnapshot): GraphSnapshot {
  return {
    ...snapshot,
    nodes: snapshot.nodes.map((node) => ({ ...node })),
    edges: snapshot.edges.map((edge) => ({
      ...edge,
      source: endpointId(edge.source),
      target: endpointId(edge.target),
    })),
  }
}

export function graphTimeBounds(snapshot: GraphSnapshot): { min: number; max: number } {
  const times = snapshot.nodes
    .filter((node) => node.validTimeKnown !== false)
    .flatMap((node) => [Date.parse(node.validFrom), Date.parse(node.validTo ?? node.recordedAt)])
  const finite = times.filter(Number.isFinite)
  const now = Date.now()
  return {
    min: finite.length ? Math.min(...finite) : now,
    max: Math.max(finite.length ? Math.max(...finite) : now, now),
  }
}

export function escapeHtml(value: string): string {
  return value.replace(/[&<>'"]/g, (character) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    "'": '&#39;',
    '"': '&quot;',
  })[character]!)
}

export function formatMoment(value: string | number): string {
  const date = typeof value === 'number' ? new Date(value) : new Date(value)
  return new Intl.DateTimeFormat(undefined, {
    year: 'numeric',
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date)
}
