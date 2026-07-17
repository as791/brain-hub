import { describe, expect, it } from 'vitest'
import { demoGraph } from '../demoGraph'
import {
  applySceneBudget,
  boundedSubgraph,
  endpointId,
  filterAtTime,
  localSearch,
  shortestPath,
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
