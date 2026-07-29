import { describe, expect, it } from 'vitest'
import type { BrainEdge, BrainNode, GraphSnapshot } from '../types'
import { workStatus } from './workStatus'

const node = (id: string, kind: BrainNode['kind'], metadata: Record<string, unknown> = {}): BrainNode => ({
  id, kind, metadata, label: id, summary: '', validFrom: '2026-01-01T00:00:00Z', recordedAt: '2026-01-01T00:00:00Z',
  confidence: 1, confidenceClass: 'EXTRACTED', provenance: [], evidence: [], tags: [],
})

const edge = (source: string, target: string, relation: string): BrainEdge => ({
  id: `${source}-${target}`, source, target, relation, explanation: relation,
  validFrom: '2026-01-01T00:00:00Z', recordedAt: '2026-01-01T00:00:00Z', confidence: 1,
  confidenceClass: 'EXTRACTED', evidence: [], provenance: { actor: 'test' },
})

describe('work status projection', () => {
  it('makes blocker and test evidence explicit without changing graph data', () => {
    const graph: GraphSnapshot = {
      nodes: [node('ship', 'Task'), node('approval', 'Decision'), node('suite', 'Artifact', { record_type: 'test_result', outcome: 'passed' })],
      edges: [edge('approval', 'ship', 'BLOCKS'), edge('suite', 'ship', 'VERIFIES')],
    }

    const status = workStatus(graph)

    expect(status.tasks[0].state).toBe('blocked')
    expect(status.tasks[0].blockers.map((item) => item.id)).toEqual(['approval'])
    expect(status.tests[0].outcome).toBe('passed')
    expect(status.tests[0].verifies.map((item) => item.id)).toEqual(['ship'])
  })
})
