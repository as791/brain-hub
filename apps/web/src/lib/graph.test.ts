import { describe, expect, it } from 'vitest'
import { demoGraph } from '../demoGraph'
import {
  applySceneBudget,
  boundedSubgraph,
  endpointId,
  filterAtTime,
  localSearch,
  MAX_GRAPH_HOPS,
  shortestPath,
  associativePath,
  associativeSubgraph,
} from './graph'

describe('temporal graph projection', () => {
  it('excludes nodes and edges that are not valid yet', () => {
    const graph = filterAtTime(demoGraph, '2026-06-15T00:00:00.000Z')
    expect(graph.nodes.some((node) => node.id === 'artifact-ui')).toBe(false)
    expect(graph.nodes.some((node) => node.id === 'decision-local')).toBe(true)
    expect(graph.edges.every((edge) =>
      graph.nodes.some((node) => node.id === endpointId(edge.source)) &&
      graph.nodes.some((node) => node.id === endpointId(edge.target)),
    )).toBe(true)
  })

  it('honors validTo at the selected time', () => {
    const expired = {
      ...demoGraph,
      nodes: demoGraph.nodes.map((node) => node.id === 'decision-local'
        ? { ...node, validTo: '2026-06-14T12:00:00.000Z' }
        : node),
    }
    expect(filterAtTime(expired, '2026-06-15T00:00:00.000Z').nodes.some((node) => node.id === 'decision-local')).toBe(false)
  })
})

describe('strict anchored traversal', () => {
  it('supports an explicit depth of up to 20 hops', () => {
    expect(MAX_GRAPH_HOPS).toBe(20)
    expect(boundedSubgraph(demoGraph, 'ws-brain', MAX_GRAPH_HOPS).nodes).toHaveLength(
      demoGraph.nodes.length,
    )
  })

  it('never includes a node beyond the requested radius', () => {
    const oneHop = boundedSubgraph(demoGraph, 'ws-brain', 1)
    expect(oneHop.nodes.some((node) => node.id === 'ws-brain')).toBe(true)
    expect(oneHop.nodes.some((node) => node.id === 'decision-sqlite')).toBe(false)

    const twoHops = boundedSubgraph(demoGraph, 'ws-brain', 2)
    expect(twoHops.nodes.some((node) => node.id === 'decision-sqlite')).toBe(true)
  })

  it('returns an empty projection for an unknown anchor instead of falling back globally', () => {
    expect(boundedSubgraph(demoGraph, 'missing', 2).nodes).toEqual([])
  })

  it('ranks text only inside the bounded graph', () => {
    const result = localSearch(demoGraph, 'sqlite', 'artifact-ui', 1)
    expect(result.hits.some((hit) => hit.node.id === 'decision-sqlite')).toBe(false)
  })
})

describe('bidirectional associative traversal', () => {
  it('follows both incoming and outgoing connections', () => {
    const neighborhood = associativeSubgraph(demoGraph, 'ws-brain', 1)

    expect(neighborhood.nodes.some((node) => node.id === 'topic-graph')).toBe(true)
    expect(neighborhood.nodes.some((node) => node.id === 'actor-user')).toBe(true)
    expect(neighborhood.edges.some((edge) => endpointId(edge.target) === 'ws-brain')).toBe(true)
    expect(neighborhood.edges.some((edge) => endpointId(edge.source) === 'ws-brain')).toBe(true)
  })

  it('retains cross-links and records each node distance from focus', () => {
    const neighborhood = associativeSubgraph(demoGraph, 'ws-brain', 20)

    expect(neighborhood.edges.length).toBeGreaterThanOrEqual(neighborhood.nodes.length - 1)
    expect(neighborhood.nodes.find((node) => node.id === 'decision-sqlite')?.neighborhoodDepth).toBe(2)
    expect(new Set(neighborhood.nodes.map((node) => node.id)).size).toBe(neighborhood.nodes.length)
  })

  it('returns a path regardless of stored edge direction', () => {
    expect(associativePath(demoGraph, 'ws-brain', 'decision-sqlite', 20)).toEqual([
      'ws-brain',
      'topic-graph',
      'decision-sqlite',
    ])
    expect(associativePath(demoGraph, 'ws-brain', 'actor-user', 20)).toEqual([
      'ws-brain',
      'actor-user',
    ])
  })
})

describe('evidence paths and budgets', () => {
  it('builds an explainable path with the lowest edge confidence as its floor', () => {
    const path = shortestPath(demoGraph, 'ws-brain', 'claim-anchor')
    expect(path).not.toBeNull()
    expect(path?.steps.map((step) => step.edge.relation)).toContain('VERIFIES')
    expect(path?.confidence).toBeLessThanOrEqual(0.78)
    expect(path?.explanation).toContain('Brain Hub product')
  })

  it('enforces scene node and edge caps while retaining the anchor', () => {
    const limited = applySceneBudget(demoGraph, 5, 4, 'ws-brain')
    expect(limited.nodes).toHaveLength(5)
    expect(limited.edges.length).toBeLessThanOrEqual(4)
    expect(limited.nodes.some((node) => node.id === 'ws-brain')).toBe(true)
    expect(limited.truncated).toBe(true)
  })
})
