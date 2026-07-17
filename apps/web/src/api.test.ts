import { afterEach, describe, expect, it, vi } from 'vitest'
import { brainHubApi, normalizeSnapshot, searchRequestToWire } from './api'

afterEach(() => vi.restoreAllMocks())

describe('daemon JSON compatibility', () => {
  it('normalizes canonical snake_case recursively', () => {
    const graph = normalizeSnapshot({
      projection_version: 7,
      anchor_id: 'node-1',
      nodes: [{
        id: 'node-1',
        title: 'Local-first privacy',
        type: 'DECISION',
        summary: 'Keep sensitive content local.',
        valid_time: { start: '2026-07-01T00:00:00Z', end: null },
        recorded_time: { start: '2026-07-02T00:00:00Z', end: null },
        review_state: 'ACCEPTED',
        properties: { tags: ['privacy'] },
        provenance: {
          actor_id: 'actor-user',
          extractor: 'brainhub-core',
          extractor_version: '1.2.0',
          evidence: [{ source_event_id: 'event-0001', locator: 'brainhub://decision', content_hash: 'sha256:abc', visibility: 'LOCAL' }],
        },
      }],
      edges: [{
        id: 'edge-1',
        source_id: 'node-1',
        target_id: 'node-2',
        relation: 'DEPENDS_ON',
        explanation: 'A relation.',
        valid_time: { start: '2026-07-01T00:00:00Z', end: null },
        recorded_time: { start: '2026-07-02T00:00:00Z', end: null },
        confidence_score: 0.8,
        confidence_class: 'EXTRACTED',
        actor_id: 'actor-user',
        extractor: 'brainhub-core',
        extractor_version: '1.2.0',
        evidence: [{ source_event_id: 'event-0001', locator: 'brainhub://edge', visibility: 'LOCAL' }],
      }],
    })

    expect(graph.anchorId).toBe('node-1')
    expect(graph.cursor).toBe('projection:7')
    expect(graph.nodes[0].kind).toBe('Decision')
    expect(graph.nodes[0].validFrom).toBe('2026-07-01T00:00:00Z')
    expect(graph.nodes[0].evidence[0].contentHash).toBe('sha256:abc')
    expect(graph.nodes[0].provenance[0].actor).toBe('actor-user')
    expect(graph.edges[0].source).toBe('node-1')
    expect(graph.edges[0].confidence).toBe(0.8)
    expect(graph.edges[0].confidenceClass).toBe('EXTRACTED')
  })

  it('serializes search inputs with canonical snake_case keys', () => {
    expect(searchRequestToWire({
      query: 'privacy',
      anchorId: 'node-1',
      hops: 2,
      validAt: '2026-07-17T00:00:00Z',
      filters: { kinds: ['Decision'] },
    })).toEqual({
      query: 'privacy',
      anchor_id: 'node-1',
      hops: 2,
      valid_at: '2026-07-17T00:00:00Z',
      limit: undefined,
      scope: 'anchored',
      filters: {
        kinds: ['Decision'],
      },
    })
  })

  it('converts canonical path nodes and edges into explained UI steps', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({
      nodes: [
        { id: 'a', type: 'TOPIC', title: 'A', summary: '', valid_time: { start: '2026-07-01T00:00:00Z' }, recorded_time: { start: '2026-07-02T00:00:00Z' }, provenance: { actor_id: 'actor', evidence: [] } },
        { id: 'b', type: 'DECISION', title: 'B', summary: '', valid_time: { start: '2026-07-01T00:00:00Z' }, recorded_time: { start: '2026-07-02T00:00:00Z' }, provenance: { actor_id: 'actor', evidence: [] } },
      ],
      edges: [{ id: 'e1', source_id: 'a', target_id: 'b', relation: 'DEPENDS_ON', explanation: 'A needs B.', confidence_score: 0.8, confidence_class: 'INFERRED', valid_time: { start: '2026-07-01T00:00:00Z' }, recorded_time: { start: '2026-07-02T00:00:00Z' }, evidence: [], actor_id: 'actor' }],
      projection_version: 9,
    }), { status: 200, headers: { 'content-type': 'application/json' } }))

    const result = await brainHubApi.path('a', 'b', '2026-07-17T00:00:00Z', 2)
    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect(JSON.parse(String(init.body))).toEqual({
      source_id: 'a',
      target_id: 'b',
      valid_at: '2026-07-17T00:00:00Z',
      max_length: 2,
    })
    expect(result.steps).toHaveLength(1)
    expect(result.steps[0].from.label).toBe('A')
    expect(result.steps[0].edge.relation).toBe('DEPENDS_ON')
    expect(result.confidence).toBe(0.8)
  })
})
