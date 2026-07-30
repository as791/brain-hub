import type { BrainNode, GraphSnapshot } from '../types'
import { endpointId } from './graph'

const UUID = /\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b/gi
const SESSION_REFERENCE = /\bsessions?\/session[_/-][a-z0-9-]{8,}\b/gi
const OPAQUE_LABEL = /^(?:sessions?\/)?(?:session|run|workstream)[_/-][a-z0-9-]{8,}$/i

export function safeDisplayText(value: unknown, fallback: string): string {
  if (typeof value !== 'string') return fallback
  const trimmed = value.trim()
  if (!trimmed || OPAQUE_LABEL.test(trimmed)) return fallback
  const cleaned = trimmed.replace(UUID, '').replace(SESSION_REFERENCE, '').replace(/\s{2,}/g, ' ').trim()
  return cleaned || fallback
}

function sourceName(node: BrainNode, graph: GraphSnapshot): string | undefined {
  const direct = node.provenance.map((item) => item.agent).find(Boolean)
  if (direct) return safeDisplayText(direct, '') || undefined

  const runIds = node.kind === 'Workstream'
    ? graph.edges.filter((edge) => edge.relation.toUpperCase() === 'HAS_RUN' && endpointId(edge.source) === node.id).map((edge) => endpointId(edge.target))
    : [node.id]
  const actorIds = graph.edges
    .filter((edge) => runIds.includes(endpointId(edge.source)) && edge.relation.toUpperCase() === 'ASSERTED_BY')
    .map((edge) => endpointId(edge.target))
  const actor = graph.nodes.find((candidate) => candidate.kind === 'Actor' && actorIds.includes(candidate.id))
  return actor ? safeDisplayText(actor.label.split('/')[0], '') || undefined : undefined
}

export function displayTitle(node: BrainNode, graph: GraphSnapshot): string {
  const fallback = node.kind === 'Run' ? 'Captured activity' : node.kind === 'Workstream' ? 'Captured workstream' : `Captured ${node.kind.toLowerCase()}`
  const metadata = node.metadata ?? {}
  const explicit = [metadata.telemetryName, metadata.displayName, metadata.sessionTitle, metadata.workstreamTitle, metadata.runTitle, metadata.name]
    .map((value) => safeDisplayText(value, ''))
    .find(Boolean)
  if (explicit) return explicit
  if (metadata.titleSource !== 'generated-safe-metadata') {
    const label = safeDisplayText(node.label, '')
    if (label) return label
  }
  const source = sourceName(node, graph)
  return source ? `${source} ${node.kind === 'Workstream' ? 'workstream' : 'activity'}` : fallback
}

export function formatLocalTime(node: BrainNode): string {
  if (node.validTimeKnown === false) return 'Not captured yet'
  const time = Date.parse(node.validFrom || node.recordedAt)
  return Number.isNaN(time)
    ? 'Not captured yet'
    : new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(time)
}
